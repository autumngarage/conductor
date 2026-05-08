"""Weighted accounting for Conductor-managed tool-use budgets.

The iteration cap is an operator-facing "units of agent work" budget, not a
literal syscall counter. Read-only discovery is intentionally cheap because
late-stage agent work often converges through many small Read/Glob/Grep calls.
Writes are heavier because they mutate the repo, and verbose Bash is heavier
because command output usually means the model must spend a larger reasoning
turn interpreting logs. Unknown tools keep the legacy 1.0 unit cost so new
event shapes fail conservatively instead of silently extending the budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SMALL_READ_MAX_LINES = 200
SMALL_EDIT_MAX_CHANGED_LINES = 20
SMALL_WRITE_MAX_LINES = 100
SMALL_BASH_MAX_OUTPUT_CHARS = 500


@dataclass
class _ObservedToolCall:
    name: str | None
    args: dict[str, Any]
    weight: float


@dataclass
class ToolBudgetCounter:
    """Accumulate raw and weighted tool-call usage from provider events."""

    raw_count: int = 0
    weighted_total: float = 0.0
    _calls_by_id: dict[str, _ObservedToolCall] = field(default_factory=dict)
    _unmatched_calls: list[_ObservedToolCall] = field(default_factory=list)

    def observe_event(self, event: dict[str, Any]) -> None:
        start = _tool_call_start(event)
        if start is not None:
            call_id, name, args = start
            weight = tool_call_weight(name, args=args)
            started_call = _ObservedToolCall(name=name, args=args, weight=weight)
            self.raw_count += 1
            self.weighted_total += weight
            if call_id:
                self._calls_by_id[call_id] = started_call
            else:
                self._unmatched_calls.append(started_call)
            return

        result = _tool_call_result(event)
        if result is None:
            return
        call_id, output = result
        completed_call = self._pop_observed_call(call_id)
        if completed_call is None:
            return
        final_weight = tool_call_weight(
            completed_call.name,
            args=completed_call.args,
            result=output,
        )
        self.weighted_total += final_weight - completed_call.weight
        completed_call.weight = final_weight

    def _pop_observed_call(self, call_id: str | None) -> _ObservedToolCall | None:
        if call_id:
            observed = self._calls_by_id.pop(call_id, None)
            if observed is not None:
                return observed
        if self._unmatched_calls:
            return self._unmatched_calls.pop(0)
        return None


def tool_call_weight(
    name: str | None,
    *,
    args: dict[str, Any] | None = None,
    result: str | None = None,
) -> float:
    args = args or {}
    if name == "Read":
        if result is not None:
            return 0.3 if _line_count(result) <= SMALL_READ_MAX_LINES else 1.0
        requested_lines = _requested_read_lines(args)
        if requested_lines is None or requested_lines <= SMALL_READ_MAX_LINES:
            return 0.3
        return 1.0
    if name in {"Glob", "Grep"}:
        return 0.3
    if name == "Edit":
        return 0.6 if _edit_changed_lines(args) <= SMALL_EDIT_MAX_CHANGED_LINES else 1.0
    if name == "Write":
        content_lines = _line_count(str(args.get("content") or ""))
        return 1.0 if content_lines <= SMALL_WRITE_MAX_LINES else 2.0
    if name == "Bash":
        if result is None:
            return 0.5
        return 0.5 if len(result) <= SMALL_BASH_MAX_OUTPUT_CHARS else 1.5
    return 1.0


def _tool_call_start(
    event: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, Any]] | None:
    kind = event.get("type")
    raw_item = event.get("item")
    item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
    item_type = item.get("type")
    if kind == "tool_use":
        return (
            _string_or_none(event.get("call_id") or event.get("id")),
            _string_or_none(event.get("name") or event.get("tool_name")),
            _coerce_args(event.get("arguments") or event.get("args")),
        )
    if kind == "item.completed" and item_type in {
        "function_call",
        "tool_use",
        "tool_call",
    }:
        return (
            _string_or_none(item.get("call_id") or item.get("id")),
            _string_or_none(item.get("name") or item.get("tool_name")),
            _coerce_args(item.get("arguments") or item.get("args")),
        )
    return None


def _tool_call_result(event: dict[str, Any]) -> tuple[str | None, str] | None:
    kind = event.get("type")
    raw_item = event.get("item")
    item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
    item_type = item.get("type")
    if kind == "tool_result":
        return (
            _string_or_none(event.get("call_id") or event.get("id")),
            _result_text(event),
        )
    if kind == "item.completed" and item_type in {
        "function_call_output",
        "tool_result",
    }:
        return (
            _string_or_none(item.get("call_id") or item.get("id")),
            _result_text(item),
        )
    return None


def _coerce_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _result_text(container: dict[str, Any]) -> str:
    for key in ("output", "result", "content"):
        value = container.get(key)
        if isinstance(value, str):
            return value
    return ""


def _requested_read_lines(args: dict[str, Any]) -> int | None:
    for key in ("limit", "line_count", "lines"):
        value = args.get(key)
        if isinstance(value, int) and value > 0:
            return value
    start = args.get("start_line")
    end = args.get("end_line")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return end - start + 1
    return None


def _edit_changed_lines(args: dict[str, Any]) -> int:
    old = str(args.get("old_string") or "")
    new = str(args.get("new_string") or "")
    return max(_line_count(old), _line_count(new))


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
