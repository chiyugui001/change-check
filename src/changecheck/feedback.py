"""Bounded, deduplicated hook feedback; full findings stay in the report."""
from __future__ import annotations

from collections import Counter

from .common import digest


def compact(value, limit=180):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def error_groups(findings):
    groups = {}
    for finding in findings:
        if finding.get("severity") != "ERROR":
            continue
        # Line shifts alone do not constitute a new problem. Locations remain in the report.
        key = digest({k: finding.get(k) for k in ("file", "rule", "message", "source")})
        groups.setdefault(key, finding)
    return groups


def prepare(state, report, warnings, event, stop_active, report_path):
    """Return (compact message, block Stop). This never approves or edits findings."""
    notice = state.setdefault("feedback", {})
    findings = report.get("findings", [])
    counts = Counter(f.get("severity") for f in findings)
    errors = error_groups(findings)
    current = set(errors)
    seen = set(notice.get("errors", [])) & current
    blocked = set(notice.get("blocked_errors", [])) & current
    unseen = current - seen
    warning_keys = {digest(w): w for w in warnings}
    new_warnings = [w for k, w in warning_keys.items() if k not in notice.get("warnings", [])]
    # Review candidates are summarized at Stop, never sent as an automatic repair command.
    review_counts = Counter((f.get("file"), f.get("rule")) for f in findings
                            if f.get("severity") == "REVIEW")
    review_key = digest(sorted((file, rule, count) for (file, rule), count in review_counts.items()))
    changed_reviews = bool(review_counts) and review_key != notice.get("review_summary")
    is_stop = event == "Stop"
    block = is_stop and not stop_active and bool(current - blocked)
    if is_stop:
        # One continuation per unresolved error group, including across subsequent user turns.
        blocked.update(current)
    if block:
        detail_keys = current
    elif unseen:
        detail_keys = unseen
    else:
        detail_keys = set()
    emit = bool(detail_keys or new_warnings or (is_stop and changed_reviews))
    notice["errors"] = sorted(current)
    notice["blocked_errors"] = sorted(blocked)
    notice["warnings"] = sorted(warning_keys)
    if not review_counts or is_stop:
        notice["review_summary"] = review_key
    if not emit:
        return "", False
    summary = (f"change-check：本会话 {len(report.get('changed', []))} 个修改文件，"
               f"{counts['ERROR']} ERROR、{counts['REVIEW']} REVIEW。")
    lines = [summary]
    if block:
        lines.append("存在确定错误，请按报告修正；同组错误只提醒继续一次，提交前仍须通过完整门禁。")
    elif detail_keys:
        lines.append("新增或变化的确定错误：")
    elif counts["REVIEW"]:
        lines.append("REVIEW 是待复核候选，不是确定错误；保留至显式 AI 审查或提交前复核，不要求机械改写。")
    for key in sorted(detail_keys)[:3]:
        f = errors[key]
        lines.append(compact(f"{f.get('file')}:{f.get('line', 1)} {f.get('rule')}：{f.get('message')}"))
    if len(detail_keys) > 3:
        lines.append(f"其余 {len(detail_keys) - 3} 组错误见完整报告。")
    if new_warnings:
        lines.append("范围或执行未确认：" + compact("；".join(new_warnings), 240))
    lines.append("完整报告：" + str(report_path) + "（cache.report；未确认或有候选不等于检查通过）")
    return "\n".join(lines), block


def render(agent, event, message, block):
    """Separate display-only Stop summaries from host continuation instructions."""
    if not message:
        return 0, "", ""
    import json
    if len(message) > 3500:
        message = message[:3400] + "\n更多登记目录的完整报告保留在本机 sessions/ 下。"
    if agent == "kimi":
        return (2, "", message) if block else (0, message, "")
    if block:
        value = {"decision": "block", "reason": message}
    elif event == "Stop":
        value = {"systemMessage": message}
    else:
        value = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}
    return 0, json.dumps(value, ensure_ascii=False), ""
