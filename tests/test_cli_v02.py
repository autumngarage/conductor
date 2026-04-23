"""Tests for the v0.2 CLI surface additions.

Covers:
  - new flags on `conductor call` (--prefer, --effort, --exclude, --verbose-route)
  - the new `conductor exec` subcommand
  - the new `conductor route` dry-run subcommand
  - the new `conductor config show` subcommand
  - route-log formatting (stderr output on --auto)

Provider responses are faked through configured() stubs + provider.call()
mocks; no real CLIs or HTTP endpoints are invoked.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers import (
    CallResponse,
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    KimiProvider,
    OllamaProvider,
)
from conductor.router import reset_health


@pytest.fixture(autouse=True)
def _clean_health():
    reset_health()
    yield
    reset_health()


def _stub_all_configured(mocker, configured_names: set[str]) -> None:
    """Patch configured() on every provider class."""
    classes = {
        "kimi": KimiProvider,
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
    }
    for name, cls in classes.items():
        ok = name in configured_names
        mocker.patch.object(
            cls,
            "configured",
            lambda self, _ok=ok, _n=name: (_ok, None if _ok else f"{_n} stub not configured"),
        )


def _fake_response(provider: str = "claude", model: str = "sonnet") -> CallResponse:
    return CallResponse(
        text="hello",
        provider=provider,
        model=model,
        duration_ms=1234,
        usage={
            "input_tokens": 50,
            "output_tokens": 10,
            "thinking_tokens": 4_000,
            "cached_tokens": 0,
        },
        cost_usd=0.01,
        raw={},
    )


# ---------------------------------------------------------------------------
# call — new flags
# ---------------------------------------------------------------------------


def test_call_auto_prefer_best_routes_to_frontier(mocker):
    _stub_all_configured(mocker, {"claude", "ollama"})
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--task", "hi"]
    )

    assert result.exit_code == 0
    assert "hello" in result.stdout
    # Route log on stderr names the chosen provider and prefer mode.
    assert "[conductor] best" in result.stderr
    assert "→ claude" in result.stderr
    assert "tier: frontier" in result.stderr


def test_call_auto_effort_max_flows_to_provider(mocker):
    _stub_all_configured(mocker, {"claude"})
    call_mock = mocker.patch.object(
        ClaudeProvider, "call", return_value=_fake_response("claude")
    )

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--effort", "max", "--task", "hi"]
    )

    assert result.exit_code == 0
    # The `effort` kwarg reaches provider.call() so the provider can translate.
    assert call_mock.call_args.kwargs["effort"] == "max"
    # Stderr log carries the effort choice.
    assert "effort=max" in result.stderr


def test_call_auto_exclude_skips_named_provider(mocker):
    _stub_all_configured(mocker, {"claude", "codex"})
    codex_call = mocker.patch.object(
        CodexProvider, "call", return_value=_fake_response("codex")
    )
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        [
            "call", "--auto", "--prefer", "best",
            "--exclude", "claude", "--task", "hi",
        ],
    )

    assert result.exit_code == 0
    assert codex_call.called  # codex picked because claude was excluded
    assert "→ codex" in result.stderr


def test_call_invalid_prefer_errors_with_hint():
    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "beast", "--task", "hi"]
    )
    assert result.exit_code == 2
    assert "--prefer='beast'" in result.output or "--prefer=beast" in result.output
    assert "best" in result.output


def test_call_invalid_effort_errors_with_hint():
    result = CliRunner().invoke(
        main, ["call", "--auto", "--effort", "maxx", "--task", "hi"]
    )
    assert result.exit_code == 2
    assert "--effort" in result.output
    assert "max" in result.output


def test_call_effort_accepts_integer_budget():
    # Just the parse path; no provider invocation needed.
    result = CliRunner().invoke(
        main, ["call", "--auto", "--effort", "-5", "--task", "hi"]
    )
    # Negative integer rejected with UsageError.
    assert result.exit_code == 2


def test_call_with_provider_and_exclude_contradict():
    result = CliRunner().invoke(
        main,
        ["call", "--with", "claude", "--exclude", "claude", "--task", "hi"],
    )
    assert result.exit_code == 2
    assert "contradict" in result.output


def test_call_prefer_without_auto_errors():
    result = CliRunner().invoke(
        main,
        ["call", "--with", "claude", "--prefer", "best", "--task", "hi"],
    )
    assert result.exit_code == 2
    assert "only meaningful with --auto" in result.output


def test_call_resume_requires_with(mocker):
    _stub_all_configured(mocker, {"claude"})
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))
    result = CliRunner().invoke(
        main, ["call", "--auto", "--resume", "abc-123", "--task", "hi"]
    )
    assert result.exit_code == 2
    assert "--resume requires --with" in result.output


def test_call_resume_passes_session_id_to_provider(mocker):
    _stub_all_configured(mocker, {"claude"})
    call_mock = mocker.patch.object(
        ClaudeProvider, "call", return_value=_fake_response("claude")
    )
    result = CliRunner().invoke(
        main,
        ["call", "--with", "claude", "--resume", "sess-xyz", "--task", "hi"],
    )
    assert result.exit_code == 0
    # Verify resume_session_id was forwarded as a kwarg.
    assert call_mock.call_args.kwargs["resume_session_id"] == "sess-xyz"


def test_call_silent_route_suppresses_log(mocker):
    _stub_all_configured(mocker, {"claude"})
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main, ["call", "--auto", "--silent-route", "--task", "hi"]
    )

    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert "[conductor]" not in result.stderr  # suppressed


def test_call_verbose_route_prints_full_ranking(mocker):
    _stub_all_configured(mocker, {"claude", "codex", "ollama"})
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        ["call", "--auto", "--prefer", "best", "--verbose-route", "--task", "hi"],
    )

    assert result.exit_code == 0
    # Verbose mode emits the ranking table.
    assert "route decision" in result.stderr
    assert "1. claude" in result.stderr
    assert "codex" in result.stderr
    assert "ollama" in result.stderr


# ---------------------------------------------------------------------------
# exec — new subcommand
# ---------------------------------------------------------------------------


def test_exec_auto_routes_to_tool_capable_provider(mocker):
    _stub_all_configured(mocker, {"claude", "kimi"})
    exec_mock = mocker.patch.object(
        ClaudeProvider, "exec", return_value=_fake_response("claude")
    )

    result = CliRunner().invoke(
        main,
        [
            "exec", "--auto", "--prefer", "best",
            "--tools", "Read,Grep,Edit",
            "--sandbox", "read-only",
            "--task", "review the diff",
        ],
    )

    assert result.exit_code == 0
    assert exec_mock.called
    # kimi would be skipped by the tools filter (supported_tools=frozenset()).
    assert "→ claude" in result.stderr


def test_exec_unknown_tool_errors_with_hint():
    result = CliRunner().invoke(
        main,
        [
            "exec", "--auto",
            "--tools", "Read,NotARealTool",
            "--task", "hi",
        ],
    )
    assert result.exit_code == 2
    assert "NotARealTool" in result.output


def test_exec_unknown_sandbox_errors_with_hint():
    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--sandbox", "reed-only", "--task", "hi"],
    )
    assert result.exit_code == 2
    assert "--sandbox='reed-only'" in result.output
    assert "read-only" in result.output


def test_exec_with_kimi_tools_raises_unsupported(mocker):
    _stub_all_configured(mocker, {"kimi"})

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "kimi", "--tools", "Edit", "--task", "hi"],
    )

    # kimi's exec() raises UnsupportedCapability on tools — exit 2.
    assert result.exit_code == 2
    assert "UnsupportedCapability" in result.stderr or "not supported" in result.stderr


# ---------------------------------------------------------------------------
# route — dry-run subcommand
# ---------------------------------------------------------------------------


def test_route_prints_chosen_provider_without_calling(mocker):
    _stub_all_configured(mocker, {"claude", "codex"})
    # Critical: route must NOT invoke provider.call() / provider.exec().
    call_mock = mocker.patch.object(ClaudeProvider, "call")

    result = CliRunner().invoke(
        main, ["route", "--prefer", "best", "--tags", "code-review"]
    )

    assert result.exit_code == 0
    assert "would pick: claude" in result.output
    assert "tier: frontier" in result.output
    assert not call_mock.called  # router dry-run makes no calls


def test_route_json_mode(mocker):
    _stub_all_configured(mocker, {"claude"})
    result = CliRunner().invoke(
        main, ["route", "--prefer", "best", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["provider"] == "claude"
    assert payload["prefer"] == "best"


def test_route_no_configured_provider_exits_nonzero(mocker):
    _stub_all_configured(mocker, set())
    result = CliRunner().invoke(main, ["route"])
    assert result.exit_code == 2
    assert "no provider" in result.output.lower()


# ---------------------------------------------------------------------------
# config show
# ---------------------------------------------------------------------------


def test_config_show_reports_defaults():
    result = CliRunner().invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "effective config" in result.output
    assert "balanced" in result.output  # default prefer
    assert "medium" in result.output  # default effort


def test_config_show_surfaces_env_overrides(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_PREFER", "best")
    monkeypatch.setenv("CONDUCTOR_EFFORT", "max")
    result = CliRunner().invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "prefer   = best" in result.output
    assert "effort   = max" in result.output
    assert "(from: env)" in result.output


def test_config_show_json():
    result = CliRunner().invoke(main, ["config", "show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective"]["prefer"] == "balanced"
    assert payload["effective"]["tags"] == []
    assert payload["effective"]["with"] is None
    assert payload["known_providers"]  # non-empty list


def test_config_show_includes_tags_and_with(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_TAGS", "code-review,long-context")
    monkeypatch.setenv("CONDUCTOR_WITH", "claude")
    result = CliRunner().invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "tags" in result.output
    assert "code-review,long-context" in result.output
    assert "with" in result.output
    assert "claude" in result.output


def test_config_show_json_tracks_all_env_sources(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_TAGS", "code-review")
    monkeypatch.setenv("CONDUCTOR_WITH", "codex")
    result = CliRunner().invoke(main, ["config", "show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective"]["tags"] == ["code-review"]
    assert payload["effective"]["with"] == "codex"
    assert payload["sources"]["CONDUCTOR_TAGS"] == "env"
    assert payload["sources"]["CONDUCTOR_WITH"] == "env"


# ---------------------------------------------------------------------------
# call/exec — graceful 5xx fallback
# ---------------------------------------------------------------------------


def test_call_fallback_on_5xx(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"claude", "codex"})
    # claude fails with 5xx; codex succeeds.
    mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderHTTPError("HTTP 503: service unavailable"),
    )
    mocker.patch.object(
        CodexProvider, "call", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--task", "hi"]
    )

    assert result.exit_code == 0
    assert "hello" in result.stdout
    # Fallback message on stderr.
    assert "claude failed" in result.stderr
    assert "falling back" in result.stderr
    assert "→ codex" in result.stderr


def test_call_fallback_on_rate_limit(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"claude", "codex"})
    mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderHTTPError("Anthropic returned 429 rate limit"),
    )
    mocker.patch.object(
        CodexProvider, "call", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--task", "hi"]
    )

    assert result.exit_code == 0
    assert "claude failed (rate-limit)" in result.stderr


def test_call_no_fallback_on_auth_error(mocker):
    from conductor.providers.interface import ProviderConfigError

    _stub_all_configured(mocker, {"claude", "codex"})
    # Config errors never trigger fallback — they indicate the user's setup
    # is broken, not a transient outage.
    mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderConfigError("claude login required"),
    )
    codex_call = mocker.patch.object(
        CodexProvider, "call", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--task", "hi"]
    )

    assert result.exit_code == 2
    assert "login required" in result.stderr
    assert not codex_call.called  # no fallback attempted


def test_call_fallback_exhausted_propagates_last_error(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"claude", "codex"})
    mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderHTTPError("HTTP 503: unavailable"),
    )
    mocker.patch.object(
        CodexProvider,
        "call",
        side_effect=ProviderHTTPError("HTTP 502: bad gateway"),
    )

    result = CliRunner().invoke(
        main, ["call", "--auto", "--prefer", "best", "--task", "hi"]
    )

    assert result.exit_code == 1
    # Last error (from codex) should surface to the user.
    assert "502" in result.stderr or "bad gateway" in result.stderr


def test_call_with_single_provider_does_not_retry(mocker):
    """--with disables fallback (user picked explicitly)."""
    from conductor.providers.interface import ProviderHTTPError

    mocker.patch.object(
        ClaudeProvider,
        "configured",
        return_value=(True, None),
    )
    mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderHTTPError("HTTP 503: unavailable"),
    )

    result = CliRunner().invoke(
        main, ["call", "--with", "claude", "--task", "hi"]
    )

    assert result.exit_code == 1
    assert "503" in result.stderr
