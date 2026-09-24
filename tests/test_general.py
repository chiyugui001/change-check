from __future__ import annotations

import contextlib
import io
import json
import sys
from unittest.mock import patch

from test_changecheck import Fixture, STUB
from changecheck import cli, review, integration
from changecheck.common import CheckError, SOURCE, add_root, digest, git, save_json, update_root
from changecheck.modules import c_rules
from changecheck.repository import load_snapshot
from changecheck.engine import run_checks


class General(Fixture):
    def setUp(self):
        super().setUp()
        self.item["profile"].update(modules=["code", "config", "document"], rule_sources=[])
        update_root(self.item)

    def stage(self, name, text):
        self.write(name, text)
        git(self.root, "add", name)

    def codes(self):
        return {f["rule"] for f in self.check()[1]["findings"]}

    def test_plain_project_has_no_knowledge_dependencies(self):
        other = self.base / "plain"
        other.mkdir()
        (other / "sample.py").write_text("print('hello')\n")
        item = add_root(other)
        item["profile"].update(skill="", template="", standards="", rule_sources=[])
        report = run_checks(load_snapshot(other), item)
        self.assertEqual(report["exit_code"], 0)
        self.assertNotIn("knowledge", report["modules"])

    def test_python_syntax_and_config_duplicates_block(self):
        for name, content in [("bad.py", "if True print('x')"), ("bad.json", '{"a":1,"a":2}'),
                              ("bad.yaml", "a: 1\na: 2\n"), ("bad.toml", "a = 1\na = 2\n")]:
            self.stage(name, content)
        findings = self.check()[1]["findings"]
        self.assertEqual({f["file"] for f in findings}, {"bad.py", "bad.json", "bad.yaml", "bad.toml"})
        self.assertTrue(all(f["severity"] == "ERROR" for f in findings))

    def test_unknown_extension_cannot_succeed(self):
        self.stage("sample.rs", "fn main() {}")
        self.assertIn("CORE001", self.codes())

    def test_external_language_checker_still_requires_code_ai_rules(self):
        self.stage("sample.rs", "fn main() {}")
        self.command(["{python}", "-c", "from pathlib import Path;assert Path('sample.rs').read_text()=='fn main() {}'"])
        report = self.check()[1]
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("CODE002", review.required_rules(report))

    def test_non_git_unknown_binary_is_not_silently_skipped(self):
        other = self.base / "plain-binary"
        other.mkdir()
        (other / "image.bin").write_bytes(b"\x00\x01")
        item = add_root(other)
        snap = load_snapshot(other)
        self.assertIn("image.bin", snap.changed)
        report = run_checks(snap, item)
        self.assertIn("CORE001", {f["rule"] for f in report["findings"]})

    def test_all_includes_unchanged_binary_coverage(self):
        (self.root / "asset.bin").write_bytes(b"\x00\x01")
        git(self.root, "add", "asset.bin")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "asset")
        snap = load_snapshot(self.root, all_files=True)
        self.assertIn("asset.bin", snap.changed)
        report = run_checks(snap, self.item)
        self.assertTrue(any(f["file"] == "asset.bin" and f["rule"] == "CORE001" for f in report["findings"]))

    def test_project_policy_is_read_from_index(self):
        self.stage("change-check.json", json.dumps({"modules": ["code"]}))
        self.stage("sample.json", "{}")
        self.write("change-check.json", json.dumps({"modules": ["code", "config"]}))
        self.assertIn("CORE001", self.codes())

    def test_project_policy_cannot_replace_reviewer(self):
        self.stage("change-check.json", '{"reviewer":{"kind":"command","argv":["fake"]}}')
        with self.assertRaises(CheckError):
            self.check()

    def test_plain_document_does_not_require_frontmatter(self):
        self.stage("README.md", "# Readme\n\nUsage.\n")
        self.assertFalse(self.check()[1]["findings"])
        self.stage("README.md", "# Readme\n```python\nx=1\n")
        self.assertIn("DOC001", self.codes())

    def command(self, argv, **extra):
        self.item["profile"]["commands"] = [{"id": "fixture", "argv": argv,
            "include": ["**"], "stages": ["edit", "commit"], **extra}]
        update_root(self.item)

    def test_commands_use_staged_binary_and_text(self):
        (self.root / "asset.bin").write_bytes(b"staged")
        git(self.root, "add", "asset.bin")
        (self.root / "asset.bin").write_bytes(b"working")
        self.stage("source.py", "value=1\n")
        self.write("source.py", "value=2\n")
        self.command(["{python}", "-c", "from pathlib import Path; assert Path('asset.bin').read_bytes()==b'staged'; assert Path('source.py').read_text()=='value=1\\n'"])
        report = self.check()[1]
        self.assertEqual(report["exit_code"], 0, report["findings"])
        self.assertTrue(any(e["checker"] == "fixture" and e["status"] == "passed" for e in report["executions"]))

    def test_command_failure_and_missing_program_block(self):
        self.stage("sample.py", "value=1\n")
        for argv in [["{python}", "-c", "raise SystemExit(7)"], [str(self.base / "missing.exe")]]:
            with self.subTest(argv=argv):
                self.command(argv)
                self.assertIn("CMD001", self.codes())

    def test_input_rewrite_invalidates_command(self):
        self.stage("sample.py", "value=1\n")
        self.command(["{python}", "-c", "from pathlib import Path;Path('sample.py').write_text('fixed')"])
        report = self.check()[1]
        self.assertTrue(any("改写" in f["message"] for f in report["findings"]))
        self.assertEqual((self.root / "sample.py").read_text(), "value=1\n")

    def test_commit_only_commands_and_required_roles(self):
        self.stage("sample.py", "value=1\n")
        self.command(["{python}", "-c", "raise SystemExit(4)"], stages=["commit"], role="test")
        self.assertNotIn("CMD001", {f["rule"] for f in self.check(False)[1]["findings"]})
        self.assertIn("CMD001", self.codes())
        self.item["profile"].update(commands=[], required_roles=["build", "test"])
        update_root(self.item)
        self.assertEqual(sum(f["rule"] == "CMD001" for f in self.check()[1]["findings"]), 2)

    def test_single_module_command_cannot_be_used_for_gate(self):
        self.stage("sample.py", "value=1\n")
        self.stage("bad.json", "not-json")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["check", "--root", str(self.root), "--module", "code"]), 0)
            self.assertEqual(cli.main(["check", "--root", str(self.root)]), 1)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.parser().parse_args(["gate", "--module", "code"])

    def test_install_plain_project_without_skills(self):
        other = self.base / "plain"
        other.mkdir()
        git(other, "init", "-q")
        with patch("sys.stdin.isatty", return_value=False), patch("changecheck.cli.claude_command", return_value=[sys.executable]), contextlib.redirect_stdout(io.StringIO()):
            # Existing knowledge fixture also has a custom reviewer; doctor checks both safely.
            self.assertEqual(cli.main(["install", "--root", str(other), "--agents", "", "--skip-login-check"]), 0)
            self.assertEqual(cli.main(["remove", str(other)]), 0)

    def test_incremental_independent_units_reuse_unaffected_unit(self):
        self.stage("a.py", "value=1\n")
        self.stage("b.py", "value=1\n")
        self.item["profile"]["review_units"] = [
            {"id": n, "include": [n+".py"], "dependencies": []} for n in ("a", "b")]
        update_root(self.item)
        with patch("changecheck.review.invoke_reviewer", side_effect=self.approval) as invoke:
            snap, report = self.check()
            review.review_snapshot(snap, self.item, report)
            self.assertEqual(invoke.call_count, 2)
            self.stage("a.py", "value=2\n")
            snap, report = self.check()
            result, cached = review.review_snapshot(snap, self.item, report)
            self.assertEqual(invoke.call_count, 3)
            self.assertFalse(cached)
            self.assertEqual({u["unit"] for u in result["units"] if u["cached"]}, {"b"})

    def test_incremental_dependency_change_expands_review(self):
        self.stage("a.py", "value=1\n")
        self.stage("shared.py", "value=1\n")
        self.item["profile"]["review_units"] = [{"id": "a", "include": ["a.py"], "dependencies": ["shared.py"]}]
        update_root(self.item)
        with patch("changecheck.review.invoke_reviewer", side_effect=self.approval) as invoke:
            snap, report = self.check()
            review.review_snapshot(snap, self.item, report)
            self.stage("shared.py", "value=2\n")
            snap, report = self.check()
            result, _ = review.review_snapshot(snap, self.item, report)
            self.assertEqual(invoke.call_count, 4)
            self.assertTrue(all(not u["cached"] for u in result["units"]))

    def test_incremental_failure_is_not_cached(self):
        self.stage("a.py", "value=1\n")
        self.item["profile"]["review_units"] = [{"id": "a", "include": ["a.py"], "dependencies": []}]
        update_root(self.item)
        with patch("changecheck.review.invoke_reviewer", side_effect=CheckError("model unavailable")) as invoke:
            for _ in range(2):
                snap, report = self.check()
                with self.assertRaisesRegex(CheckError, "model unavailable"):
                    review.review_snapshot(snap, self.item, report)
            self.assertEqual(invoke.call_count, 2)

    def test_overlapping_review_units_rejected(self):
        self.stage("a.py", "value=1\n")
        self.item["profile"]["review_units"] = [{"id": n, "include": ["*.py"], "dependencies": []} for n in ("a", "b")]
        update_root(self.item)
        snap, report = self.check()
        with self.assertRaisesRegex(CheckError, "重叠"):
            review.review_snapshot(snap, self.item, report)

    def test_renamed_install_upgrades_verified_git_wrapper(self):
        integration.install_git_hook(self.item)
        info = self.item["profile"]["git_hook"]
        from pathlib import Path
        path = Path(info["directory"]) / "pre-commit"
        path.write_text(path.read_text().replace((SOURCE / "main.py").as_posix(), "F:/old/kb-check/src/main.py"))
        info["hashes"][str(path)] = digest(path.read_bytes())
        update_root(self.item)
        integration.install_git_hook(self.item)
        self.assertIn((SOURCE / "main.py").as_posix(), path.read_text())
        self.assertNotIn("F:/old", path.read_text())
        integration.uninstall_git_hook(self.item)
        self.assertNotEqual(git(self.root, "config", "--local", "--get", "core.hooksPath", check=False).returncode, 0)

    def test_legacy_agent_marker_is_replaced_and_other_hooks_preserved(self):
        path = integration.config_path("claude")
        save_json(path, {"hooks": {"PostToolUse": [{"hooks": [
            {"type": "command", "command": "old --marker kb-check-managed"},
            {"type": "command", "command": "user-owned"}]}]}})
        integration.configure_agent("claude")
        text = path.read_text()
        self.assertNotIn("kb-check-managed", text)
        self.assertIn("change-check-managed", text)
        self.assertIn("user-owned", text)

    def test_incremental_schema_uses_code_rules_and_rejects_document_only_response(self):
        self.stage("sample.py", "value=1\n")
        snap, report = self.check()
        self.assertEqual(set(review.required_rules(report)), {"CODE001", "CODE002", "CODE003", "CODE004"})
        result = self.approval(snap, self.item, report, {})
        result["rule_checks"][0]["rule"] = "AI001"
        with self.assertRaises(CheckError):
            review.validate_review(result, report, snap)

    def test_custom_process_receives_mixed_rules_and_schema(self):
        self.stage("sample.py", "value=1\n")
        self.stage("config.json", '{"value":1}\n')
        self.stub.write_text(STUB.replace('"AI00"+str(i)', 'r').replace(
            'for i in range(1, 10)', 'for r in data["required_rules"]'), encoding="utf-8")
        snap, report = self.check()
        result, cached = review.review_snapshot(snap, self.item, report)
        self.assertFalse(cached)
        self.assertEqual({r["rule"] for r in result["rule_checks"]},
                         {"CODE001", "CODE002", "CODE003", "CODE004", "CFG001", "CFG002"})

    @staticmethod
    def approval(snap, item, report, reviewer):
        name = next(n for n in report["changed"] if n in snap.data or n in snap.before)
        ref = {"file": name, "line": 1, "end_line": 1, "snapshot": "current" if name in snap.data else "baseline"}
        return {"approved": True, "checked_files": report["changed"], "blockers": [], "evidence_gaps": [],
                "dispositions": [], "pending_items": [], "summary": "isolated fixture approval",
                "rule_checks": [{"rule": r, "applicable": True, "evidence": "fixture evidence at line one",
                                 "locations": [ref]} for r in review.required_rules(report)]}


