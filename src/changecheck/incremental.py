"""Declared review boundaries with conservative fallback for unassigned changes."""
import copy
from datetime import datetime, timezone

from .common import CheckError, digest, home, read_json
from .project import CONFIG, matches
from .storage import policy, save_artifact


def units(snap, profile, report):
    definitions = profile.get("review_units", [])
    universe = snap.names | set(snap.before) | snap.changed
    owners = {}
    for unit in definitions:
        for name in universe:
            if matches(name, unit["include"]):
                if name in owners:
                    raise CheckError("审查单元 include 重叠：" + name)
                owners[name] = unit["id"]
    common = {n for n in universe if n == CONFIG or n in snap.external or
              n.rsplit("/", 1)[-1] in ("AGENTS.md", "CLAUDE.md")}
    planned = []
    assigned = set()
    for unit in definitions:
        owned = {n for n, owner in owners.items() if owner == unit["id"]}
        context = owned | common | {n for n in universe if matches(n, unit["dependencies"])}
        triggers = snap.changed & context
        if not triggers:
            continue
        # Dependency changes require checking the impacted unit even if its own files did not change.
        targets = (snap.changed & owned) or owned
        if not targets:
            continue
        planned.append((unit["id"], targets, context))
        assigned.update(targets & snap.changed)
    remainder = (snap.changed | {f["file"] for f in report["findings"] if f["severity"] == "REVIEW"}) - assigned
    if remainder:
        planned.append(("unassigned", remainder, universe))
    return planned


def review_units(snap, item, report, reviewer):
    from .review import (invoke_reviewer, review_cache_enabled, required_rules,
                         validate_review, verify_unchanged)
    results = []
    enabled = review_cache_enabled(reviewer)
    mode = "staged" if snap.staged else "worktree"
    for name, targets, context in units(snap, report["policy"], report):
        partial = copy.copy(snap)
        partial.data = {n: raw for n, raw in snap.data.items() if n in context}
        partial.names = snap.names & context
        partial.changed = set(targets)
        partial.before = {n: raw for n, raw in snap.before.items() if n in context}
        partial.identities = {n: raw for n, raw in snap.identities.items() if n in context}
        subreport = {**report, "changed": sorted(targets), "review_unit": name,
                     "findings": [f for f in report["findings"] if f["file"] in targets],
                     "writing_files": [n for n in report["writing_files"] if n in targets],
                     "review_context": sorted(context)}
        subreport["ai_rules"] = required_rules(subreport)
        fingerprint = digest({"unit": name, "mode": mode, "targets": sorted(targets),
                              "context": partial.identities, "names": sorted(partial.names),
                              "baseline": {n: digest(raw) for n, raw in partial.before.items()},
                              "rules": report["implementation"], "findings": subreport["findings"]})
        cache = home() / "reviews" / item["id"] / mode / (fingerprint + ".json")
        fresh = cache.exists() and cache.stat().st_mtime >= datetime.now(timezone.utc).timestamp() - policy()["days"] * 86400
        existing = read_json(cache) if enabled and fresh else None
        cached = isinstance(existing, dict) and existing.get("fingerprint") == fingerprint
        result = existing["result"] if cached else invoke_reviewer(partial, item, subreport, reviewer)
        validate_review(result, subreport, partial)
        verify_unchanged(snap, item, report)
        if enabled and not cached:
            save_artifact(cache, {"mode": mode, "unit": name, "fingerprint": fingerprint,
                                  "result": result, "approved_at": datetime.now(timezone.utc).isoformat()})
        results.append({"unit": name, "files": sorted(targets), "cached": cached, "result": result})
    if not set(report["changed"]) <= {n for result in results for n in result["files"]}:
        raise CheckError("增量审查未覆盖全部变动")
    return {"approved": True, "checked_files": report["changed"], "units": results,
            "summary": "全部受影响审查单元通过"}, all(r["cached"] for r in results)
