from __future__ import annotations

import datetime as dt
import fnmatch
import html
import json
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from .common import CheckError, digest
from .context import snapshot_name


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        name = loader.construct_object(key, deep=deep)
        if name in result:
            raise ValueError(f"重复 YAML 字段：{name}")
        result[name] = loader.construct_object(value, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def frontmatter(text):
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    if not match:
        raise ValueError("缺少或未闭合 Frontmatter")
    data = yaml.load(match[1], Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError("Frontmatter 必须是键值对象")
    return data, match.end()


def visible_lines(text, keep_inline=False):
    """Preserve original line numbers, excluding frontmatter, comments and code."""
    lines = text.splitlines()
    fence = None
    in_front = bool(lines and lines[0].strip() == "---")
    in_comment = False
    result = []
    for n, raw in enumerate(lines, 1):
        if in_front:
            if n > 1 and raw.strip() == "---":
                in_front = False
            continue
        marker = re.match(r"^\s{0,3}([" + chr(96) + r"~]{3,})(.*)$", raw)
        if marker:
            value = marker[1]
            if fence and value[0] == fence[0] and len(value) >= len(fence) and not marker[2].strip():
                fence = None
            elif not fence:
                fence = value
            continue
        if fence:
            continue
        if "<!--" in raw:
            in_comment = True
        if in_comment:
            if "-->" in raw:
                in_comment = False
            continue
        if not keep_inline:
            raw = re.sub(chr(96) + r"+[^" + chr(96) + r"]*" + chr(96) + "+", "", raw)
        if raw.startswith("    ") and not raw.lstrip().startswith(("-", "*")):
            continue
        result.append((n, raw))
    return result, fence is not None


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) or
               (p.startswith("**/") and fnmatch.fnmatchcase(name, p[3:])) for p in patterns)


def typed_document(name, profile, kind):
    return (not Path(name).name.startswith("_") and
            not matches(Path(name).name, profile.get("naming_exempt", [])) and
            matches(name, profile.get(kind + "_globs", [])))


def writing_document(name, profile):
    """Records remain evidence; their historical wording is not normative prose."""
    return (name.endswith(".md") and not Path(name).name.startswith("_") and
            not matches(Path(name).name, profile.get("naming_exempt", [])) and
            not matches(name, profile.get("exclude", [])) and
            not any(token in name for token in ("会议记录", "会议纪要", "确认记录", "评审记录")) and
            "模板" not in Path(name).parts and "知识库规范" not in Path(name).parts)


def location(file, line, end_line=None, snapshot="current"):
    return {"file": file, "line": line, "end_line": end_line or line, "snapshot": snapshot}


def section_span(text, line):
    headings = [(n, len(re.match(r"^#+", raw)[0])) for n, raw in visible_lines(text, True)[0]
                if re.match(r"^#{1,6}\s+", raw)]
    preceding = [(n, level) for n, level in headings if n <= line]
    if not preceding:
        return None
    start, level = preceding[-1]
    end = next((n - 1 for n, depth in headings if n > start and depth <= level), len(text.splitlines()))
    return start, end, level


