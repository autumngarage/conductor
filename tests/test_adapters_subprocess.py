"""Tests for the subprocess-based adapters (claude, codex, gemini).

All three call external CLIs. We stub ``subprocess.run`` and
``shutil.which`` so tests run with no dependencies on the real binaries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

import pytest

from conductor.providers.claude import ClaudeProvider
from conductor.providers.codex import CodexProvider
from conductor.providers.gemini import GEMINI_AUTH_ENV_VARS, GeminiProvider
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderStalledError,
)
from conductor.session_log import SessionLog


def _strip_gemini_auth_env(monkeypatch) -> None:
    """Clear any GEMINI/GOOGLE auth env vars inherited from the host shell.

    Without this, tests that exercise the OAuth-file path silently
    succeed via the env-var fast path on developer machines.
    """
    for var in GEMINI_AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["stub"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

CLAUDE_JSON = """{
    "result": "hello from claude",
    "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 2},
    "duration_ms": 1234,
    "total_cost_usd": 0.002,
    "session_id": "abc"
}"""


def test_claude_configured_when_cli_present_and_authed(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout='{"loggedIn": true}'),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is True and reason is None


def test_claude_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    # Reason names the actionable login command + env-var fallback.
    assert "claude auth login" in reason
    assert "ANTHROPIC_API_KEY" in reason


def test_claude_configured_false_when_not_authed(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout='{"loggedIn": false}'),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "claude auth login" in reason
    assert "ANTHROPIC_API_KEY" in reason


def test_claude_configured_false_when_auth_probe_exits_nonzero(mocker):
    """Older CLI versions without `auth status` exit non-zero — surface,
    don't silently treat as authed."""
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(
            stderr="error: unknown command 'auth'", returncode=2
        ),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "exited 2" in reason
    assert "upgrade" in reason.lower()


def test_claude_configured_false_when_auth_probe_times_out(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "could not verify" in reason


def test_claude_configured_false_when_auth_probe_returns_non_json(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout="hello not json"),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not JSON" in reason


def test_claude_call_returns_normalized_response(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )

    response = ClaudeProvider().call("hi")

    assert response.text == "hello from claude"
    assert response.provider == "claude"
    assert response.model == "sonnet"
    assert response.usage["input_tokens"] == 10
    assert response.usage["output_tokens"] == 3
    assert response.usage["cached_tokens"] == 2
    assert response.usage["effort"] == "medium"
    assert response.usage["thinking_budget"] == 8_000
    assert response.cost_usd == 0.002
    assert response.duration_ms == 1234
    assert response.session_id == "abc"


def test_claude_call_passes_resume_session_id_as_resume_flag(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    captured = mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )
    ClaudeProvider().call("hi", resume_session_id="abc-123")
    args = captured.call_args.args[0]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "abc-123"


def test_claude_call_omits_resume_flag_when_session_id_none(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    captured = mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )
    ClaudeProvider().call("hi")
    args = captured.call_args.args[0]
    assert "--resume" not in args


def test_claude_call_raises_config_error_when_cli_missing(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)
    with pytest.raises(ProviderConfigError):
        ClaudeProvider().call("hi")


def test_claude_call_raises_on_non_zero_exit(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stderr="auth failed", returncode=1),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "auth failed" in str(exc.value)


def test_claude_call_raises_on_is_error_true(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(
            stdout='{"is_error": true, "result": "permission denied"}'
        ),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "permission denied" in str(exc.value)


def test_claude_call_raises_on_malformed_json(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout="not json"),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "json" in str(exc.value).lower()


def test_claude_timeout_maps_to_provider_error(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    )
    with pytest.raises(ProviderError) as exc:
        ClaudeProvider().call("hi")
    assert "timed out" in str(exc.value)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

CODEX_NDJSON = (
    '{"type":"session.created","session_id":"sess-codex-1"}\n'
    '{"type":"item.started","item":{"type":"agent_message"}}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
)


class _FakeScheduledPipe:
    def __init__(
        self,
        schedule: list[tuple[float, str]],
        *,
        hang_after_schedule: bool,
        terminated: threading.Event,
        on_eof,
    ) -> None:
        self._schedule = schedule
        self._hang_after_schedule = hang_after_schedule
        self._terminated = terminated
        self._on_eof = on_eof
        self._idx = 0

    def readline(self) -> str:
        if self._idx < len(self._schedule):
            delay, line = self._schedule[self._idx]
            self._idx += 1
            if self._terminated.wait(delay):
                return ""
            return line
        if self._hang_after_schedule:
            self._terminated.wait()
        if self._on_eof is not None:
            self._on_eof()
        return ""


class _FakePopen:
    def __init__(
        self,
        *,
        stdout_schedule: list[tuple[float, str]],
        stderr_schedule: list[tuple[float, str]] | None = None,
        hang_after_stdout: bool = False,
        returncode: int = 0,
    ) -> None:
        self.args: list[str] | None = None
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._configured_returncode = returncode
        self._terminated_event = threading.Event()
        self._finished_event = threading.Event()
        self.stdout = _FakeScheduledPipe(
            stdout_schedule,
            hang_after_schedule=hang_after_stdout,
            terminated=self._terminated_event,
            on_eof=self._finish_success,
        )
        self.stderr = _FakeScheduledPipe(
            stderr_schedule or [],
            hang_after_schedule=False,
            terminated=self._terminated_event,
            on_eof=None,
        )

    def _finish_success(self) -> None:
        if self.returncode is None:
            self.returncode = self._configured_returncode
            self._finished_event.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._finished_event.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=self.args or "codex", timeout=timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._terminated_event.set()
        self._finished_event.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._terminated_event.set()
        self._finished_event.set()


def _patch_codex_popen(mocker, fake: _FakePopen):
    def factory(args, **kwargs):
        fake.args = args
        return fake

    return mocker.patch("conductor.providers.codex.subprocess.Popen", side_effect=factory)


def test_codex_configured_when_cli_present_and_authed(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout="Logged in using ChatGPT"),
    )
    ok, reason = CodexProvider().configured()
    assert ok is True and reason is None


def test_codex_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value=None)
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    assert "codex login" in reason
    assert "OPENAI_API_KEY" in reason


