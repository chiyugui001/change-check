from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from changecheck import feedback


def finding(severity="ERROR", rule="TEST001", line=1, message="test error", file="example.py"):
    return {"severity": severity, "rule": rule, "line": line, "file": file,
            "message": message, "source": "fixture"}


class Feedback(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def notify(self, *findings, event="PostToolUse", active=False, warnings=()):
        report = {"changed": ["example.py"], "findings": list(findings), "exit_code": int(bool(findings)),
                  "ai_review": "not_run"}
        original = copy.deepcopy(report)
        result = feedback.prepare(self.state, report, list(warnings), event, active, Path("session.json"))
        self.assertEqual(report, original, "Notification must not erase findings or approve a report")
        return result

    def test_success_is_completely_silent_on_every_platform(self):
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            message, block = self.notify(event=event)
            for agent in ("claude", "codex", "kimi", "zcode"):
                self.assertEqual(feedback.render(agent, event, message, block), (0, "", ""))

    def test_reviews_wait_until_stop_then_summarize_once_without_blocking(self):
        findings = [finding("REVIEW", "D004", line=n, message="候选" * 100) for n in range(1, 24)]
        self.assertEqual(self.notify(*findings), ("", False))
        message, block = self.notify(*findings, event="Stop")
        self.assertFalse(block)
        self.assertIn("0 ERROR、23 REVIEW", message)
        self.assertNotIn("候选" * 10, message)
        self.assertLess(len(message), 400)
        code, out, err = feedback.render("claude", "Stop", message, block)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(set(json.loads(out)), {"systemMessage"})
        self.assertEqual(self.notify(*findings, event="Stop"), ("", False))

    def test_error_details_are_not_repeated_but_first_stop_still_blocks(self):
        f = finding()
        self.assertTrue(self.notify(f)[0])
        self.assertEqual(self.notify(f), ("", False))
        message, block = self.notify(f, event="Stop")
        self.assertTrue(block)
        self.assertIn("确定错误", message)
        self.assertEqual(self.notify(f, event="Stop", active=True), ("", False))
        self.assertEqual(self.notify(f, event="Stop"), ("", False))

    def test_new_error_after_previous_stop_can_block_once(self):
        self.notify(finding(), event="Stop")
        message, block = self.notify(finding(), finding(rule="NEW002"), event="Stop")
        self.assertTrue(block)
        self.assertIn("NEW002", message)
        self.assertEqual(self.notify(finding(), finding(rule="NEW002"), event="Stop"), ("", False))

    def test_fixed_then_reintroduced_error_is_not_suppressed(self):
        self.notify(finding(), event="Stop")
        self.assertEqual(self.notify(event="Stop"), ("", False))
        self.assertTrue(self.notify(finding(), event="Stop")[1])

    def test_line_shift_does_not_create_a_new_notification(self):
        self.notify(finding(line=2), event="Stop")
        self.assertEqual(self.notify(finding(line=200), event="Stop"), ("", False))

    def test_warning_is_deduplicated_and_can_reappear_after_recovery(self):
        self.assertTrue(self.notify(warnings=["范围未确认"])[0])
        self.assertEqual(self.notify(warnings=["范围未确认"], event="Stop"), ("", False))
        self.notify()
        self.assertTrue(self.notify(warnings=["范围未确认"], event="Stop")[0])

    def test_active_stop_does_not_restart_the_continuation_loop(self):
        message, block = self.notify(finding(), event="Stop", active=True)
        self.assertFalse(block)
        self.assertTrue(message)
        self.assertEqual(self.notify(finding(), event="Stop"), ("", False))

    def test_new_review_count_gets_one_updated_summary(self):
        f = finding("REVIEW")
        self.notify(f, event="Stop")
        self.assertEqual(self.notify(f, f), ("", False))
        message, block = self.notify(f, f, event="Stop")
        self.assertFalse(block)
        self.assertIn("2 REVIEW", message)
        self.assertEqual(self.notify(f, f, event="Stop"), ("", False))

    def test_many_long_errors_are_bounded_but_report_remains_complete(self):
        findings = [finding(rule=f"RULE{i}", message="x" * 10000) for i in range(100)]
        message, block = self.notify(*findings, event="Stop")
        self.assertTrue(block)
        self.assertLess(len(message), 1200)
        self.assertIn("100 ERROR", message)
        self.assertIn("其余 97 组", message)

    def test_multiple_root_output_is_bounded(self):
        code, output, _ = feedback.render("claude", "Stop", "x" * 12000, True)
        self.assertEqual(code, 0)
        self.assertLess(len(json.loads(output)["reason"]), 3500)

    def test_session_notice_state_is_isolated(self):
        self.notify(finding())
        self.state = {}
        self.assertTrue(self.notify(finding())[0])

    def test_kimi_uses_exit_two_only_for_a_block(self):
        self.assertEqual(feedback.render("kimi", "Stop", "review summary", False), (0, "review summary", ""))
        self.assertEqual(feedback.render("kimi", "Stop", "fix error", True), (2, "", "fix error"))
