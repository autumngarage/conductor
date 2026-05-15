"""Regression test for scripts/codex-review.sh fallback cascade.

Reproduces the silent-failure bug this PR fixes: previously, when codex
exited non-zero (usage limit, rate limit, transient API error), the
script's stderr was discarded and the fail-open path exited 0 with only
"reviewer exit N" logged — the push proceeded without a real review.

These tests assert the new behavior: a runtime failure on one reviewer
falls through to the next available reviewer, and the failure cause is
surfaced in the cascade chain summary.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from conductor import cli
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
    ProviderError,
    review_contract,
)
from conductor.router import (
    RankedCandidate,
    RouteDecision,
    reset_health,
    reset_review_health_cache,
)

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
LEGACY_SKIP_REASON = "pre-2.0 shell cascade contract; conductor routing tests cover issue #127"


@pytest.fixture(autouse=True)
def _clean_health(monkeypatch, tmp_path):
    reset_health()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    reset_review_health_cache()
    yield
    reset_review_health_cache()
    reset_health()


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
            lambda self, _ok=ok, _n=name: (_ok, None if _ok else f"{_n} not configured"),
        )
        if hasattr(cls, "review_configured"):
            mocker.patch.object(
                cls,
                "review_configured",
                lambda self, _ok=ok, _n=name: (
                    _ok,
                    None if _ok else f"{_n} review not configured",
                ),
            )


def _fake_response(provider: str) -> CallResponse:
    return CallResponse(
        text=f"{provider} reviewed",
        provider=provider,
        model="test-model",
        duration_ms=10,
        usage={},
        raw={},
    )


def _candidate(name: str) -> RankedCandidate:
    return RankedCandidate(
        name=name,
        tier="frontier",
        tier_rank=4,
        matched_tags=("code-review",),
        tag_score=1,
        cost_score=0.0,
        latency_ms=1,
        health_penalty=0.0,
        combined_score=1.0,
    )


def _decision(*names: str) -> RouteDecision:
    return RouteDecision(
        provider=names[0],
        prefer="best",
        effort="high",
        thinking_budget=0,
        tier="frontier",
        task_tags=("code-review",),
        matched_tags=("code-review",),
        tools_requested=(),
        sandbox="none",
        ranked=tuple(_candidate(name) for name in names),
        candidates_skipped=(),
    )


def test_review_chain_walks_past_codex_to_next_code_review_provider(mocker) -> None:
    from conductor.providers.interface import ProviderStalledError

    _stub_all_configured(mocker, {"claude", "codex", "openrouter"})
    claude_review = mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderStalledError("claude review stalled"),
    )
    codex_review = mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert claude_review.called
    assert codex_review.called
    assert openrouter_call.called
    assert result.stdout == "openrouter reviewed\n"
    assert "claude review unavailable (stall)" in result.stderr
    assert "codex review unavailable (stall)" in result.stderr
    assert "codex (stall), claude (stall), openrouter (success)" in result.stderr


def test_review_fallback_call_uses_conductor_owned_budget(mocker) -> None:
    from conductor.providers.interface import ProviderStalledError

    _stub_all_configured(mocker, {"claude", "codex", "openrouter"})
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
        return_value=_fake_response("openrouter"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--timeout",
            "7",
            "--max-stall-seconds",
            "3",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert openrouter_call.call_args.kwargs["timeout_sec"] == 75
    assert openrouter_call.call_args.kwargs["max_stall_sec"] == 75
    assert "review gate budget: timeout=300s stall=75s" in result.stderr
    assert "ignored caller timeout=7s max-stall=3s" in result.stderr


def test_review_fallback_attempts_share_one_deadline(mocker, monkeypatch) -> None:
    from conductor.providers.interface import ProviderStalledError

    now = [1_000.0]
    seen: list[tuple[str, int | None, int | None]] = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])

    def claude_stalls(*_args, timeout_sec=None, max_stall_sec=None, **_kwargs):
        seen.append(("claude", timeout_sec, max_stall_sec))
        now[0] += 225
        raise ProviderStalledError("claude review stalled")

    def codex_succeeds(*_args, timeout_sec=None, max_stall_sec=None, **_kwargs):
        seen.append(("codex", timeout_sec, max_stall_sec))
        return _fake_response("codex")

    mocker.patch.object(ClaudeProvider, "review", side_effect=claude_stalls)
    mocker.patch.object(CodexProvider, "review", side_effect=codex_succeeds)

    response, _fallbacks = cli._invoke_review_with_fallback(
        _decision("claude", "codex"),
        task="Review this merge using the project reviewer guide.",
        effort="high",
        cwd=None,
        timeout_sec=300,
        max_stall_sec=180,
        base=None,
        commit=None,
        uncommitted=False,
        title=None,
        silent=True,
        fallback_deadline_monotonic=cli._review_gate_deadline(300),
    )

    assert response.provider == "codex"
    assert seen == [("claude", 120, 120), ("codex", 75, 75)]


def test_review_fallback_reserves_budget_for_ready_final_provider(
    mocker, monkeypatch
) -> None:
    from conductor.providers.interface import ProviderStalledError

    now = [1_000.0]
    seen: list[tuple[str, int | None, int | None]] = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])

    def stalled(name: str):
        def fail(*_args, timeout_sec=None, max_stall_sec=None, **_kwargs):
            seen.append((name, timeout_sec, max_stall_sec))
            now[0] += timeout_sec or 0
            raise ProviderStalledError(f"{name} review stalled")

        return fail

    def gemini_succeeds(*_args, timeout_sec=None, max_stall_sec=None, **_kwargs):
        seen.append(("gemini", timeout_sec, max_stall_sec))
        return _fake_response("gemini")

    mocker.patch.object(ClaudeProvider, "review", side_effect=stalled("claude"))
    mocker.patch.object(CodexProvider, "review", side_effect=stalled("codex"))
    mocker.patch.object(OpenRouterProvider, "call", side_effect=stalled("openrouter"))
    mocker.patch.object(GeminiProvider, "review", side_effect=gemini_succeeds)

    response, _fallbacks = cli._invoke_review_with_fallback(
        _decision("claude", "codex", "openrouter", "gemini"),
        task="Review this merge using the project reviewer guide.",
        effort="high",
        cwd=None,
        timeout_sec=300,
        max_stall_sec=60,
        base=None,
        commit=None,
        uncommitted=False,
        title=None,
        silent=True,
        max_fallbacks=4,
        fallback_deadline_monotonic=cli._review_gate_deadline(300),
    )

    assert response.provider == "gemini"
    assert seen == [
        ("claude", 120, 60),
        ("codex", 60, 60),
        ("openrouter", 60, 60),
        ("gemini", 60, 60),
    ]


def test_review_budget_exhaustion_before_provider_is_reported(
    mocker, monkeypatch
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    mocker.patch.object(GeminiProvider, "review", return_value=_fake_response("gemini"))
    deadline = cli._review_gate_deadline(10)
    now[0] += 11

    with pytest.raises(ProviderError) as exc_info:
        cli._invoke_review_with_fallback(
            _decision("gemini"),
            task="Review this merge using the project reviewer guide.",
            effort="high",
            cwd=None,
            timeout_sec=10,
            max_stall_sec=5,
            base=None,
            commit=None,
            uncommitted=False,
            title=None,
            silent=True,
            max_fallbacks=1,
            fallback_deadline_monotonic=deadline,
        )

    message = str(exc_info.value)
    assert "gemini (stall)" in message
    assert "review gate budget exhausted before trying gemini" in message


def test_large_review_codex_contract_failure_falls_back_to_openrouter_next(
    mocker, capsys
) -> None:
    _stub_all_configured(mocker, {"codex", "claude", "openrouter"})
    codex_review = mocker.patch.object(
        CodexProvider,
        "review",
        return_value=CallResponse(
            text="The review completed but omitted the required sentinel.",
            provider="codex",
            model="codex-review",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )
    claude_review = mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=CallResponse(
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
            provider="claude",
            model="sonnet",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=CallResponse(
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
            provider="openrouter",
            model="test-model",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )

    response, fallbacks = cli._invoke_review_with_fallback(
        replace(
            _decision("codex", "claude", "openrouter"),
            estimated_input_tokens=8_000,
        ),
        task=(
            "Review this merge. The last line must be exactly "
            "CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED."
        ),
        effort="high",
        cwd=None,
        timeout_sec=300,
        max_stall_sec=75,
        base=None,
        commit=None,
        uncommitted=False,
        title=None,
        silent=False,
        fallback_deadline_monotonic=cli._review_gate_deadline(300),
    )

    captured = capsys.readouterr()
    assert response.provider == "openrouter"
    assert fallbacks == ["codex"]
    assert codex_review.called
    assert openrouter_call.called
    assert not claude_review.called
    assert "codex review failed (output-contract)" in captured.err
    assert "falling back → openrouter" in captured.err
    assert (
        "review tried providers: codex (output-contract), openrouter (success)"
        in captured.err
    )


def test_large_review_codex_timeout_falls_back_to_openrouter_next(mocker) -> None:
    from conductor.providers.interface import ProviderStalledError

    _stub_all_configured(mocker, {"codex", "claude", "openrouter"})
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review timed out after 300s"),
    )
    claude_review = mocker.patch.object(
        ClaudeProvider,
        "review",
        return_value=CallResponse(
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
            provider="claude",
            model="sonnet",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=CallResponse(
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
            provider="openrouter",
            model="test-model",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )

    response, fallbacks = cli._invoke_review_with_fallback(
        replace(
            _decision("codex", "claude", "openrouter"),
            estimated_input_tokens=8_000,
        ),
        task=(
            "Review this merge. The last line must be exactly "
            "CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED."
        ),
        effort="high",
        cwd=None,
        timeout_sec=300,
        max_stall_sec=75,
        base=None,
        commit=None,
        uncommitted=False,
        title=None,
        silent=True,
        fallback_deadline_monotonic=cli._review_gate_deadline(300),
    )

    assert response.provider == "openrouter"
    assert fallbacks == ["codex"]
    assert openrouter_call.called
    assert not claude_review.called


def test_review_auto_skips_provider_after_recent_contract_failure(mocker) -> None:
    _stub_all_configured(mocker, {"codex", "openrouter"})
    codex_review = mocker.patch.object(
        CodexProvider,
        "review",
        return_value=CallResponse(
            text="Review completed without the required sentinel.",
            provider="codex",
            model="codex-review",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=CallResponse(
            text="No blocking issues found.\nCODEX_REVIEW_CLEAN",
            provider="openrouter",
            model="test-model",
            duration_ms=10,
            usage={},
            raw={},
        ),
    )

    first = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "2",
            "--brief",
            "Review this merge. Last line must be CODEX_REVIEW_CLEAN.",
        ],
    )

    assert first.exit_code == 0, first.output
    assert codex_review.call_count == 1
    assert openrouter_call.call_count == 1

    codex_review.reset_mock()
    openrouter_call.reset_mock()
    second = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--verbose-route",
            "--max-fallbacks",
            "2",
            "--brief",
            "Review this merge. Last line must be CODEX_REVIEW_CLEAN.",
        ],
    )

    assert second.exit_code == 0, second.output
    assert not codex_review.called
    assert openrouter_call.call_count == 1
    assert "recent review output-contract failure" in second.stderr


def test_review_rate_limit_fallback_caps_late_provider_timeout(mocker) -> None:
    from conductor.providers.interface import (
        ProviderError,
        ProviderHTTPError,
        ProviderStalledError,
    )

    seen: list[tuple[int | None, int | None]] = []
    mocker.patch.object(
        GeminiProvider,
        "review",
        side_effect=ProviderHTTPError("Gemini returned 429 rate limit"),
    )
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderHTTPError("Anthropic returned 429 rate limit"),
    )

    def openrouter_stalls(*_args, timeout_sec=None, max_stall_sec=None, **_kwargs):
        seen.append((timeout_sec, max_stall_sec))
        raise ProviderStalledError("openrouter review stalled")

    mocker.patch.object(OpenRouterProvider, "call", side_effect=openrouter_stalls)

    with pytest.raises(ProviderError) as exc_info:
        cli._invoke_review_with_fallback(
            _decision("gemini", "claude", "openrouter"),
            task="Review this merge using the project reviewer guide.",
            effort="high",
            cwd=None,
            timeout_sec=300,
            max_stall_sec=75,
            base=None,
            commit=None,
            uncommitted=False,
            title=None,
            silent=True,
            fallback_deadline_monotonic=cli._review_gate_deadline(300),
        )

    assert seen == [(75, 75)]
    assert "gemini (rate-limit), claude (rate-limit), openrouter (stall)" in str(
        exc_info.value
    )
    assert "Last error: ProviderStalledError: openrouter review stalled" in str(
        exc_info.value
    )


def test_exec_code_review_routes_cap_default_agent_iterations(mocker) -> None:
    invoke = mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response("codex"), []),
    )
    mocker.patch("conductor.cli.pick", return_value=(CodexProvider(), _decision("codex")))
    mocker.patch(
        "conductor.cli._scale_dispatch_defaults",
        side_effect=lambda **kwargs: (kwargs["timeout_sec"], kwargs["max_stall_sec"]),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--tags",
            "code-review",
            "--effort",
            "high",
            "--no-preflight",
            "--silent-route",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert invoke.call_args.kwargs["max_iterations"] == cli.REVIEW_GATE_MAX_EXEC_ITERATIONS
    assert invoke.call_args.kwargs["fallback_deadline_monotonic"] is not None
    assert invoke.call_args.kwargs["fallback_budget_label"] == "review gate budget"


def test_review_with_openrouter_backed_provider_uses_call_prompt(mocker) -> None:
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("deepseek-reasoner"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--with",
            "deepseek-reasoner",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "deepseek-reasoner reviewed\n"
    assert openrouter_call.called


def test_review_patch_context_git_timeout_surfaces_context_error(
    mocker, tmp_path: Path
) -> None:
    mocker.patch.object(
        review_contract.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["git", "diff"], 30),
    )

    with pytest.raises(review_contract.ReviewContextError, match="timed out"):
        review_contract.build_review_patch_context(
            base="main",
            commit=None,
            uncommitted=False,
            cwd=str(tmp_path),
        )


def test_review_max_fallbacks_caps_total_attempts(mocker) -> None:
    from conductor.providers.interface import ProviderStalledError

    _stub_all_configured(mocker, {"claude", "codex", "openrouter"})
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
        return_value=_fake_response("openrouter"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "2",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 1
    assert not openrouter_call.called
    assert "codex (stall), claude (stall)" in result.stderr
    assert "Skipped by --max-fallbacks=2: openrouter." in result.stderr


def test_review_exhaustion_reports_providers_skipped_by_max_fallbacks(
    mocker,
) -> None:
    from conductor.providers.interface import ProviderStalledError

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
        GeminiProvider,
        "review",
        side_effect=ProviderStalledError("gemini review stalled"),
    )
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response("openrouter"),
    )

    with pytest.raises(cli.ReviewInfrastructureError) as exc_info:
        cli._invoke_review_with_fallback(
            _decision("claude", "codex", "gemini", "openrouter"),
            task="Review this merge using the project reviewer guide.",
            effort="high",
            cwd=None,
            timeout_sec=300,
            max_stall_sec=75,
            base=None,
            commit=None,
            uncommitted=False,
            title=None,
            silent=True,
            max_fallbacks=3,
            fallback_deadline_monotonic=cli._review_gate_deadline(300),
        )

    assert not openrouter_call.called
    message = str(exc_info.value)
    assert "claude (stall), codex (stall), gemini (stall)" in message
    assert "Skipped by --max-fallbacks=3: openrouter." in message
    assert "deadline exhausted before trying openrouter" not in message
    payload = exc_info.value.error_response
    assert payload["review"]["skipped_by_max_fallbacks"] == ["openrouter"]


def test_review_exhausted_error_includes_tried_provider_trail(mocker) -> None:
    from conductor.providers.interface import ProviderHTTPError, ProviderStalledError

    _stub_all_configured(mocker, {"claude", "codex", "openrouter"})
    mocker.patch.object(
        ClaudeProvider,
        "review",
        side_effect=ProviderHTTPError("Anthropic returned 429 rate limit"),
    )
    mocker.patch.object(
        CodexProvider,
        "review",
        side_effect=ProviderStalledError("codex review stalled"),
    )
    mocker.patch.object(
        OpenRouterProvider,
        "call",
        side_effect=ProviderHTTPError("OpenRouter returned HTTP 503"),
    )

    result = CliRunner().invoke(
        main,
        [
            "review",
            "--auto",
            "--max-fallbacks",
            "3",
            "--brief",
            "Review this merge using the project reviewer guide.",
        ],
    )

    assert result.exit_code == 1
    assert (
        "review infrastructure failed before any provider returned a valid verdict"
        in result.stderr
    )
    assert "no review findings were emitted" in result.stderr
    assert "codex (stall), claude (rate-limit), openrouter (5xx)" in result.stderr
    assert "Provider status:" in result.stderr
    assert (
        "claude: rate-limit - ProviderHTTPError: Anthropic returned 429 rate limit"
        in result.stderr
    )
    assert "openrouter: 5xx - ProviderHTTPError: OpenRouter returned HTTP 503" in result.stderr
    assert "ProviderHTTPError: OpenRouter returned HTTP 503" in result.stderr
    assert "Next step:" in result.stderr
    assert "continue the coding/review task directly in the driving agent" in result.stderr


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    path.chmod(0o755)


def _make_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with two commits so MERGE_BASE..HEAD has a diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\n")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\nfeature line\n")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, env=env, check=True)
    return repo


def _run_script(repo: Path, fakes_dir: Path, extra_env: dict | None = None,
                timeout: int = 60) -> subprocess.CompletedProcess:
    # Scrub pre-commit/pre-push hook signals that may leak in from the test
    # runner's environment (e.g. when pytest itself runs under a pre-push
    # hook). The script's `should_skip_pre_push_review` would otherwise
    # think we're pushing a non-default branch and skip review entirely.
    inherited = {k: v for k, v in os.environ.items()
                 if not (k.startswith("PRE_COMMIT") or k.startswith("CODEX_REVIEW")
                         or k == "TOUCHSTONE_REVIEWER")}
    env = {
        **inherited,
        "PATH": f"{fakes_dir}:{os.environ.get('PATH', '')}",
        "CODEX_REVIEW_BASE": "HEAD~1",  # avoid origin fetch
        "CODEX_REVIEW_MODE": "review-only",
        "CODEX_REVIEW_DISABLE_CACHE": "1",
        "CODEX_REVIEW_TIMEOUT": "5",  # keep tests fast; we never exercise the real budget
        "TOUCHSTONE_ROOT": str(repo),
        "NO_COLOR": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT_PATH)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_config(repo: Path, reviewers: list[str], on_error: str = "fail-open",
                  mode: str = "review-only") -> None:
    body = textwrap.dedent(f"""
        [codex_review]
        max_iterations = 1
        max_diff_lines = 5000
        cache_clean_reviews = false
        safe_by_default = true
        mode = "{mode}"
        on_error = "{on_error}"
        unsafe_paths = []

        [review]
        enabled = true
        reviewers = {reviewers!r}
    """).lstrip("\n")
    (repo / ".codex-review.toml").write_text(body)
    # Commit so the worktree is clean before review runs — otherwise the
    # config file shows up as untracked and trips WORKTREE_DIRTY_BEFORE_REVIEW.
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", ".codex-review.toml"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, env=env, check=True)


# ---------------------------------------------------------------------------
# fake reviewer factories
# ---------------------------------------------------------------------------

FAKE_CODEX_RATE_LIMIT = r'''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    msg='{"type":"error","status":429,'
    msg="${msg}\"error\":{\"type\":\"rate_limit_exceeded\","
    msg="${msg}\"message\":\"Too many requests\"}}"
    echo "ERROR: $msg" >&2
    exit 1
    ;;
esac
exit 1
'''

FAKE_CODEX_MALFORMED = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "I reviewed your code and it looks fine but I will not emit the sentinel."
    echo "warning: tool restriction prevented me from running tests" >&2
    exit 0
    ;;
esac
exit 1
'''

FAKE_CODEX_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "ERROR: usage limit exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

FAKE_CLAUDE_CLEAN = '''
#!/usr/bin/env bash
case "$1" in
  auth)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  -p)
    echo "Looks fine."
    echo "CODEX_REVIEW_CLEAN"
    exit 0
    ;;
esac
exit 1
'''

FAKE_CLAUDE_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  auth)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  -p)
    echo "ERROR: 401 unauthorized" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex writes a file (simulating a partial edit) then exits non-zero
# without ever emitting CODEX_REVIEW_FIXED. In fix mode the cascade must
# discard this partial edit before falling through, otherwise claude would
# review-or-bless un-blessed work.
FAKE_CODEX_PARTIAL_EDIT = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    # Simulate codex starting an edit then crashing mid-flight.
    echo "partial edit from codex that will never be blessed" >> README
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex commits its work then fails — the worktree is clean but HEAD
# moved without the script's auto-fix path blessing it. The cascade must
# detect this and abort.
FAKE_CODEX_COMMITS_THEN_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "edit from codex" >> README
    git -c user.name=t -c user.email=t@t add README
    git -c user.name=t -c user.email=t@t commit -q -m "unauthorized commit by codex"
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex stashes its work then fails — clean tree, unchanged HEAD, but a
# new stash entry hides reviewer-authored state.
FAKE_CODEX_STASHES_THEN_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "edit from codex" >> README
    git stash push -q -m "hidden by codex" -- README
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex creates a sideways branch with an unauthorized commit and
# checks it out (HEAD moves to a different ref entirely). Worktree
# stays "clean" relative to the new HEAD but the count-based check
# would compare zero new commits ahead of the original HEAD.
FAKE_CODEX_CHECKOUT_SIDEWAYS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    git -c user.name=t -c user.email=t@t checkout -q -b sideways
    echo "edit from codex" >> README
    git -c user.name=t -c user.email=t@t add README
    git -c user.name=t -c user.email=t@t commit -q -m "sideways unauthorized"
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex switches to a different branch that points at the same sha
# (e.g. a fresh branch off HEAD) and then fails. HEAD-sha is unchanged
# but the next reviewer would auto-commit on the wrong branch.
FAKE_CODEX_SWITCH_BRANCH = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    git -c user.name=t -c user.email=t@t branch sideways-same-sha
    git -c user.name=t -c user.email=t@t checkout -q sideways-same-sha
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex drops the existing stash and adds its own — same count,
# different content. Catches the count-vs-sha distinction.
FAKE_CODEX_SWAP_STASH = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    # Drop whatever is on top, then push our own.
    git stash drop -q stash@{0} 2>/dev/null || true
    echo "edit from codex" >> README
    git stash push -q -m "swapped by codex" -- README
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_usage_limit_falls_through_to_next_reviewer(tmp_path: Path) -> None:
    """Codex hits a rate limit; cascade falls through to claude, which clears."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_RATE_LIMIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "falling back to Claude" in result.stdout
    assert "codex:usage-limit" in result.stdout
    assert "claude:clean" in result.stdout
    assert "ALL CLEAR" in result.stdout


