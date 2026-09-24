from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from unittest.mock import patch

from test_changecheck import Fixture, STUB
from changecheck import cli, review, storage
from changecheck.common import CheckError, add_root, find_root, home, read_json, save_json, update_root


class Retention(Fixture):
    def artifact(self, category="reviews", number=1, age=0, size=10):
        name = (f"{self.item['id']}/staged/{number:064x}.json" if category == "reviews" else
                f"{self.item['id']}-{number}.json" if category == "backups" else self.item["id"] + ".json")
        path = home() / category / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size)
        stamp = time.time() - age * 86400 - number
        os.utime(path, (stamp, stamp))
        return path

    def test_defaults_and_preview_do_not_delete(self):
        expired = self.artifact(age=31)
        result = storage.cleanup(True)
        self.assertEqual(result["policy"], storage.DEFAULTS)
        self.assertEqual(result["selected"][0]["reason"], "expired")
        self.assertTrue(expired.exists())

    def test_age_and_count_limits_preserve_configuration(self):
        self.artifact(number=1)
        extra = self.artifact(number=2)
        expired = self.artifact("reports", age=31)
        registry = (home() / "registry.json").read_bytes()
        storage.configure({"review_limit": 1})
        self.assertTrue(extra.exists())
        storage.cleanup()
        self.assertFalse(extra.exists())
        self.assertFalse(expired.exists())
        self.assertEqual((home() / "registry.json").read_bytes(), registry)

    def test_modes_have_separate_count_limits(self):
        staged = self.artifact()
        worktree = staged.parent.parent / "worktree" / staged.name
        worktree.parent.mkdir()
        worktree.write_bytes(staged.read_bytes())
        storage.configure({"review_limit": 1})
        storage.cleanup()
        self.assertTrue(staged.exists() and worktree.exists())

    def test_global_byte_limit_removes_oldest_across_categories(self):
        latest = self.artifact("reports", number=1, size=700000)
        oldest = self.artifact("reviews", number=2, size=700000)
        storage.configure({"max_mib": 1})
        result = storage.cleanup()
        self.assertLessEqual(result["after_bytes"], 1024 * 1024)
        self.assertTrue(latest.exists())
        self.assertFalse(oldest.exists())

    def test_backup_limit_is_per_source_configuration(self):
        for number in range(1, 5):
            self.artifact("backups", number=number)
        storage.cleanup()
        self.assertEqual(len(list((home() / "backups").glob("*.json"))), 3)

    def test_unknown_files_and_linked_folders_are_not_cleaned(self):
        path = self.artifact(age=31)
        unknown = path.parent / "personal-notes.json"
        unknown.write_text("keep")
        real_linked = storage.linked
        with patch("changecheck.storage.linked", side_effect=lambda p: p == home() / "reviews" or real_linked(p)):
            self.assertEqual(storage.cleanup()["selected"], [])
        self.assertTrue(path.exists())
        storage.cleanup()
        self.assertTrue(unknown.exists())

    def test_invalid_policy_does_not_replace_valid_configuration(self):
        storage.configure({"days": 10})
        before = (home() / "retention.json").read_bytes()
        with self.assertRaises(CheckError):
            storage.configure({"days": 0})
        self.assertEqual((home() / "retention.json").read_bytes(), before)

    def test_generated_write_enforces_retention(self):
        expired = self.artifact(age=31)
        storage.save_artifact(home() / "reports" / (self.item["id"] + ".json"), {"status": "ok"})
        self.assertFalse(expired.exists())

    def test_expired_approval_is_never_reused(self):
        self.change()
        snap, report = self.check()
        review.review_snapshot(snap, self.item, report)
        cached = next((home() / "reviews" / self.item["id"] / "staged").glob("*.json"))
        old = time.time() - 31 * 86400
        os.utime(cached, (old, old))
        self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
        self.assertEqual(self.counter.read_text(), "2")

    def test_cli_storage_and_cleanup(self):
        expired = self.artifact(age=31)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["storage", "--days", "14"]), 0)
            self.assertEqual(cli.main(["cleanup", "--dry-run"]), 0)
            self.assertTrue(expired.exists())
            self.assertEqual(cli.main(["cleanup"]), 0)
        self.assertFalse(expired.exists())