def test_codex_configured_false_when_not_authed(mocker):
    """`codex login status` exits non-zero when the user isn't authed."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stderr="not logged in", returncode=1),
    )
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "codex login" in reason
    assert "OPENAI_API_KEY" in reason
    assert "--with-api-key" in reason


def test_codex_configured_false_when_auth_probe_times_out(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=5),
    )
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "could not verify" in reason


def test_codex_call_parses_ndjson_and_usage(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    response = CodexProvider().call("hi")

    assert response.text == "hello from codex"
    assert response.provider == "codex"
    assert response.usage["input_tokens"] == 5
    assert response.usage["output_tokens"] == 2
    assert response.session_id == "sess-codex-1"


def test_codex_call_translates_effort_to_reasoning_effort_config(mocker):
    """Codex CLI 0.125.0 dropped --effort in favor of `-c model_reasoning_effort=`.
    Conductor must emit the new form. Regression test for the silent breakage
    where conductor's call passed `--effort minimal` to a 0.125.0 codex CLI,
    which exited 2 with `error: unexpected argument '--effort' found`."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("hi", effort="minimal")
    args = captured.call_args.args[0]
    # Old (broken) form must not appear.
    assert "--effort" not in args, (
        "codex CLI >= 0.125.0 removed --effort; conductor must use -c instead. "
        f"args={args!r}"
    )
    # New form: -c model_reasoning_effort=minimal must be present together.
    assert "-c" in args, f"missing -c flag, args={args!r}"
    c_idx = args.index("-c")
    assert args[c_idx + 1] == "model_reasoning_effort=minimal", (
        f"expected `model_reasoning_effort=minimal`, got {args[c_idx + 1]!r}"
    )


