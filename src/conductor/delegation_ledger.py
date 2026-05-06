"""Append-only delegation accounting ledger.

The ledger is persisted derived state by design: upstream provider responses
and per-exec session logs are noisy, fragmented, and may be expensive or
impossible for operators to reconstruct later. This NDJSON stream is the
external truth for delegation billing and time/accounting visibility.

Storage is intentionally simple for schema version 1: one JSON object per line
at ``~/.cache/conductor/delegations.ndjson`` under ``offline_mode._cache_dir()``.
File rotation is future work; v1 only appends.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from conductor._time_filter import parse_timestamp, since_cutoff
from conductor.offline_mode import _cache_dir

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

SCHEMA_VERSION = 1
LEDGER_FILENAME = "delegations.ndjson"
COMMANDS = ("ask", "call", "review", "exec", "council")
STATUSES = ("ok", "error", "stalled", "timeout", "quota")

DelegationStatus = Literal["ok", "error", "stalled", "timeout", "quota"]
CouncilRole = Literal["parent", "member", "synthesis"]


@dataclass(frozen=True)
class DelegationEvent:
    delegation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    command: str = "call"
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    duration_ms: int | None = None
    status: DelegationStatus = "ok"
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    tags: list[str] = field(default_factory=list)
    session_log_path: str | None = None
    schema_version: int = SCHEMA_VERSION
    parent_delegation_id: str | None = None
    council_role: CouncilRole | None = None
    members: list[dict] | None = None
    synthesis_delegation_id: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        return {
            "delegation_id": payload.pop("delegation_id"),
            **payload,
        }


def ledger_path() -> Path:
    return _cache_dir() / LEDGER_FILENAME


def record_delegation(event: dict | DelegationEvent) -> None:
    """Append one delegation event, warning on failure without breaking dispatch."""
    payload = _normalize_event(event)
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, separators=(",", ":")))
            fh.write("\n")
    except OSError as e:
        print(f"[conductor] ledger write failed: {e}", file=sys.stderr)


def read_delegations(
    *,
    last: int | None = None,
    since: str | timedelta | None = None,
    command: str | None = None,
    provider: str | None = None,
    include_members: bool = False,
    delegation_id: str | None = None,
    path: Path | None = None,
) -> Iterable[dict]:
    events = list(_iter_events(path or ledger_path()))
    cutoff = since_cutoff(since)
    filtered = []
    for event in events:
        if not include_members and event.get("parent_delegation_id"):
            continue
        if delegation_id is not None and not _matches_delegation_id(event, delegation_id):
            continue
        if command is not None and event.get("command") != command:
            continue
        if provider is not None and event.get("provider") != provider:
            continue
        if cutoff is not None and parse_timestamp(event.get("timestamp")) < cutoff:
            continue
        filtered.append(event)
    if last is not None:
        filtered = filtered[-last:]
    return filtered


def _normalize_event(event: dict | DelegationEvent) -> dict:
    payload = event.to_dict() if isinstance(event, DelegationEvent) else dict(event)
    base = DelegationEvent().to_dict()
    base.update(payload)
    base["schema_version"] = SCHEMA_VERSION
    if not base.get("delegation_id"):
        base["delegation_id"] = uuid.uuid4().hex
    if not base.get("timestamp"):
        base["timestamp"] = datetime.now(UTC).isoformat()
    base["tags"] = list(base.get("tags") or [])
    return base


def _iter_events(path: Path) -> Iterable[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as e:
        print(f"[conductor] ledger read failed: {e}", file=sys.stderr)
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[conductor] ledger read skipped malformed line: {e}", file=sys.stderr)
            continue
        if isinstance(payload, dict):
            yield payload


def _matches_delegation_id(event: dict, delegation_id: str) -> bool:
    if event.get("delegation_id") == delegation_id:
        return True
    members = event.get("members") or []
    if isinstance(members, list):
        return any(
            isinstance(member, dict) and member.get("delegation_id") == delegation_id
            for member in members
        )
    return False
