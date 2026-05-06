from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from click.testing import CliRunner

from conductor.cli import main
from conductor.providers import (
    CallResponse,
    ClaudeProvider,
    CodexProvider,
    DeepSeekChatProvider,
    DeepSeekReasonerProvider,
    GeminiProvider,
    KimiProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderStalledError,
)
from conductor.router import reset_health
from conductor.session_log import SessionLog

if TYPE_CHECKING:
    from pathlib import Path


def _stub_all_configured(mocker, configured_names: set[str]) -> None:
    classes = {
        "kimi": KimiProvider,
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "deepseek-chat": DeepSeekChatProvider,
        "deepseek-reasoner": DeepSeekReasonerProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
        "openrouter": OpenRouterProvider,
    }
    for name, cls in classes.items():
        ok = name in configured_names
        mocker.patch.object(
            cls,
            "configured",
            lambda self, _ok=ok, _name=name: (
                _ok,
                None if _ok else f"{_name} stub not configured",
            ),
        )
        mocker.patch.object(
            cls,
            "health_probe",
            lambda self, timeout_sec=30.0, _ok=ok, _name=name: (
                _ok,
                None if _ok else f"{_name} preflight failed",
            ),
        )


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_session_meta(
    root: Path,
    *,
    session_id: str,
    updated_at: datetime,
    status: str = "finished",
    provider: str | None = "claude",
) -> None:
    sessions_root = root / "conductor" / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": f"run-{session_id}",
        "session_id": session_id,
        "log_path": str(sessions_root / f"{session_id}.ndjson"),
        "status": status,
        "started_at": (updated_at - timedelta(minutes=1)).isoformat(),
        "updated_at": updated_at.isoformat(),
        "finished_at": updated_at.isoformat() if status != "running" else None,
        "provider": provider,
        "explicit_log_path": False,
    }
    (sessions_root / f"run-{session_id}.meta.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _seed_session_filter_records(root: Path) -> datetime:
    now = datetime.now(UTC)
    for index in range(32):
        age = (
            timedelta(minutes=index)
            if index < 18
            else timedelta(hours=2, minutes=index)
        )
        _write_session_meta(
            root,
            session_id=f"sess-{index:02d}",
            updated_at=now - age,
            status="running" if index % 4 == 0 else "finished",
            provider="codex" if index % 3 == 0 else "claude",
        )
    return now


def _session_rows(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("sess-")]


def test_exec_log_file_writes_structured_ndjson_for_auto_route(
    mocker,
    monkeypatch,
    tmp_path: Path,
) -> None:
    reset_health()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _stub_all_configured(mocker, {"claude"})

    def _fake_exec(self, task, model=None, **kwargs):
        session_log = kwargs["session_log"]
        session_log.set_session_id("sess-auto-1")
        session_log.emit(
            "tool_call",
            {"provider": "claude", "name": "Read", "args": {"path": "README.md"}},
        )
        session_log.emit(
            "subagent_message",
            {"provider": "claude", "token_count": 23, "text": "working"},
        )
        return CallResponse(
            text="done",
            provider="claude",
            model="sonnet",
            duration_ms=321,
            usage={"input_tokens": 12, "output_tokens": 4, "thinking_tokens": 7},
            cost_usd=0.02,
            session_id="sess-auto-1",
            raw={},
        )

    mocker.patch.object(ClaudeProvider, "exec", _fake_exec)
    log_path = tmp_path / "session.ndjson"

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--tools",
            "Read",
            "--sandbox",
            "read-only",
            "--task",
            "review the diff",
            "--log-file",
            str(log_path),
        ],
    )

    assert result.exit_code == 0, result.output
    events = _read_events(log_path)
    assert all({"ts", "event", "data"} <= set(event) for event in events)
    kinds = [event["event"] for event in events]
    assert "route_decision" in kinds
    assert "provider_started" in kinds
    assert "tool_call" in kinds
    assert "subagent_message" in kinds
    assert "provider_finished" in kinds
    assert "usage" in kinds
    route_event = next(event for event in events if event["event"] == "route_decision")
    assert route_event["data"]["provider"] == "claude"
    usage_event = next(event for event in events if event["event"] == "usage")
    assert usage_event["data"]["usage"]["output_tokens"] == 4


def test_exec_default_log_file_uses_provider_session_id(
    mocker,
    monkeypatch,
    tmp_path: Path,
) -> None:
    reset_health()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _stub_all_configured(mocker, {"claude"})

    def _fake_exec(self, task, model=None, **kwargs):
        session_log = kwargs["session_log"]
        session_log.set_session_id("sess-default-1")
        session_log.emit(
            "subagent_message",
            {"provider": "claude", "token_count": 11, "text": "done"},
        )
        return CallResponse(
            text="done",
            provider="claude",
            model="sonnet",
            duration_ms=210,
            usage={"input_tokens": 8, "output_tokens": 2},
            cost_usd=0.01,
            session_id="sess-default-1",
            raw={},
        )

    mocker.patch.object(ClaudeProvider, "exec", _fake_exec)

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "claude", "--task", "write the patch"],
    )

    assert result.exit_code == 0, result.output
    sessions_root = tmp_path / "conductor" / "sessions"
    log_path = sessions_root / "sess-default-1.ndjson"
    assert log_path.exists()
    events = _read_events(log_path)
    assert events[-1]["event"] == "usage"
    assert {path.name for path in sessions_root.glob("*.ndjson")} == {"sess-default-1.ndjson"}


