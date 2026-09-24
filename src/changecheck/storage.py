"""Bounded retention for generated artifacts; configuration and hooks are excluded."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .common import CheckError, digest, home, lock, read_json, save_json

DEFAULTS = {"days": 30, "review_limit": 100, "backup_limit": 3, "max_mib": 200,
            "session_limit": 100}
PATTERNS = {
    "reports": r"[a-f0-9]{12}(?:-(?:staged|worktree)-review)?\.json",
    "reviews": r"[a-f0-9]{12}/(?:staged|worktree)/[a-f0-9]{64}\.json",
    "hook-cache": r"[a-f0-9]{12}\.json",
    "backups": r"[a-f0-9]{12}-[0-9]+\.(?:json|toml)",
    "sessions": r"[a-f0-9]{12}/[a-f0-9]{64}\.json",
}


def policy():
    value = read_json(home() / "retention.json", {})
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise CheckError("存储策略字段无效")
    value = {**DEFAULTS, **value}
    if any(type(v) is not int or v <= 0 for v in value.values()):
        raise CheckError("存储策略必须为正整数")
    return value


def linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def artifacts():
    base = home().resolve()
    result = []
    for category, pattern in PATTERNS.items():
        folder = base / category
        if not folder.exists() or linked(folder):
            continue
        for directory, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if not linked(Path(directory) / d)]
            for name in files:
                path = Path(directory) / name
                if linked(path) or not re.fullmatch(pattern, path.relative_to(folder).as_posix()):
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(base):
                    raise CheckError("存储路径越出状态目录：" + str(path))
                stat = path.stat()
                group = (path.parent.relative_to(base).as_posix() if category in ("reviews", "sessions")
                         else name.split("-", 1)[0] if category == "backups" else category)
                result.append({"path": path, "category": category, "group": group,
                               "size": stat.st_size, "mtime": stat.st_mtime})
    return result


def cleanup_locked(dry_run=False):
    config = policy()
    entries = sorted(artifacts(), key=lambda e: (e["mtime"], str(e["path"])), reverse=True)
    groups, selected, remaining = {}, [], []
    cutoff = time.time() - config["days"] * 86400
    for entry in entries:
        category = entry["category"]
        key = (category, entry["group"])
        count = groups.get(key, 0)
        limit = config[{"reviews": "review_limit", "sessions": "session_limit"}.get(category, "backup_limit")]
        expired = entry["mtime"] < cutoff
        excess = category in ("reviews", "backups", "sessions") and count >= limit
        if expired or excess:
            selected.append({**entry, "reason": "expired" if expired else "count"})
        else:
            groups[key] = count + 1
            remaining.append(entry)
    size = sum(e["size"] for e in remaining)
    ceiling = config["max_mib"] * 1024 * 1024
    while size > ceiling and remaining:
        entry = remaining.pop()
        size -= entry["size"]
        selected.append({**entry, "reason": "size"})
    base = home().resolve()
    for entry in selected:
        path = entry["path"]
        # Recheck every ancestor immediately before unlink; never recurse through a junction.
        if any(linked(p) for p in (path, *path.parents) if p != base and p.is_relative_to(base)):
            raise CheckError("清理期间存储路径变为链接，停止清理：" + str(path))
        if not path.resolve().is_relative_to(base):
            raise CheckError("拒绝清理状态目录以外的路径")
        if not dry_run:
            path.unlink()
    return {"policy": config, "dry_run": dry_run, "before_bytes": sum(e["size"] for e in entries),
            "after_bytes": size, "files": len(entries),
            "selected": [{"path": str(e["path"]), "bytes": e["size"], "reason": e["reason"]}
                         for e in selected]}


def cleanup(dry_run=False):
    with lock("storage", seconds=30):
        return cleanup_locked(dry_run)


def save_artifact(path, value):
    with lock("storage", seconds=30):
        save_json(path, value)
        cleanup_locked()


def backup_config(path):
    if path.exists():
        with lock("storage", seconds=30):
            destination = home() / "backups" / (digest(str(path))[:12] + "-" + str(time.time_ns()) + path.suffix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            cleanup_locked()


def configure(changes):
    with lock("storage", seconds=30):
        current = policy()
        current.update(changes)
        if any(type(v) is not int or v <= 0 for v in current.values()):
            raise CheckError("存储策略必须为正整数")
        save_json(home() / "retention.json", current)
    return cleanup(dry_run=True)
