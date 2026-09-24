from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .common import (CheckError, SKILL_SCRIPTS, add_root, canonical, find_root,
                     home, lock, read_json, roots, save_json, skill_candidates, update_root, run)
from .engine import implementation_fingerprint, print_report, run_checks
from .integration import (configure_agent, detect_agents, install_git_hook, record_event,
                          uninstall_git_hook)
from .repository import load_snapshot, repo_root
from .review import (claude_command, codex_command, review_cache_enabled, reviewer_identity,
                     review_snapshot, validate_reviewer)
from .context import attach_references
from . import storage
from .project import CONFIG, FIELDS, MODULES, effective_profile, module_for, validate
from .resources import complete_profile, require_resources, resource_issues, set_resource


def parser():
    result = argparse.ArgumentParser(description="变更脚本检查与提交前审查（无后台服务）")
    result.add_argument("--default-root", default=str(Path.cwd()), help=argparse.SUPPRESS)
    result.add_argument("--state", help="本机登记与报告目录")
    commands = result.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="登记项目")
    add.add_argument("path", nargs="?")
    commands.add_parser("list", help="查看已登记目录")
    remove = commands.add_parser("remove", help="移除登记，不删除项目文件")
    remove.add_argument("path")
    for name in ("check", "review", "gate"):
        command = commands.add_parser(name)
        command.add_argument("--root")
        command.add_argument("--json", action="store_true")
        if name == "check":
            command.add_argument("--module", choices=sorted(MODULES), help="仅检查所选模块；不能替代提交门禁")
        group = command.add_mutually_exclusive_group()
        group.add_argument("--staged", action="store_true")
        group.add_argument("--changed", action="store_true")
        group.add_argument("--all", action="store_true")
    install = commands.add_parser("install", help="交互安装")
    install.add_argument("--root")
    install.add_argument("--modules", help="启用模块，逗号分隔；默认根据规范目录选择")
    install.add_argument("--agents", help="非交互选择，例如 codex,kimi")
    install.add_argument("--reviewer", choices=["claude", "codex", "command"])
    install.add_argument("--reviewer-program", help="手动指定内置审查器的 CLI 程序路径")
    install.add_argument("--reviewer-argv-json", help="自定义审查命令的 JSON 参数数组")
    install.add_argument("--reviewer-model", help="审查模型名称；不维护模型名称白名单")
    install.add_argument("--skill")
    install.add_argument("--standards")
    install.add_argument("--template")
    install.add_argument("--skip-login-check", action="store_true",
                         help="跳过登录状态探测，实际提交仍必须成功审查")
    config = commands.add_parser("config", help="查看有效项目策略或写入 change-check.json")
    config.add_argument("--root")
    config.add_argument("--file", help="从 JSON 文件导入项目策略，不修改机器审查器")
    config.add_argument("--modules", help="写入所选模块，逗号分隔")
    commands.add_parser("detect", help="探测 AI 工具，不修改配置")
    commands.add_parser("doctor", help="检查登记、依赖和接入状态")
    commands.add_parser("uninstall", help="移除全部本工具接入，保留项目文件")
    reviewer = commands.add_parser("reviewer", help="查看或设置审查器，不安装钩子、不调用模型")
    reviewer.add_argument("--root")
    reviewer.add_argument("--kind", choices=["claude", "codex", "command"])
    model = reviewer.add_mutually_exclusive_group()
    model.add_argument("--model", help="指定审查模型名称")
    model.add_argument("--use-default-model", action="store_true", help="取消固定模型，使用客户端默认值")
    reviewer.add_argument("--program", help="Claude Code 或 Codex CLI 程序路径")
    reviewer.add_argument("--timeout", type=int, help="审查超时秒数")
    reviewer.add_argument("--argv-json", help="自定义审查命令的 JSON 参数数组")
    state = commands.add_parser("storage", help="查看存储占用或配置保留策略")
    for key in storage.DEFAULTS:
        state.add_argument("--" + key.replace("_", "-"), type=int)
    clean = commands.add_parser("cleanup", help="按保留策略清理生成记录")
    clean.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    hook = commands.add_parser("hook")
    hook.add_argument("--agent", required=True, choices=["codex", "kimi", "zcode", "claude"])
    hook.add_argument("--marker", default="")
    return result


def prompt_path(default):
    if not sys.stdin.isatty():
        raise CheckError("非交互环境请显式提供路径")
    return input(f"项目路径（留空使用 {default}）：").strip().strip('"') or default


