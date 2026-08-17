"""Structured text presentation for DSH projections and catalogs."""

from __future__ import annotations

from typing import Any


def projection_values(session_row: dict[str, Any]) -> dict[str, Any]:
    return ((session_row.get("projections") or {}).get("values") or {})


def session_label(row: dict[str, Any]) -> str:
    values = projection_values(row)
    title = values.get("title") or "未命名会话"
    permission = (values.get("permissions") or {}).get("currentValue") or "?"
    pressure = values.get("contextPressure") or {}
    used = pressure.get("pressureTokens")
    window = pressure.get("contextWindow")
    context = f" context={used}/{window}" if used is not None and window is not None else ""
    return (
        f"{str(row.get('sessionId', '?'))[:16]} {'运行中' if row.get('running') else '空闲'} "
        f"{title} | permission={permission}{context}"
    )


def current_goal(history_value: dict[str, Any]) -> dict[str, Any] | None:
    projection = ((history_value.get("projections") or {}).get("values") or {}).get("goal")
    if not isinstance(projection, dict):
        return None
    goal = projection.get("goal")
    return goal if isinstance(goal, dict) else None
