from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from changecheck import cli, integration, review
from changecheck.checks import (anchor_span, extract_links, frontmatter, generic_checks, has_anchor,
                            location, resolve_link, task_links)
from changecheck.common import (CheckError, TOOL, add_root, digest, find_root, git, home,
                           read_json, roots, run, save_json, update_root)
from changecheck.context import attach_references, verify_references
from changecheck.engine import run_checks, script_check
from changecheck.repository import load_snapshot

FIELDS = ["customer", "modules", "source", "git_branch", "git_commit", "author",
          "reviewed_by", "modified", "status", "priority", "version", "tags"]


def doc(body="# 文档\n\n已确认内容。\n", modified="2026-09-24 12:00:00", status="draft"):
    return ("---\nreviewed_by: " + ("Chi" if status == "active" else "AI-draft") +
            f"\nmodified: {modified}\nstatus: {status}\ntags: [测试]\n---\n\n" + body)


STUB = '''import json, pathlib, sys, time
inp, out, mode, count = sys.argv[1:]
data = json.loads(pathlib.Path(inp).read_text(encoding="utf-8"))
counter = pathlib.Path(count)
counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
if mode == "timeout": time.sleep(3)
if mode == "exit": sys.exit(9)
folder = pathlib.Path(inp).parent.parent
def ref(f):
    if f.get("primary"):
        return {k: f["primary"][k] for k in ("file", "line", "end_line", "snapshot")}
    return {"file": f["file"], "line": f["line"], "end_line": f["line"],
            "snapshot": "current" if (folder / f["file"]).is_file() else "baseline"}
source = next((n for n in data["changed_files"] if (folder / n).is_file()), None)
snapshot = "current"
if source is None:
    source = data["changed_files"][0]
    snapshot = "baseline"
path = folder / (".baseline" if snapshot == "baseline" else "") / source
lines = path.read_text(encoding="utf-8-sig").splitlines()
line = next((n for n, s in enumerate(lines, 1) if s.startswith("#")), 1)
where = {"file": source, "line": line, "end_line": line, "snapshot": snapshot}
result = {"approved": True, "summary": "fixture reviewed", "checked_files": data["changed_files"],
 "blockers": [], "evidence_gaps": [],
 "dispositions": [{"id": f["id"], "disposition": "accepted", "reason": "fixture evidence", "locations": [ref(f)]}
   for f in data["script_report"]["findings"] if f["severity"] == "REVIEW"],
 "rule_checks": [{"rule": "AI00"+str(i), "applicable": True, "evidence": "fixture evidence at document title",
                  "locations": [where]} for i in range(1, 10)],
 "pending_items": [{"source": ref(f), "tasks": [], "status": "not_required",
                    "reason": "fixture classification only, not a real semantic decision", "candidate_ids": [f["id"]]}
                   for f in data["script_report"]["findings"] if f["rule"] == "KB008"]}
if mode == "reject": result["approved"] = False
if mode == "scope": result["checked_files"] = []
if mode == "candidate": result["dispositions"] = []
if mode == "rule": result["rule_checks"].pop()
if mode == "gap": result["evidence_gaps"] = ["missing source"]
if mode == "malformed": result["dispositions"] = [None]
if mode == "extra": result["unexpected"] = True
if mode == "generic": result["rule_checks"][-1]["evidence"] = "文案符合规范"
if mode == "no_location": result["rule_checks"][-1]["locations"] = []
if mode == "bad_line": result["rule_checks"][-1]["locations"] = [{**where, "line": 9999, "end_line": 9999}]
if mode == "skip_writing": result["rule_checks"][-1]["applicable"] = False
pathlib.Path(out).write_text("not json" if mode == "json" else json.dumps(result), encoding="utf-8")
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="change-check-tests-")
        self.base = Path(self.temp.name)
        self.root = self.base / "中文 库"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"CHANGE_CHECK_HOME": str(self.base / "state"),
            "CHANGE_CHECK_USER_HOME": str(self.base / "user"), "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "PYTHONUTF8": "1"})
        self.env.start()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "CHANGE_CHECK_REVIEW"):
            os.environ.pop(key, None)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "changecheck-test@example.invalid")
        git(self.root, "config", "user.name", "KB test")
        git(self.root, "config", "core.autocrlf", "false")
        self.write("_INDEX.md", doc("# 入口\n\n[[中文]]\n"))
        self.write("PENDING-REVIEW.md", doc("# 待审核\n\n[[_INDEX]]\n"))
        self.write("中文.md", doc())
        self.write("知识库规范/frontmatter规范.md", doc("# 规范\n\n" + " → ".join(FIELDS)))
        self.write("模板/详设.md", doc("# 详设\n\n## 1. 概述\n"))
        self.item = add_root(self.root)
        # Test generic mechanics separately from the user's installed skill sources.
        self.item["profile"]["rule_sources"] = []
        update_root(self.item)
        git(self.root, "add", ".")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "baseline")
        self.stub = self.base / "reviewer.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.counter = self.base / "calls"
        self.reviewer()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def reviewer(self, mode="ok"):
        self.item["profile"]["reviewer"] = {"kind": "command", "timeout": 1 if mode == "timeout" else 10,
            "argv": [sys.executable, str(self.stub), "{input}", "{output}", mode, str(self.counter)]}
        update_root(self.item)

    def change(self, body="# 文档\n\n修改后内容。\n", staged=True):
        self.write("中文.md", doc(body, modified="2026-09-24 12:01:00"))
        if staged:
            git(self.root, "add", "中文.md")

    def check(self, staged=True):
        snap = load_snapshot(self.root, staged=staged)
        return snap, run_checks(snap, self.item)


class Paths(Fixture):
    def test_multiple_roots_deduplicate_and_longest_match(self):
        second = self.root / "子库"
        second.mkdir()
        child = add_root(second)
        self.assertEqual(add_root(self.root / ".")["id"], self.item["id"])
        self.assertEqual(len(roots()), 2)
        self.assertEqual(find_root(second / "doc")["id"], child["id"])

    def test_blank_input_uses_current_directory(self):
        with contextlib.chdir(self.root), patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value=""):
            for command in ("install", "add"):
                with self.subTest(command=command):
                    args = cli.parser().parse_args([command])
                    self.assertEqual(cli.prompt_path(args.default_root), str(self.root))

    def test_remove_never_deletes_knowledge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["remove", str(self.root)]), 0)
        self.assertEqual(roots(), [])
        self.assertTrue((self.root / "中文.md").exists())


class Checks(Fixture):
    def test_valid_change(self):
        self.change()
        self.assertEqual(self.check()[1]["findings"], [])

    def test_unstaged_fix_does_not_hide_staged_error(self):
        self.change("# 文档\n\n[[不存在]]\n")
        self.change(staged=False)
        self.assertIn("KB003", {f["rule"] for f in self.check()[1]["findings"]})
        self.assertEqual(self.check(False)[1]["findings"], [])

    def test_new_repository_without_head(self):
        repo = self.base / "new"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "中文.md").write_text(doc(), encoding="utf-8")
        git(repo, "add", ".")
        snap = load_snapshot(repo, True)
        self.assertEqual(snap.head, "")
        self.assertEqual(snap.changed, {"中文.md"})

    def test_deleted_target_checks_unchanged_inbound_links(self):
        git(self.root, "rm", "中文.md")
        report = self.check()[1]
        self.assertTrue(any(f["file"] == "_INDEX.md" and f["rule"] == "KB003" for f in report["findings"]))

    def test_changed_heading_checks_inbound(self):
        self.write("_INDEX.md", doc("# 入口\n\n[[中文#文档]]"))
        git(self.root, "add", ".")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "link")
        self.change("# 另一个标题\n")
        self.assertTrue(any(f["file"] == "_INDEX.md" for f in self.check()[1]["findings"]))

    def test_untracked_document_not_ignored(self):
        self.write("新增.md", "# 没有属性")
        report = self.check(False)[1]
        self.assertIn("新增.md", report["changed"])
        self.assertTrue(any(f["file"] == "新增.md" and f["rule"] == "KB001" for f in report["findings"]))

    def test_duplicate_yaml_and_bad_types(self):
        with self.assertRaises(ValueError):
            frontmatter("---\nstatus: draft\nstatus: active\n---\n")
        self.change()
        path = self.root / "中文.md"
        path.write_text(path.read_text(encoding="utf-8").replace("tags: [测试]", "tags: 错误"), encoding="utf-8")
        git(self.root, "add", "中文.md")
        self.assertTrue(any(f["rule"] == "KB001" for f in self.check()[1]["findings"]))

    def test_conflict_and_fence(self):
        self.change("# 文档\n<<<<<<< HEAD\n正文\n=======\n另文\n>>>>>>> branch\n```c\n")
        self.assertGreaterEqual(sum(f["rule"] == "KB005" for f in self.check()[1]["findings"]), 3)

    def test_unmerged_index_fails(self):
        oid = git(self.root, "rev-parse", "HEAD:中文.md").stdout.decode().strip()
        git(self.root, "update-index", "--force-remove", "中文.md")
        run(["git", "-C", self.root, "update-index", "--index-info"],
            data=(f"100644 {oid} 1\t中文.md\n100644 {oid} 2\t中文.md\n").encode())
        with self.assertRaises(CheckError):
            load_snapshot(self.root, True)

    def test_link_parsing_excludes_examples_and_resolves_references(self):
        text = '# 文档\n`[[假目标]]`\n```md\n[[假目标2]]\n```\n[[中文\\|说明]]\n[文本][id]\n[id]: <中文.md>\n[x](文件(一).md)\n'
        self.assertEqual([x[1] for x in extract_links(text)], ["中文", "中文.md", "文件(一).md"])
        self.assertTrue(has_anchor("## `page_header` 用途", "page_header 用途"))
        self.assertEqual(resolve_link("_INDEX.md", "中文.md", {"中文.md"}, False)[0], "中文.md")

    def test_ambiguous_basename(self):
        result = resolve_link("其他/文档.md", "同名", {"甲/同名.md", "乙/同名.md"})
        self.assertIn("不明确", result[2])

    def test_ledger_hash_mismatch(self):
        self.change()
        self.write("AI/Index/compile_index.json", json.dumps({"compiled_files": [{
            "source_file": "中文.md", "source_hash": "sha256:" + "0" * 64,
            "output_files": ["_INDEX.md"]}]}))
        git(self.root, "add", ".")
        self.assertTrue(any(f["rule"] == "KC002" for f in self.check()[1]["findings"]))

    def test_raw_and_active_create_review_candidates(self):
        self.write("Raw/原文.md", "original")
        self.write("中文.md", doc(status="active"))
        git(self.root, "add", ".")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "sources")
        self.write("Raw/原文.md", "changed")
        self.change()
        git(self.root, "add", ".")
        findings = self.check()[1]["findings"]
        self.assertEqual(sum(f["rule"] == "KC003" for f in findings), 2)

    def test_missing_script_is_runtime_failure(self):
        self.change("# 文档\n```mermaid\nflowchart LR\n A[开始] --> B[结束]\n```\n")
        self.item["profile"]["skill"] = str(self.base / "missing")
        with self.assertRaises(CheckError):
            self.check()

    def test_explicit_external_evidence_snapshot_and_change(self):
        external = self.base / "外部.md"
        external.write_text("evidence", encoding="utf-8")
        self.item["profile"]["references"] = [{"path": str(external), "mount": "来源/外部.md"}]
        update_root(self.item)
        self.change()
        snap, report = self.check()
        self.assertEqual(snap.text("来源/外部.md"), "evidence")
        external.write_text("changed", encoding="utf-8")
        with self.assertRaises(CheckError):
            review.review_snapshot(snap, self.item, report)

    def test_external_cannot_overwrite_staged(self):
        self.item["profile"]["references"] = [{"path": str(self.root / "中文.md"), "mount": "中文.md"}]
        with self.assertRaises(CheckError):
            self.check()

    def test_external_standards_and_template_paths(self):
        standard = self.base / "共享规范"
        standard.mkdir()
        (standard / "frontmatter规范.md").write_text(" → ".join(FIELDS), encoding="utf-8")
        self.item["profile"]["standards"] = str(standard)
        update_root(self.item)
        self.change()
        self.assertEqual(self.check()[1]["findings"], [])

    def test_requirement_template_and_coverage_map(self):
        self.write("模板/需求.md", doc("# 模板\n\n## 1. 验收\n"))
        self.write("需求/功能.md", doc("# 功能\n\n## 1. 验收\n\n可观察结果。\n"))
        self.write("需求/_INDEX.md", doc("# 导航\n\n[[需求/功能]]\n"))
        mapping = {"mappings": [{"id": "REQ-1", "source": "中文.md",
                                "target": "需求/功能.md", "acceptance": "需求/功能.md#1. 验收"}]}
        self.write("覆盖.json", json.dumps(mapping, ensure_ascii=False))
        self.item["profile"]["coverage_maps"] = ["覆盖.json"]
        update_root(self.item)
        git(self.root, "add", ".")
        findings = self.check()[1]["findings"]
        self.assertFalse(any(f["rule"].startswith("RD") for f in findings))
        mapping["mappings"][0]["acceptance"] = "需求/功能.md#不存在"
        self.write("覆盖.json", json.dumps(mapping, ensure_ascii=False))
        git(self.root, "add", ".")
        self.assertTrue(any(f["rule"] == "RD001" for f in self.check()[1]["findings"]))

    def test_asset_deletion_invalidates_working_fingerprint(self):
        self.write("图片.bin", "asset")
        before = load_snapshot(self.root).fingerprint()
        (self.root / "图片.bin").unlink()
        self.assertNotEqual(load_snapshot(self.root).fingerprint(), before)

    def test_runtime_script_invalid_json_is_not_success(self):
        script = self.base / "invalid.py"
        script.write_text('print("not json")', encoding="utf-8")
        with self.assertRaises(CheckError):
            script_check(script, "中文.md", self.root, [])


class Reviews(Fixture):
    def test_timeout_terminates_reviewer_child(self):
        marker = self.base / "late-output"
        child = self.base / "child.py"
        child.write_text('import pathlib,time\np=pathlib.Path(' + repr(str(marker)) +
                         ')\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.05)', encoding="utf-8")
        parent = self.base / "parent.py"
        parent.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,' + repr(str(child)) + '])\ntime.sleep(10)', encoding="utf-8")
        with self.assertRaises(CheckError):
            run([sys.executable, parent], timeout=0.5)
        # Windows process-tree termination takes time. Assert no writes after
        # run() returns, rather than assuming taskkill always finishes in 1.5 s.
        returned = marker.read_bytes() if marker.exists() else None
        time.sleep(.3)
        self.assertEqual(marker.read_bytes() if marker.exists() else None, returned)

    def test_pass_and_same_snapshot_cache(self):
        self.change()
        snap, report = self.check()
        self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
        self.assertTrue(review.review_snapshot(snap, self.item, report)[1])
        self.assertEqual(self.counter.read_text(), "1")

    def test_content_change_invalidates_cache(self):
        self.change()
        review.review_snapshot(*self.review_args())
        self.change("# 文档\n\n第二次变动\n")
        self.assertFalse(review.review_snapshot(*self.review_args())[1])
        self.assertEqual(self.counter.read_text(), "2")

    def review_args(self):
        snap, report = self.check()
        return snap, self.item, report

    def test_rules_change_during_review_fails(self):
        self.change()
        snap, report = self.check()
        self.item["profile"]["naming_exempt"].append("CUSTOM.md")
        update_root(self.item)
        with self.assertRaises(CheckError):
            review.review_snapshot(snap, self.item, report)

    def test_index_change_during_review_fails(self):
        self.change()
        snap, report = self.check()
        self.change("# 文档\n其他更改\n")
        with self.assertRaises(CheckError):
            review.review_snapshot(snap, self.item, report)

    def test_errors_prevent_model_call(self):
        self.change("# 文档\n[[错误链接]]\n")
        with self.assertRaises(CheckError):
            review.review_snapshot(*self.review_args())
        self.assertFalse(self.counter.exists())

    def test_fail_closed_for_invalid_or_missing_review(self):
        self.change()
        for mode in ("reject", "scope", "rule", "gap", "malformed", "extra", "json", "exit", "timeout"):
            with self.subTest(mode=mode):
                self.reviewer(mode)
                with self.assertRaises(CheckError):
                    review.review_snapshot(*self.review_args())

    def test_unhandled_review_candidate_blocks(self):
        self.change()
        snap, report = self.check()
        report["findings"].append({"id": "review-item", "severity": "REVIEW", "rule": "WR001",
                                   "file": "中文.md", "line": 8})
        self.reviewer("candidate")
        # Regenerate the rule identity after selecting this review fixture.
        from changecheck.engine import implementation_fingerprint
        report["implementation"] = implementation_fingerprint(self.item["profile"])
        with self.assertRaises(CheckError):
            review.review_snapshot(snap, self.item, report)


class WritingChecks(Fixture):
    def test_actual_nonempty_content_findings_keep_source_context(self):
        sentence = "采样完成后由温度告警模块依次核对探针编号与告警阈值，并记录本次温度数据及设备时间供后续导出。"
        self.change("# 文档\n\n## 输入\n\n" + sentence + "\n\n## 输出\n\n" + sentence + "\n")
        snap, report = self.check()
        candidates = [f for f in report["findings"] if f["rule"] == "D001"]
        self.assertTrue(candidates)
        finding = candidates[0]
        self.assertEqual(finding["primary"]["snapshot"], "current")
        self.assertEqual(finding["primary"]["text"], sentence)
        self.assertTrue(finding["related"])
        self.assertIn("score", finding)
        review.review_snapshot(snap, self.item, report)

    def test_actual_removed_content_points_to_baseline(self):
        sentence = "断电恢复必须先校验记录头的校验和，再根据提交标记选择最后一条完整记录，丢弃未完成写入的周期数据。"
        self.change("# 文档\n\n## 恢复条件\n\n" + sentence + "\n")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "baseline rule")
        self.write("中文.md", doc("# 文档\n\n已精简。\n", modified="2026-09-24 12:02:00"))
        git(self.root, "add", "中文.md")
        snap, report = self.check()
        lost = [f for f in report["findings"] if f["rule"] == "D004"]
        self.assertTrue(lost)
        self.assertEqual(lost[0]["primary"]["snapshot"], "baseline")
        self.assertIn(sentence, lost[0]["primary"]["text"])
        self.assertGreater(lost[0]["line"], len(snap.text("中文.md").splitlines()))
        review.review_snapshot(snap, self.item, report)

    def test_invalid_content_candidate_protocol_fails(self):
        script = self.base / "validate_detailed_design_content.py"
        result = {"results": [{"findings": [{"rule": "D001", "severity": "REVIEW", "reason": "example",
                                            "primary": {"line": "not an integer"}}]}]}
        script.write_text("import json\nprint(json.dumps(" + repr(result) + "))", encoding="utf-8")
        with self.assertRaisesRegex(CheckError, "上下文无效"):
            script_check(script, "中文.md", self.root, [])

    def test_process_wording_is_review_and_examples_are_excluded(self):
        self.change("# 文档\n\n用户已确认：周期数据写入闪存。\n\n暂按既有容量上限执行。\n\n"
                    "> 用户已确认：历史引用。\n\n`暂按样例`\n\n```text\n暂按示例执行\n```\n")
        findings = self.check()[1]["findings"]
        wording = [f for f in findings if f["rule"] == "WR001"]
        self.assertEqual(len(wording), 2)
        self.assertTrue(all(f["severity"] == "REVIEW" for f in wording))

    def test_historical_records_are_not_normative_wording(self):
        for name in ("Raw/输入.md", "Archive/归档.md", "记录/会议纪要.md", "记录/确认记录.md"):
            self.write(name, doc("# 历史\n\n用户已确认：暂按这个数值，验证待确认。\n"))
        git(self.root, "add", ".")
        self.assertFalse(any(f["rule"] in ("WR001", "KB008") for f in self.check()[1]["findings"]))

    def test_requirement_word_does_not_mean_pending_verification(self):
        self.change("# 文档\n\n对照配置的需求模板核对章节。\n")
        self.assertFalse(any(f["rule"] == "KB008" for f in self.check()[1]["findings"]))

    def test_version_changes_need_review_unchanged_legacy_does_not(self):
        self.write("中文.md", doc().replace("tags:", "version: 1\ntags:"))
        git(self.root, "add", "中文.md")
        self.assertTrue(any(f["rule"] == "KB007" for f in self.check()[1]["findings"]))
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "legacy version")
        self.write("中文.md", doc("# 文档\n\n改变正文。\n").replace("tags:", "version: 1\ntags:"))
        git(self.root, "add", "中文.md")
        self.assertFalse(any(f["rule"] == "KB007" for f in self.check()[1]["findings"]))
        self.write("中文.md", doc().replace("tags:", "version: 2\ntags:"))
        git(self.root, "add", "中文.md")
        self.assertTrue(any(f["rule"] == "KB007" for f in self.check()[1]["findings"]))

    def pending_fixture(self, forward="[[任务清单#^power]]", back="[[中文#供电]]", extra=""):
        self.change("# 文档\n\n## 供电\n\n断电恢复待验证。" + forward + "\n" + extra)
        self.write("任务清单.md", doc("# 任务\n\n- [ ] 执行断电恢复测试，记录复位后的读回值。" + back + " ^power\n"))
        self.write("_INDEX.md", doc("# 入口\n\n[[中文]]、[[任务清单]]\n", modified="2026-09-24 12:01:00"))
        git(self.root, "add", ".")
        snap, report = self.check()
        finding = next(f for f in report["findings"] if f["rule"] == "KB008")
        return snap, report, finding

    def test_task_source_and_backlink_are_mechanically_located(self):
        snap, report, finding = self.pending_fixture()
        linked, problems = task_links(snap, "中文.md", finding["line"])
        self.assertEqual(problems, [])
        self.assertEqual(len(linked), 1)
        self.assertTrue(linked[0]["backlink"])
        self.assertEqual(finding["severity"], "REVIEW")
        self.assertIn("仍需核对", finding["message"])

    def test_footer_or_document_level_task_link_is_insufficient(self):
        cases = [("", "\n## 关联\n\n[[任务清单#^power]]\n"), ("[[任务清单]]", "")]
        for forward, extra in cases:
            with self.subTest(forward=forward):
                snap, _, finding = self.pending_fixture(forward=forward, extra=extra)
                self.assertEqual(task_links(snap, "中文.md", finding["line"])[0], [])

    def test_missing_and_wrong_backlink_remain_candidates(self):
        for back in ("", "[[中文]]", "[[中文#文档]]"):
            # A document H1 is not a specific source anchor for a subsection.
            with self.subTest(back=back):
                snap, _, finding = self.pending_fixture(back=back)
                linked, _ = task_links(snap, "中文.md", finding["line"])
                self.assertFalse(linked[0]["backlink"])

    def test_ambiguous_task_heading_does_not_point_to_one_task(self):
        snap, _, finding = self.pending_fixture(forward="[[任务清单#任务]]")
        snap.data["任务清单.md"] += "\n- [ ] 另一件工作。\n".encode()
        self.assertFalse(task_links(snap, "中文.md", finding["line"])[0])

    def test_table_row_requires_its_own_task_link(self):
        self.change("# 文档\n\n## 条件\n\n| 项目 | 验证 |\n|---|---|\n| 恢复 | 待验证 |\n\n[[任务清单#^power]]\n")
        self.write("任务清单.md", doc("# 任务\n\n- [ ] 执行测试。[[中文#条件]] ^power\n"))
        snap = load_snapshot(self.root)
        line = next(n for n, raw in enumerate(snap.text("中文.md").splitlines(), 1) if "待验证" in raw)
        self.assertEqual(task_links(snap, "中文.md", line)[0], [])

    def test_task_anchor_on_continuation_line(self):
        text = "# 任务\n\n- [ ] 执行测试。\n  [[中文#供电]] ^power\n"
        self.assertEqual(anchor_span(text, "^power"), (3, 4))


class ReviewProtocol(Fixture):
    pending_fixture = WritingChecks.pending_fixture

    def test_empty_or_generic_evidence_cannot_approve(self):
        self.change()
        for mode in ("generic", "no_location", "bad_line", "skip_writing"):
            with self.subTest(mode=mode):
                self.reviewer(mode)
                snap, report = self.check()
                with self.assertRaises(CheckError):
                    review.review_snapshot(snap, self.item, report)

    def test_pending_mapping_requires_every_candidate(self):
        snap, report, finding = self.pending_fixture()
        result, _ = review.review_snapshot(snap, self.item, report)
        result["pending_items"] = []
        with self.assertRaisesRegex(CheckError, "全部 KB008"):
            review.validate_review(result, report, snap)

    def test_pending_mapping_checks_real_bidirectional_links(self):
        snap, report, finding = self.pending_fixture()
        result, _ = review.review_snapshot(snap, self.item, report)
        linked, _ = task_links(snap, "中文.md", finding["line"])
        entry = result["pending_items"][0]
        entry.update(status="linked", tasks=[linked[0]["target"]],
                     reason="断电恢复原文和同一测试事项双向关联，测试完成后记录读回值。")
        review.validate_review(result, report, snap)
        snap.data["任务清单.md"] = snap.data["任务清单.md"].replace("[[中文#供电]]".encode(), b"none")
        with self.assertRaisesRegex(CheckError, "直链及对应回链"):
            review.validate_review(result, report, snap)

    def test_ai_can_report_pending_item_not_found_by_keyword_script(self):
        snap, report, finding = self.pending_fixture()
        result, _ = review.review_snapshot(snap, self.item, report)
        report["findings"] = [f for f in report["findings"] if f["id"] != finding["id"]]
        result["dispositions"] = [d for d in result["dispositions"] if d["id"] != finding["id"]]
        result["pending_items"][0]["candidate_ids"] = []
        result["pending_items"][0]["status"] = "blocking"
        with self.assertRaisesRegex(CheckError, "AI 审查未通过"):
            review.validate_review(result, report, snap)

    def test_pending_source_cannot_point_to_another_section(self):
        snap, report, finding = self.pending_fixture(extra="\n## 其他\n\n无关内容。\n")
        result, _ = review.review_snapshot(snap, self.item, report)
        result["pending_items"][0]["source"]["line"] = len(snap.text("中文.md").splitlines())
        result["pending_items"][0]["source"]["end_line"] = result["pending_items"][0]["source"]["line"]
        with self.assertRaisesRegex(CheckError, "未覆盖"):
            review.validate_review(result, report, snap)

    def test_deleted_raw_candidate_uses_baseline_location(self):
        self.write("Raw/来源.md", doc("# 来源\n\n历史材料。\n"))
        git(self.root, "add", ".")
        git(self.root, "-c", "core.hooksPath=", "commit", "-qm", "source baseline")
        git(self.root, "rm", "Raw/来源.md")
        snap, report = self.check()
        self.assertTrue(any(f["severity"] == "REVIEW" for f in report["findings"]))
        result, _ = review.review_snapshot(snap, self.item, report)
        self.assertEqual(result["dispositions"][0]["locations"][0]["snapshot"], "baseline")

    def test_version_candidate_cannot_skip_authorization_rule(self):
        self.write("中文.md", doc(modified="2026-09-24 12:01:00").replace("tags:", "version: 2\ntags:"))
        git(self.root, "add", "中文.md")
        snap, report = self.check()
        result, _ = review.review_snapshot(snap, self.item, report)
        next(r for r in result["rule_checks"] if r["rule"] == "AI006")["applicable"] = False
        with self.assertRaisesRegex(CheckError, "不能跳过 AI006"):
            review.validate_review(result, report, snap)

    def test_pending_table_row_cannot_be_hidden_by_section_location(self):
        snap, report, finding = self.pending_fixture()
        result, _ = review.review_snapshot(snap, self.item, report)
        body = snap.text("中文.md").splitlines()
        body[finding["line"] - 1] = "| 恢复 | 待验证 |"
        snap.data["中文.md"] = "\n".join(body).encode()
        result["pending_items"][0]["source"]["line"] -= 2
        with self.assertRaisesRegex(CheckError, "段落或表格行"):
            review.validate_review(result, report, snap)


class DailyReviews(Fixture):
    def test_explicit_changed_review_uses_worktree_and_reports_status(self):
        self.change("# 文档\n\n工作区修改。\n", staged=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["review", "--root", str(self.root), "--changed", "--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "worktree")
        self.assertEqual(report["changed"], ["中文.md"])
        self.assertEqual(report["ai_review"], "passed")
        self.assertEqual(read_json(home() / "reports" / (self.item["id"] + ".json"))["ai_review"], "passed")
        self.assertFalse(git(self.root, "diff", "--cached", "--name-only").stdout.strip())

    def test_daily_and_staged_caches_are_isolated(self):
        self.change()
        working, wreport = self.check(False)
        staged, sreport = self.check(True)
        self.assertFalse(review.review_snapshot(working, self.item, wreport)[1])
        self.assertTrue(review.review_snapshot(working, self.item, wreport)[1])
        self.assertFalse(review.review_snapshot(staged, self.item, sreport)[1])
        self.assertEqual(self.counter.read_text(), "2")
        self.assertTrue((home() / "reports" / (self.item["id"] + "-worktree-review.json")).is_file())
        self.assertTrue((home() / "reports" / (self.item["id"] + "-staged-review.json")).is_file())

    def test_worktree_changes_during_review_invalidate_result(self):
        for operation in ("edit", "add", "delete"):
            with self.subTest(operation=operation):
                self.change(staged=False)
                snap, report = self.check(False)
                actual_run = review.run
                def mutate(*args, **kwargs):
                    value = actual_run(*args, **kwargs)
                    if operation == "edit":
                        self.write("中文.md", doc("# 文档\n\n再次更改。\n"))
                    elif operation == "add":
                        self.write("新增.md", doc())
                    else:
                        (self.root / "中文.md").unlink()
                    return value
                with patch("changecheck.review.run", side_effect=mutate), self.assertRaisesRegex(CheckError, "工作区内容"):
                    review.review_snapshot(snap, self.item, report)
                if (self.root / "新增.md").exists():
                    (self.root / "新增.md").unlink()

    def test_gate_rejects_worktree_and_review_rejects_all(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for command, flag in (("gate", "--changed"), ("gate", "--all"), ("review", "--all")):
                self.assertEqual(cli.main([command, "--root", str(self.root), flag]), 2)
        self.assertFalse(self.counter.exists())

    def test_default_review_still_uses_staged_content(self):
        self.change()
        self.change("# 文档\n\n[[工作区错误]]\n", staged=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["review", "--root", str(self.root), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "staged")

    def test_changed_attachment_bytes_invalidate_worktree_fingerprint(self):
        self.write("附件.bin", "first")
        snap = load_snapshot(self.root)
        self.write("附件.bin", "other")
        self.assertNotEqual(snap.fingerprint(), load_snapshot(self.root).fingerprint())

    def test_external_evidence_changed_during_daily_review_blocks(self):
        evidence = self.base / "证据.md"
        evidence.write_text("第一份测试记录", encoding="utf-8")
        self.item["profile"]["references"] = [{"path": str(evidence), "mount": "证据.md"}]
        update_root(self.item)
        self.change(staged=False)
        snap, report = self.check(False)
        actual_run = review.run
        def mutate(*args, **kwargs):
            value = actual_run(*args, **kwargs)
            evidence.write_text("第二份测试记录", encoding="utf-8")
            return value
        with patch("changecheck.review.run", side_effect=mutate), self.assertRaisesRegex(CheckError, "外部规范或证据"):
            review.review_snapshot(snap, self.item, report)

    def test_daily_check_and_hooks_never_invoke_model(self):
        self.change(staged=False)
        self.item["profile"]["agents"] = ["codex"]
        update_root(self.item)
        with patch("changecheck.cli.review_snapshot", side_effect=AssertionError("unexpected model call")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["check", "--root", str(self.root), "--changed"]), 0)
            with patch("sys.stdin", io.StringIO('{"hook_event_name":"Stop"}')), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["hook", "--agent", "codex"]), 0)
        self.assertFalse(self.counter.exists())


class ReviewerSettings(Fixture):
    def command(self, *args):
        out, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            code = cli.main(["reviewer", "--root", str(self.root), *args])
        return code, out.getvalue(), error.getvalue()

    def fake_codex(self, model=None):
        capture = self.base / "codex-args.json"
        fake = self.base / "codex-fixture.py"
        prefix = ("import json,pathlib,sys\nargs=sys.argv[1:]\npathlib.Path(" + repr(str(capture)) +
                  ").write_text(json.dumps(args))\n"
                  "selected=args[args.index('-m')+1] if '-m' in args else 'client-default'\n"
                  "if selected == 'missing-model':\n sys.stderr.write('model_not_found: missing-model')\n sys.exit(7)\n"
                  "folder=pathlib.Path(args[args.index('-C')+1])\n"
                  "sys.argv=[sys.argv[0],str(folder/'.change-check-review/review-input.json'),"
                  "args[args.index('-o')+1],'ok'," + repr(str(self.counter)) + "]\n")
        fake.write_text(prefix + STUB, encoding="utf-8")
        self.item["profile"]["reviewer"] = {"kind": "codex", "timeout": 10}
        if model is not None:
            self.item["profile"]["reviewer"]["model"] = model
        update_root(self.item)
        return fake, capture

    def test_show_does_not_call_model_or_change_hooks(self):
        before = git(self.root, "config", "--local", "--get", "core.hooksPath", check=False).stdout
        code, out, error = self.command()
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(out)["updated"])
        self.assertFalse(self.counter.exists())
        self.assertEqual(git(self.root, "config", "--local", "--get", "core.hooksPath", check=False).stdout, before)

    def test_set_model_is_per_root_and_can_return_to_client_default(self):
        second = self.base / "第二个库"
        second.mkdir()
        other = add_root(second)
        original = other["profile"]["reviewer"]
        code, out, error = self.command("--kind", "codex", "--model", "model-A", "--timeout", "42")
        self.assertEqual(code, 0, error)
        configured = find_root(self.root)["profile"]["reviewer"]
        self.assertEqual(configured, {"kind": "codex", "timeout": 42, "model": "model-A"})
        self.assertEqual(find_root(second)["profile"]["reviewer"], original)
        self.assertTrue(json.loads(out)["cache_enabled"])
        code, out, error = self.command("--use-default-model")
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(out)["cache_enabled"])
        self.assertNotIn("model", find_root(self.root)["profile"]["reviewer"])

    def test_invalid_settings_do_not_overwrite_existing_profile(self):
        before = find_root(self.root)["profile"]["reviewer"]
        for args in (("--kind", "codex", "--model", ""), ("--timeout", "0"),
                     ("--kind", "command", "--argv-json", "{}"),
                     ("--kind", "command", "--argv-json", "not json"),
                     ("--kind", "codex", "--program", " ")):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args)[0], 2)
                self.assertEqual(find_root(self.root)["profile"]["reviewer"], before)

    def test_command_model_must_be_used_and_placeholder_must_be_configured(self):
        self.assertEqual(self.command("--model", "model-A")[0], 2)
        args = [sys.executable, "adapter.py", "{model}", "{input}", "{output}"]
        self.assertEqual(self.command("--argv-json", json.dumps(args))[0], 2)
        code, out, error = self.command("--argv-json", json.dumps(args), "--model", "model-A")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out)["reviewer"]["model"], "model-A")

    def test_unknown_command_placeholders_are_rejected(self):
        for argument in ("{unknown}", "{model.name}", "{model!r}", "{model:>10}", "{broken"):
            with self.subTest(argument=argument), self.assertRaises(CheckError):
                review.validate_reviewer({"kind": "command", "argv": ["adapter", argument]})

    def test_explicit_codex_model_is_passed_and_cached(self):
        fake, capture = self.fake_codex("model-A")
        self.change()
        snap, report = self.check()
        with patch("changecheck.review.codex_command", return_value=[sys.executable, str(fake)]):
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
            self.assertTrue(review.review_snapshot(snap, self.item, report)[1])
        args = read_json(capture)
        self.assertEqual(args[args.index("-m") + 1], "model-A")
        self.assertEqual(self.counter.read_text(), "1")
        record = read_json(home() / "reports" / (self.item["id"] + "-staged-review.json"))
        self.assertEqual(record["reviewer"]["requested_model"], "model-A")
        self.assertEqual(record["status"], "passed")
        self.assertTrue(record["cached"])
        self.assertEqual(record["changed_files"], ["中文.md"])

    def test_unpinned_codex_model_never_reuses_ai_cache(self):
        fake, _ = self.fake_codex()
        self.change()
        snap, report = self.check()
        with patch("changecheck.review.codex_command", return_value=[sys.executable, str(fake)]):
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
            self.assertFalse(review.review_snapshot(snap, self.item, report)[1])
        self.assertEqual(self.counter.read_text(), "2")
        self.assertFalse((home() / "reviews" / self.item["id"] / "staged").exists())

    def test_missing_model_fails_without_fallback_or_using_old_approval(self):
        fake, capture = self.fake_codex("model-A")
        self.change()
        with patch("changecheck.review.codex_command", return_value=[sys.executable, str(fake)]):
            snap, report = self.check()
            review.review_snapshot(snap, self.item, report)
            self.item["profile"]["reviewer"]["model"] = "missing-model"
            update_root(self.item)
            snap, report = self.check()
            with self.assertRaisesRegex(CheckError, "审查器=codex，模型=missing-model.*model_not_found"):
                review.review_snapshot(snap, self.item, report)
        record = read_json(home() / "reports" / (self.item["id"] + "-staged-review.json"))
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["result"])
        self.assertEqual(self.counter.read_text(), "1")
        self.assertEqual(read_json(capture)[read_json(capture).index("-m") + 1], "missing-model")
        self.assertEqual(len(list((home() / "reviews" / self.item["id"] / "staged").glob("*.json"))), 1)

    def test_custom_command_receives_selected_model(self):
        capture = self.base / "model-name"
        self.stub.write_text("import pathlib,sys\npathlib.Path(" + repr(str(capture)) +
                             ").write_text(sys.argv.pop())\n" + STUB, encoding="utf-8")
        self.item["profile"]["reviewer"].update(model="custom-model")
        self.item["profile"]["reviewer"]["argv"].append("{model}")
        update_root(self.item)
        self.change()
        snap, report = self.check()
        review.review_snapshot(snap, self.item, report)
        self.assertEqual(capture.read_text(), "custom-model")

    def test_failed_review_record_contains_error_and_no_approval_cache(self):
        self.reviewer("reject")
        self.change()
        snap, report = self.check()
        with self.assertRaises(CheckError):
            review.review_snapshot(snap, self.item, report)
        record = read_json(home() / "reports" / (self.item["id"] + "-staged-review.json"))
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["result"]["approved"])
        self.assertIn("finished_at", record)
        self.assertFalse((home() / "reviews" / self.item["id"] / "staged").exists())


class Hooks(Fixture):
    def test_installer_and_uninstaller_in_isolated_home(self):
        if not (Path(self.item["profile"]["skill"]) / "scripts/validate_detailed_design_format.py").is_file():
            self.skipTest("详设技能未安装")
        with patch("changecheck.cli.codex_command", return_value=[sys.executable]), contextlib.redirect_stdout(io.StringIO()):
            args = ["install", "--root", str(self.root), "--agents", "codex,kimi,zcode",
                    "--reviewer", "codex", "--reviewer-model", "fixture-model", "--skip-login-check"]
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(find_root(self.root)["profile"]["reviewer"]["model"], "fixture-model")
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(["uninstall"]), 0)
        self.assertEqual(roots(), [])
        self.assertTrue((self.root / "中文.md").is_file())
        for agent in ("codex", "kimi", "zcode"):
            self.assertNotIn(integration.MARK, integration.config_path(agent).read_text(encoding="utf-8"))

    def test_failed_review_blocks_actual_commit(self):
        self.reviewer("reject")
        integration.install_git_hook(self.item)
        baseline = git(self.root, "rev-parse", "HEAD").stdout
        self.change()
        self.assertNotEqual(git(self.root, "commit", "-qm", "rejected", check=False).returncode, 0)
        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout, baseline)

    def test_existing_custom_hooks_path_restored(self):
        hooks = self.root / "shared-hooks"
        hooks.mkdir()
        git(self.root, "config", "--local", "core.hooksPath", "shared-hooks")
        integration.install_git_hook(self.item)
        integration.uninstall_git_hook(self.item)
        self.assertEqual(git(self.root, "config", "--local", "core.hooksPath").stdout.strip(), b"shared-hooks")

    def test_kimi_stop_uses_blocking_exit_code(self):
        self.item["profile"]["agents"] = ["kimi"]
        update_root(self.item)
        self.change("# 文档\n[[缺失]]\n", staged=False)
        with patch("sys.stdin", io.StringIO('{"hook_event_name":"Stop"}')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["hook", "--agent", "kimi"]), 2)

    def test_preserve_original_hook_and_restore_on_uninstall(self):
        original = self.root / ".git/hooks/pre-commit"
        original.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        original.chmod(0o755)
        integration.install_git_hook(self.item)
        integration.install_git_hook(self.item)
        self.change()
        result = git(self.root, "commit", "-qm", "blocked", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.counter.exists())
        integration.uninstall_git_hook(self.item)
        self.assertEqual(git(self.root, "config", "--local", "--get", "core.hooksPath", check=False).returncode, 1)
        self.assertIn("exit 7", original.read_text())

    def test_actual_commit_runs_gate(self):
        integration.install_git_hook(self.item)
        self.change()
        result = git(self.root, "commit", "-qm", "through gate", check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.counter.read_text(), "1")

    def test_commit_only_respects_temporary_index(self):
        integration.install_git_hook(self.item)
        self.change()
        self.write("无关.md", "invalid")
        git(self.root, "add", "无关.md")
        result = git(self.root, "commit", "--only", "-qm", "only selected", "中文.md", check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only").stdout.decode().strip('"\r\n') != "", True)

    def test_modified_hooks_not_overwritten_or_deleted(self):
        integration.install_git_hook(self.item)
        hook = Path(self.item["profile"]["git_hook"]["directory"]) / "pre-commit"
        hook.write_text("user modified", encoding="utf-8")
        with self.assertRaises(CheckError):
            integration.uninstall_git_hook(self.item)
        self.assertTrue(hook.exists())

    def test_all_agent_configs_preserve_unrelated_settings(self):
        import tomlkit
        for agent in ("codex", "kimi", "zcode", "claude"):
            with self.subTest(agent=agent):
                path = integration.config_path(agent)
                path.parent.mkdir(parents=True, exist_ok=True)
                if agent == "kimi":
                    path.write_text('theme = "dark"\n[[hooks]]\nevent = "Stop"\ncommand = "keep-me"\n', encoding="utf-8")
                else:
                    data = {"theme": "dark", "hooks": {"Stop": [{"hooks": [
                        {"type": "command", "command": "keep-me"},
                        {"type": "command", "command": integration.MARK}]}]}}
                    if agent == "zcode":
                        data["hooks"] = {"enabled": True, "events": data["hooks"]}
                    save_json(path, data)
                integration.configure_agent(agent)
                integration.configure_agent(agent)
                self.assertEqual(path.read_text(encoding="utf-8").count(integration.MARK), 2)
                integration.configure_agent(agent, remove=True)
                text = path.read_text(encoding="utf-8")
                self.assertIn("keep-me", text)
                self.assertIn("dark", text)
                self.assertNotIn(integration.MARK, text)

    def test_hook_feedback_and_recursion_guard(self):
        self.item["profile"]["agents"] = ["codex"]
        update_root(self.item)
        self.change("# 文档\n[[缺失]]\n", staged=False)
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"hook_event_name":"Stop"}')), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["hook", "--agent", "codex"]), 0)
        self.assertEqual(json.loads(output.getvalue())["decision"], "block")
        with patch.dict(os.environ, {"CHANGE_CHECK_REVIEW": "1"}), patch("sys.stdin", io.StringIO("invalid")):
            self.assertEqual(cli.main(["hook", "--agent", "codex"]), 0)


class ExistingSkills(Fixture):
    def test_real_diagram_and_detailed_design_adapters(self):
        skill = Path(self.item["profile"]["skill"])
        if not (skill / "scripts/validate_detailed_design_format.py").exists():
            self.skipTest("当前机器未安装详设技能")
        self.item["profile"]["skill"] = str(skill)
        template = TOOL.parents[1] / "公共知识库/模板/详设.md"
        if not template.exists():
            template = TOOL / "resources/templates/详设.md"
        if not template.exists():
            self.skipTest("当前机器未提供对应的实际详设模板")
        self.write("模板/详设.md", template.read_text(encoding="utf-8-sig"))
        self.write("详设/测试设计.md", doc("# 测试设计\n\n```mermaid\nflowchart LR\n A[开始] --> B[结束]\n```\n"))
        self.write("详设/_INDEX.md", doc("# 入口\n\n[[详设/测试设计]]\n"))
        git(self.root, "add", ".")
        report = self.check()[1]
        executed = {e["checker"] for e in report["executions"]}
        for part in ("format", "struct_comments", "boundaries", "content"):
            self.assertIn(f"validate_detailed_design_{part}.py", executed)
        self.assertTrue(report["findings"])


if __name__ == "__main__":
    unittest.main()
