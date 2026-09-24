"""Resolve optional local rule sources before generated distribution resources."""
from pathlib import Path

from .common import CheckError, SKILL_SCRIPTS, TOOL

RESOURCE_ROOT = TOOL / "resources"
RULE_SKILLS = ("obsidian-kb", "knowledge-compiler", "requirement-decomposition", "firmware-detailed-design")
RULE_REFERENCES = {
    "knowledge-compiler": ("workflow.md", "templates.md", "compile-index.md"),
    "requirement-decomposition": ("method.md", "risk-analysis.md"),
    "firmware-detailed-design": ("content-review.md", "format-validation.md", "review-checklist.md"),
}


def rule_sources(skill):
    local = Path(skill).parent if skill else None
    result = []
    for name in RULE_SKILLS:
        candidates = ([local / name] if local else []) + [RESOURCE_ROOT / "skills" / name]
        folder = next((p for p in candidates if (p / "SKILL.md").is_file()), None)
        if folder:
            result.append(str(folder / "SKILL.md"))
            for reference in RULE_REFERENCES.get(name, ()):
                path = folder / "references" / reference
                if path.is_file():
                    result.append(str(path))
    return result


def complete_profile(profile):
    """Fill unconfigured resources only. Never replace a configured missing path."""
    if "knowledge" not in profile.get("modules", ["knowledge"]):
        return profile
    defaults = {"standards": RESOURCE_ROOT / "standards",
                "template": RESOURCE_ROOT / "templates" / "详设.md",
                "skill": RESOURCE_ROOT / "skills" / "firmware-detailed-design"}
    for key, path in defaults.items():
        if not profile.get(key) and path.exists():
            profile[key] = str(path)
    if not profile.get("requirement_template") and profile.get("template"):
        # A sibling requirement template follows the same source as the design template.
        requirement = Path(profile["template"]).with_name("需求.md")
        if not requirement.is_absolute() or requirement.is_file():
            profile["requirement_template"] = str(requirement)
    if "rule_sources" not in profile:
        profile["rule_sources"] = rule_sources(profile.get("skill"))
    return profile


def set_resource(profile, key, value):
    if key == "template" and profile.get("template"):
        previous = str(Path(profile["template"]).with_name("需求.md"))
        if profile.get("requirement_template") == previous:
            profile.pop("requirement_template")
    profile[key] = value
    if key == "skill":
        profile["rule_sources"] = rule_sources(value)
    complete_profile(profile)


def resource_issues(root, profile):
    if "knowledge" not in profile.get("modules", ["knowledge"]):
        return []
    root = Path(root)
    issues = []
    for key, leaf, label in (("standards", "frontmatter规范.md", "知识库规范"),
                             ("template", "", "详设模板"),
                             ("requirement_template", "", "需求模板")):
        value = profile.get(key)
        if not value or not (root / value / leaf).is_file():
            # Legacy local installations may not use requirement documents.
            if key != "requirement_template":
                issues.append(label + "缺失；请指定有效路径或使用含 resources 的独立包")
    skill = Path(profile.get("skill") or ".") / "scripts"
    issues.extend("缺少必检脚本：" + str(skill / name)
                  for name in SKILL_SCRIPTS if not (skill / name).is_file())
    return issues


def require_resources(root, profile):
    issues = resource_issues(root, profile)
    if issues:
        raise CheckError("\n".join(issues))
