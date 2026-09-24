"""Daily script checkpoints and outstanding findings; never used by the staged gate."""
from __future__ import annotations

import base64
import copy
import difflib
import hashlib
import re
from pathlib import Path

from .checks import extract_links, resolve_link
from .common import MAX_BYTES, CheckError, digest
from .project import matches, module_for
from .context import snapshot_name

LOCAL_RULES = {"WR001", "C001"}
CHANGE_RULES = {"D004", "KB007", "KC003"}


def line_delta(before, after):
    old = before.decode("utf-8-sig", "replace").splitlines()
    new = after.decode("utf-8-sig", "replace").splitlines()
    mapping, ranges, touched = {}, [], set()
    for kind, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if kind == "equal":
            mapping.update((i + 1, c + i - a + 1) for i in range(a, b))
        else:
            ranges.append({"kind": kind, "before": [a + 1, b], "after": [c + 1, d]})
            touched.update(range(c + 1, d + 1))
    # Changes to Markdown parsing context can affect otherwise equal source lines.
    structural = any(re.search(r"<!--|-->|^\s*(?:#{1,6}\s|`{3}|~{3}|---\s*$)", line)
                     for r in ranges for lines, key in ((old, "before"), (new, "after"))
                     for line in lines[max(0, r[key][0] - 1):r[key][1]])
    return {"ranges": ranges, "lines": sorted(touched), "mapping": mapping,
            "markdown_context_changed": structural}


def dependencies(name, snap, profile):
    """Known cross-file inputs. Unknown project commands still run as whole units."""
    values = {}
    if module_for(name, profile) == "knowledge":
        if name in snap.data:
            for _, target, wiki in extract_links(snap.text(name)):
                resolved, anchor, error = resolve_link(name, target, snap.names, wiki)
                values[target] = [resolved, anchor, error, snap.identities.get(resolved)]
        for value in (profile.get("pending", "PENDING-REVIEW.md"),
                      profile.get("ledger", "AI/Index/compile_index.json"),
                      *profile.get("coverage_maps", [])):
            values[value] = snap.identities.get(value)
        parent = Path(name).parent
        while True:
            index = (parent / "_INDEX.md").as_posix()
            values[index] = snap.identities.get(index)
            if parent == Path("."):
                break
            parent = parent.parent
    for unit in profile.get("review_units", []):
        if matches(name, unit["include"]):
            values["unit:" + unit["id"]] = {n: snap.identities.get(n) for n in sorted(snap.names)
                                               if matches(n, unit["dependencies"])}
    return digest(values)


def policy_key(snap, profile, implementation):
    evidence = {}
    for field in ("standards", "template", "requirement_template"):
        prefix = snapshot_name(profile.get(field))
        if prefix:
            evidence.update({n: h for n, h in snap.identities.items()
                             if n == prefix or n.startswith(prefix.rstrip("/") + "/")})
    return digest([implementation, evidence, snap.external])


def prepare(snap, state, profile, implementation):
    changed = set(snap.changed)
    checkpoints = state.get("checkpoints", {})
    key = policy_key(snap, profile, implementation)
    invalidated = state.get("checkpoint_policy") not in (None, key)
    expanded, uncertain = [], []
    for name, checkpoint in checkpoints.items():
        if name in changed:
            continue
        current = snap.identities.get(name)
        if name in snap.names and name not in snap.data:
            with (snap.root / name).open("rb") as stream:
                current = hashlib.file_digest(stream, "sha256").hexdigest()
        if current != checkpoint["hash"]:
            uncertain.append(name)
            continue  # External changes are not attributed to this session.
        if invalidated or checkpoint.get("dependencies") != dependencies(name, snap, profile):
            expanded.append(name)
            snap.changed.add(name)
            if name in snap.names and name not in snap.data:
                snap.identities[name] = current
            if "body" in checkpoint:
                snap.before[name] = base64.b64decode(checkpoint["body"])
    snap.delta = {}
    for name in snap.changed:
        previous = checkpoints.get(name)
        before = snap.before.get(name, b"")
        after = snap.data.get(name, b"")
        delta = line_delta(before, after)
        delta["reuse_local"] = bool(previous and not invalidated and name not in expanded)
        # Unchanged lines are reusable only against the exact previous checked body.
        delta["reuse_local"] &= bool(previous and previous["hash"] == digest(before))
        snap.delta[name] = delta
    snap.scope.update({"baseline": "last_script_check", "delta_files": sorted(changed),
                       "expanded_files": expanded, "policy_invalidated": invalidated,
                       "unconfirmed_files": uncertain,
                       "ranges": {n: v["ranges"] for n, v in snap.delta.items()}})
    return key


