"""Tests for the subprocess-based adapters (claude, codex, gemini).

All three call external CLIs. We stub ``subprocess.run`` and
``shutil.which`` so tests run with no dependencies on the real binaries.
"""

from __future__ import annotations

import subprocess

import pytest

from conductor.providers.claude import ClaudeProvider
from conductor.providers.codex import CodexProvider
from conductor.providers.gemini import GeminiProvider
from conductor.providers.interface import (
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)


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


def test_claude_configured_when_cli_present(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    ok, reason = ClaudeProvider().configured()
    assert ok is True and reason is None


def test_claude_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)
    ok, reason = ClaudeProvider().configured()
    assert ok is False and "claude" in reason


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
    '{"type":"item.started","item":{"type":"agent_message"}}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
)


def test_codex_configured_when_cli_present(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    ok, _ = CodexProvider().configured()
    assert ok is True


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


def test_gemini_configured_when_cli_present(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    ok, _ = GeminiProvider().configured()
    assert ok is True


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
