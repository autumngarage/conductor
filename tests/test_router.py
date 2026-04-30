"""Tests for the auto-mode router.

All built-in providers expose their own ``configured()`` (env-var or CLI check);
we stub those directly so tests run without real CLIs/env vars.
"""

from __future__ import annotations

import pytest

from conductor.router import (
    DEFAULT_PRIORITY,
    InvalidRouterRequest,
    NoConfiguredProvider,
    mark_auth_failed,
    mark_rate_limited,
    pick,
    reset_health,
)


@pytest.fixture(autouse=True)
def _clean_health():
    """Reset session-local health state between tests."""
    reset_health()
    yield
    reset_health()


@pytest.fixture(autouse=True)
def _isolated_conductor_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)


def _write_router_defaults(home_dir, body: str) -> None:
    config_dir = home_dir / ".config" / "conductor"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "router.toml").write_text(body, encoding="utf-8")


def _stub_configured(mocker, results: dict[str, bool]):
    """Patch ``configured()`` on each provider class with a fixed result.

    Keys in ``results`` are provider identifiers; values are True/False.
    Providers missing from the dict default to False (unconfigured).
    """
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        DeepSeekChatProvider,
        DeepSeekReasonerProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
        OpenRouterProvider,
    )

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
        ok = results.get(name, False)
        mocker.patch.object(
            cls,
            "configured",
            lambda self, _ok=ok: (_ok, None if _ok else "stub not configured"),
        )


def test_pick_returns_only_configured_provider(mocker):
    _stub_configured(mocker, {"ollama": True})
    provider, decision = pick([])
    assert provider.name == "ollama"
    assert decision.provider == "ollama"


def test_pick_scores_by_tag_overlap(mocker):
    _stub_configured(mocker, {"kimi": True, "claude": True, "ollama": True})
    # "local" is only on ollama; it should win even though priority says kimi first.
    provider, decision = pick(["local"])
    assert provider.name == "ollama"
    assert decision.score == 1
    assert decision.matched_tags == ("local",)


def test_pick_priority_tiebreak(mocker):
    _stub_configured(mocker, {"kimi": True, "claude": True})
    # No tags → both score 0 → priority order decides → kimi.
    provider, _ = pick([])
    assert provider.name == "kimi"


def test_pick_priority_tiebreak_with_equal_tag_score(mocker):
    _stub_configured(mocker, {"kimi": True, "gemini": True})
    # "long-context" matches both kimi and gemini; priority says kimi first.
    provider, decision = pick(["long-context"])
    assert provider.name == "kimi"
    assert decision.score == 1


def test_pick_higher_tag_score_beats_priority(mocker):
    _stub_configured(mocker, {"kimi": True, "ollama": True})
    # Both configured, but "offline" and "local" are ollama-only tags.
    # Ollama scores 2 vs kimi's 0 → ollama wins despite lower priority.
    provider, decision = pick(["offline", "local"])
    assert provider.name == "ollama"
    assert decision.score == 2


def test_pick_raises_when_no_provider_configured(mocker):
    _stub_configured(mocker, {})
    with pytest.raises(NoConfiguredProvider) as exc:
        pick(["long-context"])
    # Error lists what was skipped so users can see which CLI/env is missing.
    assert "Skipped" in str(exc.value)


def test_pick_empty_tags_falls_back_to_priority(mocker):
    _stub_configured(
        mocker, {"kimi": True, "claude": True, "codex": True, "gemini": True}
    )
    provider, decision = pick([])
    assert provider.name == "kimi"
    assert decision.matched_tags == ()


def test_route_decision_surfaces_skipped_with_reasons(mocker):
    _stub_configured(mocker, {"kimi": True})
    _, decision = pick(["cheap"])
    skipped_names = {name for name, _ in decision.candidates_skipped}
    assert skipped_names == {
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
        "openrouter",
    }


def test_priority_order_is_stable():
    # Guardrail: the default priority is part of the project's opinionated
    # default. A change here should be a deliberate doctrine-level decision,
    # not a drive-by edit. Priority entries that don't map to a registered
    # provider (e.g. "mistral" before its adapter lands) are silently
    # skipped by `pick()` — they reserve the slot for future adapters.
    assert DEFAULT_PRIORITY == ("kimi", "claude", "mistral", "codex", "gemini", "ollama")


def test_openrouter_can_now_participate_in_auto_routing(mocker):
    _stub_configured(mocker, {"claude": True, "openrouter": True})
    provider, decision = pick(["cheap"])
    assert provider.name == "openrouter"
    assert decision.provider == "openrouter"