def test_codex_call_with_resume_uses_resume_subcommand(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("follow-up", resume_session_id="sess-codex-1")
    args = captured.call_args.args[0]
    # `codex exec resume <id> "<prompt>"` is the documented shape
    assert args[1] == "exec" and args[2] == "resume"
    assert args[3] == "sess-codex-1"
    assert args[4] == "follow-up"
    # When resuming, --ephemeral does not apply (resume implies persistence).
    assert "--ephemeral" not in args


def test_codex_call_raises_when_no_agent_message(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(
            stdout='{"type":"turn.completed","usage":{}}\n'
        ),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        CodexProvider().call("hi")
    assert "agent_message" in str(exc.value)


def test_codex_call_raises_on_non_zero_exit(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stderr="login required", returncode=1),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        CodexProvider().call("hi")
    assert "login required" in str(exc.value)


def test_codex_exec_default_timeout_is_unbounded(mocker):
    """Regression: `conductor exec --with codex` (no --timeout) used to default
    to 300s, which silently killed long agent sessions and lost the session_id.
    The default is now no-timeout; exec uses the streaming Popen path and
    must complete without imposing a wall-clock cap when the caller omits it."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)]
    )
    captured = _patch_codex_popen(mocker, fake)
    CodexProvider().exec("hi")
    assert captured.called
    assert fake.terminated is False


def test_codex_exec_explicit_timeout_is_honored(mocker):
    """Caller-supplied --timeout still works — sentinel pattern must not
    swallow an explicitly-passed integer."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, '{"type":"session.created","session_id":"sess-slow"}\n')],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05, max_stall_sec=None)
    assert "timed out" in str(exc.value)
    assert fake.terminated is True


def test_codex_call_keeps_constructor_default_timeout(mocker):
    """Single-turn call() retains the constructor-default HTTP-style timeout
    (180s) — the no-timeout change is scoped to exec(), where long agent
    sessions are normal."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("hi")
    # Default constructor timeout is 180s — see CODEX_REQUEST_TIMEOUT_SEC.
    assert captured.call_args.kwargs["timeout"] == 180.0


def test_codex_timeout_recovers_session_id_from_partial_ndjson(mocker):
    """When codex emits a session.created event but then runs past the wall
    clock, the user must be able to --resume from the partial session_id.
    Pre-fix, the TimeoutExpired path threw away the captured stdout entirely,
    leaving the user with 22 minutes of churn and no recovery handle."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = (
        '{"type":"session.created","session_id":"sess-partial-7"}\n'
        '{"type":"item.started","item":{"type":"agent_message"}}\n'
    )
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in partial.splitlines(keepends=True)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)
    msg = str(exc.value)
    assert "timed out" in msg
    assert "sess-partial-7" in msg, (
        "Timeout error must surface the partial session_id so --resume "
        f"is actionable. Got: {msg!r}"
    )
    assert "--resume sess-partial-7" in msg


def test_codex_timeout_without_partial_session_id_still_raises_clean(mocker):
    """If codex died before emitting session.created, the timeout error
    should still raise cleanly without crashing on the missing ID."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)
    msg = str(exc.value)
    assert "timed out" in msg
    assert "session_id" not in msg  # Nothing to surface


def test_codex_call_timeout_handles_bytes_stdout_from_subprocess(mocker):
    """subprocess.TimeoutExpired.output can be bytes when text=False is used
    elsewhere in the call path. Recovery must decode defensively."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial_bytes = (
        b'{"type":"session.created","session_id":"sess-bytes-9"}\n'
    )
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd="codex", timeout=5, output=partial_bytes, stderr=b""
        ),
    )
    with pytest.raises(ProviderError) as exc:
        CodexProvider().call("hi")
    assert "sess-bytes-9" in str(exc.value)


