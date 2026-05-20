"""Tests for the deterministic semantic routing matrix."""

from __future__ import annotations

import pytest

from conductor.openrouter_model_stacks import (
    OPENROUTER_CODING_HIGH,
    OPENROUTER_CODING_MAX,
    OPENROUTER_REVIEW_CHEAP,
)
from conductor.semantic import SEMANTIC_KINDS, plan_for


@pytest.mark.parametrize("kind", SEMANTIC_KINDS)
@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "max"])
def test_every_semantic_kind_has_every_effort_bucket(kind, effort):
    plan = plan_for(kind, effort)

    assert plan.kind == kind
    assert plan.effort_bucket == effort
    assert plan.candidates


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "max"])
def test_council_is_openrouter_only(effort):
    plan = plan_for("council", effort)

    assert plan.mode == "council"
    assert [candidate.provider for candidate in plan.candidates] == ["openrouter"]
    assert plan.council_member_models
    assert plan.council_synthesis_models


@pytest.mark.parametrize("effort", ["minimal", "low", "medium"])
def test_low_and_medium_code_stay_on_openrouter_before_agentic_exec(effort):
    plan = plan_for("code", effort)

    assert plan.mode == "call"
    assert plan.candidates[0].provider == "openrouter"
    assert plan.candidates[0].models == ()


@pytest.mark.parametrize("effort", ["high", "max"])
def test_high_code_escalates_to_agentic_coding_stack(effort):
    plan = plan_for("code", effort)

    assert plan.mode == "exec"
    assert [candidate.provider for candidate in plan.candidates] == [
        "codex",
        "openrouter",
        "ollama",
    ]
    openrouter_candidate = plan.candidates[1]
    expected_stack = (
        OPENROUTER_CODING_MAX if effort == "max" else OPENROUTER_CODING_HIGH
    )
    assert openrouter_candidate.models == expected_stack
    assert openrouter_candidate.models[0] == "openai/gpt-5.3-codex"
    assert "openrouter/auto" not in openrouter_candidate.models
    assert "google/gemini-2.5-flash-lite" not in openrouter_candidate.models
    assert plan.tools == frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    assert plan.sandbox == "none"


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "max"])
def test_review_falls_back_to_cheap_openrouter_stack(effort):
    plan = plan_for("review", effort)

    assert plan.mode == "review"
    assert [candidate.provider for candidate in plan.candidates] == [
        "codex",
        "claude",
        "openrouter",
    ]
    assert plan.candidates[2].models == OPENROUTER_REVIEW_CHEAP


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "max"])
def test_review_openrouter_step_never_starts_with_premium_coding_model(effort):
    """Regression guard for #501.

    Why: when codex and claude native review fail, the OpenRouter step used to
    start with `openai/gpt-5.3-codex` (premium), burning ~$30/day at the
    operator's observed volume. The cascade must fail toward the cheapest
    correct provider, not the most expensive same-category one.
    """
    plan = plan_for("review", effort)
    openrouter_models = plan.candidates[2].models

    assert openrouter_models[0] != "openai/gpt-5.3-codex"
    assert "openai/gpt-5.3-codex" not in openrouter_models
    assert "openai/gpt-5.5-pro" not in openrouter_models
    assert "anthropic/claude-opus-4.7" not in openrouter_models


def test_integer_effort_maps_into_bucketed_matrix():
    assert plan_for("research", 0).effort_bucket == "minimal"
    assert plan_for("research", 1).effort_bucket == "low"
    assert plan_for("research", 8_001).effort_bucket == "high"
    assert plan_for("research", 24_001).effort_bucket == "max"
