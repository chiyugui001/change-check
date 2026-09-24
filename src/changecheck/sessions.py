"""Attribute daily checks to paired tool calls in one host session, not Git dirt."""
from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path

from .common import CheckError, MAX_BYTES, SKIP_DIRS, digest, home, read_json, walk_files
from .repository import is_text, load_snapshot
from .storage import save_artifact

READ_TOOLS = {"read", "readfile", "read_file", "glob", "grep", "ls", "listdir",
              "webfetch", "websearch", "askuserquestion", "exitplanmode"}
WRITE_TOOLS = {"write", "edit", "multiedit", "writefile", "strreplacefile",
               "strreplace", "notebookedit", "editfile", "deletefile", "createfile"}


def field(event, snake, camel):
    return event.get(snake, event.get(camel))


def session_key(agent, event):
    identity = field(event, "session_id", "sessionId")
    return digest([agent, identity]) if isinstance(identity, str) and identity else None


def state_path(item, key):
    return home() / "sessions" / item["id"] / (key + ".json")


def new_state(agent):
    return {"format": 1, "agent": agent, "files": {}, "pending": {}, "uncertain": [],
            "warnings": [], "changes": {}, "started": time.time()}


def tool_scope(event):
    """None means opaque/local tool; [] means a known read-only tool."""
    name = field(event, "tool_name", "toolName") or ""
    name = re.sub(r"[^a-z0-9]", "", name.lower())
    args = field(event, "tool_input", "toolInput") or {}
    if not isinstance(args, dict):
        args = {"command": args} if isinstance(args, str) else {}
    if name in READ_TOOLS:
        return []
    if name in WRITE_TOOLS:
        def targets(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [p for v in value for p in targets(v)]
            if isinstance(value, dict):
                return [p for k in ("file_path", "path", "filePath", "notebook_path", "target_file",
                                   "paths", "file_paths", "files", "edits", "operations")
                        for p in targets(value.get(k))]
            return []
        paths = targets(args)
        return paths or None
    if name in {"applypatch", "functionsapplypatch"}:
        text = args.get("command", args.get("input", args.get("patch", "")))
        paths = re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)\r?$",
                           text, re.M) if isinstance(text, str) else []
        return [p.strip() for p in paths] or None
    return None


def relative_paths(root, paths, cwd):
    if paths is None:
        return None
    names = set()
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            if not cwd:
                raise CheckError("事件缺少 cwd，无法确定相对写入路径")
            path = Path(cwd) / path
        path = path.resolve()
        if path.is_relative_to(root):
            name = path.relative_to(root).as_posix()
            if not any(p in SKIP_DIRS or p.startswith(".change-check") for p in Path(name).parts):
                names.add(name)
    return sorted(names)


def capture(root, names=None, bodies=True):
    """No Git baseline. Text before-images are bounded; binary bodies are never saved."""
    result, total = {}, 0
    paths = walk_files(root) if names is None else ((n, root / n) for n in names)
    for name, path in paths:
        # Junction targets are separate registered projects, not implicit ownership.
        if not path.resolve().is_relative_to(root):
            continue
        if not path.exists():
            result[name] = {"hash": None}
            continue
        if not path.is_file():
            continue
        text = bodies and is_text(name)
        if text:
            total += path.stat().st_size
            if total > MAX_BYTES:
                raise CheckError("会话执行前文本超过 40 MiB，无法完整记录检查基线")
            raw = path.read_bytes()
            result[name] = {"hash": digest(raw), "body": base64.b64encode(raw).decode("ascii")}
        else:
            with path.open("rb") as stream:
                result[name] = {"hash": hashlib.file_digest(stream, "sha256").hexdigest()}
    return result


def overlap(left, right):
    if left is None and right is None:
        return ["*"]
    if left is None:
        return right
    if right is None:
        return left
    return sorted(set(left) & set(right))


def mark_overlaps(path, state, call, pending):
    # The caller holds the root lock. Persist both sides before either tool executes.
    for other_path in path.parent.glob("*.json"):
        other = state if other_path == path else read_json(other_path, {})
        changed = False
        for other_call, active in other.get("pending", {}).items():
            if other_path == path and other_call == call:
                continue
            common = overlap(pending["paths"], active["paths"])
            if common:
                pending["conflicts"] = sorted(set(pending["conflicts"]) | set(common))
                active["conflicts"] = sorted(set(active["conflicts"]) | set(common))
                changed = True
        if changed and other_path != path:
            save_artifact(other_path, other)