def select_kind(reviewer, kind):
    selected = dict(reviewer)
    if kind != selected.get("kind"):
        for key in ("model", "program", "argv"):
            selected.pop(key, None)
    selected["kind"] = kind
    return selected


def suggested_reviewer(agents, profile):
    if profile.get("reviewer_selected") or profile.get("git_hook"):
        return profile["reviewer"]["kind"]
    if len(agents) == 1:
        return agents[0]
    if "claude" in agents or not agents:
        return "claude"
    return next((name for name in agents if name in ("claude", "codex")), "claude")


def install_reviewer(args, agents, item):
    selected = args.reviewer
    suggestion = suggested_reviewer(agents, item["profile"])
    supported = suggestion in ("claude", "codex", "command")
    if selected is None:
        if not supported:
            print(f"{suggestion} 已支持编辑钩子，但尚无内置审查适配；请选择 claude、codex 或 command。")
        if sys.stdin.isatty():
            default = suggestion if supported else ""
            selected = input(f"审查器 claude/codex/command [{default or '需手动选择'}]（回车采用默认）：").strip() or default
        elif supported:
            selected = suggestion
        else:
            raise CheckError(f"{suggestion} 尚无内置审查适配，请用 --reviewer 明确选择，或配置 command")
    if selected not in ("claude", "codex", "command"):
        raise CheckError("审查器必须选择 claude、codex 或 command")
    reviewer = select_kind(item["profile"]["reviewer"], selected)
    if args.reviewer_program:
        if selected == "command":
            raise CheckError("command 审查器请使用 --reviewer-argv-json")
        reviewer["program"] = str(Path(args.reviewer_program).expanduser().resolve())
    if args.reviewer_argv_json is not None:
        if selected != "command":
            raise CheckError("--reviewer-argv-json 只用于 command 审查器")
        reviewer["argv"] = json.loads(args.reviewer_argv_json)
    elif selected == "command" and not reviewer.get("argv") and sys.stdin.isatty():
        reviewer["argv"] = json.loads(input("输入自定义适配程序的 JSON 参数数组："))
    if args.reviewer_model is not None:
        reviewer["model"] = args.reviewer_model
    elif sys.stdin.isatty():
        default_model = reviewer.get("model") or "客户端/适配器默认"
        model = input(f"审查模型 [{default_model}]（回车保留；输入 - 使用默认）：").strip()
        if model == "-":
            reviewer.pop("model", None)
        elif model:
            reviewer["model"] = model
    validate_reviewer(reviewer)
    print("审查选择：" + json.dumps(reviewer_identity(reviewer), ensure_ascii=False))
    if selected in ("claude", "codex"):
        command = (claude_command if selected == "claude" else codex_command)(reviewer.get("program"))
        if not args.skip_login_check:
            login_args = ["auth", "status"] if selected == "claude" else ["login", "status"]
            result = run(command + login_args, check=False)
            if result.returncode:
                raise CheckError(selected + " 登录状态检查失败；请先登录再安装")
    item["profile"]["reviewer"] = reviewer
    item["profile"]["reviewer_selected"] = True


def do_reviewer(args):
    initial = find_root(args.root)
    with lock(initial["id"], seconds=30):
        item = find_root(initial["path"])
        reviewer = dict(item["profile"].get("reviewer", {}))
        changes = any(getattr(args, key) is not None for key in
                      ("kind", "model", "program", "timeout", "argv_json")) or args.use_default_model
        if args.kind:
            reviewer = select_kind(reviewer, args.kind)
        if args.program is not None:
            if reviewer.get("kind", "claude") == "command":
                raise CheckError("command 审查器请通过 --argv-json 指定程序")
            if not args.program.strip():
                raise CheckError("程序路径不能为空")
            reviewer["program"] = str(Path(args.program).expanduser().resolve())
        if args.argv_json is not None:
            if reviewer.get("kind", "codex") != "command":
                raise CheckError("--argv-json 需要 --kind command")
            reviewer["argv"] = json.loads(args.argv_json)
        if args.use_default_model:
            reviewer.pop("model", None)
        elif args.model is not None:
            reviewer["model"] = args.model
        if args.timeout is not None:
            reviewer["timeout"] = args.timeout
        validate_reviewer(reviewer)
        if changes:
            item["profile"]["reviewer"] = reviewer
            item["profile"]["reviewer_selected"] = True
            update_root(item)
        print(json.dumps({"root": item["path"], "reviewer": reviewer,
                          "selection": reviewer_identity(reviewer),
                          "cache_enabled": review_cache_enabled(reviewer),
                          "model_availability": "未探测；由实际审查调用验证", "updated": changes},
                         ensure_ascii=False, indent=2))
        return 0