def test_codex_exec_stall_watchdog_kills_silent_provider(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-stalled-1"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "stalled" in msg
    assert "sess-stalled-1" in msg
    assert "--resume sess-stalled-1" in msg
    assert fake.terminated is True


def test_codex_exec_streams_output_resets_stall_clock(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-streaming-1"}\n',
        '{"type":"item.started","item":{"type":"agent_message"}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0.1, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    response = CodexProvider().exec("hi", max_stall_sec=2, liveness_interval_sec=0)

    assert response.text == "done"
    assert response.session_id == "sess-streaming-1"
    assert fake.terminated is False


def test_codex_exec_no_stall_watchdog_by_default(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-no-watchdog"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)

    assert "timed out" in str(exc.value)
    assert not isinstance(exc.value, ProviderStalledError)
    assert fake.terminated is True


def test_codex_exec_emits_liveness_signal_to_stderr(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-liveness"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec("hi", max_stall_sec=0.25, liveness_interval_sec=0.1)

    assert "[conductor] no output from codex for " in capsys.readouterr().err


def test_codex_exec_heartbeat_reports_tool_calls_from_session_log(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    lines = [
        '{"type":"session.created","session_id":"sess-heartbeat-tools"}\n',
        (
            '{"type":"item.completed","item":{"type":"function_call","name":"Read",'
            '"arguments":{"path":"README.md"}}}\n'
        ),
    ]
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in lines],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-tools.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "1 tool call" in err


def test_codex_exec_heartbeat_reports_subagent_tokens_from_session_log(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    lines = [
        '{"type":"session.created","session_id":"sess-heartbeat-tokens"}\n',
        (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"working","token_count":1200}}\n'
        ),
    ]
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in lines],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-tokens.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "1.2k tokens received since last heartbeat" in err


def test_codex_exec_heartbeat_marks_zero_progress_as_possibly_stalled(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    partial = '{"type":"session.created","session_id":"sess-heartbeat-stalled"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-stalled.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "0 tool calls, 0 tokens — possibly stalled" in err


def test_codex_exec_heartbeat_falls_back_when_session_log_missing(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-heartbeat-fallback"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec("hi", max_stall_sec=0.25, liveness_interval_sec=0.1)

    err = capsys.readouterr().err
    assert "[conductor] no output from codex for " in err
    assert "tool call" not in err
    assert "tokens received" not in err
    assert "possibly stalled" not in err


def test_codex_call_unaffected_by_streaming_changes(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    popen_mock = mocker.patch("conductor.providers.codex.subprocess.Popen")
    run_mock = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    response = CodexProvider().call("hi")

    assert response.text == "hello from codex"
    assert run_mock.called
    assert not popen_mock.called


def test_codex_exec_emits_session_id_to_stderr_when_received(mocker, capsys):
    """Once codex emits the `session.created` NDJSON event, conductor must
    surface the session_id on stderr immediately so a wrapping agent can
    correlate logs and (if needed) `--resume` mid-flight. Without this the
    session_id only became visible at completion or on error."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-early-1"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi", liveness_interval_sec=0)

    captured = capsys.readouterr()
    assert "[conductor] codex session_id=sess-early-1" in captured.err, (
        f"Expected session_id stderr line. Got: {captured.err!r}"
    )


def test_codex_exec_emits_session_id_only_once(mocker, capsys):
    """If codex emits multiple session.created events (shouldn't happen,
    but the parser must tolerate it), conductor surfaces the first one
    and stays quiet on duplicates — operator stderr stays clean."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-first"}\n',
        '{"type":"session.created","session_id":"sess-second"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi", liveness_interval_sec=0)

    err = capsys.readouterr().err
    assert err.count("[conductor] codex session_id=") == 1
    assert "sess-first" in err
    assert "sess-second" not in err


def test_codex_exec_writes_forensic_envelope_on_stall(mocker, tmp_path, monkeypatch):
    """Envelope captures everything needed to attribute a stall:
    command, cwd, conductor version, partial stdout, partial stderr.
    Independent of whether codex emitted any NDJSON."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-forensic-1"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_dir = tmp_path / "conductor"
    log_files = list(log_dir.glob("codex-*.json"))
    assert len(log_files) == 1, f"expected one envelope file, got {log_files}"
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "stall"
    # The CLI argv keeps the literal `codex` (not the resolved PATH).
    # The envelope's separate `codex_path` field is what shutil.which()
    # resolved at the time of failure.
    assert envelope["command"][0] == "codex"
    assert envelope["codex_path"] == "/usr/bin/codex"
    assert "exec" in envelope["command"]
    assert envelope["captured_stdout"] == partial
    assert envelope["conductor_version"]  # whatever it is, it's set
    assert str(log_files[0]) in msg


def test_codex_exec_writes_forensic_envelope_on_streaming_timeout(
    mocker, tmp_path, monkeypatch
):
    """Same envelope behavior on the wall-clock timeout path."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = (
        '{"type":"session.created","session_id":"sess-forensic-2"}\n'
        '{"type":"item.started","item":{"type":"agent_message"}}\n'
    )
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in partial.splitlines(keepends=True)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.1, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_files = list((tmp_path / "conductor").glob("codex-*.json"))
    assert len(log_files) == 1
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "timeout"
    assert envelope["captured_stdout"] == partial


def test_codex_exec_writes_envelope_when_codex_emits_zero_bytes(
    mocker, tmp_path, monkeypatch
):
    """The high-leverage case from .cortex/journal/2026-04-26-codex-exec-
    wedge-trace.md: codex wedges *before* session.created fires and
    produces no output at all. Pre-fix, this left the wrapping agent
    with nothing — no session_id, no NDJSON file, nothing to attribute
    the failure to. The envelope must still be written, capturing
    (command, cwd, conductor_version) so an operator can pin the run."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec(
            "test prompt body",
            max_stall_sec=0.05,
            liveness_interval_sec=0,
        )

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_files = list((tmp_path / "conductor").glob("codex-*.json"))
    assert len(log_files) == 1, (
        "Envelope MUST be written even when codex emitted zero bytes — "
        "this is the wedge-before-session.created class of failure that "
        "had no diagnostics pre-fix."
    )
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "stall"
    assert envelope["captured_stdout"] == ""
    # The prompt body must be in the captured command (last positional after
    # `codex exec`), so an operator can correlate the wedge with the request.
    assert "test prompt body" in envelope["command"]


def test_codex_exec_forensic_envelope_disk_failure_does_not_mask_real_error(
    mocker, tmp_path, monkeypatch
):
    """If the cache write itself fails (read-only fs, ENOSPC, etc.), the
    original ProviderStalledError must still propagate cleanly — losing
    the forensic envelope is acceptable, masking the original error is not."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-disk-fail"}\n'
    fake = _FakePopen(stdout_schedule=[(0, partial)], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    # Force write_text to raise — simulates a read-only fs / quota failure.
    mocker.patch(
        "conductor.providers.codex.Path.write_text",
        side_effect=OSError("disk full"),
    )

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)
    msg = str(exc.value)
    # Original error info still present:
    assert "stalled" in msg
    assert "sess-disk-fail" in msg
    # No fabricated envelope path mentioned:
    assert "forensic envelope:" not in msg


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

GEMINI_JSON = """{
    "response": "hello from gemini",
    "session_id": "xyz",
    "stats": {
        "models": {
            "gemini-2.5-pro": {"tokens": {"input": 12, "candidates": 4}}
        }
    }
}"""


def test_gemini_configured_when_cli_present_and_env_authed(mocker, monkeypatch):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    ok, reason = GeminiProvider().configured()
    assert ok is True and reason is None


def test_gemini_configured_when_oauth_creds_have_tokens(
    mocker, monkeypatch, tmp_path
):
    """Filesystem-only auth path — env vars unset, OAuth file present."""
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        '{"access_token": "ya29.fake", "refresh_token": "1//fake", '
        '"expiry_date": 0}'
    )
    # Expiry deliberately stale — we trust the CLI to refresh, so an
    # expired access_token alongside a refresh_token is still authed.
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is True and reason is None


def test_gemini_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value=None)
    ok, reason = GeminiProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    assert "GEMINI_API_KEY" in reason


