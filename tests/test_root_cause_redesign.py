from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from conductor.cli import (
    _apply_exec_commit_boundary,
    _invoke_with_fallback,
    _provider_supports_exec_max_iterations,
    _response_with_council_health,
    main,
)
from conductor.exec_boundary import capture_exec_boundary_snapshot, enforce_exec_boundary
from conductor.provider_capabilities import capabilities_for
from conductor.providers import (
    CallResponse,
    ClaudeProvider,
    CodexProvider,
    OpenRouterProvider,
    ProviderHTTPError,
)
from conductor.router import RankedCandidate, RouteDecision
from conductor.session_log import SessionLog

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _fake_response(provider: str = "codex", model: str = "model") -> CallResponse:
    return CallResponse(
        text="done",
        provider=provider,
        model=model,
        duration_ms=50,
        usage={
            "input_tokens": 123,
            "output_tokens": 10,
            "thinking_tokens": 0,
            "cached_tokens": 0,
        },
        cost_usd=0.01,
        raw={},
    )


def _candidate(name: str, score: float) -> RankedCandidate:
    return RankedCandidate(
        name=name,
        tier="frontier",
        tier_rank=4,
        matched_tags=(),
        tag_score=0,
        cost_score=0.0,
        latency_ms=100,
        health_penalty=0.0,
        combined_score=score,
    )


def _decision(*names: str) -> RouteDecision:
    ranked = tuple(_candidate(name, float(len(names) - idx)) for idx, name in enumerate(names))
    return RouteDecision(
        provider=names[0],
        prefer="balanced",
        effort="medium",
        thinking_budget=0,
        tier="frontier",
        task_tags=(),
        matched_tags=(),
        tools_requested=(),
        sandbox="none",
        ranked=ranked,
        candidates_skipped=(),
    )


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")


def test_provider_capability_facade_replaces_iteration_cap_name_set() -> None:
    codex_caps = capabilities_for(CodexProvider())
    claude_caps = capabilities_for(ClaudeProvider())

    assert codex_caps.supports_exec_iteration_cap is True
    assert claude_caps.supports_exec_iteration_cap is False
    assert _provider_supports_exec_max_iterations("codex") is True
    assert _provider_supports_exec_max_iterations("claude") is False


def test_fallback_attempt_receives_bounded_summary_not_raw_transcript(mocker) -> None:
    long_brief = "x" * 5_000
    first_error = ProviderHTTPError("HTTP 503: " + ("upstream unavailable " * 200))
    second_error = ProviderHTTPError("HTTP 429: rate limited")
    provider_instances = {
        "claude": object.__new__(ClaudeProvider),
        "codex": object.__new__(CodexProvider),
        "openrouter": object.__new__(OpenRouterProvider),
    }
    mocker.patch("conductor.cli.get_provider", side_effect=provider_instances.__getitem__)
    mocker.patch.object(ClaudeProvider, "call", side_effect=first_error)
    mocker.patch.object(CodexProvider, "call", side_effect=second_error)
    openrouter_call = mocker.patch.object(
        OpenRouterProvider,
        "call",
        return_value=_fake_response(provider="openrouter"),
    )

    response, fallbacks = _invoke_with_fallback(
        _decision("claude", "codex", "openrouter"),
        mode="call",
        task=long_brief,
        model=None,
        effort="medium",
        tools=frozenset(),
        sandbox="none",
        cwd=None,
        timeout_sec=None,
        max_stall_sec=None,
        start_timeout_sec=None,
        silent=True,
    )

    assert response.provider == "openrouter"
    assert fallbacks == ["claude", "codex"]
    prompt = openrouter_call.call_args.args[0]
    assert prompt.startswith(long_brief)
    assert "Conductor fallback context:" in prompt
    assert "HTTP 503" in prompt
    assert "HTTP 429" in prompt
    assert len(prompt) < len(long_brief) + 1_800
    fallback_raw = response.raw["conductor_fallback"]
    assert fallback_raw["attempt_count"] == 3
    assert fallback_raw["chain_input_tokens"] == 123