# ---------------------------------------------------------------------------
# v0.2 — prefer modes
# ---------------------------------------------------------------------------


def test_prefer_best_picks_highest_tier(mocker):
    # claude + codex are frontier; kimi is strong; ollama is local.
    _stub_configured(mocker, {"claude": True, "kimi": True, "ollama": True})
    provider, decision = pick([], prefer="best")
    # claude wins on tier-rank (frontier=4 > strong=3 > local=1).
    assert provider.name == "claude"
    assert decision.tier == "frontier"
    assert decision.prefer == "best"


def test_prefer_cheapest_picks_lowest_cost(mocker):
    # ollama is free (cost=0); everything else costs something.
    _stub_configured(mocker, {"claude": True, "kimi": True, "ollama": True})
    provider, decision = pick([], prefer="cheapest")
    assert provider.name == "ollama"
    assert decision.prefer == "cheapest"


def test_prefer_fastest_picks_lowest_latency(mocker):
    # gemini's typical_p50_ms=1800 < codex=2000 < claude=2500.
    _stub_configured(mocker, {"claude": True, "gemini": True, "codex": True})
    provider, decision = pick([], prefer="fastest")
    assert provider.name == "gemini"
    assert decision.prefer == "fastest"


def test_prefer_balanced_matches_v01_behavior(mocker):
    # Balanced is the v0.1 pure-tag-overlap behavior.
    _stub_configured(mocker, {"kimi": True, "ollama": True})
    provider, _ = pick(["local"], prefer="balanced")
    assert provider.name == "ollama"


def test_invalid_prefer_raises_with_fix_it_hint():
    with pytest.raises(InvalidRouterRequest) as exc:
        pick([], prefer="beast")
    msg = str(exc.value)
    assert "prefer='beast'" in msg
    assert "best" in msg  # fuzzy suggest lands on "best"


# ---------------------------------------------------------------------------
# v0.2 — tools filters
# ---------------------------------------------------------------------------


def test_tools_filter_excludes_providers_without_capability(mocker, monkeypatch):
    # Simulate a pre-v0.3.1 kimi/ollama (empty supported_tools) so the
    # capability filter visibly removes them. After v0.3.1 both providers
    # support the full tool set, but the filter logic itself must still
    # kick in for any provider that genuinely lacks a requested tool.
    from conductor.providers.kimi import KimiProvider
    from conductor.providers.ollama import OllamaProvider

    monkeypatch.setattr(KimiProvider, "supported_tools", frozenset())
    monkeypatch.setattr(OllamaProvider, "supported_tools", frozenset())
    _stub_configured(mocker, {"claude": True, "kimi": True, "ollama": True})
    provider, decision = pick([], tools={"Edit"})
    assert provider.name == "claude"
    skipped_names = {name for name, _ in decision.candidates_skipped}
    assert "kimi" in skipped_names
    assert "ollama" in skipped_names


def test_unknown_tool_name_raises():
    with pytest.raises(InvalidRouterRequest) as exc:
        pick([], tools={"NotARealTool"})
    assert "NotARealTool" in str(exc.value)


# ---------------------------------------------------------------------------
# v0.2 — exclude
# ---------------------------------------------------------------------------


def test_exclude_skips_named_providers(mocker):
    _stub_configured(mocker, {"claude": True, "codex": True})
    provider, decision = pick([], prefer="best", exclude={"claude"})
    assert provider.name == "codex"
    skipped_names = {name for name, _ in decision.candidates_skipped}
    assert "claude" in skipped_names


def test_exclude_all_raises(mocker):
    _stub_configured(mocker, {"claude": True})
    with pytest.raises(NoConfiguredProvider):
        pick([], exclude={"claude"})


def test_pick_skips_persistently_muted_providers(mocker):
    from click.testing import CliRunner

    from conductor.cli import main

    _stub_configured(mocker, {"claude": True, "codex": True})
    muted = CliRunner().invoke(main, ["providers", "mute", "claude"])
    assert muted.exit_code == 0, muted.output

    provider, decision = pick([], prefer="best")
    assert provider.name == "codex"
    skipped = dict(decision.candidates_skipped)
    assert skipped["claude"] == "muted persistently"


# ---------------------------------------------------------------------------
# v0.2 — effort translation
# ---------------------------------------------------------------------------


def test_effort_translates_to_thinking_budget(mocker):
    _stub_configured(mocker, {"claude": True})
    _, decision = pick([], prefer="best", effort="max")
    # claude.effort_to_thinking["max"] == 64_000
    assert decision.thinking_budget == 64_000
    assert decision.effort == "max"