def test_gemini_configured_false_when_no_env_and_no_oauth_file(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    missing = tmp_path / "does_not_exist.json"
    ok, reason = GeminiProvider(oauth_creds_path=missing).configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "GEMINI_API_KEY" in reason
    assert "GOOGLE_API_KEY" in reason


def test_gemini_configured_false_when_oauth_file_corrupt(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text("not valid json {{")
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is False
    assert "could not parse" in reason


def test_gemini_configured_false_when_oauth_file_has_no_tokens(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text('{"unrelated_field": "value"}')
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is False
    assert "no usable tokens" in reason


def test_gemini_configured_when_only_google_application_credentials_set(
    mocker, monkeypatch, tmp_path
):
    """Vertex AI ADC path — GOOGLE_APPLICATION_CREDENTIALS is sufficient."""
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    missing = tmp_path / "does_not_exist.json"  # unreachable; env wins
    ok, reason = GeminiProvider(oauth_creds_path=missing).configured()
    assert ok is True and reason is None


def test_gemini_call_parses_json_and_usage(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )

    response = GeminiProvider().call("hi")

    assert response.text == "hello from gemini"
    assert response.provider == "gemini"
    assert response.usage["input_tokens"] == 12
    assert response.usage["output_tokens"] == 4
    assert response.session_id == "xyz"


def test_gemini_call_with_resume_passes_resume_flag(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    captured = mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )
    GeminiProvider().call("follow-up", resume_session_id="latest")
    args = captured.call_args.args[0]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "latest"


def test_gemini_call_falls_back_to_plain_text(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout="plain text reply"),
    )

    response = GeminiProvider().call("hi")

    assert response.text == "plain text reply"
    assert response.usage["input_tokens"] is None


def test_gemini_call_passes_model_flag(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    run_mock = mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )
    GeminiProvider().call("hi", model="gemini-2.5-flash")
    args = run_mock.call_args.args[0]
    assert "-m" in args and args[args.index("-m") + 1] == "gemini-2.5-flash"


def test_gemini_call_raises_on_empty_stdout(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=""),
    )
    with pytest.raises(ProviderHTTPError):
        GeminiProvider().call("hi")


# ---------------------------------------------------------------------------
# Live subprocess smoke
# ---------------------------------------------------------------------------

LIVE_SMOKE_DISABLED = not os.environ.get("RUN_LIVE_SMOKE")
LIVE_SMOKE_REASON = "set RUN_LIVE_SMOKE=1 to run live subprocess smoke tests"
LIVE_ONE_TOKEN_PROMPT = "Reply with exactly: OK"


def _assert_live_response_shape(response: CallResponse, *, provider: str) -> None:
    assert isinstance(response, CallResponse)
    assert response.provider == provider
    assert response.model
    assert response.text.strip()
    assert isinstance(response.duration_ms, int)
    assert response.duration_ms >= 0
    assert isinstance(response.usage, dict)


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_live_claude_call_returns_call_response_shape():
    response = ClaudeProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="claude")


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_live_codex_call_returns_call_response_shape():
    response = CodexProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="codex")


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("gemini") is None, reason="gemini CLI not installed")
def test_live_gemini_call_returns_call_response_shape():
    response = GeminiProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="gemini")