def do_check(args):
    item = find_root(args.root)
    if args.command == "gate" and (args.changed or args.all):
        raise CheckError("提交门禁只接受暂存快照，不能使用 --changed 或 --all")
    if args.command == "review" and args.all:
        raise CheckError("请明确使用 review --changed 或 review --staged")
    staged = args.staged or args.command == "gate" or (args.command == "review" and not args.changed)
    if args.command == "review" and not staged and repo_root(Path(item["path"])) != Path(item["path"]).resolve():
        raise CheckError("日常 review --changed 要求登记目录本身是 Git 根目录")
    with lock(item["id"], seconds=30):
        snap = load_snapshot(item["path"], staged, all_files=args.all)
        if getattr(args, "module", None):
            selected = effective_profile(item["profile"], snap)
            if args.module not in selected.get("modules", ["knowledge"]):
                raise CheckError("项目未启用模块：" + args.module)
            snap.changed = {n for n in snap.changed if module_for(n, selected) == args.module}
            report = run_checks(snap, item, selected_module=args.module)
            report["selected_module"] = args.module
        else:
            report = run_checks(snap, item)
        if args.command in ("review", "gate"):
            if any(f["severity"] == "ERROR" for f in report["findings"]):
                if args.json:
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                else:
                    print_report(report)
                return 1
            result, cached = review_snapshot(snap, item, report)
            report["review"] = result
            report["review_cached"] = cached
            report["exit_code"] = 0
            report["ai_review"] = "passed"
            storage.save_artifact(home() / "reports" / (item["id"] + ".json"), report)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_report(report)
            if "review" in report:
                print(("提交前" if staged else "日常") + " AI 审查通过" + ("（复用同一快照记录）" if cached else ""))
        return report["exit_code"]


def do_install(args):
    root = args.root or prompt_path(args.default_root)
    item = add_root(root)
    previously_knowledge = "knowledge" in item["profile"].get("modules", ["knowledge"])
    item["profile"].setdefault("modules", ["knowledge", "code", "config"])
    project_policy = read_json(Path(item["path"]) / CONFIG, {})
    if not isinstance(project_policy, dict) or set(project_policy) - FIELDS:
        raise CheckError("项目策略包含未知字段")
    item["profile"].update(project_policy)
    if args.modules:
        item["profile"]["modules"] = [x.strip() for x in args.modules.split(",")]
    elif sys.stdin.isatty() and args.agents is None:
        current = ",".join(item["profile"]["modules"])
        selected = input(f"检查模块 [{current}]（知识库填 knowledge,code,config；回车保留）：").strip()
        if selected:
            item["profile"]["modules"] = [x.strip() for x in selected.split(",")]
    validate(item["profile"])
    knowledge_enabled = "knowledge" in item["profile"].get("modules", ["knowledge"])
    for key in ("skill", "standards", "template"):
        if getattr(args, key):
            set_resource(item["profile"], key, getattr(args, key))
    if knowledge_enabled and not previously_knowledge:
        item["profile"].pop("rule_sources", None)
    complete_profile(item["profile"])
    if knowledge_enabled and sys.stdin.isatty() and (args.agents is None or args.reviewer is None):
        candidates = skill_candidates()
        if len(candidates) > 1 and not args.skill:
            print("已发现多个技能位置：\n" + "\n".join(candidates))
        for key, label in (("standards", "知识库规范目录"), ("template", "详设模板"), ("skill", "校验脚本目录")):
            if not getattr(args, key):
                value = input(f"{label} [{item['profile'][key]}]（回车保留或输入路径）：").strip().strip('"')
                if value:
                    set_resource(item["profile"], key, value)
    print("项目：" + item["path"])
    print("启用模块：" + ", ".join(item["profile"]["modules"]))
    print("本机登记与报告目录：" + str(home()))
    if knowledge_enabled:
        print("知识库规范：" + item["profile"]["standards"])
        print("校验脚本：" + item["profile"]["skill"])
        require_resources(item["path"], item["profile"])
    found = detect_agents()
    for index, candidate in enumerate(found, 1):
        print(f"{index}. {candidate['agent']} — {candidate['state']}")
    if args.agents is None:
        if not sys.stdin.isatty():
            raise CheckError("非交互安装需显式指定 --agents；审查器默认跟随受支持的接入平台")
        selected = input("选择接入工具编号/名称，逗号分隔（留空只安装 Git 钩子）：").strip()
        parts = [x.strip() for x in selected.split(",") if x.strip()]
        agents = [found[int(x)-1]["agent"] if x.isdigit() and 1 <= int(x) <= len(found)
                  else x for x in parts]
    else:
        agents = [x.strip() for x in args.agents.split(",") if x.strip()]
    if set(agents) - {"codex", "kimi", "zcode", "claude"}:
        raise CheckError("工具名称无效")
    install_reviewer(args, agents, item)
    item["profile"]["agents"] = agents
    from .integration import config_path
    import tomlkit
    for agent in agents:
        path = config_path(agent)
        if path.exists():
            if agent == "kimi":
                tomlkit.parse(path.read_text(encoding="utf-8-sig"))
            else:
                existing = read_json(path)
                if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
                    raise CheckError("工具配置结构不合法：" + str(path))
    update_root(item)
    for agent in agents:
        configure_agent(agent)
    if repo_root(Path(item["path"])) == Path(item["path"]).resolve():
        install_git_hook(item)
    active_agents = {a for registered in roots() for a in registered["profile"].get("agents", [])}
    for agent in list(read_json(home() / "agents.json", {})):
        if agent not in active_agents:
            configure_agent(agent, remove=True)
    print("配置已写入。AI 钩子状态为待实际事件验证；Codex 新钩子须在宿主内信任。")
    return do_doctor()


