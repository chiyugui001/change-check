#!/usr/bin/env python3
"""Validate a detailed-design document Frontmatter against its current template."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

import yaml


SOURCE_FIELDS = ("source", "git_branch", "git_commit")
FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL
)
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
MODIFIED_FORMAT = "%Y-%m-%d %H:%M:%S"
ALLOWED_STATUS = {"draft", "active"}


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: yaml.Loader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def parse_frontmatter(path: Path) -> dict[str, Any]:
    """Read and parse a document's opening YAML Frontmatter."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc

    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError("opening YAML Frontmatter not found")

    try:
        data = yaml.load(match.group("body"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be a YAML mapping")
    if not all(isinstance(key, str) for key in data):
        raise ValueError("Frontmatter field names must be strings")
    return data


def is_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "<" not in value


def validate_values(data: dict[str, Any], source_backed: bool) -> list[str]:
    """Return human-readable field-value violations."""
    errors: list[str] = []

    if source_backed:
        for field in ("source", "git_branch"):
            if not is_nonempty_text(data.get(field)):
                errors.append(f"`{field}` must be a non-empty concrete string")
        commit = data.get("git_commit")
        if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
            errors.append("`git_commit` must be a 7-64 character hexadecimal commit ID")

    if not is_nonempty_text(data.get("reviewed_by")):
        errors.append("`reviewed_by` must be a non-empty string")

    modified = data.get("modified")
    try:
        dt.datetime.strptime(str(modified), MODIFIED_FORMAT)
    except (TypeError, ValueError):
        errors.append(f"`modified` must match {MODIFIED_FORMAT}")

    if data.get("status") not in ALLOWED_STATUS:
        errors.append("`status` must be `draft` or `active`")

    tags = data.get("tags")
    if not isinstance(tags, list) or not tags or not all(is_nonempty_text(tag) for tag in tags):
        errors.append("`tags` must be a non-empty list of non-empty strings")

    return errors


def print_items(title: str, values: list[str]) -> None:
    if values:
        print(f"  {title}: {', '.join(values)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate detailed-design Frontmatter against its current template."
    )
    parser.add_argument("document", type=Path, help="detailed-design Markdown document")
    parser.add_argument("--template", type=Path, required=True, help="current detailed-design template")
    parser.add_argument(
        "--non-source",
        action="store_true",
        help="validate a non-source detailed design; omit source/git fields together",
    )
    args = parser.parse_args()

    print("== Detailed-design Frontmatter validation ==")
    print(f"Document: {args.document}")
    print(f"Template: {args.template}")
    print(f"Profile: {'non-source' if args.non_source else 'source-backed'}")

    try:
        template_data = parse_frontmatter(args.template)
        document_data = parse_frontmatter(args.document)
    except ValueError as exc:
        print(f"[FAIL] YAML syntax: {exc}")
        print("RESULT: FAIL")
        return 1

    print("[PASS] YAML syntax")

    template_keys = list(template_data)
    if args.non_source:
        missing_source_template_fields = [
            field for field in SOURCE_FIELDS if field not in template_keys
        ]
        if missing_source_template_fields:
            print("[FAIL] 模板字段集合")
            print_items("模板缺少源码字段", missing_source_template_fields)
            print("[FAIL] 字段顺序（未检查：模板无完整源码字段组）")
            print("[FAIL] 字段值约束（未检查：模板无完整源码字段组）")
            print("RESULT: FAIL")
            return 1
        expected_keys = [key for key in template_keys if key not in SOURCE_FIELDS]
    else:
        expected_keys = template_keys

    actual_keys = list(document_data)
    missing = [key for key in expected_keys if key not in document_data]
    extra = [key for key in actual_keys if key not in expected_keys]
    set_ok = not missing and not extra
    if set_ok:
        print("[PASS] 模板字段集合")
    else:
        print("[FAIL] 模板字段集合")
        print_items("模板期望", expected_keys)
        print_items("文档实际", actual_keys)
        print_items("缺少", missing)
        print_items("模板外字段", extra)

    order_ok = actual_keys == expected_keys
    if order_ok:
        print("[PASS] 字段顺序")
    else:
        print("[FAIL] 字段顺序")
        print_items("模板顺序", expected_keys)
        print_items("文档顺序", actual_keys)

    value_errors = validate_values(document_data, source_backed=not args.non_source)
    if value_errors:
        print("[FAIL] 字段值约束")
        for error in value_errors:
            print(f"  - {error}")
    else:
        print("[PASS] 字段值约束")

    success = set_ok and order_ok and not value_errors
    print(f"RESULT: {'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
