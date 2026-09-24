#!/usr/bin/env python3
"""Read-only Mermaid label checks; semantic review and rendering remain separate.

Exit 0: supported label formats passed; 1: ERROR/REVIEW; 2: input error.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Finding:
    line: int
    rule: str
    message: str
    severity: str = "ERROR"


@dataclass
class Label:
    line: int
    text: str


HAN = re.compile(r"[\u3400-\u9fff]")
BREAK = re.compile(r"<br\s*/?>|\n", re.I)
CODE = re.compile(
    r"\b[A-Za-z_]\w*\s*\(|\b[A-Za-z_]\w*_[A-Za-z0-9_]+\b|"
    r"\b[A-Za-z_]\w*\.(?:c|h|cpp|hpp|py)\b|"
    r"\b[A-Za-z_][\w.-]*/(?=[\w/.-]*[a-z])[\w/.*-]+"
)
IDENTIFIER = re.compile(r"[A-Za-z_][\w./*-]{1,}")
NODE = re.compile(r"\b([A-Za-z_]\w*)\s*(\[\[|\[\(|\(\[|\(\(|\{\{|\[|\(|\{|>)")
EDGE = re.compile(r"\|(?:\"([^\"]*)\"|([^|\n]*))\|")
FLOW_ARROW = r"(?:--+>|==+>|-\.+->|---+|===+|~~~)"


def clean_label(value: str) -> str:
    value = re.sub(r"#(x[0-9a-fA-F]+|[0-9]+);", r"&#\1;", value)
    value = html.unescape(value.strip().strip('"`'))
    return re.sub(r"<(?!br\b)[^>]*>", "", value, flags=re.I).strip()


def check_label(label: Label) -> list[Finding]:
    text = clean_label(label.text)
    parts = [part.strip(' "`') for part in BREAK.split(text)]
    # Mixed acronyms such as "UART/NFC 交互" are business terms, not source paths.
    if not CODE.search(text) and not any(IDENTIFIER.fullmatch(p) for p in parts):
        return []
    if not parts[0] or not HAN.search(parts[0]):
        reason = "代码标签缺少首行中文意图；先说明动作、职责或状态含义，再换行显示代码名称。"
    elif len(parts) == 1:
        reason = "中文意图与代码名称未换行；使用“中文意图<br/>代码名称”。"
    elif CODE.search(parts[0]):
        reason = "代码名称出现在首行意图中；将代码名称移到换行后的独立行。"
    elif not any(CODE.search(p) or IDENTIFIER.fullmatch(p) for p in parts[1:]):
        reason = "换行后的代码名称缺失。"
    else:
        return []
    return [Finding(label.line, "F013", reason + " 标签：" + text)]


def filtered_code(code: str) -> str:
    """Ignore comments and rendering directives without shifting source lines."""
    lines = []
    directive = False
    quoted = False
    for raw in code.splitlines():
        text = raw.strip()
        if text.startswith("%%{"):
            directive = True
        if directive or text.startswith("%%"):
            lines.append("")
            if "}%%" in text:
                directive = False
        else:
            for index, char in enumerate(raw):
                if char == '"' and (index == 0 or raw[index - 1] != "\\"):
                    quoted = not quoted
                if not quoted and raw[index:index + 2] == "%%":
                    raw = raw[:index]
                    break
            lines.append(raw)
    return "\n".join(lines)


def flow_labels(base: int, code: str) -> tuple[list[Label], list[Finding]]:
    labels: list[Label] = []
    findings: list[Finding] = []
    declarations: set[str] = set()
    references: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    for match in EDGE.finditer(code):
        labels.append(Label(base + code.count("\n", 0, match.start()) + 1,
                            next(v for v in match.groups() if v is not None)))
        spans.append(match.span())
    # Mask edge labels so a function in an edge cannot be parsed as a node.
    masked = list(code)
    for start, end in spans:
        masked[start:end] = ["\n" if c == "\n" else " " for c in code[start:end]]
    node_code = "".join(masked)
    consumed = 0
    for match in NODE.finditer(node_code):
        if match.start() < consumed:
            continue
        prefix = node_code[node_code.rfind("\n", 0, match.start()) + 1:match.start()]
        if re.match(r"\s*(?:classDef|style|linkStyle|click|class)\b", prefix):
            continue
        name, opening = match.groups()
        closers = {"[": "]", "(": ")", "{": "}", ">": "]"}
        stack = [closers[c] for c in opening]
        quote = False
        index = match.end()
        label_end = None
        while index < len(node_code) and stack:
            char = node_code[index]
            if char == '"' and (index == 0 or node_code[index - 1] != "\\"):
                quote = not quote
            elif not quote:
                if char in "[({":
                    stack.append(closers[char])
                elif char == stack[-1]:
                    if len(stack) == len(opening) and label_end is None:
                        label_end = index
                    stack.pop()
            index += 1
        line = base + code.count("\n", 0, match.start()) + 1
        if stack:
            findings.append(Finding(line, "F013", "节点标签无法完整解析，须人工复核并实际渲染。", "REVIEW"))
            continue
        declarations.add(name)
        value = node_code[match.end():label_end]
        if value.startswith(("/", "\\")) and value.endswith(("/", "\\")):
            value = value[1:-1]
        labels.append(Label(line, value))
        # Preserve the node id when removing its displayed shape.
        spans.append((match.start() + len(name), index))
        consumed = index
    remainder = list(code)
    for start, end in spans:
        remainder[start:end] = ["\n" if c == "\n" else " " for c in code[start:end]]
    for offset, raw in enumerate("".join(remainder).splitlines(), 1):
        if re.match(r"\s*(?:classDef|style|linkStyle|click|class)\b", raw):
            continue
        if "@{" in raw:
            findings.append(Finding(base + offset, "F013", "新版节点属性语法须人工复核标签；当前解析器不覆盖。", "REVIEW"))
        standalone = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*;?\s*", raw)
        if standalone and standalone[1] not in {"end", "flowchart", "graph"}:
            references.setdefault(standalone[1], base + offset)
        # Normalize inline edge labels before resolving implicit node displays.
        for pattern in (r"--\s+(.+?)\s+-->", r"-\.\s*(.+?)\s*\.->", r"==\s+(.+?)\s+==>"):
            for match in re.finditer(pattern, raw):
                labels.append(Label(base + offset, match[1].strip('"')))
            raw = re.sub(pattern, " --> ", raw)
        for pattern in (rf"\b([A-Za-z_]\w*)\s*{FLOW_ARROW}",
                        rf"{FLOW_ARROW}\s*([A-Za-z_]\w*)\b"):
            for match in re.finditer(pattern, raw):
                references.setdefault(match[1], base + offset)
    labels += [Label(n, name) for name, n in references.items() if name not in declarations]
    return labels, findings


def sequence_labels(base: int, code: str) -> list[Label]:
    labels: list[Label] = []
    declared: set[str] = set()
    references: dict[str, int] = {}
    for offset, raw in enumerate(code.splitlines(), 1):
        line = base + offset
        declaration = re.match(r"\s*(?:create\s+)?(?:participant|actor)\s+(\w+)(?:\s+as\s+(.+))?\s*$", raw)
        if declaration:
            declared.add(declaration[1])
            labels.append(Label(line, declaration[2] or declaration[1]))
            continue
        message = re.match(r"\s*(\w+)\s*[-<>=x)o]+[+-]?\s*(\w+)\s*:\s*(.*)", raw)
        if message:
            references.setdefault(message[1], line)
            references.setdefault(message[2], line)
            labels.append(Label(line, message[3]))
        elif re.match(r"\s*note\s", raw, re.I) and ":" in raw:
            labels.append(Label(line, raw.split(":", 1)[1]))
        else:
            branch = re.match(r"\s*(?:alt|else|opt|loop|break|critical|option|par|and)\s+(.+)", raw)
            if branch:
                labels.append(Label(line, branch[1]))
    labels += [Label(n, name) for name, n in references.items() if name not in declared]
    return labels


def state_labels(base: int, code: str) -> list[Label]:
    labels: list[Label] = []
    declared: set[str] = set()
    pseudo: set[str] = set()
    references: dict[str, int] = {}
    for offset, raw in enumerate(code.splitlines(), 1):
        line = base + offset
        alias = re.match(r'\s*state\s+"([^"]+)"\s+as\s+(\w+)', raw)
        description = re.match(r"\s*(\w+)\s*:\s*(.+)", raw)
        special = re.match(r"\s*state\s+(\w+)\s+<<", raw)
        if special:
            pseudo.add(special[1])
        elif alias or description:
            name, value = (alias[2], alias[1]) if alias else (description[1], description[2])
            declared.add(name)
            labels.append(Label(line, value))
        else:
            transition = re.match(r"\s*(\w+|\[\*\])\s*-->\s*(\w+|\[\*\])(?:\s*:\s*(.*))?", raw)
            if transition:
                for name in transition.group(1, 2):
                    if name != "[*]":
                        references.setdefault(name, line)
                if transition[3]:
                    labels.append(Label(line, transition[3]))
            else:
                state = re.match(r"\s*state\s+(\w+)", raw)
                if state:
                    references.setdefault(state[1], line)
                elif re.fullmatch(r"\s*[A-Za-z_]\w*\s*", raw) and not raw.strip().startswith("stateDiagram"):
                    references.setdefault(raw.strip(), line)
    labels += [Label(n, name) for name, n in references.items() if name not in declared | pseudo]
    return labels


def check_diagram_labels(line: int, code: str) -> list[Finding]:
    code = filtered_code(code)
    header = code.lstrip().splitlines()[0] if code.strip() else ""
    findings: list[Finding] = []
    if re.match(r"(?:flowchart|graph)\b", header):
        labels, findings = flow_labels(line, code)
    elif header.startswith("sequenceDiagram"):
        labels = sequence_labels(line, code)
    elif header.startswith("stateDiagram"):
        labels = state_labels(line, code)
    else:
        return [Finding(line, "F013", "此图类型尚未自动检查意图标签，须逐图人工复核。", "REVIEW")]
    for label in labels:
        findings += check_label(label)
    return sorted(findings, key=lambda f: f.line)


def check_document(text: str) -> tuple[int, list[Finding]]:
    text = re.sub(r"<!--.*?(?:-->|$)", lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
    findings: list[Finding] = []
    fence = language = ""
    body: list[str] = []
    start = count = 0
    for n, raw in enumerate(text.splitlines(), 1):
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", raw.strip()):
                if language == "mermaid":
                    count += 1
                    findings += check_diagram_labels(start, "\n".join(body))
                fence = ""
            else:
                body.append(raw)
        else:
            marker = re.match(r"\s*(`{3,}|~{3,})(.*)$", raw)
            if marker:
                fence, language, start, body = marker[1], marker[2].strip(), n, []
    if fence and language == "mermaid":
        findings.append(Finding(start, "F013", "Mermaid 围栏未闭合，无法完成标签校验。"))
    return count, findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    reports = []
    status = 0
    for path in args.documents:
        try:
            text = path.read_text(encoding="utf-8-sig")
            if path.suffix.lower() == ".mmd":
                count, findings = 1, check_diagram_labels(0, text)
            else:
                count, findings = check_document(text)
            reports.append({"file": str(path), "diagrams": count,
                            "findings": [asdict(f) for f in findings]})
            status = max(status, int(bool(findings)))
        except (OSError, UnicodeError) as exc:
            reports.append({"file": str(path), "error": str(exc)})
            status = 2
    coverage = "流程图、时序图、状态图的常见代码标签格式；中文意图准确性及渲染另行复核，其他图类型报 REVIEW。"
    if args.json:
        print(json.dumps({"coverage": coverage, "results": reports, "exit_code": status}, ensure_ascii=False, indent=2))
    else:
        print(coverage)
        for report in reports:
            print(f"{report['file']}: " + (f"INPUT_ERROR {report['error']}" if "error" in report else f"{report['diagrams']} diagram(s)"))
            for finding in report.get("findings", []):
                print(f"{report['file']}:{finding['line']}: {finding['severity']} F013: {finding['message']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
