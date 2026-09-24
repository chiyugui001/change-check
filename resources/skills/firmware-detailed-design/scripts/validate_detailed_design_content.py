#!/usr/bin/env python3
"""Read-only candidates for duplicate/misplaced design content and lost wording.

Every finding is REVIEW, never a semantic verdict. Exit 0: no candidates;
1: candidates need review; 2: bad arguments or unreadable input.
No external services, automatic editing, or project-specific identifiers.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

from validate_detailed_design_boundaries import table_cells


MIN_CHARS = 32
SIMILARITY = 0.88
ACTION = re.compile(r"读取|写入|保存|提交|清零|解除|返回|入队|串行|重试|失败|恢复|发布|重判|通知|订阅|不得|必须|只能|不允许|不等待", re.I)
READING_AID = re.compile(r"图例|高亮规则|颜色说明|浅蓝|浅橙|阅读说明|参见|^见\s*[§第]|^本图(?:展示|说明|覆盖)")


@dataclass(frozen=True)
class Unit:
    line: int
    end_line: int
    section: str
    role: str
    kind: str
    text: str
    context: str = ""
    block: str = ""


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    reason: str
    primary: Unit
    related: Unit | None = None
    score: float | None = None
    differences: tuple[str, ...] = ()


def normalize(text: str) -> str:
    """Remove presentation only: preserve identifiers, numbers and negation."""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"\[\[([^]\n]+)\]\]", lambda m: m[1].split("|")[-1], text)
    text = re.sub(r"!?\[([^]\n]*)\]\([^\n]*?\)", r"\1", text)
    text = html.unescape(text).replace("`", "").replace("**", "")
    return re.sub(r"\s+", "", text)


def role_of(titles: list[str]) -> str:
    path = "/".join(titles)
    if "概览" in path:
        return "overview"
    if re.search(r"风险|待确认", path):
        return "risk"
    for title in reversed(titles):
        for pattern, role in ((r"关键时序|初始化时序|变更时序|事件时序|开关时序|时序图", "sequence"),
                              (r"依赖关系|依赖图", "dependency"),
                              (r"事件|回调", "event"), (r"函数|接口", "function"),
                              (r"数据|字段|结构体|实例与访问", "data"),
                              (r"配置参数", "configuration")):
            if re.search(pattern, title):
                return role
    return "other"


def extract(text: str) -> list[Unit]:
    """Small Markdown scanner with source locations; not a Markdown/Mermaid AST."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").splitlines()
    units: list[Unit] = []
    headings: list[tuple[int, str]] = []
    label = ""
    comment = False
    fence = ""
    language = ""
    diagram_context: list[str] = []
    header: list[str] = []
    table_start = 0
    pending: list[str] = []
    pending_start = 0
    pending_end = 0
    frontmatter = bool(lines and lines[0].strip() == "---")

    def emit(line: int, end: int, kind: str, value: str, context: str = "") -> None:
        titles = [h[1] for h in headings]
        role = "purpose" if label == "用途" else role_of(titles + [label])
        units.append(Unit(line, end, " / ".join(titles), role, kind, value, context or label, label))

    def flush() -> None:
        if pending:
            emit(pending_start, pending_end, "prose", "\n".join(pending))
            pending.clear()

    for index, original in enumerate(lines):
        number = index + 1
        line = original.strip()
        if frontmatter:
            if index and line in {"---", "..."}:
                frontmatter = False
            continue
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = language = ""
                continue
            if language == "mermaid":
                if re.match(r"^(alt|opt|break|loop|critical|par)\b", line):
                    diagram_context.append(line)
                    emit(number, number, "diagram", line, " / ".join(diagram_context))
                elif line.startswith("else"):
                    if diagram_context:
                        diagram_context[-1] = line
                    emit(number, number, "diagram", line, " / ".join(diagram_context))
                elif line == "end":
                    if diagram_context:
                        diagram_context.pop()
                elif line.startswith(("rect ", "box ")):
                    diagram_context.append(line)
                elif ":" in line and ("->" in line or "-)" in line or line.startswith("Note ")):
                    prefix, value = line.split(":", 1)
                    emit(number, number, "diagram", value.strip(), " / ".join([*diagram_context, prefix]))
                elif "-->" in line:
                    # Flow/state graph labels remain text candidates, never interpreted as behavior.
                    emit(number, number, "diagram", line, "Mermaid relation")
            continue
        # Comments are ignored outside fences, without interpreting comment-like code.
        if comment:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1].strip()
            comment = False
        while "<!--" in line:
            before, after = line.split("<!--", 1)
            if "-->" in after:
                line = before + after.split("-->", 1)[1]
            else:
                line, comment = before, True
                break
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line)
        if marker:
            flush()
            fence, language = marker[1], marker[2].strip().lower()
            diagram_context = []
            header = []
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            flush()
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2]))
            label = ""
            header = []
            continue
        bold = re.match(r"^\*\*([^*]+?)\*\*[:：]?(.*)$", line)
        if bold:
            flush()
            label = bold[1].rstrip("：:")
            if bold[2].strip():
                emit(number, number, "prose", bold[2].strip())
            header = []
            continue
        if line.startswith("|"):
            flush()
            cells = table_cells(line)
            next_cells = table_cells(lines[index + 1]) if index + 1 < len(lines) else []
            if next_cells and all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in next_cells):
                header, table_start = cells, number
                continue
            if cells and all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in cells):
                continue
            if header and len(cells) == len(header):
                emit(number, number, "table", "；".join(cells),
                     f"table@{table_start}: " + "；".join(f"{h}={v}" for h, v in zip(header, cells)))
            continue
        header = []
        if not line or re.fullmatch(r"[-*_]{3,}", line) or line.startswith("!["):
            flush()
            continue
        bullet = re.match(r"^(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*)", line)
        if bullet:
            flush()
            line = bullet[1]
        if not pending:
            pending_start = number
        pending_end = number
        pending.append(line)
    flush()
    return units


