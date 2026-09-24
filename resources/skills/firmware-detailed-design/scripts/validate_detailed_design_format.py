#!/usr/bin/env python3
"""Read-only, whole-document format checks against the supplied design template.

Exit 0: mechanical checks passed; 1: ERROR/REVIEW findings; 2: input/profile error.
This does not establish semantic correctness or replace Mermaid rendering.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from validate_detailed_design_boundaries import table_cells
from validate_detailed_design_frontmatter import SOURCE_FIELDS, parse_frontmatter, validate_values
from validate_detailed_design_prose import LINE_LIMIT, PARAGRAPH_LIMIT, check_prose, positive_int
from validate_diagram_labels import check_diagram_labels


@dataclass
class Finding:
    line: int
    rule: str
    message: str
    severity: str = "ERROR"


@dataclass
class Table:
    line: int
    headers: list[str]
    rows: list[tuple[int, list[str]]] = field(default_factory=list)
    label: str = ""


@dataclass
class Section:
    line: int
    level: int
    number: str
    title: str
    parent: Section | None = None
    text: list[tuple[int, str]] = field(default_factory=list)
    labels: list[tuple[int, str]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    codes: list[tuple[int, str, str]] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)


def plain(value: str) -> str:
    return value.replace("`", "").replace("**", "").strip()


def canonical(title: str) -> str:
    return re.sub(r"（(?:按[^）]*|.*按需.*)）", "", plain(title)).strip()


def parse(text: str) -> tuple[list[Section], list[Finding]]:
    """Parse headings, labels, tables and fences with original line numbers."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").splitlines()
    root = Section(1, 0, "", "")
    sections = [root]
    stack = [root]
    issues: list[Finding] = []
    frontmatter = bool(lines and lines[0].strip() == "---")
    comment = False
    fence = language = ""
    code: list[str] = []
    code_line = 0
    table = None
    label = ""
    for index, original in enumerate(lines):
        n = index + 1
        line = original
        if frontmatter:
            if index and line.strip() in {"---", "..."}:
                frontmatter = False
            continue
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line.strip()):
                stack[-1].codes.append((code_line, language, "\n".join(code)))
                fence = ""
            else:
                code.append(line)
            continue
        if comment:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1]
            comment = False
        while "<!--" in line:
            before, after = line.split("<!--", 1)
            if "-->" in after:
                line = before + after.split("-->", 1)[1]
            else:
                line, comment = before, True
                break
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker:
            fence, language, code_line, code = marker[1], marker[2].strip(), n, []
            table = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            match = re.match(r"(\d+(?:\.\d+)*)\.?\s+(.+)", title)
            number = match[1] if match else ""
            title = match[2] if match else title
            if level > stack[-1].level + 1:
                issues.append(Finding(n, "F001", "标题层级跳跃。"))
            while stack[-1].level >= level:
                stack.pop()
            node = Section(n, level, number, canonical(title), stack[-1])
            stack[-1].children.append(node)
            sections.append(node)
            stack.append(node)
            table, label = None, ""
            if number and len(number.split(".")) != level - 1:
                issues.append(Finding(n, "F001", "章节编号深度与标题层级不一致。"))
            continue
        block = re.match(r"^\*\*([^*]+?)\*\*[:：]?(.*)$", line.strip())
        if block:
            label = block[1].rstrip(":：")
            stack[-1].labels.append((n, label))
        if "|" in line and not block:
            cells = table_cells(line)
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            sep = table_cells(next_line) if "|" in next_line else []
            is_sep = bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)
            if sep and all(re.fullmatch(r":?-{3,}:?", c) for c in sep):
                table = Table(n, [plain(c) for c in cells], label=label)
                stack[-1].tables.append(table)
                if len(sep) != len(cells):
                    issues.append(Finding(n + 1, "F002", "表头与分隔行列数不一致。"))
            elif table and not is_sep:
                table.rows.append((n, cells))
                if len(cells) != len(table.headers):
                    issues.append(Finding(n, "F002", f"表格应为 {len(table.headers)} 列，实际 {len(cells)} 列。"))
            elif not is_sep and line.strip().startswith("|"):
                issues.append(Finding(n, "F002", "表格缺少表头或有效分隔行。"))
        else:
            table = None
        stack[-1].text.append((n, line))
    if fence:
        issues.append(Finding(code_line, "F003", "代码围栏未闭合。"))
    if comment:
        issues.append(Finding(len(lines), "F003", "HTML 注释未闭合。"))
    if frontmatter:
        issues.append(Finding(1, "F003", "Frontmatter 未闭合。"))
    for section in sections:
        for item in section.tables:
            if not item.rows:
                issues.append(Finding(item.line, "F002", "空表格；补充实际内容或按模板省略。"))
    return sections, issues


