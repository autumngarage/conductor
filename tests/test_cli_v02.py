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
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from conductor import offline_mode
from conductor.cli import (
    SANDBOX_DEPRECATION_WARNING,
    _resolve_exec_max_iterations,
    main,
)
from conductor.network_profile import NetworkProfile
from conductor.openrouter_model_stacks import OPENROUTER_CODING_HIGH
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
    ProviderConfigError,
    ProviderError,
    ProviderExecutionError,
)
from conductor.router import RouteDecision, reset_health
from conductor.session_log import SessionLog


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


def _fake_response(
    provider: str = "claude",
    model: str = "sonnet",
    *,
    text: str = "hello",
    cost_usd: float | None = 0.01,
    output_tokens: int | None = 10,
) -> CallResponse:
    return CallResponse(
        text=text,
        provider=provider,
        model=model,
        duration_ms=1234,
        usage={
            "input_tokens": 50,
            "output_tokens": output_tokens,
            "thinking_tokens": 4_000,
            "cached_tokens": 0,
        },
        cost_usd=cost_usd,
        raw={},
    )


def _mock_issue_subprocess(mocker, *, origin: str = "git@github.com:autumngarage/conductor.git"):
    calls: list[list[str]] = []
    issue_payload = {
        "title": "Brief from issue",
        "body": "Implement the issue body.",
        "labels": [{"name": "enhancement"}, {"name": "cli"}],
        "comments": [
            {
                "author": {"login": "alice"},
                "createdAt": "2026-05-01T12:00:00Z",
                "body": "Older context.",
            },
            {
                "author": {"login": "bob"},
                "createdAt": "2026-05-02T12:00:00Z",
                "body": "Recent context.",
            },
        ],
    }

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{origin}\n", stderr="")
        if cmd[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(issue_payload),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    mocker.patch("conductor._issue_briefs.subprocess.run", side_effect=fake_run)
    return calls


def _make_diff_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    (repo / "README.md").write_text("base\nfallback diff marker\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, env=env, check=True)
    return repo


def _commit_test_change(repo: Path, name: str) -> None:
    target = repo / f"{name}.txt"
    target.write_text(f"{name}\n", encoding="utf-8")
    subprocess.run(["git", "add", target.name], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            name,
        ],
        cwd=repo,
        check=True,
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

    def read(self, size: int = -1) -> str:
        if size == 0:
            return ""
        return self.readline()


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

    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "best", "--task", "hi"])

    assert result.exit_code == 0
    assert "hello" in result.stdout
    # Route log on stderr names the chosen provider and prefer mode.
    assert "[conductor] best" in result.stderr
    assert "→ claude" in result.stderr
    assert "tier: frontier" in result.stderr


def test_call_auto_route_json_includes_prompt_size_estimate(mocker):
    _stub_all_configured(mocker, {"claude"})
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))
    prompt = "x" * 1000

    result = CliRunner().invoke(
        main,
        ["call", "--auto", "--prefer", "best", "--task", prompt, "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["usage"]["input_tokens"] == 50
    assert payload["route"]["estimated_input_tokens"] == 250
    assert payload["route"]["estimated_output_tokens"] == 500
    assert payload["route"]["estimated_thinking_tokens"] == 8_000
    assert payload["route"]["ranked"][0]["estimated_input_tokens"] == 250


def test_call_auto_effort_max_flows_to_provider(mocker):
    _stub_all_configured(mocker, {"claude"})
    call_mock = mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

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
    codex_call = mocker.patch.object(CodexProvider, "call", return_value=_fake_response("codex"))
    mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        [
            "call",
            "--auto",
            "--prefer",
            "best",
            "--exclude",
            "claude",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0
    assert codex_call.called  # codex picked because claude was excluded
    assert "→ codex" in result.stderr


def test_call_invalid_prefer_errors_with_hint():
    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "beast", "--task", "hi"])
    assert result.exit_code == 2
    assert "--prefer='beast'" in result.output or "--prefer=beast" in result.output
    assert "best" in result.output


def test_call_invalid_effort_errors_with_hint():
    result = CliRunner().invoke(main, ["call", "--auto", "--effort", "maxx", "--task", "hi"])
    assert result.exit_code == 2
    assert "--effort" in result.output
    assert "max" in result.output


def test_call_effort_accepts_integer_budget():
    # Just the parse path; no provider invocation needed.
    result = CliRunner().invoke(main, ["call", "--auto", "--effort", "-5", "--task", "hi"])
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
    result = CliRunner().invoke(main, ["call", "--auto", "--resume", "abc-123", "--task", "hi"])
    assert result.exit_code == 2
    assert "--resume requires --with" in result.output


def test_call_resume_passes_session_id_to_provider(mocker):
    _stub_all_configured(mocker, {"claude"})
    call_mock = mocker.patch.object(ClaudeProvider, "call", return_value=_fake_response("claude"))
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

    result = CliRunner().invoke(main, ["call", "--auto", "--silent-route", "--task", "hi"])

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


def test_call_auto_can_route_to_openrouter_without_catalog_restrictions(mocker, monkeypatch):
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
    assert "plugins" not in captured["payload"]


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


def test_ask_research_rejects_repo_side_effect_brief(mocker):
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
            "medium",
            "--brief",
            (
                "Synthesize a doctrine candidate, write it to "
                ".cortex/doctrine/candidate.md, commit it, push, and open a PR."
            ),
        ],
    )

    assert result.exit_code == 2
    assert not call_mock.called
    assert "uses call mode" in result.output
    assert "cannot write local files" in result.output
    assert "conductor ask --kind code --effort high" in result.output