def test_exec_boundary_commits_only_in_scope_dirty_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "src").mkdir()

    snapshot = capture_exec_boundary_snapshot(str(repo))
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("out of scope\n", encoding="utf-8")

    result = enforce_exec_boundary(
        snapshot,
        brief="Edit `src/app.py` only.",
        agent_write_set={"src/app.py", "AGENTS.md"},
    )

    assert result.status == "committed"
    assert result.committed_paths == ("src/app.py",)
    assert result.out_of_scope_paths == ("AGENTS.md",)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "src/app.py"
    ]
    assert "?? AGENTS.md" in _git(repo, "status", "--short")


def test_exec_boundary_preserves_hidden_directory_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".cortex" / "plans").mkdir(parents=True)

    snapshot = capture_exec_boundary_snapshot(str(repo))
    target = repo / ".cortex" / "plans" / "alchemist.md"
    target.write_text("plan\n", encoding="utf-8")

    result = enforce_exec_boundary(
        snapshot,
        brief="Update `.cortex/plans/alchemist.md` and commit it.",
        agent_write_set={".cortex/plans/alchemist.md"},
    )

    assert result.status == "committed"
    assert result.committed_paths == (".cortex/plans/alchemist.md",)
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        ".cortex/plans/alchemist.md"
    ]


def test_apply_exec_commit_boundary_uses_logged_write_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "src").mkdir()

    snapshot = capture_exec_boundary_snapshot(str(repo))
    target = repo / "src" / "app.py"
    target.write_text("print('ok')\n", encoding="utf-8")
    session_log = SessionLog(path=tmp_path / "session.ndjson")
    session_log.emit("tool_call", {"name": "Edit", "args": {"path": str(target)}})

    payload = _apply_exec_commit_boundary(
        snapshot=snapshot,
        brief="Edit `src/app.py` only.",
        session_log=session_log,
        cwd=str(repo),
    )

    assert payload is not None
    assert payload["status"] == "committed"
    assert payload["committed_paths"] == ["src/app.py"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "src/app.py"
    ]


def test_council_degraded_output_is_structural() -> None:
    response = CallResponse(
        text="Synthesis text.",
        provider="openrouter",
        model="synth",
        duration_ms=10,
        usage={},
        raw={
            "conductor_council": {
                "requested_member_models": ["a", "b", "c"],
                "member_models": ["a", "b"],
                "member_errors": [{"model": "b", "type": "ProviderError"}],
                "cap_hit": None,
            }
        },
    )

    updated = _response_with_council_health(response)

    assert updated.text.startswith("Council degraded: 2/3 members failed or were not reached.")
    assert updated.raw["conductor_run_health"]["status"] == "degraded"


def test_termination_knobs_are_hidden_but_still_parseable() -> None:
    exec_help = CliRunner().invoke(main, ["exec", "--help"]).output
    ask_help = CliRunner().invoke(main, ["ask", "--help"]).output

    for flag in ("--max-iterations", "--max-stall-seconds", "--timeout"):
        assert flag not in exec_help
    for flag in ("--council-timeout", "--council-max-output-tokens", "--timeout"):
        assert flag not in ask_help

    parsed = CliRunner().invoke(main, ["exec", "--max-iterations", "2"])
    assert parsed.exit_code == 2
    assert "No such option" not in parsed.output


def test_guardrail_scripts_warn_without_blocking(tmp_path: Path) -> None:
    bug = subprocess.run(
        [
            sys.executable,
            "scripts/check-bug-triage-smells.py",
            "--text",
            "raise the cap for exec",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bug.returncode == 0
    assert "root cause" in bug.stderr

    sample = tmp_path / "sample.py"
    sample.write_text('if provider == "codex":\n    pass\n', encoding="utf-8")
    provider = subprocess.run(
        [
            sys.executable,
            "scripts/check-provider-special-cases.py",
            "--fail-on-new",
            str(sample),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert provider.returncode == 1
    assert "provider-name branches" in provider.stderr
