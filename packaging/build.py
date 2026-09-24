"""Build a public, relocatable tree from allowlisted sources. Standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import zipfile

TOOL = Path(__file__).resolve().parents[1]
STANDARDS = ("frontmatter规范.md", "层级架构规范.md", "命名约定.md", "双链规范.md",
             "图表规范.md", "文件大小规范.md", "AI使用规范.md", "Git版本迭代规范.md")
SCRIPTS = tuple("validate_detailed_design_" + name + ".py" for name in
                ("frontmatter", "format", "prose", "struct_comments", "boundaries", "content")) + ("validate_diagram_labels.py",)
SKILLS = ("obsidian-kb", "knowledge-compiler", "requirement-decomposition", "firmware-detailed-design")
REFERENCES = {"knowledge-compiler": ("workflow.md", "templates.md", "compile-index.md"),
              "requirement-decomposition": ("method.md", "risk-analysis.md"),
              "firmware-detailed-design": ("content-review.md", "format-validation.md", "review-checklist.md")}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def public_markdown(raw, replacements=()):
    text = raw.decode("utf-8-sig")
    for before, after in replacements:
        text = text.replace(before.replace("\\", "/"), after.replace("\\", "/"))
        text = text.replace(before.replace("/", "\\"), after.replace("/", "\\"))
    for before, after in (("wt103-ai", "example-project"),
                          ("code/wt103", "code/example-project"), ("项目/WT103/", "项目/示例项目/")):
        text = text.replace(before, after)
    if text.startswith("---\n") or text.startswith("---\r\n"):
        parts = re.split(r"(?m)^---\r?$", text, maxsplit=2)
        if len(parts) == 3:
            parts[1] = re.sub(r"(?m)^reviewed_by: (?!AI-draft|<)[^\r\n]+", "reviewed_by: 原规范审核人", parts[1])
            text = "---".join(parts)
    return text.encode("utf-8")


def scan_public(files, forbidden_paths=()):
    """Reject common credentials and actual machine paths; never print matched values."""
    patterns = (rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
                rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})",
                rb"(?i)[a-z]:[\\/]users[\\/](?!<|example|public)[^\s\\/]+")
    paths = [str(p).encode("utf-8") for p in forbidden_paths]
    paths = [variant for value in paths for variant in (value, value.replace(b"\\", b"/"), value.replace(b"\\", b"\\\\"))]
    for name, raw in files.items():
        if any(re.search(pattern, raw) for pattern in patterns) or any(path in raw for path in paths):
            raise ValueError("发布内容含疑似凭据或本机路径，请检查文件：" + name)


def resource_names():
    names = {"standards/" + n for n in STANDARDS}
    names |= {"templates/详设.md", "templates/需求.md"}
    names |= {"skills/firmware-detailed-design/scripts/" + n for n in SCRIPTS}
    names |= {"skills/" + n + "/SKILL.md" for n in SKILLS}
    names |= {f"skills/{skill}/references/{name}" for skill, refs in REFERENCES.items() for name in refs}
    return names


def source_resources(config):
    config = Path(config).resolve()
    data = json.loads(config.read_text(encoding="utf-8-sig"))
    if set(data) != {"standards", "templates", "skills"} or set(data["skills"]) != set(SKILLS):
        raise ValueError("资源来源配置必须包含 standards、templates 和四项 skills")
    def source(value):
        path = Path(os.path.expandvars(value)).expanduser()
        return path if path.is_absolute() else config.parent / path
    files, entries = {}, {}
    for name in sorted(resource_names()):
        parts = name.split("/")
        path = (source(data[parts[0]]) / "/".join(parts[1:]) if parts[0] != "skills" else
                source(data["skills"][parts[1]]) / "/".join(parts[2:]))
        if not path.is_file():
            raise ValueError("资源源文件缺失：" + name)
        raw = path.read_bytes()
        content = public_markdown(raw) if name.endswith(".md") else raw
        files[name] = content
        entries[name] = {"source_sha256": sha(raw), "sha256": sha(content)}
    scan_public(files)
    manifest = {"format": 1, "generated": True, "transform": "public-markdown-v1",
                "instructions": "Generated from original sources; edit originals and rebuild, not these copies.",
                "files": entries}
    files["manifest.json"] = json_bytes(manifest)
    return files


def bundled_resources(tool):
    root = tool / "resources"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("format") != 1 or set(manifest.get("files", {})) != resource_names():
        raise ValueError("内置资源清单不完整；请使用 --sources 从权威源重新生成")
    result = {}
    for name, entry in manifest["files"].items():
        raw = (root / name).read_bytes()
        if sha(raw) != entry["sha256"]:
            raise ValueError("生成资源已被修改，请修改权威源后重新打包：" + name)
        result[name] = raw
    result["manifest.json"] = (root / "manifest.json").read_bytes()
    return result


def tool_files(tool):
    result = {}
    for name in ("README.md", "_INDEX.md", "install.ps1", "change-check.ps1", "requirements.txt", ".gitignore", ".gitattributes"):
        result[name] = (tool / name).read_bytes()
    for folder, extensions in (("src", {".py", ".txt"}), ("tests", {".py"}),
                               ("文档", {".md"}), ("packaging", {".py"})):
        for path in sorted((tool / folder).rglob("*")):
            if path.is_file() and path.suffix in extensions and "__pycache__" not in path.parts:
                result[path.relative_to(tool).as_posix()] = path.read_bytes()
    result["packaging/sources.example.json"] = (tool / "packaging/sources.example.json").read_bytes()
    for name in list(result):
        if name.endswith(".md"):
            replacements = [(str(tool), "C:/tools/change-check")]
            if tool.parent.name == "tools":
                replacements.append((str(tool.parent.parent), "C:/work/my-project"))
            result[name] = public_markdown(result[name], replacements)
    return result


def markdown_links(files):
    from urllib.parse import quote
    for name, raw in list(files.items()):
        if not name.endswith(".md"):
            continue
        def replace(match):
            target, _, label = match.group(1).replace("\\|", "|").partition("|")
            label = label or target
            target = target.removeprefix("tools/change-check/")
            base, _, fragment = target.partition("#")
            candidate = base + ("" if base.endswith(".md") else ".md")
            if not base:
                candidate = name
            if candidate not in files:
                candidates = [p for p in files if p.endswith("/" + candidate)]
                if len(candidates) != 1:
                    return label
                candidate = candidates[0]
            relative = os.path.relpath(candidate, str(Path(name).parent)).replace("\\", "/")
            anchor = (re.sub(r"[^\w\- ]", "", fragment).lower().replace(" ", "-") if not fragment.startswith("^")
                      else fragment[1:])
            return f"[{label}]({quote(relative)}" + ("#" + quote(anchor) if fragment else "") + ")"
        text = re.sub(r"\[\[([^\]]+)\]\]", replace, raw.decode("utf-8"))
        text = re.sub(r"(?m) \^([\w-]+)\r?$", r' <a id="\1"></a>', text)
        files[name] = text.encode("utf-8")


def build(tool, output, sources=None):
    tool, output = Path(tool).resolve(), Path(output).resolve()
    if output == tool or output in tool.parents:
        raise ValueError("输出目录不能是源码目录或其上级")
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录非空；请指定新的目录，避免覆盖已有发布内容")
    resources = source_resources(sources) if sources else bundled_resources(tool)
    files = tool_files(tool)
    files.update({"resources/" + name: raw for name, raw in resources.items()})
    # Resource hashes must describe exactly the shipped bytes, so keep their original links.
    documents = {name: raw for name, raw in files.items() if not name.startswith("resources/")}
    markdown_links(documents)
    files.update(documents)
    scan_public(files, (tool, Path.home()))
    files["SHA256SUMS.txt"] = ("\n".join(sha(raw) + "  " + name for name, raw in sorted(files.items())) + "\n").encode("utf-8")
    target = output / "change-check"
    target.mkdir(parents=True)
    for name, raw in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    archive = output / "change-check.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, raw in sorted(files.items()):
            entry = zipfile.ZipInfo("change-check/" + name, (2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(entry, raw)
    (output / "change-check.zip.sha256").write_text(sha(archive.read_bytes()) + "  change-check.zip\n", encoding="ascii")
    return {"directory": str(target), "archive": str(archive), "files": len(files),
            "resource_files": len(resources) - 1, "sha256": sha(archive.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description="生成独立发布包；不上传，不修改原规范或技能")
    parser.add_argument("--sources", help="本机权威源映射；省略则校验并复用包内 resources")
    parser.add_argument("--output", default=str(TOOL / "dist" / datetime.now().strftime("%Y%m%d-%H%M%S")))
    args = parser.parse_args()
    try:
        print(json.dumps(build(TOOL, args.output, args.sources), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("打包失败：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
