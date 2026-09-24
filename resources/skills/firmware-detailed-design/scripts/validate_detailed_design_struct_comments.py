#!/usr/bin/env python3
"""Read-only checks for C structure comments and field-table coverage.

Exit 0: no findings; 1: ERROR/REVIEW; 2: input error. Reworded table text
requires semantic review; a literal match is not proof of correctness.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from validate_detailed_design_format import parse


TOKEN = re.compile(r'(?P<comment>/\*[\s\S]*?\*/|//[^\n]*)|'
                   r'(?P<string>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|'
                   r'(?P<word>[A-Za-z_]\w*)|(?P<symbol>[^\s])')
STRUCT = re.compile(r'\b(?:typedef\s+)?struct(?:\s+([A-Za-z_]\w*))?\s*\{')
COMMENT = re.compile(r'/\*[\s\S]*?\*/|//[^\n]*')


@dataclass
class Finding:
    line: int
    rule: str
    message: str
    severity: str = "ERROR"


@dataclass
class Member:
    name: str
    line: int
    comment: str


@dataclass
class Structure:
    name: str
    line: int
    comment: str
    members: list[Member]


def plain(value: str) -> str:
    value = re.sub(r'<br\s*/?>', ' ', value, flags=re.I)
    value = re.sub(r'\[\[([^]|]+)\|([^]]+)\]\]', r'\2', value)
    value = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', value)
    return re.sub(r'\s+|`|\*\*', '', html.unescape(value)).strip('。;；')


def comment_text(value: str) -> str:
    value = re.sub(r'/\*+[!<]?|\*/|//[/!<]*', '', value)
    value = re.sub(r'^\s*\*\s?', '', value, flags=re.M)
    return re.sub(r'[@\\]brief\s*', '', value).strip()


def meaningful(value: str) -> bool:
    return plain(value).lower() not in {
        '', 'todo', 'tbd', '待补充', '待填写', '同上', '见下表', '字段说明', '...'
    }


def preceding_comment(code: str, offset: int) -> str:
    """Accept adjacent comments, excluding the previous declaration's tail."""
    prefix = code[:offset]
    matches = list(COMMENT.finditer(prefix))
    selected: list[str] = []
    for match in reversed(matches):
        if prefix[match.end():].strip():
            break
        line_start = prefix.rfind('\n', 0, match.start()) + 1
        if prefix[line_start:match.start()].strip():
            break
        selected.insert(0, comment_text(match[0]))
        prefix = prefix[:match.start()]
    return '\n'.join(selected)


def member_name(declaration: str) -> str | None:
    """Recognize ordinary fields, arrays, bit fields and function pointers."""
    if any(symbol in declaration for symbol in '{}#'):
        return None
    depth = 0
    for symbol in declaration:
        depth += (symbol in '([') - (symbol in ')]')
        if symbol == ',' and depth == 0:
            return None
    pointer = re.search(r'\(\s*\*\s*(\w+)\s*(?:\[[^]]*\]\s*)*\)', declaration)
    if pointer:
        return pointer[1]
    declaration = re.sub(r'\[[^]]*\]', '', declaration).split(':', 1)[0]
    if ',' in declaration or '(' in declaration:
        return None
    names = re.findall(r'[A-Za-z_]\w*', declaration)
    return names[-1] if len(names) >= 2 else None


def read_members(code: str, start: int, end: int, base_line: int) -> tuple[list[Member], list[Finding]]:
    tokens = list(TOKEN.finditer(code, start, end))
    members: list[Member] = []
    issues: list[Finding] = []
    pending: list[re.Match] = []
    depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        pending.append(token)
        index += 1
        if token.lastgroup == 'symbol':
            depth += (token[0] == '{') - (token[0] == '}')
        if token[0] != ';' or depth:
            continue
        # A same-line trailing comment belongs to this field, never the next.
        while index < len(tokens) and tokens[index].lastgroup == 'comment':
            after = tokens[index]
            if '\n' in code[token.end():after.start()]:
                break
            pending.append(after)
            token = after
            index += 1
        significant = [t for t in pending if t.lastgroup != 'comment']
        declaration = ' '.join(t[0] for t in significant)
        line = base_line + code.count('\n', 0, significant[0].start())
        name = member_name(declaration)
        if name is None:
            issues.append(Finding(line, 'C004', '复杂成员声明需人工检查注释：' + declaration, 'REVIEW'))
        else:
            comments = '\n'.join(comment_text(t[0]) for t in pending if t.lastgroup == 'comment')
            members.append(Member(name, line, comments))
        pending = []
    if any(t.lastgroup != 'comment' for t in pending):
        line = base_line + code.count('\n', 0, pending[0].start())
        issues.append(Finding(line, 'C004', '结构体内有未解析声明，需人工检查。', 'REVIEW'))
    return members, issues


