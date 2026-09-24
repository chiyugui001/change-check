"""Lexical C style checks; compiler/static analyzer remains a project command."""
import re
from pathlib import Path

TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
WORD = re.compile(r"[A-Za-z_]\w*|0[xX][0-9a-fA-F]+[uUlL]*|\d+(?:\.\d+)?[uUlLfF]*|[^\s]")


def masked(text):
    return TOKEN.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def check(name, text):
    findings = []
    def add(rule, message, offset=0, severity="ERROR"):
        findings.append({"file": name, "line": text.count("\n", 0, offset) + 1,
                         "rule": rule, "message": message, "severity": severity, "source": "c-style"})
    clean = masked(text)
    # Preprocessor branches remain visible, but directives/macros are not C statements.
    clean = re.sub(r"^[ \t]*#(?:[^\n\\]|\\[^\n]|\\\n)*", lambda m: re.sub(r"[^\n]", " ", m.group()), clean,
                   flags=re.M)
    tokens = [(m.group(), m.start(), m.end()) for m in WORD.finditer(clean)]
    pairs, stack = {}, []
    for i, (word, start, _) in enumerate(tokens):
        if word in ("(", "{", "["):
            stack.append(i)
        elif word in (")", "}", "]"):
            if stack and tokens[stack[-1]][0] == {")": "(", "}": "{", "]": "["}[word]:
                first = stack.pop()
                pairs[first] = i
                pairs[i] = first
            else:
                add("C099", "分支或宏使词法结构无法配对；必须由项目编译器核对", start, "REVIEW")
    for i in stack:
        add("C099", "未闭合结构或条件编译分支；必须由项目编译器核对", tokens[i][1], "REVIEW")
    offset = 0
    for line in text.splitlines(True):
        stripped = line.lstrip(" \t")
        if "\t" in line:
            add("C001", "使用 4 空格缩进，禁止 Tab", offset)
        if stripped.strip() and not stripped.startswith("#") and (len(line) - len(stripped)) % 4:
            add("C001", "缩进不是 4 空格的整数倍", offset)
        offset += len(line)
    functions = []
    depth = 0
    for i, (word, start, end) in enumerate(tokens):
        if word == "{" and depth == 0 and i and tokens[i-1][0] == ")" and i-1 in pairs:
            left = pairs[i-1]
            if left and re.fullmatch(r"[A-Za-z_]\w*", tokens[left-1][0]):
                fname = tokens[left-1][0]
                functions.append((i, pairs.get(i, i)))
                if not re.fullmatch(r"[a-z][a-z0-9_]*", fname):
                    add("C002", "函数名须使用小写下划线：" + fname, tokens[left-1][1])
                if "\n" not in text[tokens[i-1][2]:start] or text[text.rfind("\n", 0, start)+1:start].strip():
                    add("C003", "函数左大括号必须独占一行", start)
                close = pairs.get(i, i)
                # Count the declaration through the closing brace, including blank/comment lines.
                declaration = clean.rfind("\n", 0, tokens[left-1][1]) + 1
                if text.count("\n", declaration, tokens[close][2]) + 1 > 50:
                    add("C004", "函数超过 50 行", start)
                parameter_depth, count = 0, 1 if i-1 > left+1 and tokens[left+1][0] != "void" else 0
                for value, _, _ in tokens[left+1:i-1]:
                    if value in ("(", "["): parameter_depth += 1
                    if value in (")", "]"): parameter_depth -= 1
                    if value == "," and parameter_depth == 0: count += 1
                if count > 5:
                    add("C005", "函数参数超过 5 个", start)
        if word == "{": depth += 1
        if word == "}": depth = max(0, depth-1)
        body = None
        if word in ("if", "for", "while") and i+1 < len(tokens) and tokens[i+1][0] == "(":
            close = pairs.get(i+1)
            body = close+1 if close is not None else None
            # The trailing while in do {...} while (...); has no separate body.
            if word == "while" and i and tokens[i-1][0] == "}" and body is not None and body < len(tokens) and tokens[body][0] == ";":
                opening = pairs.get(i-1, 0)
                if opening and tokens[opening-1][0] == "do": body = None
        elif word in ("else", "do"):
            body = i+1
            if word == "else" and body < len(tokens) and tokens[body][0] == "if": body = None
        if body is not None and body < len(tokens):
            if tokens[body][0] != "{":
                add("C006", "控制语句必须使用大括号并换行书写语句体", start)
            else:
                brace = tokens[body]
                if "\n" in text[tokens[body-1][2]:brace[1]]:
                    add("C003", "控制语句左大括号须与条件位于同一行", brace[1])
                if body+1 < len(tokens) and "\n" not in text[brace[2]:tokens[body+1][1]]:
                    add("C006", "控制语句体及结束大括号必须换行", brace[1])
        if word in ("strcpy", "gets") and i+1 < len(tokens) and tokens[i+1][0] == "(":
            add("C008", "禁止调用危险函数 " + word, start)
        if word == "strncpy" and i+1 < len(tokens) and tokens[i+1][0] == "(":
            add("C009", "核对 strncpy 的长度与所有路径的 NULL 终止；词法检查无法证明", start, "REVIEW")
    for first, last in functions:
        nesting = 0
        for word, start, _ in tokens[first+1:last]:
            if word == "{":
                nesting += 1
                if nesting > 4: add("C007", "函数内嵌套超过 4 层", start)
            if word == "}": nesting -= 1
            if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)[uUlLfF]*", word):
                value = re.sub(r"[uUlL]+$", "", word)
                if value not in ("0", "1"):
                    add("C011", "数值常量须使用命名宏或枚举：" + word, start)
    # Declaration subset: clear scalar/pointer declarations. Complex declarators require semantic review.
    pattern = re.compile(r"\b(?P<type>(?:(?:static|extern|const|volatile|unsigned|signed|long|short)\s+)*(?:int|char|float|double|bool|size_t|u?int\d+_t|[a-z]\w*_t))\s+\**\s*(?P<name>[A-Za-z_]\w*)\s*(?=[=;,\[])")
    for match in pattern.finditer(clean):
        variable = match["name"]
        local = any(tokens[a][1] < match.start() < tokens[b][2] for a, b in functions)
        prefix = "s_" if "static" in match["type"].split() else "" if local else "g_"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", variable) or (prefix and not variable.startswith(prefix)):
            add("C002", "变量命名须小写下划线" + ("，前缀 " + prefix if prefix else "") + "：" + variable, match.start("name"))
    if Path(name).suffix.lower() == ".h":
        guard = re.search(r"^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b", masked(text), re.M)
        if not guard or not re.fullmatch(r"MODULE_[A-Z][A-Z0-9_]*_H", guard[1]) or not re.search(r"#\s*endif\b", masked(text)):
            add("C010", "头文件保护须使用 MODULE_XXX_H，并包含对应 endif")
    return findings
