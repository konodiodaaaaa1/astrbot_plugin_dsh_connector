"""Helpers for inspecting and mutating DSH's namespaced settings."""

from __future__ import annotations

import json
from typing import Any


def namespace_map(description: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("ns")): row
        for row in description.get("namespaces") or []
        if isinstance(row, dict) and row.get("ns")
    }


def parse_json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def dotted_path(path: str) -> list[str]:
    result = [segment.strip() for segment in path.split(".") if segment.strip()]
    if not result:
        raise ValueError("设置路径为空")
    return result


def read_path(value: Any, path: list[str]) -> Any:
    current = value
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(".".join(path))
        current = current[segment]
    return current


def compact_json(value: Any, limit: int = 1000) -> str:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) > limit:
        return rendered[:limit] + "..."
    return rendered


def format_namespaces(description: dict[str, Any]) -> str:
    rows = namespace_map(description)
    if not rows:
        return "DSH 没有报告设置命名空间。"
    lines = [f"【DSH 设置】writable={description.get('writable')} namespaces={len(rows)}"]
    for name, row in rows.items():
        lines.append(
            f"- {name} [{row.get('applies', '?')}] rev={row.get('revision', '?')} {compact_json(row.get('value'), 500)}"
        )
    return "\n".join(lines)