def eligible(unit: Unit, min_chars: int) -> bool:
    value = normalize(unit.text)
    # Repeated identifiers/signatures alone are not repeated requirements.
    language = len(re.findall(r"[\u4e00-\u9fff]", value)) >= 10 or len(re.findall(r"\b[a-zA-Z]{2,}\b", unit.text)) >= 7
    return unit.role not in {"overview", "purpose", "risk"} and len(value) >= min_chars and language


def similarity(left: Unit, right: Unit) -> float:
    a, b = normalize(left.text), normalize(right.text)
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def differences(left: Unit, right: Unit) -> tuple[str, ...]:
    checks = ((r"\d+(?:\.\d+)?(?:ms|秒|分钟|小时|天|s)?", "数值或单位不同"),
              (r"不允许|不得|不再|不能|不会|不重试|不保存|不发送|不|未|非|无|只|仅", "否定或限定词不同"),
              (r"[A-Za-z_]\w*", "标识符或枚举不同"))
    result = [message for pattern, message in checks
              if set(re.findall(pattern, left.text)) != set(re.findall(pattern, right.text))]
    if normalize(left.text) != normalize(right.text):
        result.append("原文不完全相同，须核对独有条件、动作与结果")
    if left.context != right.context or left.section != right.section:
        result.append("上下文不同，须核对对象、触发条件与执行阶段")
    return tuple(result)


ENUM_FILLER = re.compile(r"(?:合法|有效)(?:的)?(?:配置值|业务状态|业务值|检测方向|未知态|枚举值|取值)|无特殊(?:要求|限制)|无额外限制")


def enum_findings(text: str, units: list[Unit]) -> list[Finding]:
    """Review filler in enum tables; reuse extracted tables and source locations."""
    lines = text.lstrip("\ufeff").splitlines()
    findings: list[Finding] = []
    tables: dict[int, list[tuple[Unit, str]]] = {}
    for unit in units:
        if unit.kind != "table":
            continue
        match = re.match(r"table@(\d+):", unit.context)
        if not match:
            continue
        start = int(match[1])
        headers = [normalize(cell) for cell in table_cells(lines[start - 1])]
        if "枚举值" not in headers:
            continue
        cells = table_cells(lines[unit.line - 1])
        if len(cells) != len(headers):
            continue
        # Match whole clauses, not words inside real constraints or negations.
        clauses = [normalize(part).strip() for cell in cells
                   for part in re.split(r"[；;，,。]|<br\s*/?>", cell, flags=re.I)]
        fillers = sorted({part for part in clauses if ENUM_FILLER.fullmatch(part)})
        if fillers:
            findings.append(Finding("D005", "REVIEW",
                                    "枚举表含空泛措辞：" + "、".join(fillers)
                                    + "；删除填充语，保留同一单元格中的具体限制。", unit))
        if "取值限制" in headers:
            value = normalize(cells[headers.index("取值限制")]).strip("。；;，,")
            tables.setdefault(start, []).append((unit, value))
    for rows in tables.values():
        if all(value in {"", "-", "—", "无"} or ENUM_FILLER.fullmatch(value)
               for _, value in rows):
            findings.append(Finding("D006", "REVIEW",
                                    "整列取值限制没有独立约束；核对后省略该列，不为填表编造限制。",
                                    rows[0][0], rows[-1][0] if len(rows) > 1 else None))
    return findings