def test_effort_integer_passes_through(mocker):
    _stub_configured(mocker, {"claude": True})
    _, decision = pick([], prefer="best", effort=12_345)
    assert decision.thinking_budget == 12_345


def test_effort_on_unsupported_provider_is_zero(mocker):
    # ollama.supports_effort=False, effort_to_thinking={}
    _stub_configured(mocker, {"ollama": True})
    _, decision = pick([], prefer="best", effort="max")
    assert decision.thinking_budget == 0  # ollama can't think


# ---------------------------------------------------------------------------
# v0.2 — session-local health
# ---------------------------------------------------------------------------


def test_rate_limited_provider_is_skipped(mocker):
    _stub_configured(mocker, {"claude": True, "codex": True})
    mark_rate_limited("claude")
    provider, decision = pick([], prefer="best")
    # claude would win on priority; rate-limit pushes us to codex.
    assert provider.name == "codex"
    skipped = dict(decision.candidates_skipped)
    assert "rate-limited" in skipped["claude"]


def test_auth_failed_provider_is_skipped(mocker):
    _stub_configured(mocker, {"claude": True, "codex": True})
    mark_auth_failed("claude")
    provider, decision = pick([], prefer="best")
    assert provider.name == "codex"
    skipped = dict(decision.candidates_skipped)
    assert "auth failed" in skipped["claude"]


# ---------------------------------------------------------------------------
# v0.2 — RouteDecision shape
# ---------------------------------------------------------------------------


def test_route_decision_includes_full_ranking(mocker):
    _stub_configured(mocker, {"claude": True, "codex": True, "ollama": True})
    _, decision = pick([], prefer="best")
    # All three configured providers present in ranked (not just the winner).
    names = [r.name for r in decision.ranked]
    assert set(names) == {"claude", "codex", "ollama"}
    # Ranking is sorted descending: claude (frontier) before ollama (local).
    assert names[0] == "claude"
    assert names[-1] == "ollama"


def test_route_decision_ranked_candidates_have_tier_info(mocker):
    _stub_configured(mocker, {"claude": True, "ollama": True})
    _, decision = pick([], prefer="best")
    tiers = {r.name: r.tier for r in decision.ranked}
    assert tiers["claude"] == "frontier"
    assert tiers["ollama"] == "local"


# ---------------------------------------------------------------------------
# Shadow ranking — surface providers that would have been preferable if
# the user had configured them.
# ---------------------------------------------------------------------------


def test_shadow_off_by_default_keeps_decision_empty(mocker):
    """The flag is opt-in; existing callers (Sentinel/Touchstone) see no change."""
    _stub_configured(mocker, {"claude": True})
    _, decision = pick([], prefer="best")
    assert decision.unconfigured_shadow == ()


def test_shadow_includes_unconfigured_providers(mocker):
    # Only claude is configured. With shadow=True every other provider that
    # would otherwise be eligible should appear in unconfigured_shadow.
    _stub_configured(mocker, {"claude": True})
    _, decision = pick([], prefer="best", shadow=True)
    shadow_names = {c.name for c in decision.unconfigured_shadow}
    # Every non-claude built-in provider that passes tool filters
    # (i.e. all of them under default args) should appear.
    assert "codex" in shadow_names
    assert "kimi" in shadow_names
    assert "ollama" in shadow_names
    assert "claude" not in shadow_names  # the picked provider is not its own shadow


def test_shadow_never_returns_unconfigured_as_winner(mocker):
    # Even if a frontier provider is the strongest shadow candidate by score,
    # it must never become the winner — pick() can only return providers the
    # caller can actually invoke. Exclude claude so codex is the unambiguous
    # top shadow (both are frontier; priority would otherwise tiebreak claude).
    _stub_configured(mocker, {"ollama": True})  # ollama=local; codex=frontier shadows
    provider, decision = pick(
        ["code-review"],
        prefer="best",
        exclude=frozenset({"claude"}),
        shadow=True,
    )
    assert provider.name == "ollama"
    assert decision.provider == "ollama"
    # codex outscores ollama on tier+tag, so it should be the top shadow.
    top_shadow = decision.unconfigured_shadow[0]
    assert top_shadow.name == "codex"
    assert top_shadow.combined_score > decision.ranked[0].combined_score