def body(section: Section) -> str:
    return "\n".join(line for _, line in section.text)


def absent(section: Section) -> bool:
    text = body(section).strip().strip("。")
    return bool(re.fullmatch(r"(?:本模块)?(?:无|没有|不提供|不涉及)(?:独立业务状态机|对外函数|对外事件|事件|函数|配置参数|配置|调试指令|过程日志|待确认事项|风险|并发访问|内部调用时序)?", text))


def block_text(section: Section, name: str) -> str:
    start = next((n for n, label in section.labels if label == name), 0)
    if not start:
        return ""
    end = min([n for n, _ in section.labels if n > start] + [10**9])
    lines = [re.sub(r"^\*\*[^*]+\*\*[:：]?", "", line) if n == start else line
             for n, line in section.text if start <= n < end]
    return "\n".join(lines).strip()


def pending_headers(headers: list[str]) -> list[str]:
    """Only scenario columns may append a descriptive suffix to the template name."""
    if headers[:2] == ["对比项", "共同条件"] and len(headers) == 4:
        return headers[:2] + [re.sub(r"^(场景 [AB])[：:]\s*\S.*$", r"\1", h) for h in headers[2:]]
    return headers


def pending_value(value: str) -> bool:
    return plain(value).lower() not in {"", "—", "–", "-", "/", "待确认", "待定", "待补充", "tbd"}


def check_pending_table(table: Table, models: list[Table]) -> list[Finding]:
    model = next((candidate for candidate in models
                  if candidate.headers == pending_headers(table.headers)), None)
    if model is None:
        return [Finding(table.line, "F008", "待确认表格须使用当前模板的表头；场景 A/B 可追加冒号及说明。")]
    result: list[Finding] = []
    required_rows = [plain(cells[0]) for _, cells in model.rows if cells and not cells[0].startswith("<")]
    names = [plain(cells[0]) for _, cells in table.rows if cells]
    for name in required_rows:
        if names.count(name) != 1:
            result.append(Finding(table.line, "F008", f"待确认表格须有且仅有一行“{name}”。"))
    general = model.headers[0] in {"对比项", "项目"}
    if general and all(names.count(name) == 1 for name in required_rows):
        if [name for name in names if name in required_rows] != required_rows:
            result.append(Finding(table.line, "F008", "待确认表格固定行顺序应为：" + " / ".join(required_rows)))
    for n, cells in table.rows:
        if any(not value.strip() for value in cells):
            result.append(Finding(n, "F008", "待确认表格各单元格须填写内容；共同条件已说明且无场景差异的单元格可填“—”。"))
        if general and cells and plain(cells[0]) in required_rows:
            if len(cells) < 2 or not pending_value(cells[1]):
                result.append(Finding(n, "F008", f"{cells[0]}须填写实际说明，不能仅用空位或待确认占位。"))
        if general and cells and plain(cells[0]) == "结论":
            result.append(Finding(n, "F008", "结论应放在表后，不得另设结论行。"))
    if model.headers[0] == "对比项":
        comparisons = [cells for _, cells in table.rows
                       if len(cells) == 4 and plain(cells[0]) not in required_rows
                       and all(pending_value(value) for value in cells)]
        if not comparisons:
            result.append(Finding(table.line, "F008", "对比表至少列一个具体场景或关键节点，共同条件及 A/B 两侧均须有实际内容。"))
    if model.headers[0] != "时间点/对比项":
        return result
    # Historical time-table profiles remain valid only when the supplied template declares them.
    units = {"秒": 1, "s": 1, "分钟": 60, "min": 60, "小时": 3600, "h": 3600}
    times = {float(match[1]) * units[match[2]] for name in names
             if (match := re.fullmatch(r"(?:第\s*)?(\d+(?:\.\d+)?)\s*(秒|分钟|小时|s|min|h)", name))}
    if len(times) < 2:
        result.append(Finding(table.line, "F008", "待确认时间对照表至少列出两个不同时间点。"))
    return result


