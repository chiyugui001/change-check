from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL / "src"))
from changecheck import common, resources
from changecheck.common import CheckError

spec = importlib.util.spec_from_file_location("bundle_builder", TOOL / "packaging/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ResourceSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="change-check-resources-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        for name in builder.resource_names():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        self.patch = patch.object(resources, "RESOURCE_ROOT", self.bundle)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_missing_defaults_use_bundled_resources(self):
        profile = resources.complete_profile({"modules": ["knowledge"]})
        resources.require_resources(self.root, profile)
        self.assertEqual(Path(profile["standards"]), self.bundle / "standards")
        self.assertEqual(len(profile["rule_sources"]), 12)

    def test_explicit_broken_path_is_not_replaced(self):
        profile = resources.complete_profile({"modules": ["knowledge"], "standards": "missing"})
        self.assertEqual(profile["standards"], "missing")
        with self.assertRaisesRegex(CheckError, "知识库规范缺失"):
            resources.require_resources(self.root, profile)

    def test_code_project_does_not_acquire_knowledge_rules(self):
        profile = {"modules": ["code", "config", "document"]}
        self.assertEqual(resources.complete_profile(profile), profile)
        self.assertNotIn("standards", profile)
        with patch.object(common, "TOOL", self.root), patch.object(common, "skill_candidates", return_value=[]):
            self.assertNotIn("knowledge", common.default_profile(self.root)["modules"])

    def test_project_rules_and_local_skills_have_priority(self):
        (self.root / "知识库规范").mkdir()
        (self.root / "模板").mkdir()
        (self.root / "模板/详设.md").write_text("local")
        skill = self.root / "local-skills/firmware-detailed-design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text("local")
        with patch.object(common, "skill_candidates", return_value=[str(skill)]):
            profile = common.default_profile(self.root)
        self.assertEqual(profile["standards"], "知识库规范")
        self.assertEqual(profile["template"], "模板/详设.md")
        self.assertEqual(profile["skill"], str(skill))
        self.assertIn(str(skill / "SKILL.md"), profile["rule_sources"])

    def test_changing_template_updates_only_derived_requirement_template(self):
        profile = resources.complete_profile({"modules": ["knowledge"]})
        resources.set_resource(profile, "template", "模板/详设.md")
        self.assertEqual(profile["requirement_template"], str(Path("模板/需求.md")))
        profile["requirement_template"] = "custom.md"
        resources.set_resource(profile, "template", "其他/详设.md")
        self.assertEqual(profile["requirement_template"], "custom.md")


class Distribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="change-check-distribution-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name)
        config = TOOL / "packaging/sources.local.json"
        cls.result = builder.build(TOOL, cls.base / "release", config if config.exists() else None)
        cls.package = Path(cls.result["directory"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="change-check-clean-user-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.project = self.work / "中文 新项目"
        self.project.mkdir()
        self.env = dict(os.environ, HOME=str(self.work / "user"), USERPROFILE=str(self.work / "user"),
                        CODEX_HOME=str(self.work / "user/.codex"),
                        CHANGE_CHECK_HOME=str(self.work / "state"),
                        CHANGE_CHECK_USER_HOME=str(self.work / "user"),
                        GIT_CONFIG_GLOBAL=str(self.work / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                        PYTHONUTF8="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
            self.env.pop(key, None)

    def command(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(self.package / "src/main.py"), *args],
                                cwd=self.project, env=self.env, capture_output=True, text=True,
                                encoding="utf-8", input="", timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def install(self):
        return self.command("install", "--root", str(self.project), "--modules", "knowledge,code,config",
                            "--agents", "", "--reviewer", "command", "--reviewer-argv-json",
                            json.dumps([sys.executable, "-c", "raise SystemExit(0)"]), "--skip-login-check")

    def test_clean_user_installs_and_executes_real_bundled_checkers(self):
        subprocess.run(["git", "init", "-q", str(self.project)], env=self.env, check=True, capture_output=True)
        self.install()
        registry = json.loads((self.work / "state/registry.json").read_text(encoding="utf-8"))["roots"][0]
        for key in ("standards", "template", "skill", "requirement_template"):
            self.assertTrue(Path(registry["profile"][key]).is_relative_to(self.package / "resources"))
        self.assertIsNotNone(registry["profile"]["git_hook"])
        header = "---\nreviewed_by: AI-draft\nmodified: 2026-09-24 12:00:00\nstatus: draft\ntags: [测试]\n---\n\n"
        (self.project / "_INDEX.md").write_text(header + "# 索引\n\n[[使用说明]]\n", encoding="utf-8")
        (self.project / "PENDING-REVIEW.md").write_text(header + "# 待审核\n\n[[_INDEX]]\n", encoding="utf-8")
        document = self.project / "使用说明.md"
        document.write_text(header + "# 使用说明\n\n[[_INDEX]]\n\n```mermaid\nflowchart LR\n A[开始] --> B[结束]\n```\n", encoding="utf-8")
        report = json.loads(self.command("check", "--all", "--json"))
        self.assertFalse(report["findings"], report["findings"])
        executed = {entry["checker"] for entry in report["executions"]}
        self.assertIn("validate_diagram_labels.py", executed)
        self.assertIn("validate_detailed_design_content.py", executed)
        document.write_text(document.read_text(encoding="utf-8").replace("tags: [测试]\n", ""), encoding="utf-8")
        report = json.loads(self.command("check", "--all", "--json", expected=1))
        self.assertTrue(any(f["rule"] == "KB001" for f in report["findings"]))
        (self.project / "详设").mkdir()
        (self.project / "详设/测试详设.md").write_text(header + "# 测试详设\n\n[[_INDEX]]\n", encoding="utf-8")
        report = json.loads(self.command("check", "--all", "--json", expected=1))
        executed = {entry["checker"] for entry in report["executions"]}
        for part in ("format", "struct_comments", "boundaries", "content"):
            self.assertIn(f"validate_detailed_design_{part}.py", executed)
        self.command("doctor")
        self.command("uninstall")
        self.assertTrue(document.exists())

    def test_explicit_missing_standards_block_install(self):
        self.command("install", "--root", str(self.project), "--modules", "knowledge",
                     "--standards", str(self.work / "absent"), "--agents", "", expected=2)
        self.assertFalse((self.work / "state/agents.json").exists())

    def test_archive_is_allowlisted_and_reproducible(self):
        rebuilt = builder.build(self.package, self.work / "rebuild")
        # Markdown links are already normalized; no timestamps enter archive contents.
        self.assertEqual(Path(self.result["archive"]).read_bytes(), Path(rebuilt["archive"]).read_bytes())
        with zipfile.ZipFile(self.result["archive"]) as archive:
            names = archive.namelist()
            self.assertFalse(any("sources.local.json" in n or "/.venv/" in n or "/.git/" in n for n in names))
        self.assertNotIn(str(Path.home()), (self.package / "resources/manifest.json").read_text(encoding="utf-8"))

    def test_modified_generated_resource_blocks_repackaging(self):
        path = self.package / "resources/templates/需求.md"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\nchanged")
            with self.assertRaisesRegex(ValueError, "生成资源已被修改"):
                builder.build(self.package, self.work / "rebuild")
        finally:
            path.write_bytes(original)

    def test_git_checkout_preserves_resource_checksums(self):
        repository = self.work / "release-repo"
        repository.mkdir()
        shutil.copyfile(self.package / ".gitattributes", repository / ".gitattributes")
        shutil.copytree(self.package / "resources", repository / "resources")
        def git(*args):
            return subprocess.run(["git", "-C", str(repository), *args], env=self.env,
                                  check=True, capture_output=True, timeout=30)
        git("init", "-q")
        git("config", "core.autocrlf", "true")
        git("add", ".")
        git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        clone = self.work / "checkout"
        subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "-q", str(repository), str(clone)],
                       env=self.env, check=True, capture_output=True, timeout=30)
        self.assertEqual(builder.bundled_resources(repository), builder.bundled_resources(clone))

    def test_rebuilding_sources_refreshes_generated_copy(self):
        sources = self.work / "sources"
        for name in builder.resource_names():
            path = sources / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.package / "resources" / name).read_bytes())
        config = self.work / "sources.json"
        config.write_text(json.dumps({"standards": str(sources / "standards"),
                                      "templates": str(sources / "templates"),
                                      "skills": {n: str(sources / "skills" / n) for n in builder.SKILLS}}), encoding="utf-8")
        before = builder.source_resources(config)
        template = sources / "templates/详设.md"
        template.write_bytes(template.read_bytes() + "\n源文件新增规则。\n".encode())
        after = builder.source_resources(config)
        self.assertNotEqual(before["manifest.json"], after["manifest.json"])
        self.assertIn("源文件新增规则。", after["templates/详设.md"].decode())
        self.assertNotIn("源文件新增规则。", (self.package / "resources/templates/详设.md").read_text(encoding="utf-8"))

    def test_secret_scanner_reports_filename_without_secret(self):
        secret = "ghp_" + "a" * 36
        with self.assertRaises(ValueError) as caught:
            builder.scan_public({"accidental.txt": secret.encode()})
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("accidental.txt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