def test_shadow_carries_configured_failure_reason(mocker):
    """The reason text is what the CLI surfaces in the heads-up advisory."""
    from conductor.providers import ClaudeProvider, CodexProvider

    mocker.patch.object(
        ClaudeProvider, "configured", lambda self: (True, None)
    )
    mocker.patch.object(
        CodexProvider,
        "configured",
        lambda self: (False, "`codex` CLI not found on PATH"),
    )
    # Stub everything else as unconfigured with a generic reason.
    from conductor.providers import (
        DeepSeekChatProvider,
        DeepSeekReasonerProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (
        DeepSeekChatProvider,
        DeepSeekReasonerProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    ):
        mocker.patch.object(
            cls,
            "configured",
            lambda self: (False, "stub: not configured"),
        )

    _, decision = pick([], prefer="best", shadow=True)
    codex_shadow = next(c for c in decision.unconfigured_shadow if c.name == "codex")
    assert codex_shadow.unconfigured_reason == "`codex` CLI not found on PATH"


def test_shadow_respects_exclude_filter(mocker):
    """An excluded provider must not appear as a shadow candidate either."""
    _stub_configured(mocker, {"claude": True})
    _, decision = pick(
        [], prefer="best", exclude=frozenset({"codex"}), shadow=True
    )
    shadow_names = {c.name for c in decision.unconfigured_shadow}
    assert "codex" not in shadow_names


def test_shadow_respects_tools_filter(mocker, monkeypatch):
    """A provider that doesn't support requested tools shouldn't shadow either —
    suggesting it would be silly when even installed it couldn't do the job."""
    from conductor.providers.kimi import KimiProvider

    monkeypatch.setattr(KimiProvider, "supported_tools", frozenset())
    _stub_configured(mocker, {"claude": True})
    _, decision = pick([], tools={"Edit"}, shadow=True)
    shadow_names = {c.name for c in decision.unconfigured_shadow}
    assert "kimi" not in shadow_names


def test_shadow_ranked_by_score(mocker):
    """Top shadow candidate is the one with the highest combined_score."""
    _stub_configured(mocker, {"ollama": True})
    _, decision = pick(["code-review"], prefer="best", shadow=True)
    scores = [c.combined_score for c in decision.unconfigured_shadow]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Tag defaults — explicit natural-provider preferences from router.toml.
# ---------------------------------------------------------------------------


def test_tag_default_prefers_codex_over_claude_for_code_review(mocker, tmp_path):
    _write_router_defaults(
        tmp_path / "home",
        '[tag_defaults]\ncode-review = "codex"\n',
    )
    _stub_configured(mocker, {"claude": True, "codex": True})

    provider, decision = pick(["code-review"], prefer="best")

    assert provider.name == "codex"
    assert decision.tag_default_applied == {"code-review": "codex"}


def test_tag_default_does_not_apply_when_provider_is_unconfigured(mocker, tmp_path):
    _write_router_defaults(
        tmp_path / "home",
        '[tag_defaults]\ncode-review = "codex"\n',
    )
    _stub_configured(mocker, {"claude": True})

    provider, decision = pick(["code-review"], prefer="best")

    assert provider.name == "claude"
    assert decision.tag_default_applied == {}
    assert ("code-review", "codex", "not configured") in decision.tag_default_considered


def test_tag_default_does_not_apply_when_provider_is_excluded(mocker, tmp_path):
    _write_router_defaults(
        tmp_path / "home",
        '[tag_defaults]\ncode-review = "codex"\n',
    )
    _stub_configured(mocker, {"claude": True, "codex": True})

    provider, decision = pick(
        ["code-review"],
        prefer="best",
        exclude={"codex"},
    )

    assert provider.name == "claude"
    assert decision.tag_default_applied == {}
    assert ("code-review", "codex", "excluded by --exclude") in decision.tag_default_considered


def test_prefer_cheapest_ignores_tag_default(mocker, tmp_path):
    _write_router_defaults(
        tmp_path / "home",
        '[tag_defaults]\ncode-review = "codex"\n',
    )
    _stub_configured(mocker, {"codex": True, "ollama": True})

    provider, decision = pick(["code-review"], prefer="cheapest")

    assert provider.name == "ollama"
    assert decision.tag_default_applied == {}
    assert decision.tag_default_considered == ()


def test_repo_local_tag_default_overrides_home_config(mocker, tmp_path):
    _write_router_defaults(
        tmp_path / "home",
        '[tag_defaults]\ncode-review = "claude"\n',
    )
    repo_config = tmp_path / ".conductor"
    repo_config.mkdir(parents=True, exist_ok=True)
    (repo_config / "router.toml").write_text(
        '[tag_defaults]\ncode-review = "codex"\n',
        encoding="utf-8",
    )
    _stub_configured(mocker, {"claude": True, "codex": True})

    provider, decision = pick(["code-review"], prefer="best")

    assert provider.name == "codex"
    assert decision.tag_default_applied == {"code-review": "codex"}