class CRules(Fixture):
    def rules(self, source, name="sample.c"):
        return {f["rule"] for f in c_rules.check(name, source)}

    def test_comments_and_strings_are_not_control_statements(self):
        self.assertEqual(self.rules('int run(void)\n{\n    // if(x) bad();\n    const char *text = "if (x) strcpy(a,b)";\n    return 0;\n}\n'), set())

    def test_controls_must_be_braced_multiline(self):
        for statement in ["if (value) return 0;", "for (;;) break;", "while (value) value--;", "if (value) { return 0; }"]:
            self.assertIn("C006", self.rules("int run(int value)\n{\n    " + statement + "\n    return 0;\n}\n"))

    def test_valid_style_and_header_guard(self):
        self.assertEqual(self.rules("int run(int value)\n{\n    if (value) {\n        return 0;\n    }\n    return 1;\n}\n"), set())
        self.assertNotIn("C010", self.rules("#ifndef MODULE_SAMPLE_H\n#define MODULE_SAMPLE_H\n#endif\n", "sample.h"))
        self.assertIn("C010", self.rules("#ifndef __SAMPLE_H\n#define __SAMPLE_H\n#endif\n", "sample.h"))

    def test_names_magic_dangerous_functions_and_parameters(self):
        source = "int BadGlobal;\nstatic int wrong;\nint BadName(int a, int b, int c, int d, int e, int f) {\n\tstrcpy(a,b);\n    strncpy(a,b,16);\n    return 99;\n}\n"
        self.assertTrue({"C001", "C002", "C003", "C005", "C008", "C009", "C011"} <= self.rules(source))

    def test_function_length_and_nesting(self):
        self.assertIn("C004", self.rules("int run(void)\n{\n" + "    ;\n"*51 + "}\n"))
        self.assertIn("C007", self.rules("int run(void)\n{\n" + "    {\n"*5 + "    }\n"*5 + "}\n"))
