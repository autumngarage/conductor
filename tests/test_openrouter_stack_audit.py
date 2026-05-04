"""Tests for audited OpenRouter model stack validation."""

from __future__ import annotations

from conductor.openrouter_stack_audit import (
    StackDefinition,
    audit_openrouter_coding_stacks,
)
from conductor.providers.openrouter_catalog import CatalogSnapshot, ModelEntry


def _model(
    model_id: str,
    *,
    supports_tools: bool = True,
    supports_thinking: bool = True,
    context_length: int = 200_000,
    pricing_prompt: float = 0.001,
    pricing_completion: float = 0.002,
) -> ModelEntry:
    return ModelEntry(
        id=model_id,
        name=model_id,
        created=1_710_000_000,
        context_length=context_length,
        pricing_prompt=pricing_prompt,
        pricing_completion=pricing_completion,
        pricing_thinking=0.001 if supports_thinking else None,
        supports_thinking=supports_thinking,
        supports_tools=supports_tools,
        supports_vision=False,
    )


def _snapshot(*models: ModelEntry) -> CatalogSnapshot:
    return CatalogSnapshot(fetched_at=1_710_000_000, models=list(models))


def _stack(*models: str) -> tuple[StackDefinition, ...]:
    return (
        StackDefinition(
            name="TEST_STACK",
            effort="high",
            models=tuple(models),
        ),
    )


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def test_valid_frontier_coding_stack_has_no_errors():
    report = audit_openrouter_coding_stacks(
        _snapshot(_model("openai/gpt-5.3-codex")),
        stacks=_stack("openai/gpt-5.3-codex"),
    )

    assert report.has_errors is False
    assert _codes(report) == set()
    assert report.models[0].catalog_available is True
    assert report.models[0].direct_sendable is True


def test_stale_slug_is_error():
    report = audit_openrouter_coding_stacks(
        _snapshot(_model("openai/gpt-5.3-codex")),
        stacks=_stack("missing/model"),
    )

    assert report.has_errors is True
    assert "missing-from-catalog" in _codes(report)


def test_alias_slug_is_not_directly_sendable():
    report = audit_openrouter_coding_stacks(
        _snapshot(),
        stacks=_stack("~openai/gpt-latest"),
    )

    assert report.has_errors is True
    assert "alias-not-sendable" in _codes(report)
    assert report.models[0].direct_sendable is False
    assert "alias" in report.models[0].caveats


def test_missing_tool_support_is_error():
    report = audit_openrouter_coding_stacks(
        _snapshot(_model("openai/gpt-5.3-codex", supports_tools=False)),
        stacks=_stack("openai/gpt-5.3-codex"),
    )

    assert report.has_errors is True
    assert "missing-tool-support" in _codes(report)


def test_free_tier_models_are_not_eligible_for_coding_stack():
    report = audit_openrouter_coding_stacks(
        _snapshot(_model("qwen/qwen3-coder:free")),
        stacks=_stack("qwen/qwen3-coder:free"),
    )

    assert report.has_errors is True
    assert "free-tier-model" in _codes(report)
    assert "free-tier" in report.models[0].caveats


def test_preview_and_missing_reasoning_are_warnings_not_errors():
    report = audit_openrouter_coding_stacks(
        _snapshot(
            _model(
                "google/gemini-3.1-pro-preview",
                supports_thinking=False,
                context_length=64_000,
            )
        ),
        stacks=_stack("google/gemini-3.1-pro-preview"),
    )

    assert report.has_errors is False
    assert report.has_warnings is True
    assert {
        "preview-model",
        "missing-reasoning-support",
        "short-context",
    }.issubset(_codes(report))
