from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .common import CheckError, MAX_BYTES, SKIP_DIRS, TEXT_SUFFIXES, digest, git, run, walk_files


@dataclass
class Snapshot:
    root: Path
    staged: bool
    data: dict = field(default_factory=dict)
    names: set = field(default_factory=set)
    changed: set = field(default_factory=set)
    before: dict = field(default_factory=dict)
    identities: dict = field(default_factory=dict)
    tree: str = ""
    head: str = ""
    errors: list = field(default_factory=list)
    external: dict = field(default_factory=dict)
    modes: dict = field(default_factory=dict)
    captured: bool = False
    full_scan: bool = False

    def text(self, name):
        try:
            return self.data[name].decode("utf-8-sig")
        except UnicodeError as exc:
            raise CheckError(f"UTF-8 解码失败：{name}") from exc

    def bytes_for(self, name):
        if name in self.data:
            return self.data[name]
        if name not in self.names:
            raise CheckError(f"来源不存在：{name}")
        if self.staged:
            return git(self.root, "show", ":" + name).stdout
        return (self.root / name).read_bytes()

    def fingerprint(self):
        return digest({"mode": "staged" if self.staged else "worktree",
                       "head": self.head, "tree": self.tree, "files": self.identities,
                       "names": sorted(self.names), "changed": sorted(self.changed),
                       "external": self.external})


def is_text(name):
    return (Path(name).suffix.lower() in TEXT_SUFFIXES or
            Path(name).name in {".gitignore", ".gitattributes", ".editorconfig", "Makefile", "Dockerfile"}) and not any(
        p in SKIP_DIRS or p.startswith(".change-check") or p == ".baseline" for p in Path(name).parts)


def repo_root(root):
    result = git(root, "rev-parse", "--show-toplevel", check=False)
    return Path(os.fsdecode(result.stdout).strip()).resolve() if not result.returncode else None


def index_identity(root):
    tree = git(root, "write-tree").stdout.decode().strip()
    head = git(root, "rev-parse", "--verify", "HEAD", check=False)
    return tree, head.stdout.decode().strip() if head.returncode == 0 else ""


def load_snapshot(root, staged=False, all_files=False):
    root = Path(root).resolve()
    snap = Snapshot(root, staged)
    snap.captured = True
    snap.full_scan = all_files
    repo = repo_root(root)
    if staged and repo != root:
        raise CheckError("暂存检查要求登记路径就是该 Git 仓库根目录")
    if staged:
        snap.tree, snap.head = index_identity(root)
        entries = git(root, "ls-files", "--stage", "-z").stdout.split(b"\0")
        wanted = []
        for entry in filter(None, entries):
            meta, filename = entry.split(b"\t", 1)
            mode, oid, stage = meta.decode().split()
            name = os.fsdecode(filename)
            if stage != "0":
                raise CheckError(f"存在未解决冲突：{name}")
            snap.names.add(name)
            snap.identities[name] = oid
            snap.modes[name] = mode
            if is_text(name):
                if mode != "100644" and mode != "100755":
                    raise CheckError(f"待检文本不是普通文件：{name} ({mode})")
                wanted.append((name, oid))
        load_git_blobs(snap, wanted)
        args = ["diff", "--cached", "--name-only", "--no-renames", "-z"]
        if snap.head:
            args.append(snap.head)
        snap.changed = set(os.fsdecode(p) for p in git(root, *args).stdout.split(b"\0") if p)
    else:
        total = 0
        for name, path in walk_files(root):
            snap.names.add(name)
            if not is_text(name):
                continue
            try:
                size = path.stat().st_size
                total += size
                if total > MAX_BYTES:
                    raise CheckError("知识文本超过 40 MiB 快照上限，未执行截断检查")
                raw = path.read_bytes()
                snap.data[name] = raw
                snap.identities[name] = digest(raw)
            except OSError as exc:
                raise CheckError(f"读取失败 {name}: {exc}") from exc
        if repo == root:
            snap.head = git(root, "rev-parse", "--verify", "HEAD", check=False).stdout.decode().strip()
            for command in (["diff", "--name-only", "-z"],
                            ["diff", "--cached", "--name-only", "-z"],
                            ["ls-files", "--others", "--exclude-standard", "-z"]):
                snap.changed.update(os.fsdecode(p) for p in git(root, *command).stdout.split(b"\0") if p)
        else:
            snap.changed = set(snap.names)
    if all_files:
        snap.changed.update(snap.names)
    if not staged:
        # Non-text bodies are not model input, but changed attachments must still
        # invalidate a worktree review even when the filename remains unchanged.
        for name in (snap.changed & snap.names) - snap.data.keys():
            try:
                with (root / name).open("rb") as stream:
                    snap.identities[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as exc:
                raise CheckError(f"读取附件指纹失败 {name}: {exc}") from exc
    if snap.head:
        wanted = []
        for entry in git(root, "ls-tree", "-r", "-z", snap.head).stdout.split(b"\0"):
            if not entry:
                continue
            meta, filename = entry.split(b"\t", 1)
            _, kind, oid = meta.decode().split()
            name = os.fsdecode(filename)
            if kind == "blob" and name in snap.changed and is_text(name):
                wanted.append((name, oid))
        baseline = Snapshot(root, staged)
        load_git_blobs(baseline, wanted)
        snap.before = baseline.data
    return snap


def load_git_blobs(snap, wanted):
    if not wanted:
        return
    query = "".join(oid + "\n" for _, oid in wanted).encode()
    sizes = git_batch(snap.root, "--batch-check", query).splitlines()
    if sum(int(line.rsplit(b" ", 1)[1]) for line in sizes) > MAX_BYTES:
        raise CheckError("暂存知识文本超过 40 MiB 上限，未执行截断检查")
    output = git_batch(snap.root, "--batch", query)
    cursor = 0
    for name, _ in wanted:
        end = output.index(b"\n", cursor)
        size = int(output[cursor:end].rsplit(b" ", 1)[1])
        cursor = end + 1
        snap.data[name] = output[cursor:cursor + size]
        cursor += size + 1


def git_batch(root, mode, query):
    return run(["git", "-C", root, "cat-file", mode], data=query).stdout


@contextlib.contextmanager
def materialize(snap):
    with tempfile.TemporaryDirectory(prefix="change-check-") as directory:
        folder = Path(directory)
        for name, raw in snap.data.items():
            path = folder / name
            if not path.resolve().is_relative_to(folder.resolve()):
                raise CheckError(f"非法快照路径：{name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        yield folder
