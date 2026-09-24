from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
TOOL = SOURCE.parent
DOCS = TOOL / "文档"
MAX_BYTES = 40 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".obsidian",
             ".claude", ".codex", ".kimi", ".kimi-code", ".zcode",
             ".hinote", ".claudian", ".change-check", ".kb-check", ".codex-skill-staging"}
TEXT_SUFFIXES = {".md", ".mmd", ".json", ".yaml", ".yml", ".txt", ".toml",
                 ".py", ".ps1", ".sh", ".c", ".h", ".cpp", ".hpp", ".js", ".ts",
                 ".html", ".css", ".ini", ".cfg", ".csv"}
TEXT_SUFFIXES.update({".rs", ".go", ".java", ".kt", ".swift", ".cc", ".cxx", ".hxx",
                      ".s", ".cmake", ".mk", ".xml", ".cs", ".sql", ".tsx", ".jsx"})
SKILL_SCRIPTS = ["validate_detailed_design_" + name + ".py" for name in
                 ("frontmatter", "format", "prose", "struct_comments", "boundaries", "content")]
SKILL_SCRIPTS += ["validate_diagram_labels.py"]


class CheckError(Exception):
    pass


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def canonical(path):
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def home():
    override = os.environ.get("CHANGE_CHECK_HOME") or os.environ.get("KB_CHECK_HOME")
    if override:
        return Path(override)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    current, legacy = base / "change-check", base / "kb-check"
    # Retain an installed registry instead of silently dropping registered projects.
    return legacy if (legacy / "registry.json").is_file() and not (current / "registry.json").exists() else current


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise CheckError(f"无法读取 JSON {path}: {exc}") from exc


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".change-check-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def lock(name, seconds=10):
    path = home() / "locks" / (digest(name)[:24] + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CheckError(f"检查或配置正被占用：{path}；确认原进程结束后可清除此锁")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def run(args, cwd=None, data=None, timeout=60, env=None, check=True):
    process = None
    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([str(x) for x in args], cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
        stdout, stderr = process.communicate(input=data, timeout=timeout)
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        process.communicate()
        raise CheckError(f"运行超时 {args[0]}，已终止本次进程树") from exc
    except OSError as exc:
        raise CheckError(f"运行失败 {args[0]}: {exc}") from exc
    if check and result.returncode:
        raise CheckError(result.stderr.decode("utf-8", "replace")[-3000:] or
                         f"{args[0]} 退出 {result.returncode}")
    return result


def git(root, *args, check=True):
    return run(["git", "-C", root, *args], check=check)


def roots():
    return read_json(home() / "registry.json", {"roots": []})["roots"]


def skill_candidates():
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    candidates = [codex / "skills/firmware-detailed-design",
                  Path.home() / ".agents/skills/firmware-detailed-design",
                  TOOL / "resources/skills/firmware-detailed-design"]
    return [str(p) for p in candidates if (p / "scripts").is_dir()]


def default_profile(root):
    from .resources import complete_profile, rule_sources
    root = Path(root)
    standard = next((str(p.relative_to(root)).replace("\\", "/") for p in
                     (root / "知识库规范", root / "公共知识库/知识库规范") if p.is_dir()), "")
    template = next((str(p.relative_to(root)).replace("\\", "/") for p in
                     (root / "模板/详设.md", root / "公共知识库/模板/详设.md") if p.is_file()), "")
    skills = skill_candidates()
    profile = {"modules": ["knowledge", "code", "config"] if standard else ["code", "config", "document"],
            "commands": [], "required_roles": [], "review_units": [], "code": {"c_style": True},
            "standards": standard, "template": template, "skill": skills[0] if skills else "",
            "design_globs": ["**/详设/*.md"], "requirement_globs": ["**/需求/*.md"],
            "exclude": ["归档/**", "**/归档/**", "Archive/**", "**/Archive/**", "Raw/**", "**/Raw/**"],
            "naming_exempt": ["README*", "_INDEX.md", "_DECISIONS.md", "SKILL.md",
                              "AGENTS.md", "PENDING-REVIEW.md", "ROADMAP.md"],
            "coverage_maps": [], "rule_sources": rule_sources(skills[0] if skills else "") if standard else [], "references": [],
            "body_review_limit": 300, "body_error_limit": 500,
            "prose_line_limit": 120, "prose_paragraph_limit": 200,
            "reviewer": {"kind": "claude", "timeout": 300},
            "agents": [], "git_hook": None}
    return complete_profile(profile)


def add_root(path):
    path = Path(path).expanduser().absolute()
    if not path.is_dir():
        raise CheckError(f"目录不存在：{path}")
    with lock("registry"):
        items = roots()
        for item in items:
            if canonical(item["path"]) == canonical(path):
                return item
        item = {"id": digest(canonical(path))[:12], "path": str(path),
                "physical_path": canonical(path), "profile": default_profile(path)}
        items.append(item)
        save_json(home() / "registry.json", {"roots": items})
    return item


def update_root(item):
    with lock("registry"):
        items = roots()
        for i, old in enumerate(items):
            if old["id"] == item["id"]:
                items[i] = item
                break
        else:
            raise CheckError("项目登记已被删除")
        save_json(home() / "registry.json", {"roots": items})


def find_root(path=None):
    target = canonical(path or Path.cwd())
    items = sorted(roots(), key=lambda x: len(canonical(x["path"])), reverse=True)
    for item in items:
        base = canonical(item["path"])
        if target == base or target.startswith(base + os.sep):
            return item
    raise CheckError(f"未登记项目：{target}；先执行 add")


def walk_files(root):
    seen = set()
    for directory, dirs, files in os.walk(root, followlinks=True):
        key = canonical(directory)
        if key in seen:
            dirs[:] = []
            continue
        seen.add(key)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".change-check"))
        for name in sorted(files):
            path = Path(directory) / name
            yield path.relative_to(root).as_posix(), path
