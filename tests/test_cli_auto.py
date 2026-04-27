"""CLI tests for --auto routing."""

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

_TEST_ACCOUNT_ID = "acct-test-1234"
_CF_CHAT_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{_TEST_ACCOUNT_ID}"
    "/ai/v1/chat/completions"
)


def _stub_only_kimi_configured(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        OllamaProvider,
    )

    for cls in (ClaudeProvider, CodexProvider, GeminiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed off"))


def test_call_auto_and_with_are_mutually_exclusive(mocker):
    _stub_only_kimi_configured(mocker)
    result = CliRunner().invoke(
        main, ["call", "--with", "kimi", "--auto", "--task", "hi"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_call_requires_with_or_auto(mocker):
    result = CliRunner().invoke(main, ["call", "--task", "hi"])
    assert result.exit_code != 0
    assert "--with" in result.output or "--auto" in result.output


def test_call_auto_picks_configured_provider(monkeypatch, mocker):
    _stub_only_kimi_configured(mocker)
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)

    with respx.mock() as router:
        router.post(_CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "auto-routed"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--auto", "--tags", "long-context", "--task", "hi"]
        )

    assert result.exit_code == 0, result.output
    assert "auto-routed" in result.output


def test_call_auto_json_includes_route_decision(monkeypatch, mocker):
    _stub_only_kimi_configured(mocker)
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)

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
            main,
            ["call", "--auto", "--tags", "long-context,cheap", "--task", "hi", "--json"],
        )

    import json

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["text"] == "ok"
    assert payload["route"]["provider"] == "kimi"
    assert set(payload["route"]["task_tags"]) == {"long-context", "cheap"}


def test_call_auto_with_no_configured_providers_exits_2(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    ):
        mocker.patch.object(cls, "configured", lambda self: (False, "nope"))

    result = CliRunner().invoke(
        main, ["call", "--auto", "--tags", "cheap", "--task", "hi"]
    )
    assert result.exit_code == 2
    assert "no provider satisfies" in result.output.lower()


# ---------------------------------------------------------------------------
# Shadow hint — auto-mode tells the user when an unconfigured provider would
# have outranked the picked one. Closes the "I didn't know codex wasn't
# installed" UX gap.
# ---------------------------------------------------------------------------


def test_call_auto_emits_shadow_hint_when_unconfigured_outranks(monkeypatch, mocker):
    """Only kimi is ready, but codex/claude (frontier, code-review tag) would
    rank higher — the heads-up advisory should fire and name codex (priority
    tiebreak among frontiers; claude is excluded so codex is unambiguous)."""
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        OllamaProvider,
    )

    mocker.patch.object(ClaudeProvider, "configured", lambda self: (False, "nope"))
    mocker.patch.object(
        CodexProvider,
        "configured",
        lambda self: (False, "`codex` CLI not found on PATH"),
    )
    mocker.patch.object(GeminiProvider, "configured", lambda self: (False, "nope"))
    mocker.patch.object(OllamaProvider, "configured", lambda self: (False, "nope"))
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)

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
            main,
            [
                "call",
                "--auto",
                "--tags",
                "code-review",
                "--prefer",
                "best",
                "--exclude",
                "claude",
                "--task",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "heads-up" in result.output
    assert "codex" in result.output
    assert "CLI not found on PATH" in result.output
    assert "conductor list" in result.output


def test_call_auto_no_hint_when_winner_is_top(monkeypatch, mocker):
    """When the picked provider already outscores any shadow candidate,
    the hint must stay quiet — otherwise it becomes wallpaper."""
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        OllamaProvider,
    )

    # Claude (frontier, code-review) is configured. Nothing unconfigured can
    # outrank it for this request.
    mocker.patch.object(ClaudeProvider, "configured", lambda self: (True, None))
    mocker.patch.object(CodexProvider, "configured", lambda self: (False, "off"))
    mocker.patch.object(GeminiProvider, "configured", lambda self: (False, "off"))
    mocker.patch.object(OllamaProvider, "configured", lambda self: (False, "off"))

    # Stub claude.call so we don't spawn the real CLI.
    mocker.patch.object(
        ClaudeProvider,
        "call",
        lambda self, task, **kw: __import__(
            "conductor.providers.interface", fromlist=["CallResponse"]
        ).CallResponse(
            text="claude-stubbed",
            provider="claude",
            model="sonnet",
            duration_ms=1,
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "call",
            "--auto",
            "--tags",
            "code-review",
            "--prefer",
            "best",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "heads-up" not in result.output


def test_call_auto_silent_route_suppresses_shadow_hint(monkeypatch, mocker):
    """--silent-route is the documented escape hatch for scripted callers;
    it must squelch the shadow hint along with the rest of the route log."""
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        OllamaProvider,
    )

    mocker.patch.object(ClaudeProvider, "configured", lambda self: (False, "nope"))
    mocker.patch.object(
        CodexProvider,
        "configured",
        lambda self: (False, "`codex` CLI not found on PATH"),
    )
    mocker.patch.object(GeminiProvider, "configured", lambda self: (False, "nope"))
    mocker.patch.object(OllamaProvider, "configured", lambda self: (False, "nope"))
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, _TEST_ACCOUNT_ID)

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
            main,
            [
                "call",
                "--auto",
                "--tags",
                "code-review",
                "--prefer",
                "best",
                "--exclude",
                "claude",
                "--silent-route",
                "--task",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "heads-up" not in result.output