def read_structures(code: str, base_line: int) -> tuple[list[Structure], list[Finding]]:
    # Mask strings/comments without shifting offsets or counting fake declarations.
    masked = list(code)
    for token in TOKEN.finditer(code):
        if token.lastgroup in {'comment', 'string'}:
            masked[token.start():token.end()] = ['\n' if ch == '\n' else ' ' for ch in token[0]]
    clean = ''.join(masked)
    structures: list[Structure] = []
    issues: list[Finding] = []
    consumed = 0
    for match in STRUCT.finditer(clean):
        if match.start() < consumed:
            continue  # Nested aggregates are reported by read_members as REVIEW.
        depth, end = 1, match.end()
        while end < len(clean) and depth:
            depth += (clean[end] == '{') - (clean[end] == '}')
            end += 1
        line = base_line + code.count('\n', 0, match.start())
        if depth:
            issues.append(Finding(line, 'C004', '结构体定义未闭合。', 'REVIEW'))
            continue
        consumed = end
        alias = re.match(r'\s*([A-Za-z_]\w*)\s*;', clean[end:])
        name = alias[1] if match[0].startswith('typedef') and alias else match[1]
        if not name:
            issues.append(Finding(line, 'C004', '无法确定结构体名称，需人工检查。', 'REVIEW'))
            continue
        members, member_issues = read_members(code, match.end(), end - 1, base_line)
        issues.extend(member_issues)
        structures.append(Structure(name, line, preceding_comment(code, match.start()), members))
    return structures, issues


def compare_tables(structure: Structure, tables: list) -> list[Finding]:
    issues: list[Finding] = []
    members = {member.name: member for member in structure.members}
    for table in tables:
        field_column = next((i for i, h in enumerate(table.headers) if h in {'字段', '成员'}), None)
        columns = [(i, h) for i, h in enumerate(table.headers) if h in {'含义', '具体内容'}]
        if field_column is None or not columns:
            continue
        for line, row in table.rows:
            if len(row) != len(table.headers):
                issues.append(Finding(line, 'C004', '字段表列数不一致，无法核对注释。', 'REVIEW'))
                continue
            name = re.sub(r'\[[^]]*\]', '', plain(row[field_column]))
            member = members.get(name)
            if member is None:
                issues.append(Finding(line, 'C004', f'{structure.name} 无法映射字段 {name}，需人工核对。', 'REVIEW'))
                continue
            if not meaningful(member.comment):
                continue  # Already reported as C002 at the declaration.
            for index, heading in columns:
                expected = plain(row[index])
                if expected in {'', '—', '-', '/'}:
                    continue
                if expected not in plain(member.comment):
                    issues.append(Finding(member.line, 'C003',
                        f'{structure.name}.{name} 注释未逐字覆盖表格第 {line} 行“{heading}”：{row[index]}；核对遗漏或等义改写。',
                        'REVIEW'))
    return issues


def check_comments(text: str) -> tuple[list[Finding], dict[str, int]]:
    sections, parse_issues = parse(text)
    issues = [Finding(i.line, 'C004', i.message, 'REVIEW') for i in parse_issues if i.rule == 'F003']
    counts = {'structures': 0, 'members': 0}
    for section in sections:
        local: list[Structure] = []
        for line, language, code in section.codes:
            if language.lower() not in {'c', 'h', 'cpp', 'c++'}:
                continue
            structures, code_issues = read_structures(code, line + 1)
            local.extend(structures)
            issues.extend(code_issues)
        for structure in local:
            counts['structures'] += 1
            counts['members'] += len(structure.members)
            if not meaningful(structure.comment):
                issues.append(Finding(structure.line, 'C001', f'{structure.name} 缺少结构体用途注释。'))
            for member in structure.members:
                if not meaningful(member.comment):
                    issues.append(Finding(member.line, 'C002', f'{structure.name}.{member.name} 缺少有效字段注释。'))
            owners = [s for s in sections if structure.name in re.findall(r'[A-Za-z_]\w*', s.title)]
            if len(owners) == 1:
                tables = owners[0].tables
            elif not owners and len(local) == 1:
                tables = section.tables
            else:
                tables = []
                issues.append(Finding(structure.line, 'C004', f'{structure.name} 字段表归属不唯一或缺少独立类型小节，需人工核对。', 'REVIEW'))
            issues.extend(compare_tables(structure, tables))
    return sorted(issues, key=lambda i: (i.line, i.rule)), counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('documents', nargs='+', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    results = []
    exit_code = 0
    for path in args.documents:
        try:
            findings, counts = check_comments(path.read_text(encoding='utf-8-sig'))
            results.append({'file': str(path), **counts, 'findings': [asdict(i) for i in findings]})
            exit_code = max(exit_code, int(bool(findings)))
        except (OSError, UnicodeError) as error:
            results.append({'file': str(path), 'error': str(error)})
            exit_code = 2
    if args.json:
        print(json.dumps({'results': results, 'exit_code': exit_code}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if 'error' in result:
                print(f"{result['file']}: INPUT ERROR: {result['error']}")
                continue
            print(f"{result['file']}: {result['structures']} structures, {result['members']} fields")
            for issue in result['findings']:
                print(f"  {issue['line']}: {issue['severity']} {issue['rule']}: {issue['message']}")
            if not result['findings']:
                print('  PASS (mechanical checks only)')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
