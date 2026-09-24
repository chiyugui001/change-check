"""Snapshot-bound project policy. Machine reviewer/hook settings stay in registry."""
import copy
import fnmatch
import json
from pathlib import Path

from .common import CheckError

CONFIG = "change-check.json"
MODULES = {"knowledge", "code", "config", "document"}
FIELDS = {"modules", "commands", "required_roles", "review_units", "exclude", "code"}


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) or
               (p.startswith("**/") and fnmatch.fnmatchcase(name, p[3:])) for p in patterns)


def string_list(value, label, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value) or not all(
            isinstance(v, str) and v for v in value):
        raise CheckError(label + " 必须是字符串数组")


def validate(policy):
    modules = policy.get("modules", ["knowledge"])
    string_list(modules, "modules", True)
    if set(modules) - MODULES or len(set(modules)) != len(modules):
        raise CheckError("modules 包含未知或重复模块")
    string_list(policy.get("exclude", []), "exclude")
    string_list(policy.get("required_roles", []), "required_roles")
    if set(policy.get("required_roles", [])) - {"lint", "build", "test", "validate"}:
        raise CheckError("required_roles 含未知角色")
    commands = policy.get("commands", [])
    if not isinstance(commands, list):
        raise CheckError("commands 必须是数组")
    ids = set()
    for command in commands:
        if not isinstance(command, dict) or set(command) - {"id", "argv", "include", "stages", "timeout", "role"}:
            raise CheckError("检查命令字段无效")
        name = command.get("id")
        if not isinstance(name, str) or not name or name in ids:
            raise CheckError("命令 id 必须非空且唯一")
        ids.add(name)
        string_list(command.get("argv"), name + ".argv", True)
        string_list(command.get("include"), name + ".include", True)
        stages = command.get("stages", ["edit", "commit"])
        string_list(stages, name + ".stages", True)
        if set(stages) - {"edit", "commit"}:
            raise CheckError("stages 只能为 edit、commit")
        if command.get("role", "validate") not in {"lint", "build", "test", "validate"}:
            raise CheckError("命令 role 无效")
        if type(command.get("timeout", 120)) is not int or not 1 <= command.get("timeout", 120) <= 3600:
            raise CheckError("命令 timeout 必须在 1–3600 秒之间")
        # No shell or free-form formatting. Individual argv elements can use these tokens.
        for arg in command["argv"]:
            if ("{snapshot}" in arg or "{python}" in arg or "{files}" in arg) and arg not in (
                    "{snapshot}", "{python}", "{files}"):
                raise CheckError("命令占位符必须独占一个参数")
    units = policy.get("review_units", [])
    if not isinstance(units, list):
        raise CheckError("review_units 必须是数组")
    ids = set()
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {"id", "include", "dependencies"}:
            raise CheckError("review_units 必须包含 id、include、dependencies")
        if not isinstance(unit["id"], str) or not unit["id"] or unit["id"] in ids:
            raise CheckError("审查单元 id 必须非空且唯一")
        ids.add(unit["id"])
        string_list(unit["include"], "审查单元 include", True)
        string_list(unit["dependencies"], "审查单元 dependencies")
    code = policy.get("code", {})
    if not isinstance(code, dict) or set(code) - {"c_style"} or type(code.get("c_style", True)) is not bool:
        raise CheckError("code 仅接受布尔值 c_style")
    return policy


def effective_profile(profile, snap):
    from .resources import complete_profile
    result = copy.deepcopy(profile)
    if CONFIG in snap.data:
        try:
            config = json.loads(snap.text(CONFIG))
        except ValueError as exc:
            raise CheckError("change-check.json 无效：" + str(exc)) from exc
        if not isinstance(config, dict) or set(config) - FIELDS:
            raise CheckError("change-check.json 含未知字段；审查器、钩子只能通过本机命令设置")
        result.update(config)
    if "knowledge" not in profile.get("modules", ["knowledge"]) and "knowledge" in result.get("modules", []):
        result.pop("rule_sources", None)
    return validate(complete_profile(result))


def module_for(name, policy):
    modules = policy.get("modules", ["knowledge"])
    suffix = Path(name).suffix.lower()
    if suffix in {".md", ".mmd", ".txt"}:
        return "knowledge" if "knowledge" in modules else "document" if "document" in modules else None
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
        return "config" if "config" in modules else None
    if suffix in {".c", ".h", ".py", ".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".rs", ".go",
                  ".java", ".kt", ".swift", ".cs", ".js", ".jsx", ".ts", ".tsx", ".ps1", ".sh", ".s"}:
        return "code" if "code" in modules else None
    return None