def test_sessions_list_and_tail_use_cached_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    finished = SessionLog(session_id="sess-finished-1")
    finished.bind_provider("claude")
    finished.emit("provider_started", {"provider": "claude"})
    finished.mark_finished()

    running = SessionLog(session_id="sess-running-1")
    running.bind_provider("codex")
    running.emit("provider_started", {"provider": "codex"})

    list_result = CliRunner().invoke(main, ["sessions", "list"])

    assert list_result.exit_code == 0, list_result.output
    assert "sess-finished-1" in list_result.output
    assert "finished" in list_result.output
    assert "sess-running-1" in list_result.output
    assert "running" in list_result.output

    tail_result = CliRunner().invoke(main, ["sessions", "tail", "sess-finished-1"])

    assert tail_result.exit_code == 0, tail_result.output
    tailed = [json.loads(line) for line in tail_result.output.splitlines()]
    assert tailed[0]["event"] == "provider_started"


def test_sessions_list_defaults_to_twenty_most_recent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list"])

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert len(rows) == 20
    assert rows[0].startswith("sess-00")
    assert rows[-1].startswith("sess-19")
    assert "sess-20" not in result.output


def test_sessions_list_last_limits_most_recent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--last", "5"])

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert len(rows) == 5
    assert [row.split()[0] for row in rows] == [
        "sess-00",
        "sess-01",
        "sess-02",
        "sess-03",
        "sess-04",
    ]


def test_sessions_list_since_filters_by_updated_at(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--since", "1h"])

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert len(rows) == 18
    assert "sess-17" in result.output
    assert "sess-18" not in result.output


def test_sessions_list_status_filters_records(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--status", "running"])

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert len(rows) == 8
    assert all("running" in row for row in rows)


def test_sessions_list_provider_filters_records(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--provider", "codex"])

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert len(rows) == 11
    assert all("codex" in row for row in rows)


def test_sessions_list_filters_compose_with_and_semantics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(
        main,
        ["sessions", "list", "--since", "1h", "--status", "running", "--provider", "codex"],
    )

    assert result.exit_code == 0, result.output
    rows = _session_rows(result.output)
    assert [row.split()[0] for row in rows] == ["sess-00", "sess-12"]


def test_sessions_list_all_returns_everything_and_ignores_last(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--all", "--last", "5"])

    assert result.exit_code == 0, result.output
    assert "[conductor] --all ignores --last" in result.stderr
    assert len(_session_rows(result.output)) == 32


def test_sessions_list_last_zero_returns_everything(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--last", "0"])

    assert result.exit_code == 0, result.output
    assert len(_session_rows(result.output)) == 32


def test_sessions_list_json_returns_parseable_array(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _seed_session_filter_records(tmp_path)

    result = CliRunner().invoke(main, ["sessions", "list", "--json", "--last", "5"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 5
    assert payload[0]["session_id"] == "sess-04"
    assert payload[-1]["session_id"] == "sess-00"
    assert set(payload[-1]) == {
        "run_id",
        "session_id",
        "log_path",
        "status",
        "started_at",
        "updated_at",
        "finished_at",
        "provider",
        "explicit_log_path",
    }


def test_exec_provider_stall_records_terminal_session_metadata(
    mocker,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch.object(ClaudeProvider, "health_probe", return_value=(True, None))

    def _fake_exec(self, task, model=None, **kwargs):
        session_log = kwargs["session_log"]
        session_log.emit(
            "error",
            {
                "provider": "claude",
                "reason": "no_provider_response_within_1s",
                "last_event": "provider_started",
            },
        )
        raise ProviderStalledError("claude CLI stalled after 1s with no output")

    mocker.patch.object(ClaudeProvider, "exec", _fake_exec)
    log_path = tmp_path / "session.ndjson"

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--max-stall-seconds",
            "1",
            "--task",
            "review the diff",
            "--log-file",
            str(log_path),
        ],
    )

    assert result.exit_code == 1
    events = _read_events(log_path)
    kinds = [event["event"] for event in events]
    assert "provider_started" in kinds
    assert "error" in kinds
    assert "provider_failed" in kinds

    sessions_root = tmp_path / "conductor" / "sessions"
    meta_paths = list(sessions_root.glob("*.meta.json"))
    assert len(meta_paths) == 1
    meta = json.loads(meta_paths[0].read_text(encoding="utf-8"))
    assert meta["status"] != "running"
    assert meta["finished_at"] is not None


def test_exec_provider_stall_prints_git_recovery_hint(
    mocker,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (repo / "notes.txt").write_text("partial work\n", encoding="utf-8")

    mocker.patch.object(ClaudeProvider, "health_probe", return_value=(True, None))

    def _fake_exec(self, task, model=None, **kwargs):
        raise ProviderStalledError("claude CLI stalled after 1s with no output")

    mocker.patch.object(ClaudeProvider, "exec", _fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--max-stall-seconds",
            "1",
            "--task",
            "review the diff",
            "--cwd",
            str(repo),
        ],
    )

    assert result.exit_code == 1
    assert "conductor: claude CLI stalled after 1s with no output" in result.stderr
    assert "Recoverable git state:" in result.stderr
    assert f"repo: {repo}" in result.stderr
    assert "branch: main" in result.stderr
    assert "upstream: none configured" in result.stderr
    assert "working tree: 1 changed path(s)" in result.stderr
    assert "changed: ?? notes.txt" in result.stderr


def test_sessions_tail_without_active_session_prints_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    result = CliRunner().invoke(main, ["sessions", "tail"])

    assert result.exit_code == 0
    assert result.output.strip() == "no active session"
