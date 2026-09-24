#!/usr/bin/env python3
"""Read-only checks for public interfaces and downstream coupling in Markdown.

ERROR finds mechanically identifiable interface-list defects. REVIEW identifies
wording that needs an engineering decision; it does not prove a boundary breach.
This is a targeted Markdown scanner, not a complete semantic or Mermaid parser.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    severity: str
    message: str


def strip_hidden(text: str) -> str:
    text = text.lstrip("\ufeff")
    text = re.sub(r"\A---[^\S\n]*\n.*?\n(?:---|\.\.\.)[^\S\n]*(?:\n|$)",
                  lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
    return re.sub(r"<!--.*?(?:-->|\Z)",
                  lambda m: "\n" * m[0].count("\n"), text, flags=re.S)


def table_cells(line: str) -> list[str]:
    # Protect pipes in inline code and escaped Obsidian links.
    spans: list[str] = []

    def protect(match: re.Match) -> str:
        spans.append(match[0])
        return f"\x00{len(spans) - 1}\x00"

    line = re.sub(r"(`+).*?\1", protect, line)
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m[1])], c).strip()
            for c in cells]


def plain(text: str) -> str:
    return text.replace("`", "").replace("**", "").strip()


def scan(text: str, internal_symbols: tuple[str, ...] = (),
         consumer_tokens: tuple[str, ...] = ()) -> list[Finding]:
    lines = strip_hidden(text.replace("\r\n", "\n")).splitlines()
    private = set(internal_symbols)
    headings: list[tuple[int, str]] = []
    fence = ""
    language = ""
    rows: list[tuple[int, str, bool]] = []

    for number, line in enumerate(lines, 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line.strip()):
                fence = ""
                language = ""
            elif language in {"c", "cpp", "c++"}:
                # Static functions are private implementation, even outside an internal chapter.
                match = re.match(r"\s*static\s+[^;{}=]*?\b(\w+)\s*\(", line)
                if match:
                    private.add(match[1])
                if any("内部" in title for _, title in headings):
                    match = re.match(r"\s*([A-Z][A-Z0-9_]+)\s*(?:=|,|$)", line)
                    if match:
                        private.add(match[1])
            rows.append((number, "", False))
            continue
        if marker:
            fence, language = marker[1], marker[2].strip().lower()
            rows.append((number, "", False))
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2]))
        overview = any("概览" in title for _, title in headings)
        rows.append((number, line, overview))

    findings: list[Finding] = []
    header: list[str] = []
    for index, (number, line, overview) in enumerate(rows):
        if not overview or not line.strip().startswith("|"):
            header = []
        if overview and line.strip().startswith("|"):
            cells = table_cells(line)
            cleaned = [plain(c) for c in cells]
            if "用途" in cleaned and ("事件" in cleaned or "函数" in cleaned):
                # Only treat real Markdown tables as contracts.
                next_line = rows[index + 1][1] if index + 1 < len(rows) else ""
                separators = table_cells(next_line) if "|" in next_line else []
                header = cleaned if (len(separators) == len(cleaned) and all(
                    re.fullmatch(r":?-{3,}:?", c.strip()) for c in separators)) else []
                continue
            if not header or all(re.fullmatch(r":?-{3,}:?", c) for c in cleaned):
                continue
            if len(cells) != len(header):
                findings.append(Finding(number, "B001", "ERROR", "接口概览表列数不一致，无法可靠检查边界。"))
                continue
            row = dict(zip(header, cells))
            name = row.get("事件", row.get("函数", ""))
            if "事件" in row:
                missing = [key for key in ("事件", "方向", "发布方", "接收方", "用途")
                           if not plain(row.get(key, ""))]
                if missing:
                    findings.append(Finding(number, "B009", "ERROR", "事件概览缺少边界信息：" + "、".join(missing)))
            if re.search(r"载荷|payload|参数\s*(?:为|[:：=])|\bdata(?:_len)?\s*=", name, re.I):
                findings.append(Finding(number, "B002", "ERROR", "接口名称列混入载荷或参数说明，应移至详细定义。"))
            symbols = set(re.findall(r"\b[A-Za-z_]\w*\b", name))
            if symbols & private:
                findings.append(Finding(number, "B003", "ERROR", "概览将已标为内部的符号列作对外接口：" + ", ".join(sorted(symbols & private))))
            if plain(row.get("方向", "")).startswith("输出"):
                receiver = plain(row.get("接收方", ""))
                if receiver and not re.search(r"订阅|外部模块|下游模块|下游接收方|消息接收方|消费者|<.*>", receiver):
                    findings.append(Finding(number, "B004", "REVIEW", "输出绑定具体接收方；确认是否属于必要的稳定依赖，否则改为按需订阅。"))
                if re.search(r"(?:^|_)(?:REPORT|UPLOAD|MQTT|HTTP)(?:_|$)", plain(name), re.I):
                    findings.append(Finding(number, "B005", "REVIEW", "输出事件可能使用了消费者或通信语义，核对是否应表达本模块的业务事实。"))
                if re.search(r"(?:载荷为|payload\s*=|\bdata_len\s*=)", row.get("用途", ""), re.I):
                    findings.append(Finding(number, "B006", "REVIEW", "概览用途列包含载荷细节，检查是否应移至事件定义。"))

        # Look for prescribed downstream business, excluding explicit boundary statements.
        text_line = plain(line)
        negative = re.search(r"不得|不应|无需|不负责|不要求|不规定|不限定|不固定|不等待|自行|自主|例如|示例", text_line)
        if not negative and re.search(
            r"(?:订阅方|接收方|消费者|下游模块).{0,15}(?:必须|应当|负责|需要).{0,25}(?:组包|构造报文|编码报文|重发报文|上传|上报|同步给)", text_line):
            findings.append(Finding(number, "B007", "REVIEW", "可能规定了消费者的业务步骤；区分必要接收约束与下游实现。"))
        if consumer_tokens and not negative and any(token in line for token in consumer_tokens):
            findings.append(Finding(number, "B008", "REVIEW", "出现本次指定的消费者标识，核对是否越过模块职责边界。"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", type=Path, nargs="+")
    parser.add_argument("--internal-symbol", action="append", default=[], help="Known private symbol; repeat as needed")
    parser.add_argument("--consumer-token", action="append", default=[], help="Consumer identifier to review in prose/tables")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable findings")
    args = parser.parse_args()
    results = []
    status = 0
    for path in args.documents:
        try:
            findings = scan(path.read_text(encoding="utf-8-sig"), tuple(args.internal_symbol), tuple(args.consumer_token))
        except (OSError, UnicodeError) as exc:
            results.append({"file": str(path), "error": str(exc)})
            status = 2
            continue
        results.append({"file": str(path), "findings": [asdict(item) for item in findings]})
        if findings:
            status = max(status, 1)
    if args.json:
        print(json.dumps({"results": results, "exit_code": status}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if "error" in result:
                print(f"{result['file']}: ERROR: {result['error']}")
                continue
            for item in result["findings"]:
                print(f"{result['file']}:{item['line']}: {item['severity']} {item['rule']}: {item['message']}")
            if not result["findings"]:
                print(f"{result['file']}: PASS (自动检查未发现边界候选项)")
        print("REVIEW 需人工判定；未命中不代表上下游职责已完整或语义审查通过。")
    return status


if __name__ == "__main__":
    sys.exit(main())
