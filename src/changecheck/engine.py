from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from .checks import writing_document
from .modules.knowledge import script_check
from .project import effective_profile, matches, module_for
from .modules import knowledge, portable
from .commands import execute_commands
from .common import CheckError, DOCS, SKILL_SCRIPTS, SOURCE, TOOL, digest, home, run, save_json
from .repository import materialize
from .context import attach_references, snapshot_name, verify_references
from .storage import save_artifact


def implementation_fingerprint(profile):
    files = {p.relative_to(TOOL).as_posix(): digest(p.read_bytes())
             for folder in ("changecheck", "prompts") for p in (SOURCE / folder).rglob("*")
             if p.is_file() and p.suffix in (".py", ".txt")}
    files["src/main.py"] = digest((SOURCE / "main.py").read_bytes())
    skill = Path(profile.get("skill") or ".") / "scripts"
    for name in SKILL_SCRIPTS:
        path = skill / name
        files["skill/" + name] = digest(path.read_bytes()) if path.exists() else "missing"
    for name in ("设计方案.md", "规则覆盖清单.md"):
        files["文档/" + name] = digest((DOCS / name).read_bytes())
    for name in profile.get("rule_sources", []):
        path = Path(name)
        if not path.is_file():
            raise CheckError("规则来源文件缺失：" + name)
        files[name] = digest(path.read_bytes())
    return digest({"profile": profile, "files": files})


def run_checks(snap, item, selected_module=None):
    profile = effective_profile(item["profile"], snap)
    if selected_module:
        profile["modules"] = [selected_module]
    attach_references(snap, profile)
    start_fingerprint = implementation_fingerprint(profile)
    findings, executions = [], []
    modules = profile.get("modules", ["knowledge"])
    if "knowledge" in modules:
        found, done = knowledge.check(snap, profile)
        findings.extend(found)
        executions.extend(done)
    found, done, covered = portable.check(snap, profile)
    findings.extend(found)
    executions.extend(done)
    found, done, command_covered = execute_commands(snap, profile)
    findings.extend(found)
    executions.extend(done)
    for name in sorted(snap.changed):
        if matches(name, profile.get("exclude", [])):
            executions.append({"checker": "exclude", "file": name, "status": "excluded"})
        elif (not module_for(name, profile) or
              (module_for(name, profile) == "code" and name not in covered)) and name not in command_covered:
            findings.append({"file": name, "line": 1, "rule": "CORE001", "severity": "ERROR",
                             "message": "没有本阶段适用检查器；请配置必检命令或明确排除", "source": "change-check"})
    if start_fingerprint != implementation_fingerprint(profile):
        raise CheckError("检查期间工具或技能脚本发生变化，请重试")
    verify_references(snap, profile)
    if snap.staged:
        from .repository import index_identity
        if index_identity(snap.root) != (snap.tree, snap.head):
            raise CheckError("脚本检查期间暂存区或 HEAD 变化，必须重试")
    elif snap.captured:
        from .repository import load_snapshot
        current = load_snapshot(snap.root, all_files=snap.full_scan,
                                changed_files=snap.changed if snap.scope else None)
        original = {n: value for n, value in snap.identities.items() if n not in snap.external}
        if current.head != snap.head or current.names != snap.names - snap.external.keys() or current.identities != original:
            raise CheckError("脚本检查期间工作区输入变化，必须重试")
    unique = {}
    for finding in findings:
        key = digest({k: finding.get(k) for k in ("file", "line", "rule", "message", "primary", "related")})[:20]
        finding["id"] = key
        unique[key] = finding
    findings = sorted(unique.values(), key=lambda f: (f["file"], f["line"], f["rule"]))
    status = 1 if any(f["severity"] in ("ERROR", "REVIEW") for f in findings) else 0
    report = {"root": str(snap.root), "staged": snap.staged,
              "mode": "staged" if snap.staged else "session" if snap.scope else "worktree",
              "scope": snap.scope,
              "snapshot": snap.fingerprint(), "implementation": start_fingerprint,
              "changed": sorted(snap.changed), "executions": executions,
              "findings": findings, "exit_code": status,
              "writing_files": sorted(n for n in snap.changed if "knowledge" in modules and n in snap.data and writing_document(n, profile)),
              "modules": modules, "policy": profile,
              "stage": "commit" if snap.staged else "edit",
              "ai_review": "not_run",
              "mermaid_rendering": "not_run"}
    if not snap.delta:
        save_artifact(home() / "reports" / (item["id"] + ".json"), report)
    return report


def print_report(report):
    print(f"检查文件变动 {len(report['changed'])} 项；执行 {len(report['executions'])} 个检查单元")
    for finding in report["findings"]:
        print(f"{finding['file']}:{finding['line']}: {finding['severity']} "
              f"{finding['rule']} {finding['message']}")
        if finding.get("primary", {}).get("snapshot") == "baseline":
            print("  上述行号位于修改前基线；保留位置及上下文见 JSON 报告。")
    if not report["findings"]:
        print("脚本检查通过；不代表 AI 审查或图表渲染已完成。")
    if report.get("ai_review") == "not_run":
        print("日常 AI 语义审查未执行；仅在显式 review 或提交门禁中运行。")
