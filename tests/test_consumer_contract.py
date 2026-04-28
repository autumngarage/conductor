"""Executable contract for subprocess consumers of `conductor call`.

These tests intentionally duplicate a thin slice of lower-level CLI coverage:
the goal is to keep docs/consumers.md and downstream expectations tied to the
actual subprocess surface, not to implementation internals.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conductor import offline_mode
from conductor.cli import main
from conductor.providers import (
    TIER_RANK,
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
from conductor.router import reset_health

DOC_PATH = Path("docs/consumers.md")

CALL_JSON_REQUIRED_KEYS = {
    "text",
    "provider",
    "model",
    "duration_ms",
    "usage",
    "cost_usd",
    "session_id",
    "raw",
}
USAGE_REQUIRED_KEYS = {
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "thinking_tokens",
    "effort",
    "thinking_budget",
}
ROUTE_REQUIRED_KEYS = {
    "provider",
    "prefer",
    "effort",
    "thinking_budget",
    "tier",
    "task_tags",
    "matched_tags",
    "tools_requested",
    "sandbox",
    "ranked",
    "candidates_skipped",
    "tag_default_applied",
    "tag_default_considered",
    "unconfigured_shadow",
}
RANKED_CANDIDATE_REQUIRED_KEYS = {
    "name",
    "tier",
    "tier_rank",
    "matched_tags",
    "tag_score",
    "cost_score",
    "latency_ms",
    "health_penalty",
    "combined_score",
    "unconfigured_reason",
}


@pytest.fixture(autouse=True)
def _isolated_consumer_contract_env(monkeypatch, tmp_path):
    """Keep consumer-contract tests independent of user-local Conductor state."""
    for key in list(os.environ):
        if key.startswith("CONDUCTOR_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))
    monkeypatch.setenv(
        "CONDUCTOR_PROVIDERS_FILE",
        str(tmp_path / "config" / "providers.toml"),
    )
    monkeypatch.setenv(
        "CONDUCTOR_PROFILES_FILE",
        str(tmp_path / "config" / "profiles.toml"),
    )
    monkeypatch.setenv(
        "CONDUCTOR_ROUTER_DEFAULTS_FILE",
        str(tmp_path / "config" / "router-home.toml"),
    )
    monkeypatch.setenv(
        "CONDUCTOR_REPO_ROUTER_DEFAULTS_FILE",
        str(tmp_path / "config" / "router-repo.toml"),
    )
    offline_mode.clear()
    reset_health()
    yield
    offline_mode.clear()
    reset_health()


def _stub_configured(mocker, configured_names: set[str]) -> None:
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
            lambda self, _ok=ok, _name=name: (
                _ok,
                None if _ok else f"{_name} stub not configured",
            ),
        )


def _nullable_usage_response(provider: str, model: str) -> CallResponse:
    return CallResponse(
        text="contract-ok",
        provider=provider,
        model=model,
        duration_ms=123,
        usage={
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "thinking_tokens": None,
            "effort": "medium",
            "thinking_budget": 8_000,
        },
        cost_usd=None,
        raw={"provider_specific": True},
    )


def _assert_call_payload_contract(payload: dict[str, Any]) -> None:
    assert payload.keys() >= CALL_JSON_REQUIRED_KEYS
    assert isinstance(payload["text"], str)
    assert isinstance(payload["provider"], str)
    assert isinstance(payload["model"], str)
    assert isinstance(payload["duration_ms"], int)
    assert isinstance(payload["raw"], dict)
    assert payload["cost_usd"] is None or isinstance(payload["cost_usd"], int | float)
    assert payload["session_id"] is None or isinstance(payload["session_id"], str)

    usage = payload["usage"]
    assert usage.keys() >= USAGE_REQUIRED_KEYS
    for key in ("input_tokens", "output_tokens", "cached_tokens", "thinking_tokens"):
        assert usage[key] is None or isinstance(usage[key], int)
    assert usage["effort"] is None or usage["effort"] in {
        "minimal",
        "low",
        "medium",
        "high",
        "max",
    }
    assert usage["thinking_budget"] is None or isinstance(usage["thinking_budget"], int)


def _assert_route_payload_contract(route: dict[str, Any]) -> None:
    assert route.keys() >= ROUTE_REQUIRED_KEYS
    assert isinstance(route["provider"], str)
    assert route["prefer"] in {"best", "cheapest", "fastest", "balanced"}
    assert isinstance(route["task_tags"], list)
    assert isinstance(route["matched_tags"], list)
    assert isinstance(route["tools_requested"], list)
    assert isinstance(route["ranked"], list)
    assert route["ranked"], "route contract includes the full ranked candidate list"

    winner = route["ranked"][0]
    assert winner.keys() >= RANKED_CANDIDATE_REQUIRED_KEYS
    assert winner["tier_rank"] == TIER_RANK[winner["tier"]]
    assert isinstance(winner["combined_score"], int | float)


def _documented_call_flags() -> set[str]:
    docs = DOC_PATH.read_text(encoding="utf-8")
    start = docs.index("| Flag | Type | Stability | Notes |")
    end = docs.index("## Output", start)
    return set(re.findall(r"`(--[a-z-]+)", docs[start:end]))


def test_call_json_auto_route_is_the_consumer_contract(mocker):
    _stub_configured(mocker, {"claude"})
    mocker.patch.object(
        ClaudeProvider,
        "call",
        return_value=_nullable_usage_response("claude", "sonnet"),
    )

    result = CliRunner().invoke(
        main,
        [
            "call",
            "--auto",
            "--tags",
            "code-review,tool-use",
            "--prefer",
            "best",
            "--brief",
            "review this diff",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_call_payload_contract(payload)
    assert payload["provider"] == "claude"
    assert "route" in payload
    assert "routing" not in payload
    _assert_route_payload_contract(payload["route"])
    assert payload["route"]["provider"] == payload["provider"]
    assert set(payload["route"]["task_tags"]) == {"code-review", "tool-use"}
    assert result.stderr == ""


def test_route_json_uses_the_same_route_contract(mocker):
    _stub_configured(mocker, {"claude", "ollama"})

    result = CliRunner().invoke(
        main,
        ["route", "--tags", "code-review", "--prefer", "best", "--json"],
    )

    assert result.exit_code == 0, result.output
    _assert_route_payload_contract(json.loads(result.stdout))


def test_offline_call_is_a_documented_invocation_without_auto_or_with(
    mocker,
):
    _stub_configured(mocker, set())
    call_mock = mocker.patch.object(
        OllamaProvider,
        "call",
        return_value=_nullable_usage_response("ollama", "llama3.1"),
    )

    result = CliRunner().invoke(
        main,
        ["call", "--offline", "--brief", "local only", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert call_mock.called
    payload = json.loads(result.stdout)
    _assert_call_payload_contract(payload)
    assert payload["provider"] == "ollama"
    assert "route" not in payload
    assert offline_mode.is_active() is True


def test_json_usage_errors_remain_click_diagnostics():
    result = CliRunner().invoke(main, ["call", "--json", "--brief", "missing route"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr
    assert "Error:" in result.stderr
    assert "pass --with <id> or --auto" in result.stderr


def test_consumer_doc_only_commits_to_real_call_flags():
    result = CliRunner().invoke(main, ["call", "--help"])

    assert result.exit_code == 0, result.output
    help_text = result.output
    assert _documented_call_flags() <= set(re.findall(r"--[a-z-]+", help_text))


def test_consumer_doc_names_the_guarded_contract_edges():
    docs = DOC_PATH.read_text(encoding="utf-8")

    assert "`route` field" in docs
    assert "`routing`" not in docs
    assert '"input_tokens": "integer | null"' in docs
    assert "conductor call --offline" in docs
    assert "multi-line `Usage` / `Try` / `Error` output" in docs
    assert "tests/test_consumer_contract.py" in docs