def check_pending(section: Section, expected: Section) -> list[Finding]:
    result: list[Finding] = []
    fields = list(dict.fromkeys(re.findall(r"^ {2,}- ([^：:\r\n]+)[：:]", body(expected), re.M)))
    table_models = [table for table in expected.tables
                    if table.headers and table.headers[0] in {"时间点/对比项", "对比项", "项目"}]
    if not fields or "结论" not in fields:
        raise ValueError("模板待确认事项缺少复选框字段，不能跳过检查")
    table_only = bool(table_models) and fields == ["结论"]
    if absent(section):
        return result
    items: list[tuple[int, str, str, list[tuple[int, str, str]]]] = []
    for line_no, line in section.text:
        item = re.match(r"^- \[([ xX])\]\s+(.+)", line)
        entry_pattern = r"^ {2}- ([^：:]+)[：:]\s*(.*)$" if table_only else r"^ {2,}- ([^：:]+)[：:]\s*(.*)$"
        entry = re.match(entry_pattern, line)
        if item:
            items.append((line_no, item[1], item[2], []))
        elif entry and items:
            items[-1][3].append((line_no, entry[1], entry[2]))
        elif items and table_models and re.match(r"^ {2}\|" if table_only else r"^ {2,3}\|", line):
            continue
        elif line.strip() and not re.fullmatch(r"[-*_]{3,}", line.strip()):
            result.append(Finding(line_no, "F008", "待确认项应为 '- [ ] 事项'，下设模板字段或允许的条目内对照表；不能使用脱离条目的表格或独立说明段。"))
    if not items:
        result.append(Finding(section.line, "F008", "待确认事项缺少复选框条目；没有事项时明确写“无”。"))
    for index, (line, checked, _, entries) in enumerate(items):
        end = items[index + 1][0] if index + 1 < len(items) else 10**9
        tables = [table for table in section.tables if line < table.line < end]
        required = ["结论"] if tables else fields
        if table_only and not tables:
            result.append(Finding(line, "F008", "每个待确认项须有一张条目内表格，不能仅保留标题、字段列表或结论。"))
        if tables:
            if len(tables) != 1:
                result.append(Finding(line, "F008", "每个待确认项只保留一张对照表，避免分散共同条件。"))
            for table in tables:
                result += check_pending_table(table, table_models)
                table_end = max([table.line] + [n for n, _ in table.rows])
                if table_only:
                    text_by_line = dict(section.text)
                    if text_by_line.get(table.line - 1, "").strip() or text_by_line.get(table_end + 1, "").strip():
                        result.append(Finding(table.line, "F008", "条目内表格前后须各留空行。"))
                if any(n <= table_end for n, _, _ in entries):
                    result.append(Finding(line, "F008", "对照表替代说明字段，结论应放在表后。"))
        names = [name for _, name, _ in entries]
        if names != required:
            result.append(Finding(line, "F008", "待确认项字段及顺序应为：" + " / ".join(required)))
        for n, name, value in entries:
            if name == "结论":
                if checked == " " and value:
                    result.append(Finding(n, "F008", "未确认项的结论必须留空，不能填“待确认”。"))
                if checked.lower() == "x" and not pending_value(value):
                    result.append(Finding(n, "F008", "已勾选事项应填写实际结论。"))
            elif not value:
                result.append(Finding(n, "F008", f"{name}不能为空。"))
    return result


