"""Regression tests for the conductor CLI contract documented in docs/consumers.md.

These tests fail when:
- A documented flag is removed or renamed (flag-surface tests).
- A documented JSON field is removed, renamed, or changes type (schema tests).

Additive changes (new flags, new fields) do NOT fail these tests — consumers
must ignore unknown fields per the contract. If a documented field becomes
optional, update both docs/consumers.md and the schema constants here in the
same PR. Major-bump only for breaking changes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

# Hardcoded stable flag surface for `conductor call`.
# This is the machine-readable form of the flag table in docs/consumers.md.
# When a flag is ADDED, add it here — but don't remove flags without a major version bump.
DOCUMENTED_STABLE_FLAGS: frozenset[str] = frozenset(
    {
        "--with",
        "--auto",
        "--tags",
        "--prefer",
        "--effort",
        "--exclude",
        "--brief",
        "--brief-file",
        "--task",
        "--task-file",
        "--model",
        "--json",
        "--verbose-route",
        "--silent-route",
        "--resume",
        "--offline",
        "--no-offline",
        "--profile",
    }
)

# Documented stable flag surface for `conductor exec`.
# Mirrors call's core flags plus exec-specific additions: tools, sandbox, cwd,
# timeout, max-stall-seconds, log-file, preflight, and allow-short-brief.
DOCUMENTED_EXEC_FLAGS: frozenset[str] = frozenset(
    {
        "--with",
        "--auto",
        "--tags",
        "--prefer",
        "--effort",
        "--exclude",
        "--brief",
        "--brief-file",
        "--task",
        "--task-file",
        "--model",
        "--json",
        "--verbose-route",
        "--silent-route",
        "--resume",
        "--offline",
        "--no-offline",
        "--profile",
        "--tools",
        "--sandbox",
        "--cwd",
        "--timeout",
        "--max-stall-seconds",
        "--log-file",
        "--preflight",
        "--no-preflight",
        "--allow-short-brief",
    }
)

CALL_RESPONSE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"text", "provider", "model", "duration_ms", "usage", "cost_usd", "session_id", "raw"}
)
USAGE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "thinking_tokens",
        "effort",
        "thinking_budget",
    }
)
ROUTE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "provider",
        "prefer",
        "effort",
        "thinking_budget",
        "tier",
        "task_tags",
        "matched_tags",
        "tools_requested",
        "sandbox",
        "ranked",
    }
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_call_schema(payload: dict[str, Any]) -> None:
    missing_top = CALL_RESPONSE_REQUIRED_FIELDS - payload.keys()
    assert not missing_top, f"Missing top-level fields in CallResponse: {missing_top}"

    usage = payload["usage"]
    missing_usage = USAGE_REQUIRED_FIELDS - usage.keys()
    assert not missing_usage, f"Missing fields in usage sub-object: {missing_usage}"


def _assert_call_types(payload: dict[str, Any]) -> None:
    assert isinstance(payload["text"], str)
    assert isinstance(payload["provider"], str)
    assert isinstance(payload["model"], str)
    assert isinstance(payload["duration_ms"], int)
    assert isinstance(payload["raw"], dict)
    assert payload["cost_usd"] is None or isinstance(payload["cost_usd"], (int, float))
    assert payload["session_id"] is None or isinstance(payload["session_id"], str)

    usage = payload["usage"]
    for key in ("input_tokens", "output_tokens", "cached_tokens", "thinking_tokens"):
        assert usage[key] is None or isinstance(usage[key], int), (
            f"usage.{key} must be int | null, got {type(usage[key])}"
        )
    assert usage["effort"] is None or usage["effort"] in {
        "minimal",
        "low",
        "medium",
        "high",
        "max",
    }, f"usage.effort must be a known level or null, got {usage['effort']!r}"
    assert usage["thinking_budget"] is None or isinstance(usage["thinking_budget"], int)


def _conductor_configured() -> bool:
    """Return True if at least one provider is configured, used to gate live tests."""
    try:
        result = subprocess.run(
            ["conductor", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        providers = json.loads(result.stdout)
        return any(p.get("configured") for p in providers)
    except (json.JSONDecodeError, TypeError, OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Flag-surface assertions (no LLM call)
# ---------------------------------------------------------------------------


class TestFlagSurface:
    """Assert every documented stable flag appears in --help output.

    These tests parse subprocess output so they exercise the real CLI entry
    point, not just Click internals. They run without provider auth on every CI
    build.
    """

    def test_call_has_all_documented_stable_flags(self) -> None:
        result = subprocess.run(
            ["conductor", "call", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"conductor call --help failed: {result.stderr}"
        help_text = result.stdout
        missing = {f for f in DOCUMENTED_STABLE_FLAGS if f not in help_text}
        assert not missing, (
            f"Documented flags missing from 'conductor call --help': {sorted(missing)}"
        )

    def test_exec_has_all_documented_stable_flags(self) -> None:
        result = subprocess.run(
            ["conductor", "exec", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"conductor exec --help failed: {result.stderr}"
        help_text = result.stdout
        missing = {f for f in DOCUMENTED_EXEC_FLAGS if f not in help_text}
        assert not missing, (
            f"Documented flags missing from 'conductor exec --help': {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# JSON output schema assertions (fixture-based, no LLM call)
# ---------------------------------------------------------------------------


class TestJsonSchema:
    """Assert the documented CallResponse JSON schema holds against a committed fixture.

    The fixture (tests/fixtures/call_response_claude.json) is a real captured
    response with the unstable `raw` internals trimmed to a minimal shape.
    Tests run without provider auth.
    """

    def _load_fixture(self) -> dict[str, Any]:
        return json.loads((_FIXTURE_DIR / "call_response_claude.json").read_text())

    def test_fixture_required_fields_present(self) -> None:
        _assert_call_schema(self._load_fixture())

    def test_fixture_top_level_types(self) -> None:
        _assert_call_types(self._load_fixture())

    def test_fixture_no_route_field_on_explicit_with(self) -> None:
        fixture = self._load_fixture()
        assert "route" not in fixture, (
            "'route' field must not appear in --with responses; only --auto adds it"
        )


# ---------------------------------------------------------------------------
# Live integration test (skipped without provider auth)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_call_matches_documented_schema() -> None:
    """Invoke conductor for real and validate the JSON output against the contract.

    Skipped automatically when no provider is configured. Runs locally for
    developers with auth and can be enabled in CI via provider secrets.
    """
    if not _conductor_configured():
        pytest.skip("no provider configured")

    result = subprocess.run(
        [
            "conductor",
            "call",
            "--with",
            "claude",
            "--json",
            "--silent-route",
            "--task",
            "say: hi",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"conductor call failed:\n{result.stderr}"
    response = json.loads(result.stdout)

    _assert_call_schema(response)
    _assert_call_types(response)

    assert response["provider"] == "claude"
    assert response["text"], "response text must be non-empty"
    assert response["duration_ms"] > 0
    assert "route" not in response, "'route' must not appear on --with calls"
