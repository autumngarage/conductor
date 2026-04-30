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

import io
import json

import httpx
import pytest
import respx
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
)
from conductor.router import RouteDecision, reset_health


@pytest.fixture(autouse=True)
def _clean_health(monkeypatch, tmp_path):
    reset_health()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    yield
    reset_health()


def _stub_all_configured(mocker, configured_names: set[str]) -> None:
    """Patch configured() on every provider class."""
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
            lambda self, _ok=ok, _n=name: (_ok, None if _ok else f"{_n} stub not configured"),
        )
        mocker.patch.object(
            cls,
            "health_probe",
            lambda self, timeout_sec=30.0, _ok=ok, _n=name: (
                _ok,
                None if _ok else f"{_n} preflight failed",
            ),
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


class _FakeScheduledPipe:
    def __init__(self, schedule, *, on_eof=None) -> None:
        self._schedule = schedule
        self._idx = 0
        self._on_eof = on_eof

    def readline(self) -> str:
        if self._idx < len(self._schedule):
            line = self._schedule[self._idx]
            self._idx += 1
            return line
        if self._on_eof is not None:
            self._on_eof()
            self._on_eof = None
        return ""


class _FakePopen:
    def __init__(self, *, stdout_lines, stderr_lines=None, returncode: int = 0) -> None:
        self.args = None
        self.returncode: int | None = None
        self._configured_returncode = returncode
        self.stdout = _FakeScheduledPipe(stdout_lines, on_eof=self._finish)
        self.stderr = _FakeScheduledPipe(stderr_lines or [])
        # codex now reads the prompt from stdin (`codex exec -`); mock a
        # writable pipe so the provider's write+close path works.
        self.stdin = io.StringIO()

    def _finish(self) -> None:
        if self.returncode is None:
            self.returncode = self._configured_returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._finish()
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


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


def test_call_auto_can_route_to_openrouter_and_shortlist_cheap_models(
    mocker, monkeypatch
):
    import conductor.providers.openrouter_catalog as openrouter_catalog

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    _stub_all_configured(mocker, {"claude", "openrouter"})
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=[
            openrouter_catalog.ModelEntry(
                id="cheap/newest",
                name="Cheap Newest",
                created=500,
                context_length=64_000,
                pricing_prompt=0.001,
                pricing_completion=0.001,
                pricing_thinking=None,
                supports_thinking=False,
                supports_tools=False,
                supports_vision=False,
            ),
            openrouter_catalog.ModelEntry(
                id="expensive/older",
                name="Expensive Older",
                created=400,
                context_length=64_000,
                pricing_prompt=0.010,
                pricing_completion=0.010,
                pricing_thinking=None,
                supports_thinking=False,
                supports_tools=False,
                supports_vision=False,
            ),
        ],
    )
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "cheap/newest",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        result = CliRunner().invoke(
            main,
            ["call", "--auto", "--tags", "cheap", "--task", "hi"],
        )

    assert result.exit_code == 0, result.stderr
    assert "→ openrouter" in result.stderr
    assert captured["payload"]["model"] == "openrouter/auto"
    assert captured["payload"]["plugins"][0]["allowed_models"][0] == "cheap/newest"


# ---------------------------------------------------------------------------
# ask — semantic intent API
# ---------------------------------------------------------------------------


