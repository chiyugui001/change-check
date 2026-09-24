"""Explicit read-only evidence mounts. Never replace staged files with working files."""
from pathlib import Path

from .common import CheckError, MAX_BYTES, digest, walk_files
from .repository import is_text


def snapshot_name(value):
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return ".change-check-context/" + digest(str(path.resolve()))[:16] + "/" + path.name
    return path.as_posix()


def reference_files(profile):
    entries = []
    for field in ("standards", "template", "requirement_template"):
        value = profile.get(field)
        if value and Path(value).is_absolute():
            entries.append({"path": value, "mount": snapshot_name(value)})
    entries += profile.get("references", [])
    for entry in entries:
        source = Path(entry["path"]).expanduser().resolve()
        mount = entry["mount"].replace("\\", "/")
        if Path(mount).is_absolute() or ".." in Path(mount).parts:
            raise CheckError("引用挂载路径必须是快照内相对路径")
        if not source.exists():
            raise CheckError("配置的引用资料不存在：" + str(source))
        files = walk_files(source) if source.is_dir() else [("", source)]
        for name, path in files:
            target = mount + ("/" + name if name else "")
            # External evidence is text-only; unsupported evidence is reported to the reviewer.
            yield target, path, is_text(name or path.name)


def attach_references(snap, profile):
    for target in snap.external:
        snap.data.pop(target, None)
        snap.names.discard(target)
        snap.identities.pop(target, None)
    snap.external.clear()
    total = sum(len(b) for b in snap.data.values())
    for target, path, text in reference_files(profile):
        if target in snap.names:
            raise CheckError("外部引用与仓库文件重名，拒绝覆盖：" + target)
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_BYTES:
            raise CheckError("文本与外部证据超过 40 MiB 快照上限")
        snap.external[target] = {"path": str(path), "hash": digest(raw)}
        snap.names.add(target)
        snap.identities[target] = digest(raw)
        if text:
            snap.data[target] = raw


def verify_references(snap, profile):
    current = {target: {"path": str(path), "hash": digest(path.read_bytes())}
               for target, path, _ in reference_files(profile)}
    if current != snap.external:
        raise CheckError("检查期间外部规范或证据发生变化，必须重新检查")
