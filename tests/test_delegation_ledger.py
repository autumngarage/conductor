from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from conductor.cli import main
from conductor.delegation_ledger import DelegationEvent, read_delegations, record_delegation


def _event(**overrides) -> dict:
    payload = DelegationEvent(
        command="call",
        provider="codex",
        model="gpt-5.4",
        effort="medium",
        duration_ms=123,
        input_tokens=None,
        output_tokens=None,
        thinking_tokens=None,
        cached_tokens=None,
        cost_usd=None,
    ).to_dict()
    payload.update(overrides)
    return payload


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_schema_writes_all_commands_and_preserves_nulls(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    for command in ("ask", "call", "review", "exec", "council"):
        record_delegation(_event(command=command))

    rows = _read_lines(tmp_path / "conductor" / "delegations.ndjson")
    assert [row["command"] for row in rows] == ["ask", "call", "review", "exec", "council"]
    for row in rows:
        for field in (
            "delegation_id",
            "timestamp",
            "command",
            "provider",
            "model",
            "effort",
            "duration_ms",
            "status",
            "error",
            "input_tokens",
            "output_tokens",
            "thinking_tokens",
            "cached_tokens",
            "cost_usd",
            "tags",
            "session_log_path",
            "schema_version",
        ):
            assert field in row
        assert row["input_tokens"] is None
        assert row["cost_usd"] is None


def test_query_filters(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    now = datetime.now(UTC)
    rows = [
        _event(
            delegation_id="old",
            timestamp=(now - timedelta(hours=2)).isoformat(),
            command="call",
            provider="kimi",
        ),
        _event(
            delegation_id="a",
            timestamp=(now - timedelta(minutes=40)).isoformat(),
            command="ask",
            provider="codex",
        ),
        _event(
            delegation_id="b",
            timestamp=(now - timedelta(minutes=30)).isoformat(),
            command="exec",
            provider="claude",
        ),
        _event(
            delegation_id="c",
            timestamp=(now - timedelta(minutes=20)).isoformat(),
            command="exec",
            provider="codex",
        ),
        _event(
            delegation_id="d",
            timestamp=(now - timedelta(minutes=10)).isoformat(),
            command="review",
            provider="codex",
        ),
    ]
    for row in rows:
        record_delegation(row)

    assert [row["delegation_id"] for row in read_delegations(last=3)] == ["b", "c", "d"]
    assert [row["delegation_id"] for row in read_delegations(since="1h")] == ["a", "b", "c", "d"]
    assert [row["delegation_id"] for row in read_delegations(command="exec")] == ["b", "c"]
    assert [row["delegation_id"] for row in read_delegations(provider="codex")] == ["a", "c", "d"]


def test_council_default_list_hides_members_and_show_includes_member_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    members = [
        {"delegation_id": "member1", "provider": "openrouter", "model": "m1"},
        {"delegation_id": "member2", "provider": "openrouter", "model": "m2"},
        {"delegation_id": "member3", "provider": "openrouter", "model": "m3"},
    ]
    record_delegation(
        _event(
            delegation_id="parent",
            command="council",
            council_role="parent",
            members=members,
            synthesis_delegation_id="synth",
        )
    )
    for member in members:
        record_delegation(
            _event(
                delegation_id=member["delegation_id"],
                parent_delegation_id="parent",
                command="council",
                council_role="member",
            )
        )
    record_delegation(
        _event(
            delegation_id="synth",
            parent_delegation_id="parent",
            command="council",
            council_role="synthesis",
        )
    )

    assert [row["delegation_id"] for row in read_delegations()] == ["parent"]
    assert len(list(read_delegations(include_members=True))) == 5

    result = CliRunner().invoke(main, ["delegations", "show", "parent"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [member["delegation_id"] for member in payload["members"]] == [
        "member1",
        "member2",
        "member3",
    ]


def test_ledger_write_failure_warns_and_continues(monkeypatch, capsys):
    def fail_open(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", fail_open)

    record_delegation(_event())

    assert "[conductor] ledger write failed: disk full" in capsys.readouterr().err


def test_delegations_list_cli_filters(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for idx in range(5):
        record_delegation(_event(delegation_id=f"id{idx}", output_tokens=idx))

    result = CliRunner().invoke(main, ["delegations", "list", "--last", "3", "--json"])

    assert result.exit_code == 0, result.output
    assert [row["delegation_id"] for row in json.loads(result.output)] == [
        "id2",
        "id3",
        "id4",
    ]


def test_delegations_report_summarizes_token_efficiency(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    now = datetime.now(UTC).isoformat()
    record_delegation(
        _event(
            delegation_id="gemini-1",
            timestamp=now,
            command="exec",
            provider="gemini",
            model="gemini-2.5-pro",
            status="ok",
            duration_ms=20_000,
            input_tokens=10_000,
            output_tokens=200,
            thinking_tokens=1_000,
            cost_usd=None,
            tags=["tool-use", "code"],
            route={"provider": "codex"},
        )
    )
    record_delegation(
        _event(
            delegation_id="gemini-2",
            timestamp=now,
            command="exec",
            provider="gemini",
            model="gemini-2.5-pro",
            status="error",
            duration_ms=40_000,
            input_tokens=20_000,
            output_tokens=100,
            thinking_tokens=2_000,
            tags=["tool-use"],
        )
    )
    record_delegation(
        _event(
            delegation_id="codex-1",
            timestamp=now,
            command="ask",
            provider="codex",
            model="gpt-5.4",
            status="ok",
            duration_ms=90_000,
            input_tokens=30_000,
            output_tokens=3_000,
            thinking_tokens=0,
            cost_usd=0.6,
            tags=["code"],
        )
    )

    result = CliRunner().invoke(main, ["delegations", "report", "--since", "1h", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = {row["provider"]: row for row in payload["providers"]}
    assert rows["gemini"]["calls"] == 2
    assert rows["gemini"]["ok"] == 1
    assert rows["gemini"]["non_ok"] == 1
    assert rows["gemini"]["output_tokens_per_1k_input"] == 10.0
    assert rows["gemini"]["input_tokens_per_output_token"] == 100.0
    assert rows["gemini"]["ms_per_output_token"] == 200.0
    assert rows["codex"]["cost_per_1k_output_tokens"] == 0.2
    assert payload["route_fallbacks"] == {"codex->gemini": 1}


def test_delegations_report_tag_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    now = datetime.now(UTC).isoformat()
    record_delegation(
        _event(
            delegation_id="a",
            timestamp=now,
            provider="gemini",
            tags=["tool-use"],
            input_tokens=100,
            output_tokens=10,
        )
    )
    record_delegation(
        _event(
            delegation_id="b",
            timestamp=now,
            provider="codex",
            tags=["research"],
            input_tokens=100,
            output_tokens=10,
        )
    )

    result = CliRunner().invoke(
        main,
        ["delegations", "report", "--since", "1h", "--tag", "tool-use", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["window"]["events"] == 1
    assert [row["provider"] for row in payload["providers"]] == ["gemini"]


def test_delegations_report_preserves_multi_hop_fallback_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    now = datetime.now(UTC).isoformat()
    record_delegation(
        _event(
            delegation_id="multi-1",
            timestamp=now,
            command="review",
            provider="gemini",
            model="gemini-2.5-pro",
            status="ok",
            tags=["code-review"],
            route={"provider": "codex"},
            fallback_chain=["codex", "claude", "openrouter"],
        )
    )
    record_delegation(
        _event(
            delegation_id="single-1",
            timestamp=now,
            command="review",
            provider="gemini",
            model="gemini-2.5-pro",
            status="ok",
            tags=["code-review"],
            route={"provider": "codex"},
            fallback_chain=["codex"],
        )
    )

    result = CliRunner().invoke(main, ["delegations", "report", "--since", "1h", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["route_fallbacks"] == {
        "codex->claude->openrouter->gemini": 1,
        "codex->gemini": 1,
    }
