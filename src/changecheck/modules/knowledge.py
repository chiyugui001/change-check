from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from ..checks import frontmatter, generic_checks, location, matches, typed_document, visible_lines, writing_document
from ..common import CheckError, DOCS, SKILL_SCRIPTS, SOURCE, TOOL, digest, home, run, save_json
from ..repository import materialize
from ..context import attach_references, snapshot_name, verify_references
from ..storage import save_artifact


def script_check(script, name, folder, arguments):
    command = [sys.executable, "-B", "-X", "utf8", script, folder / name, "--json", *arguments]
    result = run(command, data=None, timeout=60, check=False,
                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1"))
    if result.returncode not in (0, 1):
        detail = result.stderr or result.stdout
        raise CheckError(f"{script.name} 运行失败：{detail.decode('utf-8', 'replace')[-1600:]}")
    try:
        output = json.loads(result.stdout.decode("utf-8-sig"))
        reports = output["results"]
        if not isinstance(reports, list) or len(reports) != 1:
            raise ValueError("结果文件数不符")
        report = reports[0]
        if "error" in report:
            raise ValueError(report["error"])
        entries = report["findings"]
        if not isinstance(entries, list):
            raise ValueError("findings 不是数组")
        if result.returncode == 1 and not entries:
            raise ValueError("退出码失败但无检查项")
        findings = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("检查项不是对象")
            if item.get("severity") not in ("ERROR", "REVIEW", "WARNING"):
                raise ValueError("未知严重程度")
            if script.name == "validate_detailed_design_content.py":
                primary = item["primary"]
                for unit in (primary, item.get("related")):
                    if unit is None:
                        continue
                    if (not isinstance(unit, dict) or type(unit.get("line")) is not int or
                            type(unit.get("end_line")) is not int or not 1 <= unit["line"] <= unit["end_line"] or
                            not all(isinstance(unit.get(k), str) for k in ("text", "section", "context"))):
                        raise ValueError("内容候选位置或上下文无效")
                if primary is None or not isinstance(item.get("reason"), str):
                    raise ValueError("内容候选缺少 primary 或 reason")
                baseline = item["rule"] == "D004"
                if baseline and not report.get("before"):
                    raise ValueError("丢失候选未提供基线")
                reference = location(name, primary["line"], primary["end_line"],
                                     "baseline" if baseline else "current")
                finding = {"file": name, "line": primary["line"], "rule": item["rule"],
                           "severity": item["severity"], "message": item["reason"],
                           "source": script.name, "primary": {**primary, **reference},
                           "related": None, "differences": item.get("differences", []),
                           "score": item.get("score")}
                if item.get("related"):
                    related = item["related"]
                    finding["related"] = {**related, **location(name, related["line"], related["end_line"])}
                findings.append(finding)
            else:
                findings.append({"file": name, "line": item["line"], "rule": item["rule"],
                                 "severity": item["severity"], "message": item["message"],
                                 "source": script.name})
        return findings
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckError(f"{script.name} 输出无效：{exc}") from exc


def check(snap, profile):
    findings, coverage = generic_checks(snap, profile)
    executions = [{"checker": "generic", "status": "completed", "rules": coverage}]
    skill = Path(profile.get("skill") or ".") / "scripts"
    targets = [n for n in sorted(snap.changed) if n in snap.data and
               Path(n).suffix in (".md", ".mmd") and
               not matches(n, profile.get("exclude", []))]
    with materialize(snap) as folder:
        for name in targets:
            design = typed_document(name, profile, "design")
            scripts = []
            if design:
                for required in SKILL_SCRIPTS:
                    if not (skill / required).is_file():
                        raise CheckError(f"缺少必检脚本：{skill / required}")
                template = snapshot_name(profile.get("template"))
                if template not in snap.data:
                    raise CheckError("详设模板缺失或未纳入本次快照")
                arguments = ["--template", folder / template,
                             "--line-limit", str(profile.get("prose_line_limit", 120)),
                             "--paragraph-limit", str(profile.get("prose_paragraph_limit", 200))]
                # Non-source designs intentionally use the skill's official non-source route.
                try:
                    meta, _ = frontmatter(snap.text(name))
                    if not any(meta.get(k) for k in ("source", "git_branch", "git_commit")):
                        arguments.append("--non-source")
                except (ValueError, yaml.YAMLError):
                    pass
                scripts.append(("validate_detailed_design_format.py", arguments))
                scripts.extend((f"validate_detailed_design_{part}.py", [])
                               for part in ("struct_comments", "boundaries", "content"))
            elif "mermaid" in snap.text(name) or name.endswith(".mmd"):
                scripts.append(("validate_diagram_labels.py", []))
            if not design and writing_document(name, profile):
                scripts.append(("validate_detailed_design_content.py", []))
            for script_name, arguments in scripts:
                script = skill / script_name
                if not script.is_file():
                    raise CheckError(f"缺少必检脚本：{script}")
                if script_name.endswith("_content.py") and name in snap.before:
                    before = folder / ".baseline" / name
                    before.parent.mkdir(parents=True, exist_ok=True)
                    before.write_bytes(snap.before[name])
                    arguments += ["--before", before]
                findings.extend(script_check(script, name, folder, arguments))
                executions.append({"checker": script_name, "file": name, "status": "completed"})
            if typed_document(name, profile, "requirement"):
                requirement_template = snapshot_name(profile.get("requirement_template") or profile.get(
                    "template", "").replace("详设.md", "需求.md"))
                if requirement_template not in snap.data:
                    raise CheckError("需求模板缺失，请配置 requirement_template")
                headings = [line for _, line in visible_lines(snap.text(requirement_template))[0]
                            if line.startswith("## ") and "<" not in line]
                for heading in headings:
                    if heading not in snap.text(name).splitlines():
                        findings.append({"file": name, "line": 1, "rule": "RD002",
                                         "severity": "ERROR", "message": "缺少模板章节：" + heading,
                                         "source": "change-check"})
                executions.append({"checker": "requirement_template", "file": name,
                                   "status": "completed"})
    return findings, executions
