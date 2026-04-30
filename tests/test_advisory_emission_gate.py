"""Regression tests for the advisory-emission gate at the CLI boundary.

The agent-wiring freshness notice is human-coaching, not a hard error.
Programmatic consumers (CliRunner tests, ``conductor call --json | jq``,
Touchstone, scripts) must not see it on stderr — parsing pipelines that
capture both streams will choke, and the JSON consumer contract guarantees
strict stderr silence on success.

These tests pin the gate behavior so the leak that broke six unrelated
tests under stale wiring (issue: stale-notice fired in non-TTY contexts)
cannot recur on the next version bump. The wiring state is mocked, so the
tests do not depend on whether the repo's own ``CLAUDE.md`` / ``AGENTS.md``
happen to be fresh or stale at run time.
"""

from __future__ import annotations

import sys

from click.testing import CliRunner

from conductor.cli import _advisory_emission_allowed, main

_FAKE_NOTICE = (
    "notice-key-abc",
    "[conductor] This repo's Conductor agent instructions are out of date.\n"
    "[conductor] AGENTS.md has conductor v0.0.0.\n"
    "[conductor] Refresh them with: conductor init --yes",
)


def _force_stale_notice(mocker):
    """Make ``conductor.agent_wiring`` claim a stale repo regardless of cwd.

    Pinning both the notice payload and ``should_emit_agent_wiring_notice``
    lets us test the CLI gate without relying on filesystem state — important
    because when the repo's wiring is freshened (PR A), a test that depended
    on real-state staleness would silently turn into a no-op.
    """
    mocker.patch(
        "conductor.agent_wiring.agent_wiring_notice",
        return_value=_FAKE_NOTICE,
    )
    mocker.patch(
        "conductor.agent_wiring.should_emit_agent_wiring_notice",
        return_value=True,
    )


def test_advisory_gate_suppresses_when_stderr_is_not_a_tty(mocker):
    """Programmatic callers (CI, CliRunner, pipes) get no advisory leak."""
    _force_stale_notice(mocker)

    result = CliRunner().invoke(main, ["list"])

    assert "[conductor] This repo" not in result.stderr
    assert "out of date" not in result.stderr


def test_advisory_gate_suppresses_when_json_mode_active(mocker, monkeypatch):
    """``--json`` callers contractually expect strict stderr silence even
    when stderr happens to still be on a TTY (e.g. ``conductor call --json
    | jq``)."""
    _force_stale_notice(mocker)
    # Pretend stderr is a TTY so the only remaining gate is the --json check.
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.argv", ["conductor", "list", "--json"])

    result = CliRunner().invoke(main, ["list", "--json"])

    assert "[conductor] This repo" not in result.stderr
    assert "out of date" not in result.stderr


def test_advisory_gate_emits_for_interactive_non_json_caller(mocker, monkeypatch):
    """Positive control: an interactive shell with stale wiring still gets
    the nudge — the gate is targeted, not a blanket suppression."""
    _force_stale_notice(mocker)
    # Simulate: stderr attached to TTY, no --json in argv.
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.argv", ["conductor", "list"])

    assert _advisory_emission_allowed() is True


def test_advisory_gate_helper_returns_false_under_pytest(monkeypatch):
    """Direct invariant on the helper: under pytest, stderr is captured and
    not a TTY, so the helper must short-circuit even before --json scanning."""
    # Force stderr.isatty -> False (the captured-stream default under pytest).
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("sys.argv", ["conductor", "list"])

    assert _advisory_emission_allowed() is False