class ReviewerSelection(Fixture):
    def test_fresh_root_defaults_to_claude(self):
        root = self.base / "another"
        root.mkdir()
        self.assertEqual(add_root(root)["profile"]["reviewer"]["kind"], "claude")

    def test_default_follows_single_agent_and_prefers_claude_for_multiple(self):
        for agents, expected in [(["claude"], "claude"), (["codex"], "codex"), (["kimi"], "kimi"),
                                 (["zcode"], "zcode"), (["codex", "claude"], "claude"), ([], "claude")]:
            with self.subTest(agents=agents):
                self.assertEqual(cli.suggested_reviewer(agents, {}), expected)

    def test_manual_choice_is_preserved_on_reinstall(self):
        profile = {"reviewer_selected": True, "reviewer": {"kind": "codex"}}
        self.assertEqual(cli.suggested_reviewer(["claude"], profile), "codex")

    def test_switching_provider_drops_incompatible_model_and_program(self):
        before = {"kind": "codex", "model": "old-model", "program": "codex.exe", "timeout": 15}
        self.assertEqual(cli.select_kind(before, "claude"), {"kind": "claude", "timeout": 15})
        self.assertEqual(before["model"], "old-model")

    def test_interactive_installer_explicitly_offers_reviewer_and_model(self):
        args = cli.parser().parse_args(["install", "--skip-login-check"])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["", "test-model"]) as prompt, \
                patch("changecheck.cli.claude_command", return_value=[sys.executable]), contextlib.redirect_stdout(io.StringIO()):
            cli.install_reviewer(args, ["claude"], self.item)
        self.assertIn("[claude]", prompt.call_args_list[0].args[0])
        self.assertIn("审查模型", prompt.call_args_list[1].args[0])
        self.assertEqual(self.item["profile"]["reviewer"]["model"], "test-model")

    def test_unsupported_agent_requires_explicit_reviewer(self):
        args = cli.parser().parse_args(["install", "--skip-login-check"])
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(CheckError, "kimi 尚无内置审查适配"):
            cli.install_reviewer(args, ["kimi"], self.item)

    def test_noninteractive_default_and_explicit_override(self):
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()), \
                patch("changecheck.cli.codex_command", return_value=[sys.executable]), \
                patch("changecheck.cli.claude_command", return_value=[sys.executable]):
            args = cli.parser().parse_args(["install", "--skip-login-check"])
            cli.install_reviewer(args, ["codex"], self.item)
            self.assertEqual(self.item["profile"]["reviewer"]["kind"], "codex")
            args = cli.parser().parse_args(["install", "--reviewer", "claude", "--skip-login-check"])
            cli.install_reviewer(args, ["codex"], self.item)
            self.assertEqual(self.item["profile"]["reviewer"]["kind"], "claude")

    def test_claude_install_and_uninstall_in_isolated_home(self):
        with patch("changecheck.cli.claude_command", return_value=[sys.executable]), \
                patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            args = ["install", "--root", str(self.root), "--agents", "claude", "--skip-login-check"]
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(find_root(self.root)["profile"]["reviewer"]["kind"], "claude")
            self.assertEqual(cli.main(["uninstall"]), 0)


class ClaudeReviews(Fixture):
    def fake(self, mode="ok", model="test-claude"):
        capture = self.base / "claude-args.json"
        script = self.base / "claude-fixture.py"
        prefix = "import json,pathlib,sys\nargs=sys.argv[1:]\n"
        prefix += "pathlib.Path(" + repr(str(capture)) + ").write_text(json.dumps(args))\n"
        if mode == "exit":
            prefix += "sys.stderr.write('model_not_found');sys.exit(7)\n"
        elif mode in ("error", "error_exit"):
            prefix += "print(json.dumps({'is_error':True,'subtype':'error_during_execution','errors':['invalid model']}));sys.exit(" + ("7" if mode == "error_exit" else "0") + ")\n"
        elif mode == "missing":
            prefix += "print(json.dumps({'is_error':False,'subtype':'success'}));sys.exit(0)\n"
        prefix += ("inp=pathlib.Path.cwd()/'.change-check-review/review-input.json'\n"
                   "out=inp.parent/'fixture-output.json'\n"
                   "sys.argv=[sys.argv[0],str(inp),str(out),'ok'," + repr(str(self.counter)) + "]\n")
        script.write_text(prefix + STUB + "\nprint(json.dumps({'is_error':False,'subtype':'success','structured_output':json.loads(pathlib.Path(out).read_text())}))\n", encoding="utf-8")
        self.item["profile"]["reviewer"] = {"kind": "claude", "timeout": 10}
        if model:
            self.item["profile"]["reviewer"]["model"] = model
        update_root(self.item)
        return script, capture

    def test_claude_model_tools_and_structured_result(self):
        script, capture = self.fake()
        self.change()
        snap, report = self.check()
        with patch("changecheck.review.claude_command", return_value=[sys.executable, str(script)]):
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
            self.assertTrue(review.review_snapshot(snap, self.item, report)[1])
        args = read_json(capture)
        self.assertEqual(args[args.index("--model") + 1], "test-claude")
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--no-session-persistence", args)
        self.assertIn("--safe-mode", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--fallback-model", args)
        self.assertEqual(self.counter.read_text(), "1")

    def test_claude_default_model_does_not_use_cache(self):
        script, _ = self.fake(model=None)
        self.change()
        snap, report = self.check()
        with patch("changecheck.review.claude_command", return_value=[sys.executable, str(script)]):
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
        self.assertEqual(self.counter.read_text(), "2")

    def test_claude_failures_are_reported_without_approval(self):
        self.change()
        for mode, message in [("exit", "model_not_found"), ("error", "invalid model"),
                              ("error_exit", "invalid model"), ("missing", "structured_output")]:
            with self.subTest(mode=mode):
                script, _ = self.fake(mode)
                snap, report = self.check()
                with patch("changecheck.review.claude_command", return_value=[sys.executable, str(script)]), \
                        self.assertRaisesRegex(CheckError, "审查器=claude，模型=test-claude.*" + message):
                    review.review_snapshot(snap, self.item, report)
                record = read_json(home() / "reports" / (self.item["id"] + "-staged-review.json"))
                self.assertEqual(record["status"], "failed")
        self.assertFalse((home() / "reviews" / self.item["id"]).exists())