def do_doctor():
    failed = False
    if not roots():
        print("尚未登记项目。")
    for item in roots():
        issues = []
        profile = complete_profile(dict(item["profile"]))
        try:
            project_policy = read_json(Path(item["path"]) / CONFIG, {})
            if not isinstance(project_policy, dict) or set(project_policy) - FIELDS:
                raise CheckError("项目策略含未知字段")
            profile.update(project_policy)
            complete_profile(profile)
            validate(profile)
        except (CheckError, ValueError) as exc:
            issues.append(str(exc))
        try:
            validate_reviewer(profile.get("reviewer", {}))
        except CheckError as exc:
            issues.append(str(exc))
        if not Path(item["path"]).is_dir():
            issues.append("根目录不存在")
        issues.extend(resource_issues(item["path"], profile))
        if isinstance(profile.get("reviewer"), dict) and profile["reviewer"].get("kind") in ("claude", "codex"):
            try:
                resolver = claude_command if profile["reviewer"]["kind"] == "claude" else codex_command
                command = resolver(profile["reviewer"].get("program"))
                run(command + ["--version"], timeout=15)
            except CheckError as exc:
                issues.append(str(exc))
        hook = profile.get("git_hook")
        if hook:
            from .common import digest, git
            current = git(item["path"], "config", "--local", "--get", "core.hooksPath",
                          check=False).stdout.decode().strip()
            if canonical(current) != canonical(hook["directory"]):
                issues.append("Git core.hooksPath 已变化")
            for path, expected in hook["hashes"].items():
                if not Path(path).exists() or digest(Path(path).read_bytes()) != expected:
                    issues.append("Git 钩子缺失/被修改：" + path)
        print(json.dumps({"path": item["path"], "id": item["id"], "problems": issues,
                          "git": "已配置" if hook else "未配置", "agents": profile.get("agents", [])},
                         ensure_ascii=False))
        failed |= bool(issues)
        mapping = Path(item["path"]) / "tools/kb-junctions.json"
        if mapping.is_file():
            registered = {canonical(r["path"]) for r in roots()}
            for entry in read_json(mapping, {}).get("mappings", []):
                mapped = Path(item["path"]) / entry["path"]
                target = Path(entry["target"])
                print(json.dumps({"mapping": entry["path"], "target_exists": target.is_dir(),
                    "target_matches": canonical(mapped) == canonical(target),
                    "target_registered": canonical(target) in registered}, ensure_ascii=False))
    print(json.dumps({"agents": read_json(home() / "agents.json", {}),
                      "events": read_json(home() / "events.json", {})}, ensure_ascii=False, indent=2))
    return 2 if failed else 0