def check_dependency_diagram(line: int, code: str) -> list[Finding]:
    """Check the stable visual boundary required for the §1.2 relation graph."""
    result: list[Finding] = []
    if not code.lstrip().startswith("flowchart"):
        return [Finding(line, "F009", "依赖关系图须使用 flowchart。")]

    groups: list[str] = []
    members: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    node_pattern = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\[([^]]*)\]|\(([^)]*)\)|\{([^}]*)\})")
    for raw in code.splitlines():
        text = raw.strip()
        group = re.match(r"subgraph\s+(.+?)\s*$", text, re.I)
        if group:
            groups.append(group[1])
            continue
        if text == "end":
            if groups:
                groups.pop()
            continue
        for node in node_pattern.finditer(text):
            name = node[1]
            label = next((value for value in node.groups()[1:] if value is not None), "")
            labels[name] = label
            members.setdefault(name, set()).update(groups)

    def matches(value: str, tokens: tuple[str, ...]) -> bool:
        lower = value.lower()
        return any(token.lower() in lower for token in tokens)

    all_groups = {group for values in members.values() for group in values}
    input_groups = {group for group in all_groups if matches(group, ("上游", "input"))}
    output_groups = {group for group in all_groups if matches(group, ("下游", "output"))}
    if not input_groups:
        result.append(Finding(line, "F009", "依赖关系图须使用 subgraph 明确“上游输入与请求”区域。"))
    if not output_groups:
        result.append(Finding(line, "F009", "依赖关系图须使用 subgraph 明确“下游使用方”区域。"))

    module_group = any(matches(group, ("本模块", "module", "模块"))
                       for group in all_groups - input_groups - output_groups)
    module_node = any(matches(label, ("本模块", "模块"))
                      and not members.get(name, set()) & (input_groups | output_groups)
                      for name, label in labels.items())
    if not module_group and not module_node:
        result.append(Finding(line, "F009", "依赖关系图须在上下游区域之间标出本模块。"))

    return result


def check_diagram(line: int, code: str) -> list[Finding]:
    result = [Finding(f.line, f.rule, f.message, f.severity)
              for f in check_diagram_labels(line, code)]
    if not code.strip().startswith("sequenceDiagram"):
        return result
    ids = re.findall(r"^\s*(?:participant|actor)\s+(\w+)", code, re.M)
    active: dict[str, int] = {}
    branches: list[str] = []
    messages: list[int] = []
    notes: dict[str, list[int]] = {"进入": [], "退出": []}
    declarations: list[int] = []
    for offset, raw in enumerate(code.splitlines(), 1):
        text = raw.strip()
        if text.startswith("%%"):
            continue
        if text.startswith(("participant ", "actor ")):
            declarations.append(offset)
        if re.match(r"\w+\s*(?:--?>>?|--?\))", text):
            messages.append(offset)
        if text.startswith("Note "):
            for word in notes:
                if word in text:
                    notes[word].append(offset)
        control = re.match(r"(alt|opt|loop|break|rect|box|par|critical)\s", text)
        if control:
            if control[1] in {"alt", "opt"} and sum(b in {"alt", "opt"} for b in branches) >= 2:
                result.append(Finding(line + offset, "F009", "时序图 alt/opt 有效嵌套达到三层；按模板使用早退或按独立链路拆分。"))
            branches.append(control[1])
        elif text == "end":
            if branches:
                branches.pop()
            else:
                result.append(Finding(line + offset, "F009", "时序图存在未匹配的 end。"))
        action = re.match(r"(activate|deactivate)\s+(\w+)", text)
        if action:
            name = action[2]
            active[name] = active.get(name, 0) + (1 if action[1] == "activate" else -1)
            if active[name] < 0:
                result.append(Finding(line + offset, "F009", "deactivate 没有对应 activate。"))
        label = None
        limit = 40
        if " as " in text and text.startswith(("participant ", "actor ")):
            label = text.split(" as ", 1)[1]
        elif text.startswith("Note ") and ":" in text:
            where, label = text.split(":", 1)
            endpoints = where.split()[-1].split(",")
            if all(p in ids for p in endpoints):
                limit *= abs(ids.index(endpoints[0]) - ids.index(endpoints[-1])) + 1
        elif re.match(r"(?:alt|else|opt|loop|break|critical|par)\s", text):
            label = text.split(" ", 1)[1]
        elif re.search(r"-[->)]", text) and ":" in text:
            label = text.split(":", 1)[1]
        if label:
            for piece in re.split(r"<br\s*/?>", label):
                piece = piece.strip()
                width = sum(2 if ord(c) > 255 else 1 for c in piece)
                if width > limit and not re.fullmatch(r"[A-Za-z_]\w*(?:\(.*\))?", piece):
                    result.append(Finding(line + offset, "F009", f"时序图文字宽度 {width} 超过 {limit}，在语义边界换行。"))
    if any(active.values()):
        result.append(Finding(line, "F009", "时序图激活区间不平衡。"))
    if branches:
        result.append(Finding(line, "F009", "时序图分支或分组未闭合。"))
    for word in ("进入", "退出"):
        if not re.search(r"^\s*Note[^\n]*" + word, code, re.M):
            result.append(Finding(line, "F009", f"时序图缺少{word}条件 Note。"))
    if messages and notes["进入"] and not any(max(declarations, default=0) < n < messages[0] for n in notes["进入"]):
        result.append(Finding(line, "F009", "进入 Note 应位于参与者声明之后、首个调用之前。"))
    if messages and notes["退出"] and max(notes["退出"]) < messages[-1]:
        result.append(Finding(line, "F009", "退出 Note 应位于本图最后一个调用或返回之后。"))
    return result


