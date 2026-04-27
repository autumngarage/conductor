"""CLI smoke tests — provider lookup, stdin fallback, error paths."""

from __future__ import annotations

import httpx
import respx
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.kimi import (
    CLOUDFLARE_ACCOUNT_ID_ENV,
    CLOUDFLARE_API_TOKEN_ENV,
    KIMI_DEFAULT_MODEL,
)
from conductor.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_DEFAULT_MODEL,
)

_TEST_ACCOUNT_ID = "acct-test-1234"
_CF_CHAT_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{_TEST_ACCOUNT_ID}"
    "/ai/v1/chat/completions"
)


def test_call_unknown_provider_shows_usage_error():
    result = CliRunner().invoke(main, ["call", "--with", "noprovider", "--task", "hi"])
    assert result.exit_code != 0
    assert "unknown provider" in result.output.lower() or "noprovider" in result.output


def test_call_missing_task_and_no_stdin_errors():
    # CliRunner attaches an empty pipe as stdin (isatty=False), so we hit the
    # empty-task branch rather than the no-task-no-stdin branch. Both signal
    # the same user error: nothing to send.
    result = CliRunner().invoke(main, ["call", "--with", "kimi"])
    assert result.exit_code != 0
    assert "task" in result.output.lower() and "empty" in result.output.lower()


def test_call_task_file_reads_file(monkeypatch, tmp_path):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    brief = tmp_path / "brief.md"
    brief.write_text("brief from file\n", encoding="utf-8")

    with respx.mock() as router:
        route = router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task-file", str(brief)]
        )

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output
    assert route.calls.last.request.content.decode("utf-8").count("brief from file") == 1


def test_call_kimi_happy_path(monkeypatch):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    with respx.mock() as router:
        router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output


def test_call_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": OPENROUTER_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello from openrouter"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            main,
            [
                "call",
                "--with",
                "openrouter",
                "--model",
                OPENROUTER_DEFAULT_MODEL,
                "--task",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "hello from openrouter" in result.output


def test_call_kimi_missing_token_exits_2(monkeypatch):
    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])
    assert result.exit_code == 2
    assert CLOUDFLARE_API_TOKEN_ENV in result.output


def test_call_kimi_missing_account_exits_2(monkeypatch):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)
    result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])
    assert result.exit_code == 2
    assert CLOUDFLARE_ACCOUNT_ID_ENV in result.output


def test_call_json_output(monkeypatch):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    # The caller banner is silenced under --json (matches existing route-log
    # silencing), so stdout stays clean for json.loads().
    with respx.mock() as router:
        router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi", "--json"]
        )

    assert result.exit_code == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["text"] == "ok"
    assert payload["provider"] == "kimi"


def test_call_emits_caller_banner_when_claude_detected(monkeypatch):
    """When CLAUDECODE is set, `conductor call` announces itself on stderr."""
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("NO_COLOR", "1")  # plain ASCII for stable assertion

    with respx.mock() as router:
        router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi"]
        )

    assert result.exit_code == 0, result.stderr
    # Banner on stderr — keeps stdout clean for subprocess parsing.
    assert "Claude Code is using Conductor → kimi" in result.stderr
    # Response on stdout, untouched.
    assert result.stdout.strip() == "hello"


def test_call_silent_route_suppresses_caller_banner(monkeypatch):
    """--silent-route silences the caller banner (and the route log)."""
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)
    monkeypatch.setenv("CLAUDECODE", "1")

    with respx.mock() as router:
        router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi", "--silent-route"]
        )

    assert result.exit_code == 0, result.stderr
    assert "Conductor" not in result.stderr
