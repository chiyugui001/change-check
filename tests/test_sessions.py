from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import time
from unittest.mock import patch

from test_changecheck import Fixture, doc
from changecheck import cli, sessions, storage
from changecheck.common import add_root, digest, home, read_json, save_json, update_root
from changecheck.engine import run_checks


class Sessions(Fixture):
    def setUp(self):
        super().setUp()
        self.item["profile"]["agents"] = ["codex", "claude", "kimi", "zcode"]
        update_root(self.item)

    def event(self, kind, session="one", call="write-1", tool="Edit", args=None, agent="codex", **extra):
        return {"hook_event_name": kind, "session_id": session, "tool_use_id": call,
                "tool_name": tool, "tool_input": args or {"file_path": str(self.root / "中文.md")},
                "cwd": str(self.root), **extra}

    def track(self, kind, agent="codex", **kwargs):
        return sessions.track(self.item, agent, self.event(kind, **kwargs))

    def invoke(self, kind, agent="codex", **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(self.event(kind, **kwargs)))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["hook", "--agent", agent])
        self.assertEqual(code, 0, err.getvalue())
        return (json.loads(out.getvalue()) if out.getvalue().strip() else {}) if agent != "kimi" else out.getvalue()

    def snapshot(self, session="one", agent="codex"):
        state, _, _ = self.track("Stop", agent=agent, session=session)
        return sessions.scoped_snapshot(self.item, state)

    def test_no_write_session_skips_existing_modified_and_untracked_files(self):
        self.change("# 历史\n[[缺失]]", staged=False)
        self.write("历史文件.md", "invalid")
        self.invoke("SessionStart")
        self.assertEqual(self.invoke("Stop"), {})
        report = read_json(home() / "reports" / (self.item["id"] + ".json"))
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["status"], "skipped_no_changes")
        self.assertFalse(self.counter.exists())

    def test_read_tool_does_not_claim_concurrent_writes(self):
        self.track("SessionStart")
        self.track("PreToolUse", tool="Read")
        self.change(staged=False)
        self.track("PostToolUse", tool="Read")
        snap, warnings = self.snapshot()
        self.assertIsNone(snap)
        self.assertEqual(warnings, [])

    def test_changed_file_uses_prewrite_dirty_content_as_baseline(self):
        before = self.write("中文.md", doc("# 别人的已有变动\n")).read_bytes()
        self.write("不属于本会话.md", "invalid")
        self.track("PreToolUse")
        self.change(staged=False)
        self.track("PostToolUse")
        snap, warnings = self.snapshot()
        self.assertEqual(snap.changed, {"中文.md"})
        self.assertEqual(snap.before, {"中文.md": before})
        self.assertEqual(warnings, [])

    def test_two_sessions_have_separate_files_and_caches(self):
        other = {"file_path": str(self.root / "other.py")}
        self.invoke("PreToolUse")
        self.invoke("PreToolUse", session="two", args=other)
        self.change(staged=False)
        self.invoke("PostToolUse")
        self.write("other.py", "value = 2\n")
        self.invoke("PostToolUse", session="two", args=other)
        self.assertEqual(self.snapshot()[0].changed, {"中文.md"})
        self.assertEqual(self.snapshot("two")[0].changed, {"other.py"})
        states = [read_json(p) for p in (home() / "sessions" / self.item["id"]).glob("*.json")]
        self.assertEqual({tuple(s["cache"]["report"]["changed"]) for s in states},
                         {("中文.md",), ("other.py",)})

    def test_same_session_id_on_two_agents_is_isolated(self):
        self.track("SessionStart", agent="claude")
        self.track("PreToolUse")
        self.change(staged=False)
        self.track("PostToolUse")
        self.assertIsNone(self.snapshot(agent="claude")[0])
        self.assertEqual(self.snapshot()[0].changed, {"中文.md"})

    def test_identical_write_and_revert_have_no_net_changes(self):
        original = (self.root / "中文.md").read_bytes()
        self.track("PreToolUse")
        self.track("PostToolUse")
        self.assertIsNone(self.snapshot()[0])
        self.track("PreToolUse", call="2")
        self.change(staged=False)
        self.track("PostToolUse", call="2")
        self.track("PreToolUse", call="3")
        (self.root / "中文.md").write_bytes(original)
        self.track("PostToolUse", call="3")
        self.assertEqual(self.snapshot(), (None, []))

    def test_apply_patch_rename_and_delete_capture_both_paths(self):
        args = {"command": "*** Begin Patch\n*** Update File: 中文.md\n*** Move to: renamed.md\n*** End Patch"}
        before = (self.root / "中文.md").read_bytes()
        self.track("PreToolUse", tool="apply_patch", args=args)
        (self.root / "中文.md").rename(self.root / "renamed.md")
        self.track("PostToolUse", tool="apply_patch", args=args)
        snap, warnings = self.snapshot()
        self.assertEqual(snap.changed, {"中文.md", "renamed.md"})
        self.assertEqual(snap.before, {"中文.md": before})
        self.assertEqual(warnings, [])

    def test_shell_uses_tool_delta_even_when_command_fails(self):
        self.write("历史.md", "invalid")
        self.track("PreToolUse", tool="Bash", args={"command": "some-generator"})
        self.write("generated.py", "broken(\n")
        self.track("PostToolUseFailure", tool="Bash", args={"command": "some-generator"})
        snap, warnings = self.snapshot()
        self.assertEqual(snap.changed, {"generated.py"})
        self.assertEqual(snap.scope["methods"], ["tool_delta"])
        self.assertEqual(warnings, [])

    def test_overlapping_shell_and_edit_are_not_attributed(self):
        self.track("PreToolUse", tool="Bash")
        self.track("PreToolUse", session="two")
        self.change(staged=False)
        self.track("PostToolUse", session="two")
        self.track("PostToolUse", tool="Bash")
        for session in ("one", "two"):
            snap, warnings = self.snapshot(session)
            self.assertIsNone(snap)
            self.assertIn("归属不明", "".join(warnings))

    def test_later_external_edit_is_not_relabelled_as_session_change(self):
        self.track("PreToolUse")
        self.change(staged=False)
        self.track("PostToolUse")
        self.write("中文.md", doc("# 其他编辑器修改\n"))
        snap, warnings = self.snapshot()
        self.assertIsNone(snap)
        self.assertIn("归属不明", "".join(warnings))

    def test_missing_pre_or_session_never_falls_back_to_git_diff(self):
        self.change(staged=False)
        for kwargs in ({}, {"session": None}):
            result = self.invoke("PostToolUse", **kwargs)
            self.assertIn("未", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("decision", result)
        self.assertFalse(self.counter.exists())

    def test_pending_background_call_does_not_import_full_worktree_at_stop(self):
        self.track("PreToolUse", tool="Bash")
        self.change(staged=False)
        snap, warnings = self.snapshot()
        self.assertIsNone(snap)
        self.assertIn("未完成", "".join(warnings))

    def test_background_launch_response_is_not_treated_as_completion(self):
        args = {"command": "generator", "run_in_background": True}
        self.track("PreToolUse", tool="Bash", args=args)
        self.change(staged=False)
        self.track("PostToolUse", tool="Bash", args=args)
        snap, warnings = self.snapshot()
        self.assertIsNone(snap)
        self.assertIn("手动检查", "".join(warnings))

    def test_duplicate_pre_post_and_resume_preserve_initial_baseline(self):
        original = (self.root / "中文.md").read_bytes()
        self.track("PreToolUse")
        self.change(staged=False)
        self.track("PreToolUse")
        self.track("PostToolUse")
        self.track("PostToolUse")
        self.track("SessionStart", source="resume")
        self.assertEqual(self.snapshot()[0].before["中文.md"], original)

    def test_no_git_root_uses_session_delta(self):
        self.root = self.base / "without-git"
        self.root.mkdir()
        self.item = add_root(self.root)
        self.write("old.py", "broken(\n")
        self.track("PreToolUse", tool="Write", args={"path": "new.py"})
        self.write("new.py", "value = 1\n")
        self.track("PostToolUse", tool="Write", args={"path": "new.py"})
        self.assertEqual(self.snapshot()[0].changed, {"new.py"})

    def test_multi_edit_and_camel_case_protocol(self):
        event = {"sessionId": "camel", "hookEventName": "PreToolUse", "toolUseId": "multi",
                 "toolName": "MultiEdit", "cwd": str(self.root),
                 "toolInput": {"edits": [{"filePath": "a.py"}, {"filePath": "b.py"}]}}
        sessions.track(self.item, "zcode", event)
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 1\n")
        event["hookEventName"] = "PostToolUse"
        state, _, _ = sessions.track(self.item, "zcode", event)
        self.assertEqual(sessions.scoped_snapshot(self.item, state)[0].changed, {"a.py", "b.py"})

    def test_expired_or_evicted_state_does_not_reuse_a_pass(self):
        self.track("PreToolUse")
        self.change(staged=False)
        _, path, _ = self.track("PostToolUse")
        old = time.time() - 31 * 86400
        os.utime(path, (old, old))
        storage.cleanup()
        self.assertFalse(path.exists())
        snap, warnings = self.snapshot()
        self.assertIsNone(snap)
        self.assertTrue(warnings)

    def test_session_storage_count_limit(self):
        storage.configure({"session_limit": 2})
        for n in range(4):
            self.track("SessionStart", session=str(n))
        self.assertEqual(len(list((home() / "sessions" / self.item["id"]).glob("*.json"))), 2)

    def test_ignored_binary_write_is_fingerprinted_and_not_silently_passed(self):
        self.write(".gitignore", "*.bin\n")
        args = {"path": "asset.bin"}
        self.track("PreToolUse", tool="Write", args=args)
        (self.root / "asset.bin").write_bytes(b"\x00binary")
        self.track("PostToolUse", tool="Write", args=args)
        snap, _ = self.snapshot()
        self.assertEqual(snap.identities["asset.bin"], digest(b"\x00binary"))
        report = run_checks(snap, self.item)
        self.assertTrue(any(f["rule"] == "CORE001" for f in report["findings"]))
        previous = snap.fingerprint()
        self.track("PreToolUse", tool="Write", args=args, call="binary2")
        (self.root / "asset.bin").write_bytes(b"\x00changed")
        self.track("PostToolUse", tool="Write", args=args, call="binary2")
        self.assertNotEqual(self.snapshot()[0].fingerprint(), previous)

    def test_session_tracking_never_changes_staged_scope(self):
        self.write("old.py", "invalid(\n")
        from changecheck.common import git
        from changecheck.repository import load_snapshot
        git(self.root, "add", "old.py")
        self.track("PreToolUse")
        self.change(staged=False)
        self.track("PostToolUse")
        self.assertEqual(self.snapshot()[0].changed, {"中文.md"})
        self.assertEqual(load_snapshot(self.root, staged=True).changed, {"old.py"})

    def test_real_script_finding_and_stop_recursion_guard(self):
        self.invoke("PreToolUse")
        self.change("# 内容\n\n[[缺失]]\n", staged=False)
        self.invoke("PostToolUse")
        self.assertEqual(self.invoke("Stop")["decision"], "block")
        self.assertNotIn("decision", self.invoke("Stop", stop_hook_active=True))
        self.assertEqual(self.invoke("Stop"), {})
        self.assertFalse(self.counter.exists())

    def test_claude_cached_candidates_only_show_one_nonblocking_stop_summary(self):
        self.invoke("PreToolUse", agent="claude")
        self.change(staged=False)
        report = {"changed": ["中文.md"], "exit_code": 1, "ai_review": "not_run",
                  "findings": [{"severity": "REVIEW", "rule": "D004", "file": "中文.md",
                                "line": n + 1, "message": "保真候选"} for n in range(23)]}
        with patch.object(cli, "run_checks", return_value=report) as check:
            self.assertEqual(self.invoke("PostToolUse", agent="claude"), {})
            self.assertEqual(self.invoke("PostToolUse", agent="claude"), {})
            summary = self.invoke("Stop", agent="claude")
            self.assertEqual(set(summary), {"systemMessage"})
            self.assertIn("23 REVIEW", summary["systemMessage"])
            self.assertEqual(self.invoke("Stop", agent="claude"), {})
            self.assertEqual(check.call_count, 1)
        saved = read_json(home() / "reports" / (self.item["id"] + ".json"))
        self.assertEqual(saved["findings"], report["findings"])
        self.assertEqual(saved["exit_code"], 1)
        self.assertEqual(saved["ai_review"], "not_run")
        self.assertFalse(self.counter.exists())