def check_format(text: str, template: str, non_source: bool = False,
                 line_limit: int = LINE_LIMIT, paragraph_limit: int = PARAGRAPH_LIMIT) -> list[Finding]:
    sections, findings = parse(text)
    pattern, template_errors = parse(template)
    if template_errors:
        raise ValueError("模板本身存在结构错误：" + template_errors[0].message)
    fixed = [s for s in pattern if s.number and (s.level <= 3 or s.parent.title == "接口概览")
             and not (s.parent is not None and s.parent.number == "4" and s.parent.title == "时序图")]
    if not fixed or not any(s.title == "待确认事项" for s in fixed):
        raise ValueError("模板缺少可识别的固定章节或待确认事项格式")
    by_number = {s.number: s for s in fixed}
    placeholders = set(re.findall(r"<[^<>\n]*[\u4e00-\u9fff][^<>\n]*>", template))
    used_numbers: set[str] = set()
    actual: dict[str, Section] = {}
    previous = -1
    for s in sections[1:]:
        if s.number:
            if s.number in used_numbers and s.number not in by_number:
                findings.append(Finding(s.line, "F001", "详细小节编号重复。"))
            used_numbers.add(s.number)
        legacy_time_chapter = (s.number == "4" and s.title == "时序与并发"
                               and by_number.get("4") is not None
                               and by_number["4"].title == "时序图")
        time_diagram_child = (s.level == 3 and s.parent is not None
                              and s.parent.number == "4" and s.parent.title == "时序图")
        legacy_time_child = (s.level == 3 and s.parent is not None
                             and s.parent.number == "4" and s.parent.title == "时序与并发")
        if s.number in by_number:
            expected = by_number[s.number]
            if s.number in actual:
                findings.append(Finding(s.line, "F004", "固定章节编号重复。"))
            actual[s.number] = s
            order = fixed.index(expected)
            if order < previous:
                findings.append(Finding(s.line, "F004", "固定章节顺序与模板不符。"))
            previous = order
            if (s.title != expected.title and not legacy_time_chapter) or s.level != expected.level:
                findings.append(Finding(s.line, "F004", f"模板要求 {'#' * expected.level} {expected.number} {expected.title}。"))
        elif s.level == 3 and s.parent and s.parent.number == "3" and s.parent.title == "状态迁移":
            # 状态迁移允许按业务状态机细分；具体语义由内容审核确认。
            pass
        elif time_diagram_child or legacy_time_child:
            # 第 4 章时序图按链路编号；兼容旧文档的时序与并发子章节。
            pass
        elif s.level in {2, 3}:
            findings.append(Finding(s.line, "F004", "模板外章节；核对是否应归入现有章节。", "REVIEW"))
        if s.number and s.parent and s.parent.number and not s.number.startswith(s.parent.number + "."):
            findings.append(Finding(s.line, "F001", "子章节编号不属于父章节。"))
    if len([s for s in sections if s.level == 1]) != 1:
        findings.append(Finding(1, "F001", "正文应有且仅有一个一级标题。"))
    optional: set[str] = set()
    if non_source:
        optional.add("模块组成")
    for expected in fixed:
        s = actual.get(expected.number)
        if s is None:
            if expected.title not in optional:
                findings.append(Finding(1, "F004", f"缺少模板章节 {expected.number} {expected.title}。"))
            continue
        if expected.title == "待确认事项":
            findings += check_pending(s, expected)
            continue
        if absent(s) and expected.title in {"函数", "事件", "关键数据结构", "函数接口详细定义",
                                            "事件与回调详细定义", "状态迁移", "配置参数", "调试指令",
                                            "过程日志", "风险清单"}:
            continue
        # Direct tables exclude those owned by repeated child sections.
        for table in expected.tables:
            if table.label:
                continue
            candidates = s.tables
            optional_table = expected.title in {"并发与临界资源", "过程日志", "状态迁移"}
            if not candidates and not optional_table:
                findings.append(Finding(s.line, "F006", "缺少模板表格：" + " / ".join(table.headers)))
            elif candidates and not any(t.headers == table.headers for t in candidates):
                # A configuration table may split the single adoption-time column.
                variant = [c for h in table.headers for c in
                           (["配置事务完成时机", "模块应用时机"] if h == "生效时机" else [h])]
                if not any(t.headers == variant for t in candidates):
                    findings.append(Finding(candidates[0].line, "F006", "表头应为：" + " / ".join(table.headers)))
        if expected.title == "依赖关系":
            diagrams = [(line, code) for line, lang, code in s.codes if lang == "mermaid"]
            if not diagrams:
                findings.append(Finding(s.line, "F009", "该章节缺少 Mermaid 关系图。"))
            else:
                for line, code in diagrams:
                    findings += check_dependency_diagram(line, code)
        if expected.title == "状态迁移":
            if not any(lang == "mermaid" and "stateDiagram" in code for _, lang, code in s.codes):
                headers = ["当前状态", "事件/条件", "下一状态", "动作", "异常路径"]
                tables = [t for t in s.tables if t.headers == headers]
                rows = [cells for t in tables for _, cells in t.rows]
                valid_rows = bool(rows) and all(len(cells) == 5 and all(plain(c) for c in cells) for cells in rows)
                states = {plain(cells[i]) for cells in rows if len(cells) == 5 for i in (0, 2)}
                if not valid_rows or len(states) != 2:
                    findings.append(Finding(s.line, "F009", "两个业务状态可仅用完整迁移表；其他状态机须提供 stateDiagram，没有状态机时明确说明。"))
        if expected.title == "时序图":
            related = []
            for node in sections:
                ancestor = node
                while ancestor is not None:
                    if ancestor is s:
                        related.append(node)
                        break
                    ancestor = ancestor.parent
            if not any(lang == "mermaid" and "sequenceDiagram" in code for node in related for _, lang, code in node.codes):
                findings.append(Finding(s.line, "F009", "时序图没有 sequenceDiagram；简单模块是否可省略需复核。", "REVIEW"))

    roles = {"关键数据结构": "type", "函数接口详细定义": "function", "事件与回调详细定义": "event",
             "调试指令": "command", "过程日志": "log"}
    for expected in fixed:
        role = roles.get(expected.title)
        s = actual.get(expected.number)
        if not role or s is None or absent(s):
            continue
        if not s.children:
            findings.append(Finding(s.line, "F005", "应按模板逐项建立四级小节，不能只给汇总表；确实没有时写“无”。"))
        for item in s.children:
            if item.level != 4:
                findings.append(Finding(item.line, "F005", "详细定义应使用四级标题。"))
            enum = any(re.search(r"\benum\b", code) for _, _, code in item.codes)
            model_index = 0 if role != "type" or enum else 1
            if len(expected.children) <= model_index:
                raise ValueError(f"模板 {expected.title} 缺少对应的逐项定义示例")
            model = expected.children[model_index]
            allowed = [name for _, name in model.labels]
            optional_labels = {"枚举值规则", "字段约束", "实例与访问", "载荷访问规则", "处理规则"}
            required = [name for name in allowed if name not in optional_labels]
            actual_labels = ["注册/传入方式" if name == "订阅示例" else name for _, name in item.labels]
            for name in required:
                if name not in actual_labels:
                    findings.append(Finding(item.line, "F005", f"缺少“{name}”说明块。"))
            sequence = [allowed.index(name) for name in actual_labels if name in allowed]
            if sequence != sorted(set(sequence)):
                findings.append(Finding(item.line, "F005", "说明块重复或顺序与模板不符。"))
            for n, name in item.labels:
                normalized = "注册/传入方式" if name == "订阅示例" else name
                if normalized not in allowed and not (role == "function" and name == "处理规则"):
                    findings.append(Finding(n, "F005", f"模板未定义“{name}”说明块；归入已有块或复核必要性。", "REVIEW"))
            # Tables inside each block use the active template, with documented variants.
            for table in item.tables:
                models = [t.headers for t in model.tables if t.label == table.label]
                if role == "type" and table.label == "枚举值定义":
                    models += [[h for h in columns if h != "取值限制"] for columns in models[:]]
                if role == "type" and table.label == "字段定义":
                    models += [["字段", "类型", "范围/枚举", "单位", "默认值", "来源", "含义"],
                               ["字段", "类型", "是否必须", "参数/返回值范围与定义", "含义"]]
                if role == "function" and table.label == "异常处理":
                    models = [["异常场景", "判定条件", "本模块处理", "对外结果/后续状态"]]
                if table.label == "处理规则":
                    models = [["条件", "处理结果"]]
                if models and table.headers not in models:
                    findings.append(Finding(table.line, "F006", "表头应为：" + " 或 ".join(" / ".join(m) for m in models)))
            required_tables = {"type": ["枚举值定义" if enum else "字段定义"],
                               "function": ["返回值定义", "调用约束"],
                               "event": ["发布与接收约束"], "command": [], "log": []}[role]
            for name in required_tables:
                if not any(t.label == name for t in item.tables):
                    findings.append(Finding(item.line, "F006", f"“{name}”须使用模板表格。"))
            if role in {"command", "log"}:
                for expected_table in model.tables:
                    value = block_text(item, expected_table.label)
                    if expected_table.label in {"参数定义", "参数解析"} and re.fullmatch(r"无(?:参数)?[。.]?", value):
                        continue
                    if not any(t.headers == expected_table.headers for t in item.tables):
                        findings.append(Finding(item.line, "F006", "缺少逐项表格：" + " / ".join(expected_table.headers)))
            for name in {"类型定义", "函数原型", "发送格式", "发送示例", "成功响应", "失败响应", "日志格式", "实际输出示例"} & set(required):
                begin = next((n for n, label in item.labels if label == name), 0)
                end = min([n for n, _ in item.labels if n > begin] + [10**9])
                if begin and not any(begin < n < end for n, _, _ in item.codes):
                    findings.append(Finding(begin, "F007", f"“{name}”须按模板使用代码围栏。"))
            if role in {"function", "command"}:
                params = block_text(item, "参数定义")
                if params and not re.fullmatch(r"无(?:输入)?参数[。.]?|无[。.]?", params) and not any(t.label == "参数定义" for t in item.tables):
                    findings.append(Finding(item.line, "F006", "存在参数时须使用参数定义表。"))
            if role == "function" and not any(t.label == "异常处理" for t in item.tables):
                findings.append(Finding(item.line, "F006", "异常处理须使用四列表格；无异常分支也应明确列出。"))
            if role == "event":
                labels = dict((name, n) for n, name in item.labels)
                if "载荷访问规则" not in labels and not re.search(r"载荷定义\*\*[:：]\s*(?:无|长度为 0|零载荷)", body(item)):
                    findings.append(Finding(item.line, "F007", "有载荷事件应说明载荷访问规则；引用或特殊零载荷表达需复核。", "REVIEW"))
            if role in {"type", "function", "event", "command", "log"}:
                preceding = text.splitlines()[:item.line - 1]
                previous_text = next((line.strip() for line in reversed(preceding) if line.strip()), "")
                if previous_text != "---":
                    findings.append(Finding(item.line, "F005", "详细定义前缺少分隔线。"))
    for s in sections:
        for n, line in [(s.line, s.title), *s.text]:
            found = [token for token in placeholders if token in line]
            if found:
                findings.append(Finding(n, "F012", "残留模板占位符：" + "、".join(sorted(found))))
        for line, language, code in s.codes:
            if language == "mermaid":
                findings += check_diagram(line, code)
        for n, name in s.labels:
            own = next((line for number, line in s.text if number == n), "")
            has_inline = bool(re.sub(r"^\s*\*\*[^*]+\*\*[:：]?\s*", "", own))
            next_block = min([number for number, _ in s.labels if number > n] + [10**9])
            content = [line for number, line in s.text if n < number < next_block and line.strip() and line.strip() != "---"]
            has_code = any(n < number < next_block for number, _, _ in s.codes)
            if not has_inline and not content and not has_code:
                findings.append(Finding(n, "F007", f"“{name}”为空说明块。"))
    for issue in check_prose(text, line_limit, paragraph_limit):
        findings.append(Finding(issue.line, "F010", f"{issue.rule} 文字长度 {issue.length} 超过 {issue.limit}；先精简再分点。"))
    return sorted(findings, key=lambda f: (f.line, f.rule))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", nargs="+", type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--non-source", action="store_true")
    parser.add_argument("--line-limit", type=positive_int, default=LINE_LIMIT)
    parser.add_argument("--paragraph-limit", type=positive_int, default=PARAGRAPH_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    status = 0
    reports = []
    try:
        template = args.template.read_text(encoding="utf-8-sig")
        template_keys = list(parse_frontmatter(args.template))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(f"template: {exc}")
    for path in args.documents:
        try:
            text = path.read_text(encoding="utf-8-sig")
            findings = check_format(text, template, args.non_source, args.line_limit, args.paragraph_limit)
            try:
                data = parse_frontmatter(path)
                keys = [k for k in template_keys if not args.non_source or k not in SOURCE_FIELDS]
                if list(data) != keys:
                    findings.append(Finding(1, "F011", "Frontmatter 字段集合或顺序与模板不符。"))
                findings += [Finding(1, "F011", error) for error in validate_values(data, not args.non_source)]
            except ValueError as exc:
                findings.append(Finding(1, "F011", f"YAML/Frontmatter: {exc}"))
            reports.append({"file": str(path), "findings": [asdict(f) for f in findings]})
            if findings:
                status = max(status, 1)
        except (OSError, UnicodeError, ValueError) as exc:
            reports.append({"file": str(path), "error": str(exc)})
            status = 2
    coverage = "全文：固定章节、逐项定义块、表格、待确认项、围栏、依赖图分区、时序图基础格式、图表意图标签 F013、文字长度、Frontmatter、模板占位符；语义和 Mermaid 实际渲染另行复核。"
    if args.json:
        print(json.dumps({"template": str(args.template), "coverage": coverage, "results": reports,
                          "exit_code": status}, ensure_ascii=False, indent=2))
    else:
        print(coverage)
        for report in reports:
            if "error" in report:
                print(f"{report['file']}: input error: {report['error']}")
                continue
            print(f"{report['file']}: {'PASS' if not report['findings'] else 'NEEDS_CORRECTION'}")
            for item in report["findings"]:
                print(f"{report['file']}:{item['line']}: {item['severity']} {item['rule']}: {item['message']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
