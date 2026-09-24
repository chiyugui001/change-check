from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import (CheckError, SOURCE, atomic_write, canonical, digest, git, home,
                     lock, read_json, roots, run, save_json, update_root)
from .repository import repo_root
from .storage import backup_config

MARK = "change-check-managed"


def config_path(agent):
    user = Path(os.environ.get("CHANGE_CHECK_USER_HOME", Path.home()))
    codex = Path(os.environ.get("CODEX_HOME", user / ".codex")) if not os.environ.get("CHANGE_CHECK_USER_HOME") else user / ".codex"
    return {"codex": codex / "hooks.json",
            "kimi": user / ".kimi-code/config.toml",
            "zcode": user / ".zcode/cli/config.json",
            "claude": user / ".claude/settings.json"}[agent]


def detect_agents():
    results = []
    applications = installed_apps()
    for agent in ("codex", "kimi", "zcode", "claude"):
        program = shutil.which(agent)
        path = config_path(agent)
        results.append({"agent": agent, "executable": program, "config": str(path),
                        "applications": applications.get(agent, []),
                        "state": "已发现命令入口" if program else "已发现应用（CLI 未确认）" if applications.get(agent) else "仅发现配置" if path.exists() else "未发现",
                        "review_supported": agent in ("claude", "codex") and bool(program)})
    # Detection is evidence only; never changes configuration or launches a model.
    return results


def installed_apps():
    result = {}
    if os.name != "nt":
        return result
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, base) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as entry:
                                name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                for agent in ("codex", "kimi", "zcode", "claude"):
                                    if agent in name.lower() and name not in result.get(agent, []):
                                        result.setdefault(agent, []).append(name)
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def command_string(agent):
    argv = [sys.executable, "-B", "-X", "utf8", str(SOURCE / "main.py"), "--state", str(home()), "hook",
            "--agent", agent, "--marker", MARK]
    if os.name == "nt":
        if any(any(c in arg for c in '%!"\r\n') for arg in argv):
            raise CheckError("Windows 钩子路径包含 shell 不支持的字符，请使用普通路径")
        return " ".join('"' + arg + '"' for arg in argv)
    return shlex.join(argv)


def managed(entry):
    if not isinstance(entry, dict):
        return False
    return any(mark in str(entry.get("command", "")) for mark in (MARK, "kb-check-managed")) or any(
        managed(x) for x in entry.get("hooks", []))


def backup(path):
    backup_config(path)


def remove_managed(entries):
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CheckError("钩子条目必须为对象")
        if "hooks" in entry:
            group = dict(entry)
            group["hooks"] = [h for h in entry["hooks"] if not managed(h)]
            if group["hooks"]:
                result.append(group)
        elif not managed(entry):
            result.append(entry)
    return result


def configure_agent(agent, remove=False):
    with lock("agent-configs"):
        return _configure_agent(agent, remove)


def _configure_agent(agent, remove=False):
    path = config_path(agent)
    if remove and not path.exists():
        return
    backup(path)
    manifest = read_json(home() / "agents.json", {})
    previous = manifest.get(agent, {}).get("previous_enabled", "absent")
    if agent == "kimi":
        try:
            import tomlkit
        except ImportError as exc:
            raise CheckError("缺少 tomlkit；先通过 install.ps1 安装独立环境") from exc
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        document = tomlkit.parse(text)
        entries = [dict(e) for e in document.get("hooks", []) if not managed(e)]
        if not remove:
            entries.extend({"event": event, "command": command_string(agent), "timeout": 120}
                           for event in ("PostToolUse", "Stop"))
        array = tomlkit.aot()
        for entry in entries:
            array.append(tomlkit.item(entry))
        if entries:
            document["hooks"] = array
        else:
            document.pop("hooks", None)
        atomic_write(path, tomlkit.dumps(document))
    else:
        data = read_json(path, {})
        holder = data.setdefault("hooks", {})
        if agent == "zcode":
            if agent not in manifest:
                previous = holder.get("enabled", "absent")
            events = holder.setdefault("events", {})
        else:
            events = holder
        for event in ("PostToolUse", "Stop"):
            entries = events.get(event, [])
            if not isinstance(entries, list):
                raise CheckError(f"{path} 中 {event} 不是数组")
            entries = remove_managed(entries)
            if not remove:
                handler = {"type": "command", "command": command_string(agent), "timeout": 120}
                entries.append({"hooks": [handler]})
            if entries:
                events[event] = entries
            else:
                events.pop(event, None)
        if agent == "zcode" and not remove:
            holder["enabled"] = True
        elif agent == "zcode" and remove and not events and holder.get("enabled") is True:
            if previous == "absent":
                holder.pop("enabled", None)
            else:
                holder["enabled"] = previous
        save_json(path, data)
    if remove:
        manifest.pop(agent, None)
    else:
        manifest[agent] = {"path": str(path), "state": "待实际触发验证",
                           "config_hash": digest(path.read_bytes()), "previous_enabled": previous}
    save_json(home() / "agents.json", manifest)


