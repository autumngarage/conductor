"""Tests for the auto-mode router.

All five providers expose their own ``configured()`` (env-var or CLI check);
we stub those directly so tests run without real CLIs/env vars.
"""

from __future__ import annotations

import pytest

from conductor.router import (
    DEFAULT_PRIORITY,
    NoConfiguredProvider,
    pick,
)


def _stub_configured(mocker, results: dict[str, bool]):
    """Patch ``configured()`` on each provider class with a fixed result.

    Keys in ``results`` are provider identifiers; values are True/False.
    Providers missing from the dict default to False (unconfigured).
    """
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    classes = {
        "kimi": KimiProvider,
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "gemini": GeminiProvider,
        "ollama": OllamaProvider,
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
    assert skipped_names == {"claude", "codex", "gemini", "ollama"}


def test_priority_order_is_stable():
    # Guardrail: the default priority is part of the project's opinionated
    # default. A change here should be a deliberate doctrine-level decision,
    # not a drive-by edit. Priority entries that don't map to a registered
    # provider (e.g. "mistral" before its adapter lands) are silently
    # skipped by `pick()` — they reserve the slot for future adapters.
    assert DEFAULT_PRIORITY == ("kimi", "claude", "mistral", "codex", "gemini", "ollama")