def scan(text: str, min_chars: int = MIN_CHARS, threshold: float = SIMILARITY) -> list[Finding]:
    units = extract(text)
    findings = enum_findings(text, units)
    for i, unit in enumerate(units):
        if unit.kind != "prose" or not eligible(unit, min_chars) or READING_AID.search(unit.text):
            continue
        matches: list[tuple[float, Unit]] = []
        for j, other in enumerate(units):
            if j == i or not eligible(other, min_chars):
                continue
            if other.kind == "prose" and j > i:
                continue
            score = similarity(unit, other)
            if score >= threshold:
                matches.append((score, other))
        if matches:
            # One closest witness per prose block keeps large reports usable.
            score, other = max(matches, key=lambda pair: (pair[0], -abs(unit.line - pair[1].line)))
            findings.append(Finding("D001", "REVIEW", "存在相同、包含或相似表述；仅展示本段最相近的一处，不能据此删除。",
                                    unit, other, round(score, 3), differences(unit, other)))
        elif unit.role in {"sequence", "dependency"} and ACTION.search(unit.text):
            related = min((u for u in units if u.section == unit.section and u.kind == "diagram"),
                          key=lambda u: abs(u.line - unit.line), default=None)
            findings.append(Finding("D002", "REVIEW", "图所在章节含独立业务说明；核对是否应入图、回归接口定义或风险表。不是重复结论。", unit, related))
        elif ACTION.search(unit.text) and not re.match(r"本模块不负责|本模块职责不包括", unit.text):
            related = min((u for u in units if u.section == unit.section and u.kind == "table"
                           and u.block == unit.block
                           and 0 < unit.line - u.end_line <= 4),
                          key=lambda u: unit.line - u.end_line, default=None)
            if related:
                findings.append(Finding("D003", "REVIEW", "表后有独立处理说明；核对是否可合入表格，保留独有规则。", unit, related))
    return findings


def compare_before(before: str, after: str, min_chars: int = MIN_CHARS) -> list[Finding]:
    """Conservative wording coverage, NOT proof that semantics were preserved."""
    current = extract(after)
    findings = []
    seen: set[tuple[str, str, str]] = set()
    for unit in extract(before):
        key = normalize(unit.text)
        context = re.sub(r"table@\d+: ", "", unit.context)
        identity = (key, unit.section, context)
        if not (eligible(unit, min_chars) or unit.role == "risk" and len(key) >= min_chars) or identity in seen:
            continue
        seen.add(identity)
        # Require both text and context for a retained definition. Relocation needs AI review.
        if any(key in normalize(u.text) and unit.section == u.section
               and context == re.sub(r"table@\d+: ", "", u.context) for u in current):
            continue
        other = max(current, key=lambda u: similarity(unit, u), default=None)
        score = similarity(unit, other) if other else None
        findings.append(Finding("D004", "REVIEW", "旧版原文已删除、改写或移动；确认必要信息在新版的保留位置，匹配分数不证明语义覆盖。",
                                unit, other, round(score, 3) if score is not None else None,
                                differences(unit, other) if other else ("新版无可匹配内容",)))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", type=Path, nargs="+")
    parser.add_argument("--min-chars", type=int, default=MIN_CHARS)
    parser.add_argument("--similarity", type=float, default=SIMILARITY)
    parser.add_argument("--before", type=Path, help="Old version, allowed with one document; D004 primary points here")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.min_chars < 1 or not 0 < args.similarity <= 1:
        parser.error("min-chars must be positive and similarity must be in (0, 1]")
    if args.before and len(args.documents) != 1:
        parser.error("--before requires exactly one current document")
    results = []
    status = 0
    for path in args.documents:
        try:
            current = path.read_text(encoding="utf-8-sig")
            findings = scan(current, args.min_chars, args.similarity)
            if args.before:
                findings += compare_before(args.before.read_text(encoding="utf-8-sig"), current, args.min_chars)
        except (OSError, UnicodeError) as exc:
            results.append({"file": str(path), "error": str(exc)})
            status = 2
            continue
        results.append({"file": str(path), "before": str(args.before) if args.before else None,
                        "findings": [asdict(f) for f in findings]})
        if findings:
            status = max(status, 1)
    if args.json:
        print(json.dumps({"results": results, "exit_code": status}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if "error" in result:
                print(f"{result['file']}: input error: {result['error']}")
                continue
            print(f"{result['file']}: {len(result['findings'])} REVIEW candidates")
            for finding in result["findings"]:
                source = result["before"] if finding["rule"] == "D004" else result["file"]
                for name, location in (("primary", source), ("related", result["file"])):
                    unit = finding[name]
                    if unit:
                        print(f"  {name}: {location}:{unit['line']}-{unit['end_line']} [{unit['section']}] {unit['kind']}")
                        print(f"    {unit['text']}")
                        print(f"    context: {unit['context']}")
                print(f"  {finding['rule']} REVIEW: {finding['reason']} score={finding['score']}")
                for detail in finding["differences"]:
                    print(f"    {detail}")
        print("只读候选检查；未命中不代表无重复或无遗漏。归属、必要重复、冲突和语义覆盖须由 AI/人工复核。")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
