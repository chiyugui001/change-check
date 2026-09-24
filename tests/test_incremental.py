from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import patch

import test_sessions
from test_changecheck import Fixture, doc
from changecheck import cli, daily, sessions
from changecheck.common import CheckError, home, read_json, save_json, update_root
from changecheck.engine import run_checks


class Incremental(Fixture):
    event = test_sessions.Sessions.event
    invoke = test_sessions.Sessions.invoke

    def setUp(self):
        super().setUp()
        self.item["profile"]["agents"] = ["claude", "codex"]
        update_root(self.item)
        self.serial = 0

    def edit(self, name, text):
        self.serial += 1
        args = {"file_path": str(self.root / name)}
        self.invoke("PreToolUse", agent="claude", call=str(self.serial), args=args)
        if text is None:
            (self.root / name).unlink()
        else:
            self.write(name, text)
        return self.invoke("PostToolUse", agent="claude", call=str(self.serial), args=args)

    def state(self):
        key = sessions.session_key("claude", self.event("Stop"))
        return read_json(sessions.state_path(self.item, key))

    def report(self):
        return self.state()["cache"]["report"]

    def plain(self):
        self.item["profile"]["modules"] = ["code", "config", "document"]
        update_root(self.item)

    def test_question_stop_never_reads_project_or_hashes_rules(self):
        self.edit("中文.md", doc("# 文档\n\n暂按现有接口处理。\n"))
        with patch.object(sessions, "scoped_snapshot", side_effect=AssertionError("snapshot on question")), \
                patch.object(cli, "implementation_fingerprint", side_effect=AssertionError("rule hashing on question")), \
                patch.object(cli, "run_checks", side_effect=AssertionError("checker on question")):
            self.invoke("Stop", agent="claude")  # Deliver this write's one pending summary.
            self.assertEqual(self.invoke("Stop", agent="claude"), {})  # A later question.
            self.invoke("PreToolUse", agent="claude", tool="Read", call="read")
            self.invoke("PostToolUse", agent="claude", tool="Read", call="read")
            self.assertEqual(self.invoke("Stop", agent="claude"), {})

    def test_second_file_reuses_first_file_and_its_unresolved_error(self):
        self.plain()
        seen = []
        def run(snap, item):
            seen.append(set(snap.changed))
            return run_checks(snap, item)
        with patch.object(cli, "run_checks", side_effect=run):
            self.edit("a.py", "invalid(\n")
            self.edit("b.py", "b = 1\n")
        self.assertEqual(seen, [{"a.py"}, {"b.py"}])
        self.assertEqual(self.report()["incremental"]["reused_files"], ["a.py"])
        self.assertTrue(any(f["file"] == "a.py" and f["rule"] == "SCRIPT001" for f in self.report()["findings"]))
        self.assertEqual(self.report()["exit_code"], 1)

    def test_delete_third_step_uses_previous_check_not_session_start(self):
        self.plain()
        first = "# 安装\n\n步骤一\n步骤二\n步骤三\n"
        self.edit("steps.md", first)
        first_bytes = (self.root / "steps.md").read_bytes()
        seen = []
        def run(snap, item):
            seen.append(snap.before["steps.md"])
            return run_checks(snap, item)
        with patch.object(cli, "run_checks", side_effect=run):
            self.edit("steps.md", first.replace("步骤三\n", ""))
        self.assertEqual(seen, [first_bytes])
        self.assertEqual(self.report()["scope"]["ranges"]["steps.md"],
                         [{"kind": "delete", "before": [5, 5], "after": [5, 4]}])

    def test_reverting_session_change_still_checks_deletion_since_checkpoint(self):
        self.plain()
        original = (self.root / "中文.md").read_text(encoding="utf-8")
        self.edit("中文.md", original + "\n新增段落。\n")
        self.edit("中文.md", original)
        self.assertEqual(self.report()["changed"], ["中文.md"])
        self.assertEqual(self.state()["files"], {})
        self.assertTrue(any(r["kind"] == "delete" for r in self.report()["scope"]["ranges"]["中文.md"]))

    def test_deletion_candidates_keep_their_original_evidence_across_later_edits(self):
        sentence = "设备必须先读取探针编号再保存周期数据，提交失败时必须保留原始记录并按原有顺序重新提交。"
        first = doc("# 文档\n\n" + sentence + "\n\n保留段落。\n")
        second = doc("# 文档\n\n保留段落。\n", modified="2026-09-24 12:02:00")
        self.edit("中文.md", first)
        self.edit("中文.md", second)
        lost = [f for f in self.report()["findings"] if f["rule"] == "D004"]
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["primary"]["snapshot"], "historical")
        self.edit("中文.md", second + "\n另一个短段落。\n")
        remaining = [f for f in self.report()["findings"] if f["rule"] == "D004"]
        self.assertEqual(remaining, lost)
        self.edit("中文.md", first)
        self.assertFalse(any(f["id"] == lost[0]["id"] for f in self.report()["findings"]))

    def test_moved_words_do_not_resolve_an_old_preservation_candidate(self):
        from changecheck.repository import Snapshot
        snap = Snapshot(self.root, False, data={"a.md": b"# Other\noriginal wording\n"})
        f = {"file": "a.md", "rule": "D004", "primary": {"text": "original wording"},
             "origin": {"before_hash": "different-context"}}
        self.assertFalse(daily.restored(f, snap))

    def test_unchanged_writing_line_reuses_finding_and_maps_line_number(self):
        text = doc("# 文档\n\n暂按现有接口处理。\n\n普通段落。\n")
        self.edit("中文.md", text)
        before = next(f for f in self.report()["findings"] if f["rule"] == "WR001")
        fresh = []
        def run(snap, item):
            report = run_checks(snap, item)
            fresh.extend(f for f in report["findings"] if f["rule"] == "WR001")
            return report
        with patch.object(cli, "run_checks", side_effect=run):
            self.edit("中文.md", text.replace("暂按", "新增短句。\n\n暂按"))
        after = next(f for f in self.report()["findings"] if f["rule"] == "WR001")
        self.assertEqual(fresh, [])
        self.assertEqual(after["line"], before["line"] + 2)
        self.edit("中文.md", text.replace("暂按现有接口处理。", "按照现有接口处理。"))
        self.assertFalse(any(f["rule"] == "WR001" for f in self.report()["findings"]))

    def test_markdown_comment_change_invalidates_visible_line_reuse(self):
        text = doc("# 文档\n\n开头。\n暂按现有接口处理。\n结尾。\n")
        self.edit("中文.md", text)
        self.edit("中文.md", text.replace("开头。", "开头。<!--").replace("结尾。", "结尾。-->"))
        self.assertFalse(any(f["rule"] == "WR001" for f in self.report()["findings"]))

    def test_c_indentation_reuse_does_not_skip_structural_rules(self):
        self.plain()
        self.edit("a.c", "int main(void)\n{\n\treturn 0;\n}\n")
        self.edit("a.c", "int main(void)\n{\n\treturn 0;\n\n")
        self.assertTrue(any(f["rule"] == "C001" for f in self.report()["findings"]))
        self.assertTrue(any(f["rule"] == "C099" for f in self.report()["findings"]))

    def test_link_target_change_expands_dependent_file_and_resolves_prior_error(self):
        self.write("目标.md", doc("# 目标\n\n## 安装\n"))
        self.edit("中文.md", doc("# 文档\n\n[[目标#安装]]\n"))
        self.edit("目标.md", doc("# 目标\n\n## 配置\n"))
        self.assertIn("中文.md", self.report()["scope"]["expanded_files"])
        self.assertTrue(any(f["file"] == "中文.md" and f["rule"] == "KB003" for f in self.report()["findings"]))
        self.edit("目标.md", doc("# 目标\n\n## 安装\n"))
        self.assertFalse(any(f["file"] == "中文.md" and f["rule"] == "KB003" for f in self.report()["findings"]))

    def test_unmodified_inbound_link_error_is_rechecked_on_target_restoration(self):
        self.write("引用.md", doc("# 引用\n\n[[目标#安装]]\n"))
        self.write("目标.md", doc("# 目标\n\n## 安装\n"))
        self.edit("目标.md", doc("# 目标\n\n## 配置\n"))
        self.assertTrue(any(f["file"] == "引用.md" and f["rule"] == "KB003" for f in self.report()["findings"]))
        self.edit("目标.md", doc("# 目标\n\n## 安装\n"))
        self.assertFalse(any(f["file"] == "引用.md" and f["rule"] == "KB003" for f in self.report()["findings"]))

    def test_failed_checker_does_not_advance_baseline_and_stop_retries_pending_work(self):
        self.plain()
        self.edit("a.py", "a = 1\n")
        checkpoint = self.state()["checkpoints"]["a.py"].copy()
        args = {"file_path": str(self.root / "a.py")}
        self.invoke("PreToolUse", agent="claude", call="fail", args=args)
        self.write("a.py", "a = 2\n")
        event = self.event("PostToolUse", call="fail", args=args)
        with patch.object(cli, "run_checks", side_effect=CheckError("checker failed")), \
                patch("sys.stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["hook", "--agent", "claude"]), 2)
        self.assertEqual(self.state()["checkpoints"]["a.py"], checkpoint)
        self.assertIn("a.py", self.state()["changes"])
        self.invoke("Stop", agent="claude")
        self.assertEqual(self.state()["changes"], {})
        self.assertNotEqual(self.state()["checkpoints"]["a.py"]["hash"], checkpoint["hash"])

    def test_rule_change_waits_for_write_then_invalidates_cached_files(self):
        self.plain()
        self.edit("a.py", "a = 1\n")
        with patch.object(cli, "implementation_fingerprint", return_value="new-rules"):
            self.invoke("Stop", agent="claude")
            self.edit("b.py", "b = 1\n")
        self.assertTrue(self.report()["scope"]["policy_invalidated"])
        self.assertIn("a.py", self.report()["scope"]["expanded_files"])

    def test_external_edit_of_old_file_is_not_relabelled_or_approved(self):
        self.plain()
        self.edit("a.py", "a = 1\n")
        self.write("a.py", "broken(\n")
        self.edit("b.py", "b = 1\n")
        self.assertNotIn("a.py", self.report()["changed"])
        self.assertNotIn("a.py", self.report()["incremental"]["reused_files"])
        self.assertTrue(self.report()["scope_warnings"])
        self.assertFalse(self.counter.exists())

    def test_read_only_shell_does_not_restart_checks_at_stop(self):
        self.plain()
        self.edit("a.py", "a = 1\n")
        self.invoke("Stop", agent="claude")
        with patch.object(sessions, "scoped_snapshot", side_effect=AssertionError("read-only shell check")):
            self.invoke("PreToolUse", agent="claude", tool="Bash", call="query")
            self.invoke("PostToolUse", agent="claude", tool="Bash", call="query")
            self.assertEqual(self.invoke("Stop", agent="claude"), {})

    def test_binary_checkpoint_is_not_reported_as_external_change(self):
        self.plain()
        self.edit("a.bin", "binary")
        self.edit("b.py", "b = 1\n")
        self.assertEqual(self.report()["scope_warnings"], [])
        self.assertIn("a.bin", self.report()["incremental"]["reused_files"])
        self.assertTrue(any(f["file"] == "a.bin" and f["rule"] == "CORE001" for f in self.report()["findings"]))

    def test_legacy_open_candidate_survives_upgrade_with_historical_location(self):
        self.plain()
        self.edit("中文.md", doc("# 文档\n\n新正文。\n"))
        state = self.state()
        state.pop("checkpoints")
        state.pop("checkpoint_policy")
        state["cache"]["report"]["findings"] = [{
            "file": "中文.md", "line": 5, "rule": "D004", "severity": "REVIEW", "message": "旧候选",
            "primary": {"file": "中文.md", "line": 5, "end_line": 5, "snapshot": "baseline", "text": "旧版原文"}}]
        state["cache"]["report"]["exit_code"] = 1
        save_json(sessions.state_path(self.item, sessions.session_key("claude", self.event("Stop"))), state)
        self.edit("other.py", "a = 1\n")
        f = next(f for f in self.report()["findings"] if f["rule"] == "D004")
        self.assertEqual(f["primary"]["snapshot"], "historical")
        self.assertEqual(f["origin"]["kind"], "legacy_change_candidate")

    def test_legacy_report_does_not_trigger_check_or_feedback_on_question(self):
        self.edit("中文.md", doc("# 文档\n\n[[不存在]]\n"))
        state = self.state()
        for key in ("checkpoints", "checkpoint_policy", "changes", "feedback_pending", "stop_seen"):
            state.pop(key, None)
        save_json(sessions.state_path(self.item, sessions.session_key("claude", self.event("Stop"))), state)
        with patch.object(sessions, "scoped_snapshot", side_effect=AssertionError("legacy question scanned")):
            self.assertEqual(self.invoke("Stop", agent="claude"), {})

    def test_binary_rule_invalidation_keeps_snapshot_identity_consistent(self):
        self.plain()
        self.edit("a.bin", "binary")
        with patch.object(cli, "implementation_fingerprint", return_value="new-rules"):
            self.edit("b.py", "b = 1\n")
        self.assertIn("a.bin", self.report()["scope"]["expanded_files"])
        self.assertTrue(any(f["file"] == "a.bin" and f["rule"] == "CORE001" for f in self.report()["findings"]))
