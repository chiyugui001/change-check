"""Portable checks with no knowledge-base template dependency."""
import ast
import configparser
import json
import re
import tomllib
from pathlib import Path

import yaml

from ..checks import UniqueLoader, visible_lines
from ..common import CheckError
from ..project import matches, module_for
from . import c_rules


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 键：" + key)
        result[key] = value
    return result


def check(snap, policy):
    findings, executions, covered = [], [], set()
    for name in sorted(snap.changed):
        if matches(name, policy.get("exclude", [])):
            continue
        module = module_for(name, policy)
        if not module or module == "knowledge":
            continue
        covered.add(name)
        def add(rule, message, line=1):
            findings.append({"file": name, "line": line, "rule": rule, "severity": "ERROR",
                             "message": message, "source": module})
        if name not in snap.names:
            executions.append({"checker": module + ":deletion", "file": name, "status": "completed"})
            continue
        if name not in snap.data:
            add("CORE002", "文件未纳入文本快照")
            continue
        try:
            text = snap.text(name)
            suffix = Path(name).suffix.lower()
            for line, value in enumerate(text.splitlines(), 1):
                if re.match(r"^(<<<<<<< |=======$|>>>>>>> )", value):
                    add("CORE003", "存在合并冲突标记", line)
            if "\x00" in text:
                add("CORE002", "文本含 NUL 字节")
            if module == "code":
                if suffix == ".py":
                    ast.parse(text, filename=name)
                elif suffix not in (".c", ".h"):
                    covered.discard(name)
                    executions.append({"checker": module, "file": name, "status": "requires_project_checker"})
                    continue
                elif policy.get("code", {}).get("c_style", True):
                    findings.extend(c_rules.check(name, text))
                else:
                    add("CORE004", "C 文件必须启用内置规则；不能通过 c_style=false 跳过脚本检查")
            elif module == "config":
                if suffix == ".json":
                    json.loads(text, object_pairs_hook=unique_json,
                               parse_constant=lambda v: (_ for _ in ()).throw(ValueError("非法 JSON 数值：" + v)))
                elif suffix == ".toml":
                    tomllib.loads(text)
                elif suffix in (".yaml", ".yml"):
                    list(yaml.load_all(text, Loader=UniqueLoader))
                else:
                    parser = configparser.ConfigParser(interpolation=None, strict=True)
                    parser.read_string(text)
            elif module == "document":
                _, unclosed = visible_lines(text)
                if unclosed:
                    add("DOC001", "围栏代码块未闭合")
        except (CheckError, ValueError, SyntaxError, yaml.YAMLError, configparser.Error) as exc:
            add("SCRIPT001", str(exc), getattr(exc, "lineno", None) or 1)
        executions.append({"checker": module, "file": name, "status": "completed"})
    return findings, executions, covered