def local_lines(snap, name, rule):
    delta = snap.delta.get(name)
    if not delta or not delta["reuse_local"]:
        return None
    if rule == "WR001" and delta["markdown_context_changed"]:
        return None
    return set(delta["lines"])


def historical(finding, snap):
    value = copy.deepcopy(finding)
    if "origin" not in value:
        name = value["file"]
        value["origin"] = {"before_hash": digest(snap.before.get(name, b"")),
                           "after_hash": snap.identities.get(name), "kind": "change_candidate"}
        for field in ("primary", "related"):
            if value.get(field):
                value[field]["snapshot"] = "historical"
                value[field]["content_hash"] = value["origin"][
                    "before_hash" if field == "primary" and value["rule"] == "D004" else "after_hash"]
    return value


def restored(finding, snap):
    # Only a byte-identical full restoration proves the original context is back.
    # The same words moved to another section do not resolve a preservation issue.
    if finding["rule"] != "D004" or finding["file"] not in snap.data:
        return False
    return digest(snap.data[finding["file"]]) == finding.get("origin", {}).get("before_hash")


def finish(snap, state, report, profile, key):
    previous = state.get("cache", {}).get("report", {})
    fresh = report["findings"]
    kept = []
    for original in previous.get("findings", []):
        f = copy.deepcopy(original)
        name, rule = f["file"], f["rule"]
        if rule in CHANGE_RULES:
            if "origin" not in f:
                # Upgrade legacy reports without mislabelling their old baseline as
                # the newly advanced checkpoint. The session still owns that body.
                tracked = state.get("files", {}).get(name, {})
                f["origin"] = {"kind": "legacy_change_candidate",
                               "before_hash": tracked.get("before", {}).get("hash"),
                               "after_hash": state.get("changes", {}).get(name, {}).get("before", {}).get(
                                   "hash", tracked.get("after"))}
                for field in ("primary", "related"):
                    if f.get(field):
                        f[field]["snapshot"] = "historical"
                        f[field]["content_hash"] = f["origin"][
                            "before_hash" if field == "primary" and rule == "D004" else "after_hash"]
            if restored(f, snap):
                continue
            # A fresh authorization candidate replaces an older one for this file/rule.
            if rule != "D004" and any(v["file"] == name and v["rule"] == rule for v in fresh):
                continue
            if rule == "KB007" and name in snap.changed:
                from .checks import frontmatter
                try:
                    if "version" not in frontmatter(snap.text(name))[0]:
                        continue
                except (CheckError, ValueError, KeyError):
                    pass
            kept.append(f)
        elif name not in snap.changed:
            if rule != "KB003" or [name, f["line"]] not in snap.checked_links:
                kept.append(f)
        elif rule in LOCAL_RULES:
            lines = local_lines(snap, name, rule)
            line = snap.delta[name]["mapping"].get(f["line"])
            if lines is not None and line is not None and line not in lines:
                f["line"] = line
                kept.append(f)
    for f in fresh:
        if f["rule"] in CHANGE_RULES:
            f = historical(f, snap)
        kept.append(f)
    unique = {}
    for f in kept:
        identity = digest({k: f.get(k) for k in ("file", "line", "rule", "message", "primary", "related", "origin")})[:20]
        f["id"] = identity
        unique[identity] = f
    report["findings"] = sorted(unique.values(), key=lambda f: (f["file"], f["line"], f["rule"]))
    report["exit_code"] = int(any(f["severity"] in ("ERROR", "REVIEW") for f in report["findings"]))
    report["incremental"] = {"baseline": "last_script_check", "checked_files": sorted(snap.changed),
                             "reused_files": sorted(set(state.get("checkpoints", {})) - snap.changed -
                                                    set(snap.scope["unconfirmed_files"])),
                             "retained_findings": len(kept) - len(fresh),
                             "local_rules": sorted(LOCAL_RULES),
                             "context_checks": "syntax_structure_links_and_project_commands"}
    checkpoints = copy.deepcopy(state.get("checkpoints", {}))
    for name in snap.changed:
        checkpoints[name] = {"hash": snap.identities.get(name), "dependencies": dependencies(name, snap, profile)}
        if name in snap.data:
            checkpoints[name]["body"] = base64.b64encode(snap.data[name]).decode("ascii")
    if sum(len(v.get("body", "")) * 3 // 4 for v in checkpoints.values()) > MAX_BYTES:
        raise CheckError("会话已检查文本超过 40 MiB，未推进检查基线；请拆分登记目录")
    # Commit the checkpoint only after every required checker returned valid output.
    state["checkpoints"] = checkpoints
    state["checkpoint_policy"] = key
    for name in snap.scope["delta_files"]:
        state.get("changes", {}).pop(name, None)
    state["cache"] = {"report": report}
    state["feedback_pending"] = True
    return report
