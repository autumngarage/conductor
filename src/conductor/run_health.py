"""Run-health analysis for exec and council outcomes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

REPEAT_TOOL_ARG_THRESHOLD = 4
NO_EDIT_TOOL_CALL_THRESHOLD = 8
COUNCIL_DEGRADED_FAILURE_RATIO = 0.30


@dataclass(frozen=True)
class RunHealthSignal:
    code: str
    severity: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunHealthReport:
    status: str
    signals: tuple[RunHealthSignal, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "signals": [signal.to_dict() for signal in self.signals],
        }


def analyze_exec_session(log_path: Path | None) -> RunHealthReport:
    if log_path is None or not log_path.exists():
        return RunHealthReport("unknown")

    tool_counts: dict[str, int] = {}
    arg_counts: dict[tuple[str, str], int] = {}
    edit_events = 0
    signals: list[RunHealthSignal] = []
    for event in _iter_events(log_path):
        if event.get("event") != "tool_call":
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        if not name:
            continue
        tool_counts[name] = tool_counts.get(name, 0) + 1
        if name in {"Edit", "Write"}:
            edit_events += 1
        arg_key = (name, _stable_json(data.get("args")))
        arg_counts[arg_key] = arg_counts.get(arg_key, 0) + 1

    repeated = [
        {"tool": tool, "count": count}
        for (tool, _args), count in arg_counts.items()
        if count >= REPEAT_TOOL_ARG_THRESHOLD
    ]
    if repeated:
        signals.append(
            RunHealthSignal(
                "repeated-tool-args",
                "warning",
                "same tool arguments repeated across the run",
                {"repeated": repeated},
            )
        )
    total_tools = sum(tool_counts.values())
    if total_tools >= NO_EDIT_TOOL_CALL_THRESHOLD and edit_events == 0:
        signals.append(
            RunHealthSignal(
                "no-diff-progress",
                "warning",
                "many tool calls completed without Edit/Write progress",
                {"tool_calls": total_tools, "tool_counts": tool_counts},
            )
        )
    return RunHealthReport("degraded" if signals else "healthy", tuple(signals))


def council_degradation(raw: dict[str, Any]) -> RunHealthReport:
    council = raw.get("conductor_council") if isinstance(raw, dict) else None
    if not isinstance(council, dict):
        return RunHealthReport("unknown")
    requested = council.get("requested_member_models") or council.get("member_models") or []
    member_models = council.get("member_models") or []
    member_errors = council.get("member_errors") or []
    total = len(requested) if isinstance(requested, list) else 0
    completed = len(member_models) if isinstance(member_models, list) else 0
    failures = len(member_errors) if isinstance(member_errors, list) else 0
    not_reached = max(0, total - completed)
    failed_or_missing = failures + not_reached
    if total <= 0:
        return RunHealthReport("unknown")
    ratio = failed_or_missing / total
    if ratio < COUNCIL_DEGRADED_FAILURE_RATIO and council.get("cap_hit") is None:
        return RunHealthReport("healthy")
    signal = RunHealthSignal(
        "council-degraded",
        "warning",
        "council completed with failed or unreached members",
        {
            "failed_or_missing": failed_or_missing,
            "total_members": total,
            "failure_ratio": ratio,
            "member_errors": member_errors,
            "skipped_member_models": council.get("skipped_member_models") or [],
            "cap_hit": council.get("cap_hit"),
        },
    )
    return RunHealthReport("degraded", (signal,))


def degraded_council_prefix(report: RunHealthReport) -> str | None:
    if report.status != "degraded" or not report.signals:
        return None
    data = report.signals[0].data
    failed = int(data.get("failed_or_missing") or 0)
    total = int(data.get("total_members") or 0)
    if failed <= 0 or total <= 0:
        return None
    return (
        f"Council degraded: {failed}/{total} members failed or were not reached. "
        "Confidence is reduced; inspect raw.conductor_council for per-member details."
    )


def _iter_events(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _stable_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(value)
