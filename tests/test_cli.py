"""CLI smoke tests — provider lookup, stdin fallback, error paths."""

from __future__ import annotations

import httpx
import respx
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.kimi import KIMI_API_KEY_ENV, KIMI_BASE_URL


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


def test_call_kimi_happy_path(monkeypatch):
    monkeypatch.setenv(KIMI_API_KEY_ENV, "sk-test")
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "kimi-k2.6",
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output


def test_call_kimi_missing_key_exits_2(monkeypatch):
    monkeypatch.delenv(KIMI_API_KEY_ENV, raising=False)
    result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])
    assert result.exit_code == 2
    assert KIMI_API_KEY_ENV in result.output


def test_call_json_output(monkeypatch):
    monkeypatch.setenv(KIMI_API_KEY_ENV, "sk-test")
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "kimi-k2.6",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi", "--json"]
        )

    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["text"] == "ok"
    assert payload["provider"] == "kimi"