def test_ask_call_mode_allows_read_only_text_recommendations(mocker):
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
            "code",
            "--effort",
            "low",
            "--brief",
            (
                "Read-only investigation; do not edit files. "
                "Expected output: files to update and regression tests to add/update."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.called


def test_ask_research_allows_text_only_test_recommendation_brief(mocker):
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
            "medium",
            "--brief",
            (
                "Read-only analysis task. Do not modify files, commit changes, "
                "push, or open a PR. Recommend focused regression tests only."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.called


def test_ask_code_call_mode_allows_text_only_code_analysis(mocker):
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
            "code",
            "--effort",
            "low",
            "--brief",
            (
                "Text-only code analysis. Do not modify files or implement the fix. "
                "Explain the likely root cause and recommend tests to add."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.called


def test_ask_code_high_routes_to_codex_exec_with_default_tools(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

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
    assert exec_mock.call_args.kwargs["sandbox"] == "none"


def test_ask_code_high_read_only_brief_restricts_exec_tools(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

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
            (
                "Read-only advisory task. Do not modify files. "
                "Inspect the repo and recommend the schema/stage approach."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert exec_mock.call_args.kwargs["tools"] == frozenset({"Read", "Grep", "Glob"})
    assert exec_mock.call_args.kwargs["sandbox"] == "read-only"
    assert "read-only brief detected; restricting exec tools to Read,Grep,Glob" in result.stderr


def test_ask_code_high_falls_back_immediately_to_openrouter_on_quota(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"codex", "openrouter"})
    codex_exec = mocker.patch.object(
        CodexProvider,
        "exec",
        side_effect=ProviderHTTPError("codex reported rate limit: out of tokens"),
    )
    openrouter_exec = mocker.patch.object(
        OpenRouterProvider,
        "exec",
        return_value=_fake_response("openrouter"),
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
    assert codex_exec.called
    assert openrouter_exec.called
    assert openrouter_exec.call_args.kwargs["models"] == OPENROUTER_CODING_HIGH
    assert "codex failed (rate-limit)" in result.stderr
    assert "falling back" in result.stderr
    assert "→ openrouter" in result.stderr


def test_ask_code_high_falls_back_to_openrouter_exec_before_ollama(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"codex", "openrouter", "ollama"})
    codex_exec = mocker.patch.object(
        CodexProvider,
        "exec",
        side_effect=ProviderHTTPError("codex reported rate limit: out of tokens"),
    )
    exec_mock = mocker.patch.object(
        OpenRouterProvider,
        "exec",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    ollama_exec = mocker.patch.object(OllamaProvider, "exec", return_value=_fake_response("ollama"))

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
    assert codex_exec.called
    assert exec_mock.called
    assert not ollama_exec.called
    assert exec_mock.call_args.kwargs["tools"] == frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    assert exec_mock.call_args.kwargs["models"] == OPENROUTER_CODING_HIGH
    assert "excluding ollama from fallback chain" in result.stderr
    assert "falling through to ollama" not in result.stderr
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "codex",
        "openrouter",
    ]
    assert payload["semantic"]["candidates"][1]["models"] == list(OPENROUTER_CODING_HIGH)


def test_ask_code_high_without_frontier_fallback_refuses_ollama(mocker):
    _stub_all_configured(mocker, {"codex", "ollama"})
    codex_exec = mocker.patch.object(
        CodexProvider,
        "exec",
        side_effect=ProviderExecutionError(
            "codex execution failed: no-op",
            provider="codex",
            status={"state": "no-op", "repo_changing": True},
        ),
    )
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "qwen2.5-coder:14b"),
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

    assert result.exit_code == 1, result.output
    assert codex_exec.called
    assert not ollama_exec.called
    assert "excluding ollama from fallback chain" in result.stderr
    assert (
        "conductor: no usable fallback for --kind code --effort high "
        "after primary codex failed (codex execution failed: no-op)."
    ) in result.stderr
    assert "Configure another frontier provider, or relax --effort to medium." in result.stderr
    assert "falling through to ollama" not in result.stderr


def test_ask_code_medium_online_excludes_ollama_from_fallback(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--effort",
            "medium",
            "--allow-short-brief",
            "--brief",
            "Explain the small code change.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_call.called
    assert not ollama_call.called
    assert result.stderr == (
        "[conductor] excluding ollama from fallback chain "
        "(online; ollama is offline-only — pass --offline to override)\n"
    )
    assert "excluding ollama from fallback chain" in result.stderr
    assert "online; ollama is offline-only" in result.stderr
    assert "falling through to ollama" not in result.stderr
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "openrouter",
    ]


def test_ask_offline_keeps_ollama_in_semantic_candidates(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "research",
            "--effort",
            "low",
            "--offline",
            "--brief",
            "Use local model.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_call.called
    assert "excluding ollama from fallback chain" not in result.stderr
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "ollama",
    ]


def test_ask_sticky_offline_keeps_ollama_without_policy_message(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    offline_mode.set_active(ttl_sec=600)
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
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
            "Use sticky local model.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_call.called
    assert "excluding ollama from fallback chain" not in result.stderr
    assert "including ollama as local fallback" not in result.stderr
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "openrouter",
        "ollama",
    ]


def test_ask_network_probe_offline_keeps_ollama_fallback(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"openrouter", "ollama"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(None, "https://1.1.1.1", 1_000),
    )
    mocker.patch.object(
        OpenRouterProvider,
        "call",
        side_effect=ProviderHTTPError("OpenRouter provider failed with HTTP 503"),
    )
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "research",
            "--effort",
            "high",
            "--brief",
            "Find the relevant background and summarize it.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_call.called
    assert "including ollama as local fallback" in result.stderr
    assert "network probe found no reachable target" in result.stderr
    assert "excluding ollama from fallback chain" not in result.stderr
    assert "openrouter failed (5xx)" in result.stderr
    assert "falling through to ollama" in result.stderr
    assert "→ ollama" in result.stderr


def test_ask_explicit_ollama_tag_keeps_ollama_fallback(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "research",
            "--effort",
            "low",
            "--tags",
            "ollama,cheap",
            "--brief",
            "Keep local fallback available.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_call.called
    assert not ollama_call.called
    assert "excluding ollama from fallback chain" not in result.stderr
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "openrouter",
        "ollama",
    ]
    assert "ollama" in payload["semantic"]["tags"]


def test_call_with_ollama_bypasses_semantic_ollama_policy(mocker):
    _stub_all_configured(mocker, {"ollama"})
    profile_mock = mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(50, "http://localhost:11434", 1_000),
    )
    ollama_call = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        ["call", "--with", "ollama", "--brief", "Use the local provider."],
    )

    assert result.exit_code == 0, result.output
    assert ollama_call.called
    assert profile_mock.called
    assert "excluding ollama from fallback chain" not in result.stderr


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
    assert payload["cost_usd"] == pytest.approx(0.04)
    assert payload["usage"]["cost_accounting_complete"] is True
    assert payload["raw"]["conductor_council"]["member_cost_usd"] == [
        0.01,
        0.01,
        0.01,
    ]
    assert payload["raw"]["conductor_council"]["synthesis_cost_usd"] == 0.01
    assert payload["raw"]["conductor_council"]["known_cost_usd"] == pytest.approx(0.04)


def test_ask_council_marks_incomplete_cost_accounting(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    responses = [
        _fake_response("openrouter", "member-a", cost_usd=0.01),
        _fake_response("openrouter", "member-b", cost_usd=None),
        _fake_response("openrouter", "member-c", cost_usd=0.03),
        _fake_response("openrouter", "synthesis", cost_usd=0.04),
    ]
    mocker.patch.object(OpenRouterProvider, "call", side_effect=responses)

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
    payload = json.loads(result.stdout)
    assert payload["cost_usd"] is None
    assert payload["usage"]["cost_accounting_complete"] is False
    assert payload["usage"]["known_cost_usd"] == pytest.approx(0.08)
    assert payload["usage"]["missing_costs"] == ["member[2]"]
    assert payload["raw"]["conductor_council"]["member_cost_usd"] == [
        0.01,
        None,
        0.03,
    ]


def test_ask_council_continues_after_member_failure(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    responses = [
        _fake_response("openrouter", "member-a", cost_usd=0.01),
        ProviderError("OpenRouter produced empty response content: finish_reason=length"),
        _fake_response("openrouter", "member-c", cost_usd=0.03),
        _fake_response("openrouter", "synthesis", cost_usd=0.04),
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
    assert call_mock.call_args_list[2].kwargs["model"] == "deepseek/deepseek-v4-pro"
    synthesis_prompt = call_mock.call_args_list[3].args[0]
    assert "## Member 2: ~moonshotai/kimi-latest" in synthesis_prompt
    assert "council member failed: ProviderError" in synthesis_prompt
    payload = json.loads(result.stdout)
    council = payload["raw"]["conductor_council"]
    assert payload["usage"]["council_complete"] is True
    assert payload["usage"]["council_failed_members"] == 1
    assert payload["usage"]["cost_accounting_complete"] is False
    assert payload["cost_usd"] is None
    assert council["member_errors"] == [
        {
            "model": "~moonshotai/kimi-latest",
            "type": "ProviderError",
            "message": "OpenRouter produced empty response content: finish_reason=length",
        }
    ]


def test_ask_council_fails_when_all_members_fail(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    call_mock = mocker.patch.object(
        OpenRouterProvider,
        "call",
        side_effect=[
            ProviderError("member one failed"),
            ProviderError("member two failed"),
            ProviderError("member three failed"),
        ],
    )

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

    assert result.exit_code != 0
    assert call_mock.call_count == 3
    assert "council failed: all member calls failed" in result.output


def test_ask_council_known_cost_cap_returns_partial_error(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    responses = [
        _fake_response("openrouter", "member-a", cost_usd=0.02),
        _fake_response("openrouter", "member-b", cost_usd=0.02),
        _fake_response("openrouter", "member-c", cost_usd=0.02),
        _fake_response("openrouter", "synthesis", cost_usd=0.02),
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
            "--council-max-cost-usd",
            "0.03",
            "--brief",
            "Debate this architecture decision.",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "known cost cap" in result.stderr
    assert call_mock.call_count == 2
    assert "models" not in call_mock.call_args_list[-1].kwargs
    payload = json.loads(result.stdout)
    council = payload["raw"]["conductor_council"]
    assert payload["usage"]["council_complete"] is False
    assert payload["usage"]["known_cost_usd"] == pytest.approx(0.04)
    assert council["complete"] is False
    assert council["cap_hit"]["kind"] == "known_cost_usd"
    assert council["cap_hit"]["completed_member_calls"] == 2
    assert council["cap_hit"]["total_member_calls"] == 3
    assert council["cap_hit"]["skipped_member_models"] == ["deepseek/deepseek-v4-pro"]
    assert council["synthesis_cost_usd"] is None
    assert "## Member 2: member-b" in payload["text"]


def test_ask_council_output_token_cap_returns_partial_error(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    responses = [
        _fake_response("openrouter", "member-a", output_tokens=12),
        _fake_response("openrouter", "member-b", output_tokens=12),
        _fake_response("openrouter", "member-c", output_tokens=12),
        _fake_response("openrouter", "synthesis", output_tokens=12),
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
            "--council-max-output-tokens",
            "12",
            "--brief",
            "Debate this architecture decision.",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "output-token cap" in result.stderr
    assert call_mock.call_count == 1
    assert call_mock.call_args_list[0].kwargs["max_tokens"] == 12
    payload = json.loads(result.stdout)
    council = payload["raw"]["conductor_council"]
    assert payload["usage"]["council_known_output_tokens"] == 12
    assert council["cap_hit"]["kind"] == "output_tokens"
    assert council["cap_hit"]["observed"] == 12
    assert council["cap_hit"]["completed_member_calls"] == 1
    assert council["member_output_tokens"] == [12]
    assert council["requested_synthesis_models"] == [
        "~google/gemini-pro-latest",
        "~openai/gpt-latest",
    ]


def test_ask_council_wall_clock_cap_returns_partial_error(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    mocker.patch("conductor.cli.time.monotonic", side_effect=[0.0, 0.0, 2.0])
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
            "--council-timeout",
            "1",
            "--brief",
            "Debate this architecture decision.",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "wall-clock cap" in result.stderr
    assert call_mock.call_count == 1
    payload = json.loads(result.stdout)
    council = payload["raw"]["conductor_council"]
    assert payload["duration_ms"] == 2000
    assert council["elapsed_ms"] == 2000
    assert council["cap_hit"]["kind"] == "wall_clock"
    assert council["cap_hit"]["elapsed_sec"] == 2.0
    assert council["cap_hit"]["completed_member_calls"] == 1
    assert council["cap_hit"]["model"] == "~google/gemini-pro-latest"


def test_council_synthesis_prompt_handles_empty_member_text():
    from conductor.cli import _council_synthesis_prompt

    response = CallResponse(
        text=None,
        provider="openrouter",
        model="member-a",
        duration_ms=10,
        usage={},
        raw={},
    )

    prompt = _council_synthesis_prompt("Debate this.", [response])

    assert "## Member 1: member-a\n\n[empty response]" in prompt
    assert "None" not in prompt


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


def test_review_auto_uses_semantic_priority_over_router_scoring(
    mocker, monkeypatch, tmp_path
):
    defaults = tmp_path / "router.toml"
    defaults.write_text('[tag_defaults]\ncode-review = "claude"\n', encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_ROUTER_DEFAULTS_FILE", str(defaults))
    monkeypatch.setenv(
        "CONDUCTOR_REPO_ROUTER_DEFAULTS_FILE",
        str(tmp_path / "missing-repo-router.toml"),
    )
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    codex_review = mocker.patch.object(
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
    assert codex_review.called
    assert not claude_review.called
    assert codex_review.call_args.kwargs["base"] == "origin/main"
    assert "→ codex" in result.stderr


def test_review_without_auto_or_with_uses_semantic_review_route(mocker, tmp_path):
    brief = tmp_path / "review.md"
    brief.write_text("Review this merge using the project reviewer guide.", encoding="utf-8")
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    codex_review = mocker.patch.object(
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
            "--base",
            "origin/main",
            "--brief-file",
            str(brief),
        ],
    )

    assert result.exit_code == 0, result.output
    assert codex_review.called
    assert not claude_review.called
    assert codex_review.call_args.kwargs["base"] == "origin/main"
    assert "→ codex" in result.stderr


def test_review_with_openrouter_uses_hosted_review_prompt(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"openrouter"})
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response(
            "openrouter",
            OPENROUTER_CODING_HIGH[0],
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--with",
            "openrouter",
            "--cwd",
            str(repo),
            "--base",
            "HEAD~1",
            "--tags",
            "code-review,tool-use",
            "--brief",
            (
                "Review this merge. The LAST line of your output must be exactly "
                "CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_call.called
    assert openrouter_call.call_args.kwargs["models"] == OPENROUTER_CODING_HIGH
    assert openrouter_call.call_args.kwargs["task_tags"] == ["code-review"]
    assert openrouter_call.call_args.kwargs["prefer"] == "best"
    prompt = openrouter_call.call_args.args[0]
    assert "Patch context for generic review fallback" in prompt
    assert "diff --git a/README.md b/README.md" in prompt
    assert result.stdout.strip().endswith("CODEX_REVIEW_CLEAN")


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
    assert (
        "review infrastructure failed before any provider returned a valid verdict"
        in result.stderr
    )
    assert "codex (stall), claude (timeout)" in result.stderr
    assert "Provider status:" in result.stderr
    assert "codex: stall - ProviderStalledError: codex review stalled" in result.stderr
    assert "claude: timeout - ProviderError: claude review timed out" in result.stderr
    assert "continue the coding/review task directly in the driving agent" in result.stderr
    assert "do not treat this Conductor output as clean or complete" in result.stderr


def test_review_auto_claude_rate_limit_falls_through_to_next_provider(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"claude", "openrouter"})
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    claude_review = mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderHTTPError("Anthropic returned 429 rate limit"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response(
            "openrouter",
            OPENROUTER_CODING_HIGH[0],
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--exclude",
            "codex",
            "--brief",
            "Review this merge. End with CODEX_REVIEW_CLEAN or BLOCKED.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert claude_review.called
    assert openrouter_call.called
    assert result.stdout.strip().endswith("CODEX_REVIEW_CLEAN")
    assert "claude review unavailable (rate-limit)" in result.stderr
    assert "falling back" in result.stderr
    assert "code review failed for all tried providers" not in result.stderr


def test_review_auto_next_provider_success_json_reports_winner_and_status(mocker):
    from conductor.providers.interface import ProviderHTTPError

    _stub_all_configured(mocker, {"claude", "openrouter"})
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderHTTPError("Anthropic returned 429 rate limit"),
    )
    mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response(
            "openrouter",
            OPENROUTER_CODING_HIGH[0],
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--exclude",
            "codex",
            "--json",
            "--brief",
            "Review this merge. End with CODEX_REVIEW_CLEAN or BLOCKED.",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["provider"] == "openrouter"
    assert payload["text"].endswith("CODEX_REVIEW_CLEAN")
    assert payload["raw"]["review_provider_statuses"] == [
        {
            "provider": "claude",
            "status": "rate-limit",
            "detail": "ProviderHTTPError: Anthropic returned 429 rate limit",
        }
    ]


def test_review_auto_output_contract_failure_falls_through_to_next_provider(mocker):
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        CodexProvider,
        "review",
        return_value=_fake_response(
            "codex",
            "codex-review",
            text="The patch looks safe, but I omitted the required marker.",
        ),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=_fake_response(
            "claude",
            "sonnet",
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip().endswith("CODEX_REVIEW_CLEAN")
    assert "codex review failed (output-contract)" in result.stderr
    assert "falling back" in result.stderr


def test_review_auto_quarantines_invalid_output_with_possible_findings(mocker):
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        CodexProvider,
        "review",
        return_value=_fake_response(
            "codex",
            "codex-review",
            text=(
                "Blocking issues\n"
                "- src/conductor/router.py:42 drops provider failures during "
                "fallback, so this must block the review gate.\n"
            ),
        ),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=_fake_response(
            "claude",
            "sonnet",
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 1
    assert "CODEX_REVIEW_CLEAN" not in result.stdout
    assert "quarantined possible findings" in result.stderr
    assert "src/conductor/router.py:42" in result.stderr
    assert "malformed output was not accepted as a review result" in result.stderr


def test_review_auto_generic_fallback_prompt_includes_diff(mocker, tmp_path):
    from conductor.providers.interface import ProviderStalledError

    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"codex", "claude", "openrouter"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderStalledError("claude review stalled"),
    )
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "test-model"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--cwd",
            str(repo),
            "--base",
            "HEAD~1",
            "--tags",
            "code-review,tool-use",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_call.call_args.kwargs["models"] == OPENROUTER_CODING_HIGH
    assert openrouter_call.call_args.kwargs["task_tags"] == ["code-review"]
    assert openrouter_call.call_args.kwargs["timeout_sec"] == 75
    assert openrouter_call.call_args.kwargs["max_stall_sec"] == 75
    prompt = openrouter_call.call_args.args[0]
    assert "Patch context for generic review fallback" in prompt
    assert "diff --git a/README.md b/README.md" in prompt
    assert "+fallback diff marker" in prompt


def test_ask_review_uses_openrouter_code_stack_instead_of_gemini(mocker, tmp_path):
    from conductor.providers.interface import ProviderStalledError

    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"codex", "claude", "gemini", "openrouter"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(GeminiProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderStalledError("claude review stalled"),
    )
    gemini_review = mocker.patch.object(
        GeminiProvider,
        "review",
        return_value=_fake_response("gemini", "gemini-2.5-pro"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", OPENROUTER_CODING_HIGH[0]),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "review",
            "--cwd",
            str(repo),
            "--base",
            "HEAD~1",
            "--tags",
            "code-review,tool-use",
            "--brief",
            "Review this merge using the project reviewer guide.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not gemini_review.called
    assert openrouter_call.called
    assert openrouter_call.call_args.kwargs["models"] == OPENROUTER_CODING_HIGH
    assert openrouter_call.call_args.kwargs["task_tags"] == ["code-review"]
    assert openrouter_call.call_args.kwargs["timeout_sec"] == 75
    assert openrouter_call.call_args.kwargs["max_stall_sec"] == 75
    prompt = openrouter_call.call_args.args[0]
    assert "Patch context for generic review fallback" in prompt
    payload = json.loads(result.stdout)
    assert [candidate["provider"] for candidate in payload["semantic"]["candidates"]] == [
        "codex",
        "claude",
        "openrouter",
    ]
    assert payload["semantic"]["candidates"][2]["models"] == list(OPENROUTER_CODING_HIGH)


def test_review_auto_generic_fallback_rejects_missing_requested_sentinel(
    mocker, tmp_path
):
    from conductor.providers.interface import ProviderStalledError

    # Missing review sentinels are provider contract failures. The CLI must not
    # manufacture a verdict for a generic call()-based review fallback.
    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"codex", "claude", "openrouter"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderStalledError("claude review stalled"),
    )
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=CallResponse(
            text=(
                "The changes consistently route through the requested path. "
                "I did not find a blocking correctness issue."
            ),
            provider="openrouter",
            model="test-model",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--cwd",
            str(repo),
            "--base",
            "HEAD~1",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 1
    assert (
        "review infrastructure failed before any provider returned a valid verdict"
        in result.stderr
    )
    assert "openrouter (output-contract)" in result.stderr
    assert "ReviewOutputContractError" in result.stderr


def test_review_auto_openrouter_no_context_blocked_is_infrastructure_failure(
    mocker, tmp_path
):
    from conductor.providers.interface import ProviderStalledError

    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"codex", "claude", "openrouter"})
    mocker.patch.object(CodexProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderStalledError("claude review stalled"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=CallResponse(
            text=(
                "I cannot access repository tools or inspect the diff, so I "
                "cannot provide an actionable code review.\nCODEX_REVIEW_BLOCKED"
            ),
            provider="openrouter",
            model="test-model",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--cwd",
            str(repo),
            "--base",
            "HEAD~1",
            "--tags",
            "code-review,tool-use",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    prompt = openrouter_call.call_args.args[0]
    assert "Patch context for generic review fallback" in prompt
    assert "diff --git a/README.md b/README.md" in prompt
    assert "+fallback diff marker" in prompt
    assert "openrouter (output-contract)" in result.stderr
    assert "missing-context" in result.stderr
    assert "cannot provide an actionable code review" in result.stderr


def test_review_auto_codex_subprocess_rejects_missing_requested_sentinel(mocker):
    # Exercises the same conductor review --auto -> codex subprocess path used
    # by scripts/codex-review.sh when codex is the selected native reviewer.
    _stub_all_configured(mocker, {"codex"})
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["codex", "review"],
            returncode=0,
            stdout=(
                "The changes consistently route through the requested path. "
                "I did not find a blocking correctness issue.\n"
            ),
            stderr="",
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--silent-route",
            "--base",
            "origin/main",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 1
    assert (
        "review infrastructure failed before any provider returned a valid verdict"
        in result.stderr
    )
    assert "codex (output-contract)" in result.stderr


def test_review_auto_codex_subprocess_retries_missing_requested_sentinel(mocker):
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess(
                args=["codex", "review"],
                returncode=0,
                stdout="No blocking issues were found in the diff.\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "review"],
                returncode=0,
                stdout=(
                    "No blocking issues were found in the diff.\n"
                    "CODEX_REVIEW_CLEAN\n"
                ),
                stderr="",
            ),
        ],
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
            "--silent-route",
            "--base",
            "origin/main",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured.call_count == 2
    assert "--base" not in captured.call_args_list[0].args[0]
    assert "Review changes against base branch/ref: origin/main" in (
        captured.call_args_list[1].kwargs["input"]
    )
    assert not claude_review.called
    assert result.stdout.strip().endswith("CODEX_REVIEW_CLEAN")


def test_review_auto_codex_subprocess_accepts_sentinel_with_footer(mocker):
    _stub_all_configured(mocker, {"codex", "claude"})
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["codex", "review"],
            returncode=0,
            stdout="No blocking issues found.\nCODEX_REVIEW_CLEAN\n---\nreview complete\n",
            stderr="",
        ),
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
            "--silent-route",
            "--brief",
            (
                "The LAST line of your output must be exactly one of these "
                "three sentinels: CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, "
                "or CODEX_REVIEW_BLOCKED."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured.called
    assert not claude_review.called
    assert result.stdout.strip().endswith("CODEX_REVIEW_CLEAN")
    assert "review complete\nCODEX_REVIEW_CLEAN" in result.stdout


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
            raw={"response": '{"response": "Plain review\\nCODEX_REVIEW_CLEAN"}'},
        ),
    )

    result = CliRunner().invoke(
        main,
        ["review", "--with", "gemini", "--brief", "End with CODEX_REVIEW_CLEAN."],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "Plain review\nCODEX_REVIEW_CLEAN\n"
    assert not result.stdout.lstrip().startswith("{")


def test_review_with_openrouter_backed_provider_uses_call_prompt(mocker):
    _stub_all_configured(mocker, {"kimi"})
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("kimi"),
    )

    result = CliRunner().invoke(
        main,
        ["review", "--with", "kimi", "--brief", "Review the PR."],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "hello\n"
    assert openrouter_call.call_args.kwargs["task_tags"] == ["code-review"]


# ---------------------------------------------------------------------------
# exec — new subcommand
# ---------------------------------------------------------------------------


def test_exec_auto_routes_to_tool_capable_provider(mocker):
    _stub_all_configured(mocker, {"claude", "kimi"})
    exec_mock = mocker.patch.object(ClaudeProvider, "exec", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--tools",
            "Read,Grep,Edit",
            "--sandbox",
            "read-only",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0
    assert exec_mock.called
    assert SANDBOX_DEPRECATION_WARNING in result.stderr
    # kimi would be skipped by the tools filter (supported_tools=frozenset()).
    assert "→ claude" in result.stderr


def test_exec_unknown_tool_errors_with_hint():
    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--tools",
            "Read,NotARealTool",
            "--task",
            "hi",
        ],
    )
    assert result.exit_code == 2
    assert "NotARealTool" in result.output


def test_exec_no_write_validation_passes_through_to_http_tool_provider(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    exec_mock = mocker.patch.object(
        OpenRouterProvider, "exec", return_value=_fake_response("openrouter")
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "openrouter",
            "--no-write-validation",
            "--no-preflight",
            "--tools",
            "Edit",
            "--task",
            "write a corrupt fixture",
        ],
    )

    assert result.exit_code == 0
    assert exec_mock.call_args.kwargs["write_validation"] is False


@pytest.mark.parametrize("sandbox", ["workspace-write", "read-only", "strict", "none", "surprise"])
def test_exec_sandbox_values_warn_once_and_are_ignored(mocker, sandbox):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--sandbox", sandbox, "--no-preflight", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr.count(SANDBOX_DEPRECATION_WARNING) == 1
    assert exec_mock.call_args.kwargs["sandbox"] == "none"


def test_exec_without_sandbox_does_not_warn(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--no-preflight", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert SANDBOX_DEPRECATION_WARNING not in result.stderr


def test_exec_auto_cheapest_code_review_online_excludes_ollama_primary(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(50, "https://1.1.1.1", 1_000),
    )
    openrouter_exec = mocker.patch.object(
        OpenRouterProvider,
        "exec",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "cheapest",
            "--tags",
            "code-review",
            "--tools",
            "Read,Grep,Glob,Bash",
            "--no-preflight",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_exec.called
    assert not ollama_exec.called
    assert "excluding ollama from fallback chain" in result.stderr
    assert "online; ollama is offline-only" in result.stderr
    assert "→ openrouter" in result.stderr


def test_exec_auto_cheapest_code_review_offline_keeps_ollama(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "cheapest",
            "--tags",
            "code-review",
            "--tools",
            "Read,Grep,Glob,Bash",
            "--offline",
            "--no-preflight",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_exec.called
    assert "excluding ollama from fallback chain" not in result.stderr


def test_exec_auto_cheapest_network_probe_offline_keeps_ollama_primary(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(None, "https://1.1.1.1", 1_000),
    )
    openrouter_exec = mocker.patch.object(
        OpenRouterProvider,
        "exec",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "cheapest",
            "--tags",
            "code-review",
            "--tools",
            "Read,Grep,Glob,Bash",
            "--no-preflight",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_exec.called
    assert not openrouter_exec.called
    assert "including ollama as local fallback" in result.stderr
    assert "network probe found no reachable target" in result.stderr
    assert "excluding ollama from fallback chain" not in result.stderr
    assert "→ ollama" in result.stderr


def test_exec_auto_explicit_ollama_tag_keeps_ollama_primary(mocker):
    _stub_all_configured(mocker, {"openrouter", "ollama"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(50, "https://1.1.1.1", 1_000),
    )
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "cheapest",
            "--tags",
            "ollama",
            "--tools",
            "Read,Grep,Glob,Bash",
            "--no-preflight",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_exec.called
    assert "excluding ollama from fallback chain" not in result.stderr
    assert "→ ollama" in result.stderr


def test_exec_with_ollama_bypasses_auto_route_exclusions(mocker):
    _stub_all_configured(mocker, {"ollama"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(50, "https://1.1.1.1", 1_000),
    )
    ollama_exec = mocker.patch.object(
        OllamaProvider,
        "exec",
        return_value=_fake_response("ollama", "llama3.2"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "ollama",
            "--tools",
            "Read,Grep,Glob,Bash",
            "--no-preflight",
            "--task",
            "review the diff",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ollama_exec.called
    assert "excluding ollama from fallback chain" not in result.stderr


def test_exec_ground_citations_default_skips_guardrail(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))
    grounding_mock = mocker.patch("conductor.cli._emit_grounding_warnings")

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--allow-short-brief",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not grounding_mock.called


def test_exec_ground_citations_warns_to_stderr_not_stdout(mocker, tmp_path):
    _stub_all_configured(mocker, {"codex"})
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
    response_text = "Check `missing_symbol` in src/foo.py:1"
    mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=_fake_response("codex", text=response_text),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--allow-short-brief",
            "--cwd",
            str(tmp_path),
            "--ground-citations",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{response_text}\n"
    assert "grounding misses: 1" in result.stderr
    assert "`missing_symbol` in src/foo.py:1" in result.stderr
    assert "grounding misses" not in result.stdout


def test_exec_ground_citations_errors_warn_without_failing(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))
    mocker.patch(
        "conductor.grounding.ground_citations",
        side_effect=OSError("permission denied"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--allow-short-brief",
            "--ground-citations",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "hello\n"
    assert "[conductor] grounding check error: permission denied" in result.stderr


def test_exec_permission_profile_auto_routes_to_enforcing_provider(mocker):
    _stub_all_configured(mocker, {"claude", "codex", "gemini"})
    claude_exec = mocker.patch.object(ClaudeProvider, "exec", return_value=_fake_response("claude"))
    codex_exec = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))
    gemini_exec = mocker.patch.object(GeminiProvider, "exec", return_value=_fake_response("gemini"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--permission-profile",
            "read-only",
            "--no-preflight",
            "--allow-short-brief",
            "--task",
            "inspect without editing",
        ],
    )

    assert result.exit_code == 0, result.output
    assert claude_exec.called
    assert not codex_exec.called
    assert not gemini_exec.called
    assert claude_exec.call_args.kwargs["tools"] == frozenset({"Read", "Grep", "Glob"})
    assert "→ claude" in result.stderr


def test_exec_read_only_brief_with_test_recommendations_returns_text(mocker):
    _stub_all_configured(mocker, {"claude"})
    exec_mock = mocker.patch.object(
        ClaudeProvider,
        "exec",
        return_value=_fake_response("claude", text="Root cause analysis."),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--permission-profile",
            "read-only",
            "--no-preflight",
            "--allow-short-brief",
            "--task",
            (
                "Read-only analysis task. Do not modify files. "
                "Recommend focused regression tests only."
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert exec_mock.call_args.kwargs["tools"] == frozenset({"Read", "Grep", "Glob"})
    assert "Root cause analysis." in result.output


def test_exec_permission_profile_rejects_non_enforcing_direct_provider(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--permission-profile",
            "read-only",
            "--no-preflight",
            "--allow-short-brief",
            "--task",
            "inspect",
        ],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "requires a provider that enforces Conductor exec tool whitelists" in (result.output)
    assert "provider 'codex' does not" in result.output


def test_exec_permission_profile_rejects_conflicting_tools():
    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--permission-profile",
            "read-only",
            "--tools",
            "Read,Edit",
            "--allow-short-brief",
            "--task",
            "inspect",
        ],
    )

    assert result.exit_code == 2
    assert "--tools conflicts with --permission-profile='read-only'" in result.output
    assert "Read,Grep,Glob" in result.output


def test_exec_task_file_dash_reads_stdin(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

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
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--brief-file", str(brief)],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.args[0].startswith("# Goal")


def test_exec_issue_builds_provider_task(mocker):
    _stub_all_configured(mocker, {"codex"})
    _mock_issue_subprocess(mocker)
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--allow-short-brief",
            "--issue",
            "123",
        ],
    )

    assert result.exit_code == 0, result.output
    task = exec_mock.call_args.args[0]
    assert "# Issue: Brief from issue (autumngarage/conductor#123)" in task
    assert "Implement the issue body." in task
    assert "Recent context." in task


def test_exec_issue_missing_gh_errors_clearly(mocker):
    _stub_all_configured(mocker, {"codex"})

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="git@github.com:autumngarage/conductor.git\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "issue", "view"]:
            raise FileNotFoundError("gh")
        raise AssertionError(f"unexpected command: {cmd!r}")

    mocker.patch("conductor._issue_briefs.subprocess.run", side_effect=fake_run)

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--issue", "123"],
    )

    assert result.exit_code == 2
    assert "--issue requires the gh CLI; install via brew install gh" in result.output


def test_exec_issue_origin_timeout_errors_clearly(mocker):
    _stub_all_configured(mocker, {"codex"})

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "config", "--get", "remote.origin.url"]:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        raise AssertionError(f"unexpected command: {cmd!r}")

    mocker.patch("conductor._issue_briefs.subprocess.run", side_effect=fake_run)

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--issue", "123"],
    )

    assert result.exit_code == 2
    assert "--issue <N> timed out after 5s" in result.output


def test_exec_issue_gh_timeout_errors_clearly(mocker):
    _stub_all_configured(mocker, {"codex"})

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="git@github.com:autumngarage/conductor.git\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "issue", "view"]:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        raise AssertionError(f"unexpected command: {cmd!r}")

    mocker.patch("conductor._issue_briefs.subprocess.run", side_effect=fake_run)

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--issue", "123"],
    )

    assert result.exit_code == 2
    assert "timed out after 30s fetching GitHub issue" in result.output
    assert "gh issue view 123 --repo autumngarage/conductor" in result.output


def test_exec_issue_not_found_errors_with_recovery_command(mocker):
    _stub_all_configured(mocker, {"codex"})

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="git@github.com:autumngarage/conductor.git\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "issue", "view"]:
            raise subprocess.CalledProcessError(
                1,
                cmd,
                output="",
                stderr="GraphQL: Could not resolve to an Issue",
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    mocker.patch("conductor._issue_briefs.subprocess.run", side_effect=fake_run)

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--issue", "123"],
    )

    assert result.exit_code == 2
    assert "could not fetch GitHub issue autumngarage/conductor#123" in result.output
    assert (
        "Run gh issue view 123 --repo autumngarage/conductor --json title,body,labels,comments"
    ) in result.output
    assert "Could not resolve to an Issue" in result.output


def test_exec_injects_auto_close_instructions_into_provider_task(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--allow-short-brief",
            "--brief",
            "Fixes #5. Do X.",
        ],
    )

    assert result.exit_code == 0, result.output
    task = exec_mock.call_args.args[0]
    assert "## Auto-close" in task
    assert "Closes #5" in task


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
    assert "exactly one of --brief, --brief-file, --task, --task-file, or stdin" in (result.output)


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
    capped exec at 300s and the partial session_id was never recoverable.
    Network scaling must NOT materialize a timeout where there wasn't one
    — apply_scaling(None, profile) returns None.
    """
    _stub_all_configured(mocker, {"codex"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(310, "https://api.openai.com", 1_000),
    )
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

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
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--timeout", "600", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["timeout_sec"] == 600


def test_exec_cli_max_stall_seconds_flag_propagates(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--max-stall-seconds",
            "60",
            "--task",
            "do it",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 60


def test_exec_cli_no_max_stall_seconds_defaults_to_360(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 360


def test_exec_auto_default_stall_caps_under_timeout_for_fallback(mocker):
    from conductor.providers.interface import ProviderStalledError

    _stub_all_configured(mocker, {"claude", "codex"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(None, "https://api.openai.com", 1_000),
    )
    claude_exec = mocker.patch.object(
        ClaudeProvider,
        "exec",
        side_effect=ProviderStalledError("claude CLI stalled"),
    )
    codex_exec = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=_fake_response("codex"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--timeout",
            "300",
            "--tools",
            "Read",
            "--task",
            "Review the diff.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert claude_exec.call_args.kwargs["max_stall_sec"] == 75
    assert codex_exec.call_args.kwargs["max_stall_sec"] == 75
    assert "claude failed (timeout)" in result.stderr


def test_exec_auto_code_review_derives_budget_without_timeout(mocker):
    _stub_all_configured(mocker, {"claude", "codex"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(None, "https://api.openai.com", 1_000),
    )
    exec_mock = mocker.patch.object(
        ClaudeProvider,
        "exec",
        return_value=_fake_response("claude"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--tags",
            "code-review",
            "--tools",
            "Read",
            "--task",
            "Review the diff.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["timeout_sec"] == 300
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 75
    assert "review gate budget: timeout=300s stall=75s" in result.stderr


def test_exec_auto_code_review_ignores_caller_timeout_budget(mocker):
    _stub_all_configured(mocker, {"claude", "codex"})
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(None, "https://api.openai.com", 1_000),
    )
    exec_mock = mocker.patch.object(
        ClaudeProvider,
        "exec",
        return_value=_fake_response("claude"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--prefer",
            "best",
            "--tags",
            "code-review",
            "--timeout",
            "10",
            "--max-stall-seconds",
            "999",
            "--tools",
            "Read",
            "--task",
            "Review the diff.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["timeout_sec"] == 300
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 75
    assert "review gate budget: timeout=300s stall=75s" in result.stderr
    assert "ignored caller timeout=10s max-stall=999s" in result.stderr


def test_exec_cli_max_stall_seconds_zero_disables_watchdog(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--max-stall-seconds", "0", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_stall_sec"] is None


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("minimal", 10),
        ("low", 15),
        ("medium", 20),
        ("high", 60),
        ("max", 80),
    ],
)
def test_exec_max_iterations_default_scales_by_effort(effort, expected):
    assert _resolve_exec_max_iterations(None, raw_effort=effort) == expected


def test_exec_max_iterations_unset_effort_preserves_legacy_cap():
    assert _resolve_exec_max_iterations(None, raw_effort=None) == 10


def test_exec_max_iterations_explicit_override_ignores_effort(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    exec_mock = mocker.patch.object(
        OpenRouterProvider, "exec", return_value=_fake_response("openrouter")
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "openrouter",
            "--max-iterations",
            "100",
            "--allow-completion-stretch",
            "--effort",
            "minimal",
            "--task",
            "do it",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_iterations"] == 100
    assert exec_mock.call_args.kwargs["allow_completion_stretch"] is True
    assert "[conductor] agent loop iteration cap: 100" in result.stderr


def test_exec_max_iterations_explicit_passes_to_codex(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--max-iterations",
            "5",
            "--task",
            "do it",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["max_iterations"] == 5
    assert "[conductor] agent loop iteration cap: 5" in result.stderr


def test_exec_max_iterations_explicit_rejects_unsupported_provider(mocker):
    _stub_all_configured(mocker, {"claude"})
    exec_mock = mocker.patch.object(
        ClaudeProvider, "exec", return_value=_fake_response("claude")
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--max-iterations",
            "5",
            "--task",
            "do it",
        ],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "--max-iterations only applies" in result.output
    assert "claude cannot honor it" in result.output


def test_exec_max_iterations_banner_only_for_supporting_providers(mocker):
    _stub_all_configured(mocker, {"claude"})
    exec_mock = mocker.patch.object(ClaudeProvider, "exec", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "claude", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.called
    assert "agent loop iteration cap" not in result.stderr


def test_exec_cli_start_timeout_flag_propagates_to_claude(mocker):
    _stub_all_configured(mocker, {"claude"})
    exec_mock = mocker.patch.object(ClaudeProvider, "exec", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--start-timeout",
            "240",
            "--task",
            "do it",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["start_timeout_sec"] == 240


def test_exec_cli_start_timeout_zero_disables_watchdog(mocker):
    _stub_all_configured(mocker, {"claude"})
    exec_mock = mocker.patch.object(ClaudeProvider, "exec", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "claude", "--start-timeout", "0", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_args.kwargs["start_timeout_sec"] is None


def test_exec_cli_preflight_blocks_exec_and_surfaces_fix_hint(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(
        CodexProvider,
        "health_probe",
        return_value=(False, "network is unreachable"),
    )
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do it"],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "[conductor] preflight failed for codex: network is unreachable" in result.stderr
    assert "[conductor] try: brew install codex && codex login" in result.stderr


def test_exec_cli_preflight_suppresses_codex_fix_for_startup_probe_failure(mocker):
    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(
        CodexProvider,
        "health_probe",
        return_value=(
            False,
            "`codex exec` startup probe exited 1: invalid_request_error: "
            "The request was rejected.: param=tools",
        ),
    )
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "do it"],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "[conductor] preflight failed for codex:" in result.stderr
    assert "[conductor] try: brew install codex && codex login" not in result.stderr


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
        sandbox="none",
        ranked=(),
        candidates_skipped=(),
        tag_default_applied={},
    )
    mocker.patch("conductor.cli.pick", return_value=("codex", fake_decision))
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

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
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--no-preflight", "--task", "do it"],
    )

    assert result.exit_code == 0, result.output
    assert not probe_mock.called
    assert exec_mock.called


def test_exec_single_brief_file_preserves_call_response_json(mocker, tmp_path):
    _stub_all_configured(mocker, {"codex"})
    brief = tmp_path / "phase1.md"
    brief.write_text("single phase work\n", encoding="utf-8")
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=_fake_response("codex", text="done"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--brief-file",
            str(brief),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["text"] == "done"
    assert "phases" not in payload
    assert exec_mock.call_count == 1
    assert exec_mock.call_args.args[0] == "single phase work"


def test_exec_multiple_brief_files_run_sequential_phases_json(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    phase1 = tmp_path / "phase1.md"
    phase2 = tmp_path / "phase2.md"
    phase1.write_text("implement it\n", encoding="utf-8")
    phase2.write_text("test it\n", encoding="utf-8")
    seen_prompts: list[str] = []

    def fake_exec(self, prompt, **kwargs):
        seen_prompts.append(prompt)
        _commit_test_change(repo, f"phase-{len(seen_prompts)}")
        return _fake_response("codex", text=f"phase {len(seen_prompts)} done")

    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", autospec=True, side_effect=fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(phase1),
            "--brief-file",
            str(phase2),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_count == 2
    assert seen_prompts[0] == "implement it"
    assert seen_prompts[1].startswith("test it\n\n## Phase 1 results")
    assert "phase-1.txt" in seen_prompts[1]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [phase["exit"] for phase in payload["phases"]] == ["ok", "ok"]
    assert [phase["commits"] for phase in payload["phases"]] == [1, 1]
    assert [phase["brief"] for phase in payload["phases"]] == [str(phase1), str(phase2)]


def test_exec_auto_phase_splits_tests_and_validation_anchors(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    brief = tmp_path / "combined.md"
    brief.write_text(
        "# Brief: implement X\n\n"
        "Build the production path.\n\n"
        "## Tests\n\n"
        "Add regression tests.\n\n"
        "## Validation\n\n"
        "Run pytest.\n",
        encoding="utf-8",
    )
    seen_prompts: list[str] = []

    def fake_exec(self, prompt, **kwargs):
        seen_prompts.append(prompt)
        return _fake_response("codex", text=f"phase {len(seen_prompts)} done")

    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", autospec=True, side_effect=fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(brief),
            "--auto-phase",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_count == 3
    assert seen_prompts[0] == "# Brief: implement X\n\nBuild the production path."
    assert seen_prompts[1].startswith("## Tests\n\nAdd regression tests.")
    assert seen_prompts[2].startswith("## Validation\n\nRun pytest.")
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [phase["brief"] for phase in payload["phases"]] == [
        "Intro",
        "Tests",
        "Validation",
    ]


def test_exec_auto_phase_no_anchors_falls_back_to_single_phase(mocker, tmp_path):
    brief = tmp_path / "combined.md"
    single_phase_body = "# Brief\n\nDo one thing.\n\n## Notes\n\nDo not split here."
    brief.write_text(f"{single_phase_body}\n", encoding="utf-8")
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=_fake_response("codex", text="done"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--brief-file",
            str(brief),
            "--auto-phase",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_count == 1
    assert exec_mock.call_args.args[0] == single_phase_body
    assert "--auto-phase: no anchor headers found" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["text"] == "done"
    assert "phases" not in payload


def test_exec_auto_phase_splits_phase_number_anchors(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    brief = tmp_path / "combined.md"
    brief.write_text(
        "## Phase 1\n\nImplement it.\n\n## Phase 2\n\nTest it.\n",
        encoding="utf-8",
    )
    seen_prompts: list[str] = []

    def fake_exec(self, prompt, **kwargs):
        seen_prompts.append(prompt)
        return _fake_response("codex", text=f"phase {len(seen_prompts)} done")

    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", autospec=True, side_effect=fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(brief),
            "--auto-phase",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen_prompts[0] == "## Phase 1\n\nImplement it."
    assert seen_prompts[1].startswith("## Phase 2\n\nTest it.")
    payload = json.loads(result.stdout)
    assert [phase["brief"] for phase in payload["phases"]] == ["Phase 1", "Phase 2"]


def test_exec_auto_phase_custom_anchor_extends_default_list(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    brief = tmp_path / "combined.md"
    brief.write_text(
        "# Brief\n\nImplement it.\n\n## Custom\n\nCustom validation.\n",
        encoding="utf-8",
    )
    seen_prompts: list[str] = []

    def fake_exec(self, prompt, **kwargs):
        seen_prompts.append(prompt)
        return _fake_response("codex", text=f"phase {len(seen_prompts)} done")

    _stub_all_configured(mocker, {"codex"})
    mocker.patch.object(CodexProvider, "exec", autospec=True, side_effect=fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(brief),
            "--auto-phase",
            "--phase-anchor",
            "## Custom",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen_prompts[0] == "# Brief\n\nImplement it."
    assert seen_prompts[1].startswith("## Custom\n\nCustom validation.")
    payload = json.loads(result.stdout)
    assert [phase["brief"] for phase in payload["phases"]] == ["Intro", "Custom"]


def test_exec_auto_phase_rejects_repeated_brief_files(mocker, tmp_path):
    phase1 = tmp_path / "phase1.md"
    phase2 = tmp_path / "phase2.md"
    phase1.write_text("implement it\n", encoding="utf-8")
    phase2.write_text("test it\n", encoding="utf-8")
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec")

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--brief-file",
            str(phase1),
            "--brief-file",
            str(phase2),
            "--auto-phase",
        ],
    )

    assert result.exit_code == 2
    assert not exec_mock.called
    assert "use `--brief-file` repeated OR `--auto-phase` on a single brief, not both" in (
        result.output
    )


def test_exec_auto_phase_default_off_preserves_single_phase_behavior(mocker, tmp_path):
    brief = tmp_path / "combined.md"
    brief.write_text(
        "# Brief\n\nImplement it.\n\n## Tests\n\nThese stay in the same phase.\n",
        encoding="utf-8",
    )
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=_fake_response("codex", text="done"),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--brief-file",
            str(brief),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert exec_mock.call_count == 1
    assert "## Tests" in exec_mock.call_args.args[0]
    payload = json.loads(result.stdout)
    assert payload["text"] == "done"
    assert "phases" not in payload


def test_exec_multiple_brief_files_first_cap_exit_aborts_json(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    phase1 = tmp_path / "phase1.md"
    phase2 = tmp_path / "phase2.md"
    phase1.write_text("implement it\n", encoding="utf-8")
    phase2.write_text("test it\n", encoding="utf-8")
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        side_effect=ProviderExecutionError(
            "Reached --max-iterations cap (1).",
            provider="codex",
            status={"state": "iteration-cap", "iteration_cap": 1},
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(phase1),
            "--brief-file",
            str(phase2),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert exec_mock.call_count == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert len(payload["phases"]) == 1
    assert payload["phases"][0]["brief"] == str(phase1)
    assert payload["phases"][0]["exit"] == "cap-exit"
    assert payload["phases"][0]["commits"] == 0
    assert payload["phases"][0]["duration_ms"] >= 0
    assert "phase 1 failed (cap-exit)" in result.stderr


def test_exec_multiple_brief_files_second_error_aborts_before_third(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    phase1 = tmp_path / "phase1.md"
    phase2 = tmp_path / "phase2.md"
    phase3 = tmp_path / "phase3.md"
    phase1.write_text("implement it\n", encoding="utf-8")
    phase2.write_text("test it\n", encoding="utf-8")
    phase3.write_text("ship it\n", encoding="utf-8")

    def fake_exec(self, prompt, **kwargs):
        if "test it" in prompt:
            raise ProviderConfigError("codex config broke")
        _commit_test_change(repo, "phase-1")
        return _fake_response("codex", text="phase 1 done")

    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", autospec=True, side_effect=fake_exec)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--no-preflight",
            "--cwd",
            str(repo),
            "--brief-file",
            str(phase1),
            "--brief-file",
            str(phase2),
            "--brief-file",
            str(phase3),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert exec_mock.call_count == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert [phase["brief"] for phase in payload["phases"]] == [str(phase1), str(phase2)]
    assert [phase["exit"] for phase in payload["phases"]] == ["ok", "error"]
    assert "phase 2 failed (error)" in result.stderr


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
        stderr_lines=["Please visit https://chatgpt.com/oauth/device to authenticate\n"],
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
        stderr_lines=["Please visit https://chatgpt.com/oauth/device to authenticate\n"],
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

    result = CliRunner().invoke(main, ["route", "--prefer", "best", "--tags", "code-review"])

    assert result.exit_code == 0
    assert "would pick: claude" in result.output
    assert "tier: frontier" in result.output
    assert not call_mock.called  # router dry-run makes no calls


def test_route_json_mode(mocker):
    _stub_all_configured(mocker, {"claude"})
    result = CliRunner().invoke(main, ["route", "--prefer", "best", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["provider"] == "claude"
    assert payload["prefer"] == "best"


def test_route_json_accepts_explicit_size_estimate(mocker):
    _stub_all_configured(mocker, {"claude"})

    result = CliRunner().invoke(
        main,
        [
            "route",
            "--prefer",
            "best",
            "--estimated-input-tokens",
            "123",
            "--estimated-output-tokens",
            "45",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["estimated_input_tokens"] == 123
    assert payload["estimated_output_tokens"] == 45
    assert payload["estimated_thinking_tokens"] == 8_000
    assert payload["ranked"][0]["estimated_input_tokens"] == 123


def test_ask_call_mode_routes_with_prompt_size_estimate(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )
    prompt = "x" * 2000

    result = CliRunner().invoke(
        main,
        ["ask", "--kind", "code", "--effort", "low", "--task", prompt, "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["route"]["estimated_input_tokens"] == 500
    assert payload["route"]["estimated_output_tokens"] == 500


def test_ask_issue_number_uses_origin_and_builds_issue_brief(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    calls = _mock_issue_subprocess(mocker)
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
            "code",
            "--effort",
            "low",
            "--issue",
            "123",
            "--issue-comment-limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    task = call_mock.call_args.args[0]
    assert "# Issue: Brief from issue (autumngarage/conductor#123)" in task
    assert "Labels: enhancement, cli" in task
    assert "Implement the issue body." in task
    assert "Older context." not in task
    assert "Recent context." in task
    assert ["git", "config", "--get", "remote.origin.url"] in calls
    assert [
        "gh",
        "issue",
        "view",
        "123",
        "--repo",
        "autumngarage/conductor",
        "--json",
        "title,body,labels,comments",
    ] in calls


def test_ask_issue_repo_override_skips_origin_lookup(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    calls = _mock_issue_subprocess(mocker)
    call_mock = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter", "openrouter/auto"),
    )

    result = CliRunner().invoke(
        main,
        ["ask", "--kind", "code", "--effort", "low", "--issue", "org/repo#123"],
    )

    assert result.exit_code == 0, result.output
    assert "# Issue: Brief from issue (org/repo#123)" in call_mock.call_args.args[0]
    assert ["git", "config", "--get", "remote.origin.url"] not in calls
    assert calls[0][:6] == ["gh", "issue", "view", "123", "--repo", "org/repo"]


def test_ask_issue_appends_brief_as_operator_context(mocker):
    _stub_all_configured(mocker, {"openrouter"})
    _mock_issue_subprocess(mocker)
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
            "code",
            "--effort",
            "low",
            "--issue",
            "123",
            "--brief",
            "extra context",
        ],
    )

    assert result.exit_code == 0, result.output
    task = call_mock.call_args.args[0]
    assert task.index("Implement the issue body.") < task.index(
        "## Operator-supplied additional context"
    )
    assert "extra context" in task


def test_ask_exec_mode_injects_auto_close_instructions_into_provider_task(mocker):
    _stub_all_configured(mocker, {"codex"})
    exec_mock = mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--effort",
            "high",
            "--no-preflight",
            "--allow-short-brief",
            "--brief",
            "Fixes #5. Do X.",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    task = exec_mock.call_args.args[0]
    assert "## Auto-close" in task
    assert "Closes #5" in task


def test_ask_code_exec_with_repo_issue_warns_estimate_is_prompt_only(mocker, tmp_path):
    _stub_all_configured(mocker, {"codex"})
    _mock_issue_subprocess(mocker)
    mocker.patch.object(CodexProvider, "exec", return_value=_fake_response("codex"))

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--effort",
            "high",
            "--cwd",
            str(tmp_path),
            "--issue",
            "123",
            "--no-preflight",
            "--allow-short-brief",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "est:" in result.stderr
    assert "token estimate is prompt-only" in result.stderr
    assert "agent/tool context are not bounded by this estimate" in result.stderr
    assert "--cwd + --issue" in result.stderr


def test_review_auto_route_includes_patch_size_estimate(mocker, tmp_path):
    repo = _make_diff_repo(tmp_path)
    _stub_all_configured(mocker, {"claude"})
    mocker.patch.object(ClaudeProvider, "review_configured", return_value=(True, None))
    mocker.patch.object(ClaudeProvider, "review", return_value=_fake_response("claude"))

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--base",
            "HEAD~1",
            "--cwd",
            str(repo),
            "--brief",
            "review",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["route"]["estimated_input_tokens"] > 2
    assert payload["route"]["estimated_output_tokens"] == 500


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
    monkeypatch.setenv("CONDUCTOR_PERMISSION_PROFILE", "read-only")
    result = CliRunner().invoke(main, ["config", "show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective"]["tags"] == ["code-review"]
    assert payload["effective"]["with"] == "codex"
    assert payload["effective"]["permission_profile"] == "read-only"
    assert payload["sources"]["CONDUCTOR_TAGS"] == "env"
    assert payload["sources"]["CONDUCTOR_WITH"] == "env"
    assert payload["sources"]["CONDUCTOR_PERMISSION_PROFILE"] == "env"


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
    mocker.patch.object(CodexProvider, "call", return_value=_fake_response("codex"))

    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "best", "--task", "hi"])

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
    mocker.patch.object(CodexProvider, "call", return_value=_fake_response("codex"))

    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "best", "--task", "hi"])

    assert result.exit_code == 0
    assert "claude failed (rate-limit)" in result.stderr
    assert "ProviderHTTPError: Anthropic returned 429 rate limit" in result.stderr


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
    codex_call = mocker.patch.object(CodexProvider, "call", return_value=_fake_response("codex"))

    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "best", "--task", "hi"])

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

    result = CliRunner().invoke(main, ["call", "--auto", "--prefer", "best", "--task", "hi"])

    assert result.exit_code == 1
    # Last error (from codex) should surface to the user.
    assert "502" in result.stderr or "bad gateway" in result.stderr


def test_invoke_with_fallback_skips_undersized_context(mocker, capsys, tmp_path):
    """Fallback chain skips a candidate whose context can't hold the brief."""
    from conductor.cli import _invoke_with_fallback
    from conductor.providers.interface import ProviderHTTPError
    from conductor.router import RankedCandidate, RouteDecision

    # Three-provider chain: claude (primary, fails) → ollama (won't fit) → codex (fits, wins).
    decision = RouteDecision(
        provider="claude",
        prefer="best",
        effort="medium",
        thinking_budget=0,
        tier="frontier",
        task_tags=(),
        matched_tags=(),
        tools_requested=(),
        sandbox="read-only",
        candidates_skipped=(),
        ranked=(
            RankedCandidate(
                name="claude",
                tier="frontier",
                tier_rank=0,
                matched_tags=(),
                tag_score=0,
                cost_score=0.0,
                latency_ms=0,
                health_penalty=0.0,
                combined_score=1.0,
            ),
            RankedCandidate(
                name="ollama",
                tier="local",
                tier_rank=2,
                matched_tags=(),
                tag_score=0,
                cost_score=0.0,
                latency_ms=0,
                health_penalty=0.0,
                combined_score=0.5,
            ),
            RankedCandidate(
                name="codex",
                tier="frontier",
                tier_rank=0,
                matched_tags=(),
                tag_score=0,
                cost_score=0.0,
                latency_ms=0,
                health_penalty=0.0,
                combined_score=0.4,
            ),
        ),
    )

    # Pin ollama's context to a tiny number so any nontrivial brief overflows.
    mocker.patch.object(OllamaProvider, "max_context_tokens", 100)

    claude_call = mocker.patch.object(
        ClaudeProvider,
        "call",
        side_effect=ProviderHTTPError("HTTP 503: unavailable"),
    )
    ollama_call = mocker.patch.object(OllamaProvider, "call")
    codex_call = mocker.patch.object(
        CodexProvider,
        "call",
        return_value=_fake_response("codex"),
    )

    # Brief at ~250 tokens (1000 chars / 4) — well over ollama's pinned 100.
    long_brief = "x" * 1000
    session_log = SessionLog(path=tmp_path / "fallback.ndjson")

    response, fallbacks = _invoke_with_fallback(
        decision,
        mode="call",
        task=long_brief,
        model=None,
        effort="medium",
        tools=frozenset(),
        sandbox="read-only",
        cwd=None,
        timeout_sec=None,
        max_stall_sec=None,
        start_timeout_sec=None,
        silent=False,
        session_log=session_log,
    )

    assert response.provider == "codex"
    assert claude_call.called
    assert not ollama_call.called  # skipped before invocation
    assert codex_call.called
    # Ollama appears in the fallback list (skipped, not attempted).
    assert "ollama" in fallbacks
    assert "codex" not in fallbacks  # codex won, so it's not "fallback used"
    assert (
        "[conductor] skipping fallback ollama: brief ~250 tokens > model context 100"
    ) in capsys.readouterr().err
    events = [
        json.loads(line)
        for line in Path(session_log.log_path).read_text(encoding="utf-8").splitlines()
    ]
    skipped = next(event for event in events if event["event"] == "fallback_skipped")
    assert skipped["data"] == {
        "provider": "ollama",
        "reason": "brief_exceeds_context",
        "brief_tokens": 250,
        "max_context_tokens": 100,
    }


def test_exclusion_rule_registry_applies_all_current_rules_without_conflict(mocker):
    from conductor.cli import (
        CODE_HIGH_REQUIRES_FRONTIER,
        CONTEXT_FIT_REQUIRED,
        EXCLUSION_RULES,
        OLLAMA_ONLINE_EXCLUSION_MESSAGE,
        OLLAMA_ONLINE_ONLY,
        PlanContext,
        _apply_planning_exclusion_rules,
        _first_matching_exclusion_rule,
        _format_exclusion_message,
    )
    from conductor.router import RankedCandidate
    from conductor.semantic import plan_for

    plan = plan_for("code", "high")
    assert (
        OLLAMA_ONLINE_ONLY,
        CONTEXT_FIT_REQUIRED,
        CODE_HIGH_REQUIRES_FRONTIER,
    ) == EXCLUSION_RULES

    online_plan, online_message = _apply_planning_exclusion_rules(
        plan,
        PlanContext(
            semantic_plan=plan,
            user_tags=(),
            offline_requested=False,
            online_probe_reachable=True,
        ),
    )
    assert online_message == OLLAMA_ONLINE_EXCLUSION_MESSAGE
    assert [candidate.provider for candidate in online_plan.candidates] == [
        "codex",
        "openrouter",
    ]

    tagged_plan, tagged_message = _apply_planning_exclusion_rules(
        plan,
        PlanContext(
            semantic_plan=plan,
            user_tags=("ollama",),
            offline_requested=False,
            online_probe_reachable=None,
        ),
    )
    assert tagged_message == OLLAMA_ONLINE_EXCLUSION_MESSAGE
    assert [candidate.provider for candidate in tagged_plan.candidates] == [
        "codex",
        "openrouter",
    ]

    runtime_candidate = RankedCandidate(
        name="ollama",
        tier="local",
        tier_rank=2,
        matched_tags=(),
        tag_score=0,
        cost_score=0.0,
        latency_ms=0,
        health_penalty=0.0,
        combined_score=0.0,
    )
    mocker.patch.object(OllamaProvider, "max_context_tokens", 100)
    runtime_context = PlanContext(
        provider=OllamaProvider(),
        brief_tokens=250,
        fallback_index=1,
    )
    runtime_rule = _first_matching_exclusion_rule(
        runtime_candidate,
        runtime_context,
        phase="runtime",
    )
    assert runtime_rule is CONTEXT_FIT_REQUIRED
    assert (
        _format_exclusion_message(runtime_rule, runtime_candidate, runtime_context)
        == "[conductor] skipping fallback ollama: "
        "brief ~250 tokens > model context 100"
    )


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

    result = CliRunner().invoke(main, ["call", "--with", "claude", "--task", "hi"])

    assert result.exit_code == 1
    assert "503" in result.stderr