def test_malformed_sentinel_falls_through(tmp_path: Path) -> None:
    """Codex emits non-sentinel output (a contract violation, not a crash);
    cascade still falls through to claude per the silent-fail-prevention spirit."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_MALFORMED)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "did not match the expected sentinel contract" in result.stdout
    assert "falling back to Claude" in result.stdout
    assert "codex:malformed" in result.stdout
    assert "claude:clean" in result.stdout
    # A malformed-sentinel reviewer can still write useful diagnostics
    # to stderr (e.g. tool-restriction warnings); those must surface
    # so the user knows *why* the contract was violated.
    assert "tool restriction prevented me from running tests" in result.stdout


def test_cascade_exhausted_records_chain(tmp_path: Path) -> None:
    """Both reviewers fail; cascade-exhausted path records the chain and
    fail-open allows the push (preserving today's exit-policy semantics).
    The bug this PR fixes is *visibility*, not the exit policy itself —
    the failure reason is now in the chain instead of swallowed."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_FAILS)

    result = _run_script(repo, fakes)

    # fail-open: cascade exhausted → exit 0 with loud message
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "All reviewers in the cascade failed" in result.stdout
    assert "codex:usage-limit" in result.stdout
    assert "claude:auth" in result.stdout
    assert "cascade exhausted" in result.stdout


def test_cascade_exhausted_blocks_when_fail_closed(tmp_path: Path) -> None:
    """Same as above but with on_error=fail-closed — push must be blocked."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], on_error="fail-closed")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_FAILS)

    result = _run_script(repo, fakes)

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "All reviewers in the cascade failed" in result.stdout
    assert "blocking push" in result.stderr or "blocking push" in result.stdout


def test_fix_mode_discards_partial_edits_before_fallthrough(tmp_path: Path) -> None:
    """In fix mode, a failed reviewer may have written partial edits before
    crashing. The cascade must NOT pass that dirty worktree to the next
    reviewer — otherwise claude could bless or auto-commit work codex
    never blessed with FIXED."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_PARTIAL_EDIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "Discarding partial edits" in result.stdout
    assert "falling back to Claude" in result.stdout
    # Worktree must be clean after the cascade — codex's un-blessed edit
    # should have been reverted before claude saw it.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
    )
    assert status.stdout == "", (
        f"Worktree should be clean post-cascade; saw: {status.stdout!r}\n"
        f"This means a failed reviewer's partial edits leaked through to the next reviewer."
    )