def block_span(text, line):
    visible = dict(visible_lines(text, True)[0])
    special = lambda value: bool(re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\||#{1,6}\s+)", value))
    if line not in visible or special(visible[line]):
        return line, line
    start = end = line
    while visible.get(start - 1, "").strip() and not special(visible[start - 1]):
        start -= 1
    while visible.get(end + 1, "").strip() and not special(visible[end + 1]):
        end += 1
    return start, end


def anchor_span(text, anchor):
    if not anchor:
        return None
    lines = visible_lines(text, True)[0]
    if anchor.startswith("^"):
        hits = [n for n, raw in lines if re.search(r"\^" + re.escape(anchor[1:]) + r"(?:\s|$)", raw)]
        if len(hits) != 1:
            return None
        task = next((unit for unit in task_units(text) if unit["line"] <= hits[0] <= unit["end_line"]), None)
        return (task["line"], task["end_line"]) if task else block_span(text, hits[0])
    hits = [n for n, raw in lines if re.match(r"^#{1,6}\s+", raw) and has_anchor(raw, anchor)]
    return section_span(text, hits[0])[:2] if len(hits) == 1 else None


def task_units(text):
    """Checkbox list entries are stable task units; headings may point to one unit."""
    lines = visible_lines(text, True)[0]
    result = []
    for i, (line, raw) in enumerate(lines):
        match = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+", raw)
        if not match:
            continue
        end = line
        for number, value in lines[i + 1:]:
            if not value.strip() or re.match(r"^\s*(?:[-*+]\s+|#{1,6}\s+)", value):
                break
            end = number
        result.append({"line": line, "end_line": end, "done": match[1].lower() == "x"})
    return result


def source_scope(text, line):
    # A table row/list entry is a local assertion. Ordinary prose may link within
    # its nearest section, but a document-wide footer or another section is not local.
    raw = text.splitlines()[line - 1]
    if re.match(r"^\s*(?:\||[-*+]\s+|\d+[.)]\s+)", raw):
        return block_span(text, line)
    section = section_span(text, line)
    if section and section[2] >= 2:
        return section[:2]
    return block_span(text, line)


def task_links(snap, file, line):
    """Return mechanical forward/backward matches; do not infer semantic equality."""
    text = snap.text(file)
    start, end = source_scope(text, line)
    found, problems = [], []
    for number, target, wiki in extract_links(text):
        if not start <= number <= end:
            continue
        destination, anchor, error = resolve_link(file, target, snap.names, wiki)
        if error or destination not in snap.data or not destination.endswith(".md"):
            continue  # KB003 handles broken targets separately.
        other = snap.text(destination)
        units = task_units(other)
        if not units:
            continue
        span = anchor_span(other, anchor)
        matched = [unit for unit in units if span and span[0] <= unit["line"] <= span[1]]
        if len(matched) != 1:
            problems.append("待办链接未定位到唯一事项：" + target)
            continue
        task = matched[0]
        backlink = False
        for backline, backtarget, backwiki in extract_links(other):
            if task["line"] <= backline <= task["end_line"]:
                backfile, backanchor, backerror = resolve_link(destination, backtarget, snap.names, backwiki)
                backspan = anchor_span(text, backanchor) if backfile == file and not backerror else None
                if backspan and start <= backspan[0] <= line <= backspan[1] <= end:
                    backlink = True
        ref = location(destination, task["line"], task["end_line"])
        if not any(item["target"] == ref for item in found):
            found.append({"target": ref, "backlink": backlink, "done": task["done"]})
        if not backlink:
            problems.append("待办未回链原文对应位置：" + target)
    return found, sorted(set(problems))


def writing_checks(snap, profile, texts, add):
    for name in sorted(snap.changed & texts.keys()):
        if not writing_document(name, profile):
            continue
        text = texts[name]
        try:
            meta, _ = frontmatter(text)
        except (ValueError, UnicodeError, yaml.YAMLError):
            continue  # Attribute errors are already reported by KB001.
        try:
            old, _ = frontmatter(snap.before[name].decode("utf-8-sig")) if name in snap.before else ({}, 0)
        except (ValueError, UnicodeError, yaml.YAMLError):
            old = {}
        if "version" in meta and ("version" not in old or meta["version"] != old["version"]):
            line = next((n for n, raw in enumerate(text.splitlines(), 1) if raw.startswith("version:")), 1)
            add(name, "KB007", "文档 version 新增或变更；必须核对用户的版本管理授权，字段合法不代表已获授权", "REVIEW", line)
        for line, raw in visible_lines(text)[0]:
            if raw.lstrip().startswith(">"):
                continue
            if re.search(r"用户(?:已)?确认\s*[：:]|用户已确认|暂按|暂定|先按", raw):
                add(name, "WR001", "过程性或保留表述候选；核对是否应直接陈述已确认方案，保留真实前提与合法引用", "REVIEW", line)
            if re.search(r"待(?:确认|核对|测试|验证)|尚(?:需|未).{0,12}(?:确认|核对|测试|验证)|需(?!求)(?:要)?.{0,8}(?:实测|验证|核对)|未(?:完成|进行|执行).{0,8}(?:测试|验证)", raw):
                links, problems = task_links(snap, name, line)
                detail = "；".join(problems) if problems else "机械双链已定位，仍需核对事项、条件与完成状态"
                if not links:
                    detail = "原文对应位置未找到直链到具体待办；文末总入口不能替代逐项关联"
                add(name, "KB008", "未决事项候选：" + detail, "REVIEW", line)


def extract_links(text):
    lines = visible_lines(text)[0]
    definitions = {}
    for _, line in lines:
        match = re.match(r'^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)', line)
        if match:
            definitions[match[1].casefold()] = match[2].strip('<>')
    for n, line in lines:
        if re.match(r'^\s{0,3}\[[^\]]+\]:', line):
            continue
        spans = []
        for match in re.finditer(r"!?\[\[([^\]]+)\]\]", line):
            value = match[1].replace("\\|", "|").split("|", 1)[0].strip()
            spans.append(match.span())
            yield n, value, True
        for start, end in reversed(spans):
            line = line[:start] + " " * (end - start) + line[end:]
        for match in re.finditer(r"!?\[[^\]]*\]\((<[^>]+>|(?:[^\s()]|\([^()]*\))+)(?:\s+['\"].*?['\"])?\)", line):
            yield n, match[1].strip("<>"), False
        for match in re.finditer(r'!?\[([^\]]+)\]\[([^\]]*)\]', line):
            label = (match[2] or match[1]).casefold()
            if label in definitions:
                yield n, definitions[label], False
        for match in re.finditer(r'(?<![\]!])\[([^\]]+)\](?![\[(])', line):
            if match[1].casefold() in definitions:
                yield n, definitions[match[1].casefold()], False


def resolve_link(source, target, names, wiki=True):
    target = html.unescape(unquote(target))
    if re.match(r"^[a-zA-Z][\w+.-]*:", target) or target.startswith("//"):
        return None, None, None
    filename, _, anchor = target.partition("#")
    if not filename:
        return source, anchor, None
    filename = filename.replace("\\", "/")
    parent = posixpath.dirname(source)
    raw_candidates = ([filename.lstrip("/")] if filename.startswith("/") else
                      [posixpath.normpath(posixpath.join(parent, filename)), filename]
                      if not wiki or filename.startswith(".") else
                      [filename, posixpath.normpath(posixpath.join(parent, filename))])
    for candidate in raw_candidates:
        for path in [candidate, candidate + ".md"] if not Path(candidate).suffix else [candidate]:
            if path in names:
                return path, anchor, None
    if wiki and "/" not in filename:
        candidates = [n for n in names if Path(n).name == filename or Path(n).stem == filename]
        if len(candidates) == 1:
            return candidates[0], anchor, None
        if len(candidates) > 1:
            return filename, anchor, "同名目标不明确：" + "、".join(sorted(candidates)[:8])
    return raw_candidates[0], anchor, "链接目标不存在：" + filename


def has_anchor(text, anchor):
    if not anchor:
        return True
    if anchor.startswith("^"):
        return bool(re.search(r"\^" + re.escape(anchor[1:]) + r"(?:\s|$)", text))
    for _, raw in visible_lines(text, keep_inline=True)[0]:
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", raw)
        if match:
            title = re.sub(r"[*`]|(?<!\w)_(?!\w)", "", match[1]).strip()
            slug = re.sub(r"[^\w\u3400-\u9fff -]", "", title).lower().replace(" ", "-")
            if anchor == title or anchor.lower() == slug:
                return True
    return False


def schema_for(snap, profile):
    if profile.get("frontmatter"):
        return profile["frontmatter"]
    standard = snapshot_name(profile.get("standards", ""))
    name = standard.rstrip("/") + "/frontmatter规范.md"
    if not standard or name not in snap.data:
        raise CheckError("未配置可用的 Frontmatter 规范；请设置 profile.standards")
    text = snap.text(name)
    arrows = next((line for line in text.splitlines() if "→" in line and "reviewed_by" in line), "")
    order = re.findall(r"[a-z][a-z_]*", arrows)
    required = ["reviewed_by", "modified", "status", "tags"]
    if not order or not set(required) <= set(order):
        raise CheckError("无法从当前规范读取字段顺序，请显式配置 frontmatter 规则")
    return {"order": order, "required": required}


def generic_checks(snap, profile):
    findings, coverage = [], []

    def add(file, rule, message, severity="ERROR", line=1):
        findings.append({"file": file, "line": line, "rule": rule,
                         "severity": severity, "message": message, "source": "change-check"})

    schema = schema_for(snap, profile)
    active = {n for n in snap.changed if n in snap.data and n.endswith(".md") and
              not matches(n, profile.get("exclude", []))}
    texts = {}
    for name in snap.data:
        if name.endswith(".md"):
            try:
                texts[name] = snap.text(name)
            except CheckError as exc:
                if name in snap.changed:
                    add(name, "KB005", str(exc))
    for name in sorted(active & texts.keys()):
        text = texts[name]
        data = {}
        try:
            data, _ = frontmatter(text)
            missing = set(schema["required"]) - data.keys()
            if missing:
                add(name, "KB001", "缺少字段：" + ", ".join(sorted(missing)))
            order = schema["order"]
            unknown = set(data) - set(order)
            if unknown:
                add(name, "KB001", "未登记字段：" + ", ".join(sorted(unknown)))
            if [k for k in data if k in order] != [k for k in order if k in data]:
                add(name, "KB001", "字段顺序不符合当前规范")
            if data.get("status") not in ("draft", "active"):
                add(name, "KB001", "status 必须为 draft 或 active")
            if not isinstance(data.get("reviewed_by"), str) or not data["reviewed_by"].strip():
                add(name, "KB001", "reviewed_by 必须是非空字符串")
            if data.get("status") == "active" and data.get("reviewed_by") == "AI-draft":
                add(name, "KB001", "active 与 AI-draft 矛盾")
            modified = str(data.get("modified", ""))
            try:
                dt.datetime.strptime(modified, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                add(name, "KB001", "modified 格式必须为 YYYY-MM-DD HH:mm:ss")
            tags = data.get("tags")
            if not isinstance(tags, list) or not tags or not all(
                    isinstance(x, str) and x.strip() for x in tags):
                add(name, "KB001", "tags 必须是非空字符串列表")
            for key in ("modules",):
                if key in data and (not isinstance(data[key], list) or
                                    not all(isinstance(x, str) for x in data[key])):
                    add(name, "KB001", key + " 必须为字符串列表")
            if "priority" in data and data["priority"] not in ("P0", "P1", "P2", "P3"):
                add(name, "KB001", "priority 必须为 P0/P1/P2/P3")
        except (ValueError, yaml.YAMLError) as exc:
            add(name, "KB001", str(exc))
        basename = Path(name).name
        if not matches(basename, profile.get("naming_exempt", [])):
            if re.search(r"\s", basename) or re.match(r"^\d+[._-]", basename):
                add(name, "KB002", "普通知识文件名不能带空格或序号前缀")
            if not re.search(r"[\u3400-\u9fff]", basename):
                add(name, "KB002", "纯英文文档名需确认是否属于代码/专有名称例外", "REVIEW")
        lines, unclosed = visible_lines(text)
        if unclosed:
            add(name, "KB005", "围栏代码块未闭合")
        for n, raw in enumerate(text.splitlines(), 1):
            if re.match(r"^(<<<<<<< |=======$|>>>>>>> )", raw):
                add(name, "KB005", "存在 Git 合并冲突标记", line=n)
        for n, raw in lines:
            if re.search(r"<(?:文档标题|待填写|具体[^>]+|填写[^>]+)>|\bTODO\b|\bTBD\b", raw):
                add(name, "KB005", "残留模板占位符", line=n)
        count = sum(bool(raw.strip()) and "|" not in raw for _, raw in lines)
        limit = int(profile.get("body_error_limit", 500))
        if count > limit:
            add(name, "KB006", f"正文 {count} 行，超过 {limit} 行上限")
        elif count > int(profile.get("body_review_limit", 300)):
            add(name, "KB006", f"正文 {count} 行，需检查主题是否可拆分", "REVIEW")
        if not basename.startswith("_") and not matches(basename, profile.get("naming_exempt", [])):
            parent = Path(name).parent
            index = None
            while True:
                candidate = (parent / "_INDEX.md").as_posix()
                if candidate in texts:
                    index = candidate
                    break
                if parent == Path("."):
                    break
                parent = parent.parent
            if not index or not any(resolve_link(index, t, snap.names, wiki)[0] == name
                                    for _, t, wiki in extract_links(texts[index])):
                add(name, "KB004", "最近一级 _INDEX.md 缺少文档入口")
            pending = profile.get("pending", "PENDING-REVIEW.md")
            if data.get("status") == "draft" and pending not in texts:
                add(name, "KB004", "草稿缺少待审核入口：" + pending)
            if data.get("status") == "draft" and pending in texts:
                linked = {resolve_link(pending, t, snap.names, wiki)[0]
                          for _, t, wiki in extract_links(texts[pending])}
                if name not in linked and index not in linked:
                    add(name, "KB004", "待审核入口未包含本文或其局部索引")
        if name in snap.before:
            try:
                before, _ = frontmatter(snap.before[name].decode("utf-8-sig"))
                if before.get("status") == "active" or before.get("status") != data.get("status"):
                    add(name, "KC003", "已审核内容或生命周期有变化，需核对授权与审核依据", "REVIEW")
                old_body = snap.before[name].decode("utf-8-sig")
                if old_body != text and before.get("modified") == data.get("modified"):
                    add(name, "KB001", "修改文档后未更新 modified")
            except (ValueError, UnicodeError, yaml.YAMLError):
                pass
    for name in snap.changed:
        if (name.startswith("Raw/") or "/Raw/" in name) and name in snap.before:
            add(name, "KC003", "原始资料被修改或删除，必须核对授权和证据保留", "REVIEW")
        if name not in snap.names and name in snap.before and name.endswith(".md"):
            try:
                data, _ = frontmatter(snap.before[name].decode("utf-8-sig"))
                if data.get("status") == "active":
                    add(name, "KC003", "已审核文档被删除，必须核对授权", "REVIEW")
            except (ValueError, UnicodeError, yaml.YAMLError):
                pass
    # Revalidate inbound links when targets or their headings changed.
    all_names = snap.names | set(snap.before)
    for name, text in texts.items():
        for line, target, wiki in extract_links(text):
            resolved, anchor, error = resolve_link(name, target, snap.names, wiki)
            old_target = resolve_link(name, target, all_names, wiki)[0]
            if name not in active and resolved not in snap.changed and old_target not in snap.changed:
                continue
            if resolved is None:
                continue
            if error:
                add(name, "KB003", error, line=line)
            elif anchor and resolved in texts and not has_anchor(texts[resolved], anchor):
                add(name, "KB003", f"标题或块引用不存在：{target}", line=line)
    ledger_checks(snap, profile, add)
    mapping_checks(snap, profile, texts, add)
    writing_checks(snap, profile, texts, add)
    coverage.extend(["KB001", "KB002", "KB003", "KB004", "KB005", "KB006",
                     "KB007", "KB008", "WR001", "KC001", "KC002", "KC003", "RD001"])
    return findings, coverage


def ledger_checks(snap, profile, add):
    ledger = profile.get("ledger", "AI/Index/compile_index.json")
    if ledger not in snap.data:
        return
    try:
        data = json.loads(snap.text(ledger))
        entries = data.get("compiled_files", data.get("compilations"))
        if not isinstance(entries, list):
            raise ValueError("账本缺少 compiled_files/compilations 数组")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("账本条目必须为对象")
            sources = entry.get("sources", [{"id": entry.get("source_file"),
                                            "hash": entry.get("source_hash")}])
            outputs = entry.get("outputs", [{"path": p} for p in entry.get("output_files", [])])
            relevant = (ledger in snap.changed or any(
                o.get("path") in snap.changed for o in outputs) or any(
                s.get("id") in snap.changed for s in sources))
            for source in sources:
                identity = source.get("id")
                key = str(identity).replace("\\", "/")
                if relevant and (not identity or key in seen):
                    add(ledger, "KC001", f"来源缺失或重复：{identity}")
                seen.add(key)
                if not relevant:
                    continue
                expected = source.get("hash", "")
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(expected)):
                    add(ledger, "KC001", f"非法来源 hash：{identity}")
                elif not urlsplit(str(identity)).scheme:
                    if key not in snap.names:
                        add(ledger, "KC002", f"本地来源不存在：{key}")
                    elif "sha256:" + digest(snap.bytes_for(key)) != expected:
                        add(ledger, "KC002", f"本地来源 hash 不一致：{key}")
                else:
                    add(ledger, "KC002", f"远程来源版本/hash 需证据核实：{identity}", "REVIEW")
            if relevant:
                if not outputs:
                    add(ledger, "KC001", "编译条目没有输出")
                for output in outputs:
                    if output.get("path") not in snap.names:
                        add(ledger, "KC001", f"输出不存在：{output.get('path')}")
    except (ValueError, AttributeError, TypeError, KeyError) as exc:
        add(ledger, "KC001", f"账本结构错误：{exc}")


def mapping_checks(snap, profile, texts, add):
    maps = profile.get("coverage_maps", [])
    requirements = [n for n in snap.changed if typed_document(n, profile, "requirement")]
    if requirements and not maps:
        for name in requirements:
            add(name, "RD001", "需求变动缺少已配置的覆盖映射，须提供来源到正文及验收的映射", "REVIEW")
    for name in maps:
        try:
            data = json.loads(snap.text(name))
            entries = data["mappings"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("mappings 必须为非空数组")
            ids = set()
            for entry in entries:
                identity = entry["id"]
                if identity in ids:
                    raise ValueError("重复映射 id：" + identity)
                ids.add(identity)
                for key in ("source", "target", "acceptance"):
                    destination, anchor, error = resolve_link(name, entry[key], snap.names)
                    if error or not destination or (anchor and destination in texts and
                                                    not has_anchor(texts[destination], anchor)):
                        add(name, "RD001", f"{identity} 的 {key} 无有效落点：{entry[key]}")
        except (ValueError, KeyError, TypeError, CheckError) as exc:
            add(name, "RD001", f"覆盖映射无效：{exc}")