def test_ask_research_low_lets_openrouter_auto_select(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    call_mock = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "research",
            "--effort",
            "low",
            "--brief",
            "Find the relevant background and summarize it.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.called
    assert call_mock.call_args.kwargs["models"] is None
    assert set(call_mock.call_args.kwargs["task_tags"]) == {
        "research",
        "long-context",
        "cheap",
    }
    payload = json.loads(result.stdout)
    assert payload["semantic"]["kind"] == "research"
    assert payload["semantic"]["mode"] == "call"
    assert payload["semantic"]["candidates"][0]["provider"] == "openrouter"
    assert payload["semantic"]["candidates"][0]["models"] == []


def test_ask_code_high_routes_to_codex_exec_with_default_tools(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--effort",
            "high",
            "--allow-short-brief",
            "--brief",
            "Implement the scoped coding change.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert exec_mock.call_args.kwargs["tools"] == frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    assert exec_mock.call_args.kwargs["sandbox"] == "workspace-write"


def test_ask_code_high_falls_back_to_openrouter_exec_before_ollama(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    exec_mock = mocker.patch.object(
        OpenRouterProvider,
        "exec",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    mocker.patch.object(OllamaProvider, "exec", return_value=_fake_response("ollama"))

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--effort",
            "high",
            "--allow-short-brief",
            "--brief",
            "Implement the scoped coding change.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert exec_mock.call_args.kwargs["tools"] == frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "codex",
        "claude",
        "openrouter",
        "ollama",
    ]


def test_ask_review_uses_native_review_route(mocker):
    _stub_all_configured(mocker, {"codex", "openrouter"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    review_mock = mocker.patch.object(
        CodexProvider,
        "review",
        return_value=_fake_response("codex", "codex-review"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "review",
            "--base",
            "origin/main",
            "--brief",
            "Review this merge.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert review_mock.called
    assert review_mock.call_args.kwargs["base"] == "origin/main"
    assert "→ codex" in result.stderr


def test_ask_council_fans_out_through_openrouter_only(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    responses = [
        _fake_response("openrouter", "member-a"),
        _fake_response("openrouter", "member-b"),
        _fake_response("openrouter", "member-c"),
        _fake_response("openrouter", "synthesis"),
    ]
    call_mock = mocker.patch.object(OpenRouterProvider, "call", side_effect=responses)

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "council",
            "--effort",
            "medium",
            "--brief",
            "Debate this architecture decision.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.call_count == 4
    first_three = [call.kwargs["model"] for call in call_mock.call_args_list[:3]]
    assert first_three == [
        "~google/gemini-pro-latest",
        "~moonshotai/kimi-latest",
        "deepseek/deepseek-v4-pro",
    ]
    assert call_mock.call_args_list[3].kwargs["models"] == (
        "~google/gemini-pro-latest",
        "~openai/gpt-latest",
    )
    payload = json.loads(result.stdout)
    assert payload["semantic"]["kind"] == "council"
    assert payload["semantic"]["candidates"][0]["provider"] == "openrouter"


def test_ask_council_rejects_offline():
    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "council",
            "--offline",
            "--brief",
            "Debate this.",
        ],
    )

    assert result.exit_code == 2
    assert "council always routes through OpenRouter" in result.output


# ---------------------------------------------------------------------------
# review — native provider review mode
# ---------------------------------------------------------------------------


def test_review_auto_routes_by_router_tag_order(mocker):
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    review_mock = mocker.patch.object(
        CodexProvider,
        "review",
        return_value=_fake_response("codex", "codex-review"),
    )
    claude_review = mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=_fake_response("claude", "sonnet"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--base",
            "origin/main",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert claude_review.called
    assert not review_mock.called
    assert claude_review.call_args.kwargs["base"] == "origin/main"
    assert "→ claude" in result.stderr


def test_review_auto_does_not_route_to_generic_code_review_tag_provider(mocker):
    _stub_all_configured(mocker, {"kimi", "deepseek-reasoner", "claude"})
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    review_mock = mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=_fake_response("claude", "sonnet"),
    )

    result = CliRunner().invoke(
        main,
        ["review", "--auto", "--brief", "Review the PR."],
    )

    assert result.exit_code == 0, result.output
    assert review_mock.called
    assert "→ claude" in result.stderr


def test_review_auto_exhausted_fallback_names_stalled_codex_and_claude(mocker):
    from conductor.providers.interface import ProviderError, ProviderStalledError

    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled after 0.05s"),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderError("claude review timed out after 1s"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 1
    assert "code review failed for all tried providers" in result.stderr
    assert "claude (timeout), codex (stall)" in result.stderr


def test_review_with_gemini_emits_plain_text_without_json_envelope(mocker):
    _stub_all_configured(mocker, {"gemini"})
    mocker.patch.object(GeminiProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        GeminiProvider,
        "review",
        return_value=CallResponse(
            text="Plain review\nCODEX_REVIEW_CLEAN",
            provider="gemini",
            model="gemini-2.5-pro",
            duration_ms=10,
            usage={},
            raw={"response": "{\"response\": \"Plain review\\nCODEX_REVIEW_CLEAN\"}"},
        ),
    )

    result = CliRunner().invoke(
        main,
        ["review", "--with", "gemini", "--brief", "End with CODEX_REVIEW_CLEAN."],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "Plain review\nCODEX_REVIEW_CLEAN\n"
    assert not result.stdout.lstrip().startswith("{")


def test_review_with_provider_without_native_review_errors():
    result = CliRunner().invoke(
        main,
        ["review", "--with", "kimi", "--brief", "Review the PR."],
    )

    assert result.exit_code == 2
    assert "does not expose native code review" in result.output


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


def test_exec_task_file_dash_reads_stdin(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task-file", "-"],
        input="do the thing from stdin\n",
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.args[0] == "do the thing from stdin"


def test_exec_brief_file_reads_file(mocker, tmp_path):
    _stub_all_configured(mocker, {"codex"})
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Goal\nRun the delegated change.\n\n# Context\nUse the repository files.",
        encoding="utf-8",
    )
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--brief-file", str(brief)],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.args[0].startswith("# Goal")


def test_exec_short_brief_warns_by_default(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--brief", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert "brief is short" in result.stderr
    assert "--brief-file" in result.stderr


def test_exec_allow_short_brief_suppresses_warning(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--brief", "do it", "--allow-short-brief"],
    )

    assert result.exit_code == 0, result.output
    assert "brief is short" not in result.stderr


def test_exec_rejects_task_and_task_file_together(mocker):
    _stub_all_configured(mocker, {"codex"})

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "hi", "--task-file", "brief.md"],
    )

    assert result.exit_code == 2
    assert "exactly one of --brief, --brief-file, --task, --task-file, or stdin" in (
        result.output
    )


def test_exec_rejects_task_and_brief_together(mocker):
    _stub_all_configured(mocker, {"codex"})

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "hi", "--brief", "better brief"],
    )

    assert result.exit_code == 2
    assert "got --task, --brief" in result.output


def test_exec_missing_task_file_errors(mocker, tmp_path):
    _stub_all_configured(mocker, {"codex"})
    missing = tmp_path / "missing-brief.md"

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task-file", str(missing)],
    )

    assert result.exit_code == 2
    assert "could not read --task-file" in result.output
    assert str(missing) in result.output


def test_exec_cli_default_passes_no_timeout_to_provider(mocker):
    """`conductor exec --with codex --task ...` (no --timeout) must hand
    the provider `timeout_sec=None` so subprocess.run runs unbounded.
    Regression for the 22-minute lost-work bug where the CLI silently
    capped exec at 300s and the partial session_id was never recoverable."""
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do the thing"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert exec_mock.call_args.kwargs["timeout_sec"] is None, (
        "exec without --timeout must pass None (unbounded). "
        f"Got timeout_sec={exec_mock.call_args.kwargs['timeout_sec']!r}"
    )


def test_exec_cli_explicit_timeout_passes_through(mocker):
    """`--timeout 600` must be honored — the no-default change must not
    block users who deliberately want to bound a CI run."""
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--timeout", "600", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["timeout_sec"] == 600


def test_exec_cli_max_stall_seconds_flag_propagates(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        [
            "exec", "--with", "codex",
            "--max-stall-seconds", "60",
            "--task", "do it",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 60


def test_exec_cli_no_max_stall_seconds_defaults_to_360(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 360


def test_exec_cli_max_stall_seconds_zero_disables_watchdog(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--max-stall-seconds", "0", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] is None


def test_exec_cli_preflight_blocks_exec_and_surfaces_fix_hint(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(
        CodexProvider,
        "health_probe",
        return_value=(False, "network is unreachable"),
    )
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do it"],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "[conductor] preflight failed for codex: network is unreachable" in result.stderr
    assert "[conductor] try: brew install codex && codex login" in result.stderr


def test_exec_auto_preflight_failure_does_not_attribute_error_on_str_provider(mocker):
    """Regression: auto-mode `pick()` may return the provider name as a string
    (test fixtures, and some legacy callers); when preflight fails the cli used
    to call `provider.name` on that string and AttributeError. This test pins
    the failure mode by mocking `pick` to return ("codex", decision) and
    health_probe to fail — the buggy code raised AttributeError; the fixed
    code emits the standard preflight-failure message and exits 2.
    """
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(
        CodexProvider,
        "health_probe",
        return_value=(False, "network is unreachable"),
    )
    fake_decision = RouteDecision(
        provider="codex",
        prefer="best",
        effort="medium",
        thinking_budget=8000,
        tier="frontier",
        task_tags=("coding",),
        matched_tags=("coding",),
        tools_requested=("Read",),
        sandbox="workspace-write",
        ranked=(),
        candidates_skipped=(),
        tag_default_applied={},
    )
    mocker.patch("conductor.cli.pick", return_value=("codex", fake_decision))
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--tags", "coding", "--tools", "Read", "--task", "do it"],
    )

    assert result.exit_code == 2, result.output
    assert not exec_mock.called
    assert "AttributeError" not in result.output
    assert "[conductor] preflight failed for codex: network is unreachable" in result.stderr


def test_exec_cli_no_preflight_skips_probe(mocker):
    _stub_all_configured(mocker, {"codex"})
    probe_mock = mocker.patch.object(
        CodexProvider,
        "health_probe",
        return_value=(False, "network is unreachable"),
    )
    exec_mock = mocker.patch.object(
        CodexProvider, "exec", return_value=_fake_response("codex")
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert not probe_mock.called
    assert exec_mock.called


def test_exec_json_surfaces_codex_auth_prompt_and_records_auth_prompts(
    mocker, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    ndjson = [
        '{"type":"session.created","session_id":"sess-auth-1"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(
        stdout_lines=ndjson,
        stderr_lines=[
            "Please visit https://chatgpt.com/oauth/device to authenticate\n"
        ],
    )
    mocker.patch(
        "conductor.providers.codex.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--task", "hi", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["text"] == "hello from codex"
    assert payload["auth_prompts"] == [
        {
            "provider": "codex",
            "message": "provider is waiting for OAuth completion",
            "source": "stderr",
            "url": "https://chatgpt.com/oauth/device",
        }
    ]
    assert "[conductor] auth required for codex" in result.stderr
    assert "complete the flow at: https://chatgpt.com/oauth/device" in result.stderr


def test_exec_json_omits_auth_prompts_when_no_notice(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    ndjson = [
        '{"type":"session.created","session_id":"sess-auth-2"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(stdout_lines=ndjson)
    mocker.patch(
        "conductor.providers.codex.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--task", "hi", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "auth_prompts" not in payload
    assert "[conductor] auth required for codex" not in result.stderr


def test_exec_non_json_still_surfaces_codex_auth_prompt(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    ndjson = [
        '{"type":"session.created","session_id":"sess-auth-3"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(
        stdout_lines=ndjson,
        stderr_lines=[
            "Please visit https://chatgpt.com/oauth/device to authenticate\n"
        ],
    )
    mocker.patch(
        "conductor.providers.codex.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "hello from codex"
    assert "[conductor] auth required for codex" in result.stderr


def test_exec_with_kimi_tools_raises_unsupported(mocker, monkeypatch):
    # Kimi remains a preset chat provider; OpenRouter's generic provider owns
    # the tool-loop fallback.
    from conductor.providers.kimi import KimiProvider

    monkeypatch.setattr(KimiProvider, "supported_tools", frozenset())
    _stub_all_configured(mocker, {"kimi"})

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "kimi", "--tools", "Edit", "--task", "hi"],
    )

    assert result.exit_code == 2
    assert (
        "UnsupportedCapability" in result.stderr
        or "not supported" in result.stderr
        or "does not support" in result.stderr
    )


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