def test_fix_mode_aborts_on_unauthorized_commits(tmp_path: Path) -> None:
    """A reviewer that commits its own work leaves a clean worktree at a
    HEAD the script never blessed. Cascade must detect HEAD movement that
    doesn't match the auto-fix counter and abort."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_COMMITS_THEN_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "moved HEAD to an un-blessed sha" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_on_sideways_checkout(tmp_path: Path) -> None:
    """A reviewer that creates a branch and commits to it leaves a clean
    tree and a HEAD that's not the script's expected sha. Detection by
    HEAD-sha (not commit count) catches this even though the diff vs the
    *original* HEAD shows zero new ancestors."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_CHECKOUT_SIDEWAYS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "moved HEAD to an un-blessed sha" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_on_branch_switch_at_same_sha(tmp_path: Path) -> None:
    """A reviewer that creates and checks out a different branch pointing
    at the same sha leaves HEAD-sha unchanged but on the wrong branch.
    Branch-name comparison must catch this."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_SWITCH_BRANCH)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "switched branches" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_on_swapped_stash(tmp_path: Path) -> None:
    """A reviewer that drops one stash and pushes another keeps the count
    constant but the content changes. SHA-list comparison catches it."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    # Plant a pre-existing stash so the reviewer has something to drop.
    env = {**os.environ, "GIT_AUTHOR_NAME": "u", "GIT_AUTHOR_EMAIL": "u@u",
           "GIT_COMMITTER_NAME": "u", "GIT_COMMITTER_EMAIL": "u@u"}
    (repo / "README").write_text("base\nfeature line\nuser stash content\n")
    subprocess.run(["git", "stash", "push", "-q", "-m", "user stash", "--",
                    "README"], cwd=repo, env=env, check=True)

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_SWAP_STASH)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "mutated the stash list" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_on_reviewer_stash(tmp_path: Path) -> None:
    """A reviewer that stashes its work leaves a clean worktree and
    unchanged HEAD, but the stash hides reviewer-authored state. Cascade
    must detect stash list growth and abort."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_STASHES_THEN_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "mutated the stash list" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_when_pre_review_worktree_dirty(tmp_path: Path) -> None:
    """If the user already had uncommitted changes before review started,
    we cannot safely distinguish their work from reviewer edits on failure.
    Refuse to fall through and block the push, so the user can sort it out."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")
    # Plant a pre-review uncommitted change.
    (repo / "README").write_text("base\nfeature line\nuser edit\n")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_PARTIAL_EDIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "refusing to fall through" in result.stdout
    assert "mixed user/reviewer state" in result.stdout