def do_hook(args):
    if os.environ.get("CHANGE_CHECK_REVIEW") == "1":
        return 0
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise CheckError("事件必须是对象")
        event_name = event.get("hook_event_name", event.get("hookEventName", "PostToolUse"))
        messages, checked = [], []
        # All registered roots are examined: shell writes may target a different cwd.
        for item in roots():
            if args.agent not in item["profile"].get("agents", []):
                continue
            with lock(item["id"], seconds=5):
                snap = load_snapshot(item["path"])
                attach_references(snap, item["profile"])
                key = snap.fingerprint() + implementation_fingerprint(effective_profile(item["profile"], snap))
                last_file = home() / "hook-cache" / (item["id"] + ".json")
                last = read_json(last_file, {})
                if last.get("key") == key:
                    report = last["report"]
                else:
                    report = run_checks(snap, item)
                    storage.save_artifact(last_file, {"key": key, "report": report})
                checked.append(item["id"])
                for finding in report["findings"]:
                    if finding["severity"] in ("ERROR", "REVIEW"):
                        messages.append(f"{item['path']}/{finding['file']}:{finding['line']} "
                                        f"{finding['severity']} {finding['rule']}: {finding['message']}")
        record_event(args.agent, event_name, checked)
        if not messages:
            print("{}")
            return 0
        message = "变更检查待处理；只修正当前任务已授权范围，其他任务变动仅报告：\n" + "\n".join(messages[:60])
        if len(messages) > 60:
            message += "\n更多问题见本机 change-check/reports，不表示检查通过。"
        if args.agent == "kimi":
            if event_name == "Stop" and not event.get("stop_hook_active"):
                print(message, file=sys.stderr)
                return 2
            print(message)
            return 0
        if event_name == "Stop" and not event.get("stop_hook_active"):
            print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=False))
            return 0
        if event_name == "Stop":
            print(json.dumps({"systemMessage": message}, ensure_ascii=False))
            return 0
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                                 "additionalContext": message}},
                         ensure_ascii=False))
        return 0
    except (CheckError, ValueError, OSError) as exc:
        print("变更检查未完成：" + str(exc), file=sys.stderr)
        return 2


def main(argv=None):
    args = parser().parse_args(argv)
    if args.state:
        os.environ["CHANGE_CHECK_HOME"] = str(Path(args.state).expanduser().resolve())
    try:
        if args.command in ("check", "review", "gate", "hook", "install", "remove", "uninstall"):
            storage.cleanup()
        if args.command == "storage":
            changes = {key: getattr(args, key) for key in storage.DEFAULTS if getattr(args, key) is not None}
            result = storage.configure(changes) if changes else storage.cleanup(dry_run=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "cleanup":
            print(json.dumps(storage.cleanup(args.dry_run), ensure_ascii=False, indent=2))
            return 0
        if args.command == "add":
            item = add_root(args.path or prompt_path(args.default_root))
            print(json.dumps(item, ensure_ascii=False, indent=2))
            print("已登记；使用 install 配置该根的 Git 钩子和 AI 接入。")
            return 0
        if args.command == "list":
            print(json.dumps(roots(), ensure_ascii=False, indent=2))
            return 0
        if args.command in ("remove", "uninstall"):
            items = roots() if args.command == "uninstall" else [find_root(args.path)]
            if args.command == "remove" and canonical(args.path) != canonical(items[0]["path"]):
                raise CheckError("删除登记必须提供精确根路径")
            for item in items:
                uninstall_git_hook(item)
                with lock("registry"):
                    save_json(home() / "registry.json",
                              {"roots": [r for r in roots() if r["id"] != item["id"]]})
            active_agents = {a for item in roots() for a in item["profile"].get("agents", [])}
            for agent in list(read_json(home() / "agents.json", {})):
                if agent not in active_agents:
                    configure_agent(agent, remove=True)
            print("登记与专属接入已移除；项目文件未删除。")
            return 0
        if args.command in ("check", "review", "gate"):
            return do_check(args)
        if args.command == "install":
            return do_install(args)
        if args.command == "config":
            item = find_root(args.root)
            target = Path(item["path"]) / CONFIG
            config = read_json(Path(args.file)) if args.file else read_json(target, {})
            if not isinstance(config, dict) or set(config) - FIELDS:
                raise CheckError("项目策略文件包含未知字段")
            if args.modules:
                config["modules"] = [x.strip() for x in args.modules.split(",")]
            validate({**item["profile"], **config})
            if args.file or args.modules:
                if target.exists():
                    storage.backup_config(target)
                save_json(target, config)
            print(json.dumps({**{k: v for k, v in item["profile"].items() if k in FIELDS}, **config}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "reviewer":
            return do_reviewer(args)
        if args.command == "detect":
            print(json.dumps(detect_agents(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            return do_doctor()
        if args.command == "hook":
            return do_hook(args)
    except (CheckError, OSError, ValueError, KeyError, EOFError, yaml.YAMLError) as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 2
    return 2