def track(item, agent, event):
    """Update one root's ledger. Return state, storage path, and whether to check."""
    key = session_key(agent, event)
    if not key:
        return None, None, False
    path = state_path(item, key)
    state = read_json(path, None)
    name = field(event, "hook_event_name", "hookEventName")
    if name == "SessionStart":
        save_artifact(path, state or new_state(agent))  # resume/compact retain the ledger
        return state, path, False
    if name == "Stop":
        # Stop is emitted after questions too. Never inspect files just to rediscover
        # an unchanged report; a pending summary is rendered by the caller from state.
        return state, path, bool(state and (state.get("changes") or state.get("feedback_pending")
                                           or sorted(state["pending"]) != state.get("pending_notice", [])
                                           or (not state.get("cache") and not state.get("stop_seen"))))
    paths = tool_scope(event)
    if paths == []:
        return state, path, False
    root = Path(item["path"]).resolve()
    paths = relative_paths(root, paths, event.get("cwd"))
    if paths == []:
        return state, path, False
    state = state or new_state(agent)
    call = field(event, "tool_use_id", "toolUseId")
    if not isinstance(call, str) or not call:
        state["warnings"] = sorted(set(state["warnings"]) | {"事件缺少工具调用编号，无法配对写入"})
        state["feedback_pending"] = True
        save_artifact(path, state)
        return state, path, name != "PreToolUse"
    call = digest(call)
    if name == "PreToolUse":
        if call in state["pending"]:
            return state, path, False  # duplicate delivery must not overwrite the baseline
        if len(state["pending"]) >= 16:
            raise CheckError("本会话有过多未完成写入调用；请结束后台任务后新开会话")
        pending = {"paths": paths, "before": capture(root, paths), "conflicts": [],
                   "started": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True)
        mark_overlaps(path, state, call, pending)
        state["pending"][call] = pending
        save_artifact(path, state)
        return state, path, False
    if name not in {"PostToolUse", "PostToolUseFailure"}:
        return state, path, False
    pending = state["pending"].pop(call, None)
    if pending is None:
        if call in state.get("completed", []):
            return state, path, False
        state["warnings"] = sorted(set(state["warnings"]) | {"缺少执行前基线，未扩大到仓库历史变动"})
        state["feedback_pending"] = True
        save_artifact(path, state)
        return state, path, True
    tool_input = field(event, "tool_input", "toolInput") or {}
    if isinstance(tool_input, dict) and tool_input.get("run_in_background"):
        state["warnings"] = sorted(set(state["warnings"]) | {
            "后台命令返回不代表写入结束；其变化未归入本会话，请完成后手动检查并通过提交门禁"})
        state["feedback_pending"] = True
        save_artifact(path, state)
        return state, path, True
    after = capture(root, pending["paths"], bodies=False)
    changed = [n for n in set(after) | set(pending["before"])
               if after.get(n, {}).get("hash") != pending["before"].get(n, {}).get("hash")]
    if changed:
        state["feedback_pending"] = True
    for n in changed:
        before = pending["before"].get(n, {"hash": None})
        value = after.get(n, {}).get("hash")
        old = state["files"].get(n)
        if ("*" in pending["conflicts"] or n in pending["conflicts"] or
                (old and old["after"] != before["hash"])):
            state["uncertain"] = sorted(set(state["uncertain"]) | {n})
            continue
        baseline = old["before"] if old else before
        changes = state.setdefault("changes", {})
        checkpoint = state.get("checkpoints", {}).get(n)
        previous = changes.get(n, {}).get("before", checkpoint or before)
        changes[n] = {"before": previous, "after": value,
                      "method": "tool_delta" if paths is None else "explicit_path"}
        if baseline["hash"] == value:
            state["files"].pop(n, None)  # restored to this session's starting content
        else:
            state["files"][n] = {"before": baseline, "after": value,
                                    "method": "tool_delta" if paths is None else "explicit_path"}
    state["completed"] = (state.get("completed", []) + [call])[-100:]
    save_artifact(path, state)
    return state, path, bool(changed or state["warnings"])


def scoped_snapshot(item, state, incremental=False):
    """Never substitute a full Git diff for missing/ambiguous session evidence."""
    warnings = list(state.get("warnings", [])) if state else ["没有本会话跟踪记录，请新开会话启用钩子"]
    if not state:
        return None, warnings
    if state["pending"]:
        warnings.append("有写入调用尚未收到完成事件，后台任务和未完成调用不计入本次检查")
    root = Path(item["path"]).resolve()
    records = state.get("changes", {}) if incremental else state["files"]
    current = capture(root, sorted(records), bodies=False)
    names = set()
    uncertain = set(state["uncertain"])
    for name, value in records.items():
        actual = current.get(name, {}).get("hash")
        if actual != value["after"]:
            uncertain.add(name)
        elif name not in uncertain and (not incremental or actual != value["before"]["hash"]):
            names.add(name)
    if uncertain:
        warnings.append("以下文件并发写入或随后被其他操作改动，归属不明，未算作本会话修改：" +
                        "、".join(sorted(uncertain)[:20]))
    if not names:
        return None, warnings
    snap = load_snapshot(root, changed_files=names)
    snap.before = {n: base64.b64decode(records[n]["before"]["body"])
                   for n in names if "body" in records[n]["before"]}
    snap.scope = {"kind": "session", "agent": state["agent"],
                  "methods": sorted({records[n]["method"] for n in names})}
    # A race between attribution and snapshot creation must not check someone else's edit.
    verified = capture(root, sorted(names), bodies=False)
    if any(verified.get(n, {}).get("hash") != records[n]["after"] or
           (n in snap.data and digest(snap.data[n]) != records[n]["after"]) for n in names):
        raise CheckError("捕获会话检查输入期间文件变化，请重试")
    return snap, warnings
