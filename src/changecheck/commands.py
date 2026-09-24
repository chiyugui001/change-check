"""Mandatory configured checks against isolated input, never unstaged fixes."""
import contextlib
import os
import tempfile
from pathlib import Path

from .common import CheckError, digest, git, run
from .project import matches

MAX_PROJECT_BYTES = 512 * 1024 * 1024


@contextlib.contextmanager
def command_snapshot(snap, policy):
    with tempfile.TemporaryDirectory(prefix="change-check-build-") as directory:
        folder = Path(directory)
        hashes = {}
        total = 0
        for name in sorted(snap.names):
            if name in snap.external or matches(name, policy.get("exclude", [])):
                continue
            path = folder / name
            if not path.resolve().is_relative_to(folder):
                raise CheckError("非法快照路径：" + name)
            mode = snap.modes.get(name, "100644")
            source = snap.root / name
            ancestors = [p for p in (source, *source.parents) if p != snap.root and p.is_relative_to(snap.root)]
            if mode not in ("100644", "100755") or (not snap.staged and any(
                    p.is_symlink() or bool(getattr(p.stat(), "st_file_attributes", 0) & 0x400) for p in ancestors)):
                raise CheckError("项目命令快照不支持链接或子模块，请将其作为独立登记项目：" + name)
            if snap.staged:
                size = int(git(snap.root, "cat-file", "-s", snap.identities[name]).stdout)
            else:
                size = source.stat().st_size
            total += size
            if total > MAX_PROJECT_BYTES:
                raise CheckError("项目命令快照超过 512 MiB；请明确排除不参与构建的产物")
            raw = snap.bytes_for(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            if mode == "100755":
                path.chmod(0o755)
            hashes[name] = digest(raw)
        yield folder, hashes


def execute_commands(snap, policy):
    stage = "commit" if snap.staged else "edit"
    findings, executions, covered = [], [], set()
    commands = []
    for command in policy.get("commands", []):
        if stage not in command.get("stages", ["edit", "commit"]):
            continue
        files = [n for n in sorted(snap.changed) if matches(n, command["include"]) and
                 not matches(n, policy.get("exclude", []))]
        if files:
            commands.append((command, files))
    if not snap.changed:
        return findings, executions, covered
    def add(name, message):
        findings.append({"file": name, "line": 1, "rule": "CMD001", "severity": "ERROR",
                         "message": message, "source": "project-command"})
    roles = {c.get("role", "validate") for c, _ in commands}
    if snap.staged:
        for role in set(policy.get("required_roles", [])) - roles:
            add("change-check.json", "提交阶段缺少适用的必检命令：" + role)
    for role in ("build", "test"):
        if role not in roles:
            executions.append({"checker": role, "status": "not_configured_or_not_applicable"})
    if not commands:
        return findings, executions, covered
    # Commands may generate artifacts but may not rewrite or delete captured inputs.
    with command_snapshot(snap, policy) as (folder, hashes):
        for command, files in commands:
            import sys
            argv = []
            for arg in command["argv"]:
                argv.extend([n for n in files if n in snap.names] if arg == "{files}" else
                            [str(folder) if arg == "{snapshot}" else sys.executable if arg == "{python}" else arg])
            env = dict(os.environ, CHANGE_CHECK_REVIEW="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
                env.pop(key, None)
            try:
                result = run(argv, cwd=folder, env=env, timeout=command.get("timeout", 120), check=False)
                output = (result.stdout + result.stderr).decode("utf-8", "replace")[-8000:]
                status = "passed" if result.returncode == 0 else "failed"
                if result.returncode:
                    add(files[0], f"{command['id']} 退出 {result.returncode}：{output}")
                modified = [n for n, value in hashes.items() if not (folder / n).is_file() or
                            digest((folder / n).read_bytes()) != value]
                if modified:
                    raise CheckError("检查命令改写了快照输入，结果无效：" + ", ".join(modified[:10]))
                executions.append({"checker": command["id"], "role": command.get("role", "validate"),
                                   "status": status, "files": files, "output": output,
                                   "snapshot": snap.fingerprint(), "stage": stage})
                covered.update(files)
            except CheckError as exc:
                add(files[0], command["id"] + "：" + str(exc))
                executions.append({"checker": command["id"], "status": "failed", "files": files})
                # Do not execute another check against mutated/uncertain input.
                break
        if not snap.staged:
            altered = [n for n, value in hashes.items() if not (snap.root / n).is_file() or
                       digest((snap.root / n).read_bytes()) != value]
            if altered:
                raise CheckError("项目命令期间原工作区输入变化：" + ", ".join(altered[:10]))
    return findings, executions, covered
