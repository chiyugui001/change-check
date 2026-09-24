#!/usr/bin/env python3
"""Read-only length checks for detailed-design Markdown prose (stdlib only)."""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path


LINE_LIMIT = 120
PARAGRAPH_LIMIT = 200
LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


@dataclass(frozen=True)
class Issue:
    line: int
    end_line: int
    rule: str
    length: int
    limit: int


def visible_text(text: str) -> str:
    """Count labels and identifiers, excluding whitespace, markup and URLs."""
    code: list[str] = []

    def hold_code(match: re.Match) -> str:
        code.append(match.group(2))
        return f"\x00{len(code) - 1}\x00"

    text = re.sub(r"(`+)(.+?)\1", hold_code, text)
    text = re.sub(r"!\[\[[^\]]+\]\]", "", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m[1].split("|")[-1], text)
    target = r"(?:[^()\n]|\([^()\n]*\))*"
    text = re.sub(r"!\[[^\]]*\]\(" + target + r"\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\(" + target + r"\)", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\[[^\]]*\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<https?://[^>]+>|https?://[^\s<>，。；]+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\w)([*_])(.+?)\1(?!\w)", r"\2", text)
    text = re.sub(r"\\([\\`*{}\[\]()#+.!_>|~-])", r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: code[int(m[1])], text)
    return re.sub(r"\s+", "", html.unescape(text))


def check_prose(text: str, line_limit: int = LINE_LIMIT,
                paragraph_limit: int = PARAGRAPH_LIMIT) -> list[Issue]:
    # Keep source line numbers while removing non-rendered comments/frontmatter.
    text = text.lstrip("\ufeff")
    text = re.sub(r"\A---[^\S\n]*\n.*?\n(?:---|\.\.\.)[^\S\n]*(?:\n|$)",
                  lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
    text = re.sub(r"<!--.*?(?:-->|\Z)",
                  lambda m: "\n" * m[0].count("\n"), text, flags=re.S)
    lines = text.splitlines()
    issues: list[Issue] = []
    block: list[tuple[int, int]] = []
    list_item = False
    fence = ""
    table = False

    def flush() -> None:
        if block:
            limit = line_limit if list_item else paragraph_limit
            length = sum(size for _, size in block)
            # A single long line already has a precise LINE diagnostic.
            if length > limit and (len(block) > 1 or length <= line_limit):
                issues.append(Issue(block[0][0], block[-1][0],
                                    "ITEM" if list_item else "PARAGRAPH", length, limit))
            block.clear()

    for index, raw in enumerate(lines):
        line_no = index + 1
        line = re.sub(r"^\s*(?:>\s*)+", "", raw)
        stripped = line.strip()
        marker = FENCE_RE.match(line)
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = ""
            continue
        if marker:
            flush()
            fence = marker[1]
            table = False
            continue
        if table:
            if "|" in line and stripped:
                continue
            table = False
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        next_line = re.sub(r"^\s*(?:>\s*)+", "", next_line)
        if "|" in line and TABLE_SEPARATOR.fullmatch(next_line):
            flush()
            table = True
            continue
        if (not stripped or re.match(r"^#{1,6}\s", stripped)
                or re.fullmatch(r"(?:[-*_]\s*){3,}|=+|-+", stripped)
                or re.match(r"^\[[^\]]+\]:\s*\S", stripped)):
            flush()
            list_item = False
            continue
        if re.fullmatch(r"\s*<[^>]+>\s*", line):
            flush()
            continue
        # Four-space code is excluded; indented list continuation is prose.
        if (line.startswith("    ") or line.startswith("\t")) and not list_item:
            flush()
            continue
        item = LIST_RE.match(line)
        if item:
            flush()
            list_item = True
            line = line[item.end():]
        size = len(visible_text(line))
        if not size:
            flush()
            continue
        if size > line_limit:
            issues.append(Issue(line_no, line_no, "LINE", size, line_limit))
        block.append((line_no, size))
    flush()
    return sorted(issues, key=lambda issue: (issue.line, issue.rule))


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", type=Path, nargs="+")
    parser.add_argument("--line-limit", type=positive_int, default=LINE_LIMIT)
    parser.add_argument("--paragraph-limit", type=positive_int, default=PARAGRAPH_LIMIT)
    args = parser.parse_args()
    status = 0
    for path in args.documents:
        try:
            issues = check_prose(path.read_text(encoding="utf-8-sig"),
                                 args.line_limit, args.paragraph_limit)
        except (OSError, UnicodeError) as exc:
            print(f"{path}: ERROR: {exc}", file=sys.stderr)
            status = 2
            continue
        for issue in issues:
            print(f"{path}:{issue.line}-{issue.end_line}: {issue.rule} "
                  f"{issue.length} > {issue.limit}: 精简重复内容，再按要点用 '- ' 逐项分行。")
        if issues:
            status = max(status, 1)
        else:
            print(f"{path}: PASS (文字长度)")
    print("仅校验文字长度；重复语义、要点拆分和关键约束保留须人工复核。")
    return status


if __name__ == "__main__":
    sys.exit(main())