def install_git_hook(item):
    root = Path(item["path"]).resolve()
    if repo_root(root) != root:
        raise CheckError("提交钩子要求登记目录本身为 Git 根目录")
    existing = item["profile"].get("git_hook")
    if existing:
        current = git(root, "config", "--local", "--get", "core.hooksPath", check=False).stdout.decode().strip()
        if canonical(current) != canonical(existing["directory"]):
            raise CheckError("core.hooksPath 已被其他工具修改，拒绝覆盖")
        # Upgrade only wrappers still matching the installation manifest.
        for path, expected in existing["hashes"].items():
            if not Path(path).exists() or digest(Path(path).read_bytes()) != expected:
                raise CheckError("已安装钩子发生变化，请先检查：" + path)
        if ((SOURCE / "main.py").as_posix() in
                (Path(existing["directory"]) / "pre-commit").read_text(encoding="utf-8")):
            return
        uninstall_git_hook(item)
    local = git(root, "config", "--local", "--get", "core.hooksPath", check=False)
    previous = local.stdout.decode().strip() if local.returncode == 0 else None
    effective = git(root, "rev-parse", "--git-path", "hooks").stdout.decode().strip()
    original = Path(effective)
    if not original.is_absolute():
        original = root / original
    original = original.resolve()
    directory = home() / "hooks" / item["id"]
    directory.mkdir(parents=True, exist_ok=True)
    hooks = {p.name for p in original.iterdir() if p.is_file() and not p.name.endswith(".sample")} \
        if original.is_dir() else set()
    hooks.add("pre-commit")
    hashes = {}
    for name in hooks:
        script = "#!/bin/sh\n# " + MARK + "\n"
        old = original / name
        if old.is_file():
            script += shlex.quote(old.as_posix()) + ' "$@" || exit $?\n'
        if name == "pre-commit":
            args = [Path(sys.executable).as_posix(), "-B", "-X", "utf8",
                    (SOURCE / "main.py").as_posix(), "gate", "--root", root.as_posix()]
            # Explicit state location is required when Git is launched from a GUI.
            script += "export CHANGE_CHECK_HOME=" + shlex.quote(home().as_posix()) + "\n"
            script += "exec " + shlex.join(args) + "\n"
        else:
            script += "exit 0\n"
        path = directory / name
        atomic_write(path, script)
        path.chmod(0o755)
        hashes[str(path)] = digest(path.read_bytes())
    git(root, "config", "--local", "core.hooksPath", str(directory.resolve()))
    item["profile"]["git_hook"] = {"directory": str(directory.resolve()), "previous": previous,
                                    "original": str(original), "hashes": hashes}
    update_root(item)


def uninstall_git_hook(item):
    info = item["profile"].get("git_hook")
    if not info:
        return
    root = item["path"]
    current = git(root, "config", "--local", "--get", "core.hooksPath", check=False).stdout.decode().strip()
    if canonical(current) != canonical(info["directory"]):
        raise CheckError("core.hooksPath 已变更；保留配置，请人工核对")
    for name, expected in info["hashes"].items():
        if not Path(name).exists() or digest(Path(name).read_bytes()) != expected:
            raise CheckError("钩子文件已被修改，保留并停止卸载：" + name)
    if info["previous"] is None:
        git(root, "config", "--local", "--unset", "core.hooksPath")
    else:
        git(root, "config", "--local", "core.hooksPath", info["previous"])
    for name in info["hashes"]:
        Path(name).unlink()
    item["profile"]["git_hook"] = None
    update_root(item)


def record_event(agent, event, checked_roots):
    with lock("events"):
        state = read_json(home() / "events.json", {})
        state[agent] = {"event": event, "roots": checked_roots, "time": time.time(),
                        "state": "已收到事件调用；是否来自真实宿主须结合实测确认"}
        save_json(home() / "events.json", state)