def test_stderr_tail_surfaced_on_failure(tmp_path: Path) -> None:
    """Reviewer stderr is no longer discarded — the rate-limit message
    must appear in the user-visible output so they know *why* it failed."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_RATE_LIMIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert "rate_limit_exceeded" in result.stdout, (
        "Codex stderr (rate-limit JSON) should be tailed into user output, "
        "not silenced. This is the core silent-failure bug.\n"
        f"stdout={result.stdout!r}"
    )


for _legacy_name in (
    "test_usage_limit_falls_through_to_next_reviewer",
    "test_malformed_sentinel_falls_through",
    "test_cascade_exhausted_records_chain",
    "test_cascade_exhausted_blocks_when_fail_closed",
    "test_fix_mode_discards_partial_edits_before_fallthrough",
    "test_fix_mode_aborts_on_unauthorized_commits",
    "test_fix_mode_aborts_on_sideways_checkout",
    "test_fix_mode_aborts_on_branch_switch_at_same_sha",
    "test_fix_mode_aborts_on_swapped_stash",
    "test_fix_mode_aborts_on_reviewer_stash",
    "test_fix_mode_aborts_when_pre_review_worktree_dirty",
    "test_stderr_tail_surfaced_on_failure",
):
    globals()[_legacy_name] = pytest.mark.skip(reason=LEGACY_SKIP_REASON)(
        globals()[_legacy_name]
    )
