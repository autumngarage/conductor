"""Tests for the subprocess-based adapters (claude, codex, gemini).

All three call external CLIs. We stub ``subprocess.run`` and
``shutil.which`` so tests run with no dependencies on the real binaries.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conductor.providers.claude import ClaudeProvider
from conductor.providers.codex import CodexProvider
from conductor.providers.gemini import GEMINI_AUTH_ENV_VARS, GeminiProvider
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)


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
