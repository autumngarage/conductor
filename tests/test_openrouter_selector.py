"""Tests for OpenRouter catalog-driven model selection."""

from __future__ import annotations

import pytest

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.openrouter_model_stacks import (
    OPENROUTER_CODING_HIGH,
    OPENROUTER_CODING_MAX,
)
from conductor.providers.interface import ProviderError
from conductor.providers.openrouter import (
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_MODELS_ARRAY_MAX,
    select_model_for_task,
)


@pytest.fixture
def fixture_catalog() -> list[openrouter_catalog.ModelEntry]:
    return [
        openrouter_catalog.ModelEntry(
            id="cheap/basic",
            name="Cheap Basic",
            created=100,
            context_length=32_000,
            pricing_prompt=0.001,
            pricing_completion=0.001,
            pricing_thinking=None,
            supports_thinking=False,
            supports_tools=False,
            supports_vision=False,
        ),
        openrouter_catalog.ModelEntry(
            id="cheap/thinker",
            name="Cheap Thinker",
            created=200,
            context_length=64_000,
            pricing_prompt=0.002,
            pricing_completion=0.002,
            pricing_thinking=0.001,
            supports_thinking=True,
            supports_tools=False,
            supports_vision=False,
        ),
        openrouter_catalog.ModelEntry(
            id="expensive/reasoner",
            name="Expensive Reasoner",
            created=300,
            context_length=128_000,
            pricing_prompt=0.009,
            pricing_completion=0.009,
            pricing_thinking=0.004,
            supports_thinking=True,
            supports_tools=True,
            supports_vision=False,
        ),
        openrouter_catalog.ModelEntry(
            id="tool/vision",
            name="Tool Vision",
            created=250,
            context_length=64_000,
            pricing_prompt=0.004,
            pricing_completion=0.004,
            pricing_thinking=None,
            supports_thinking=False,
            supports_tools=True,
            supports_vision=True,
        ),
        openrouter_catalog.ModelEntry(
            id="free/tool-model:free",
            name="Free Tool Model",
            created=260,
            context_length=64_000,
            pricing_prompt=0.0,
            pricing_completion=0.0,
            pricing_thinking=None,
            supports_thinking=False,
            supports_tools=True,
            supports_vision=False,
        ),
        openrouter_catalog.ModelEntry(
            id="long/context",
            name="Long Context",
            created=275,
            context_length=200_000,
            pricing_prompt=0.003,
            pricing_completion=0.003,
            pricing_thinking=None,
            supports_thinking=False,
            supports_tools=False,
            supports_vision=False,
        ),
        openrouter_catalog.ModelEntry(
            id="recent/general",
            name="Recent General",
            created=400,
            context_length=64_000,
            pricing_prompt=0.005,
            pricing_completion=0.005,
            pricing_thinking=None,
            supports_tools=False,
            supports_thinking=False,
            supports_vision=False,
        ),
    ]


def test_prefer_cheapest_returns_direct_model(mocker, fixture_catalog):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(["thinking"], "cheapest", "medium")

    assert payload == {"model": "cheap/thinker", "reasoning": None}


def test_prefer_best_returns_unrestricted_auto_without_catalog(mocker, fixture_catalog):
    load_catalog = mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task([], "best", "medium")

    assert payload["model"] == OPENROUTER_DEFAULT_MODEL
    assert payload["reasoning"] == {"effort": "medium"}
    assert "plugins" not in payload
    load_catalog.assert_not_called()


@pytest.mark.parametrize(
    ("prefer", "effort", "expected_stack"),
    [
        ("best", "high", OPENROUTER_CODING_HIGH),
        ("balanced", "medium", OPENROUTER_CODING_HIGH),
        ("best", "max", OPENROUTER_CODING_MAX),
    ],
)
def test_tool_use_best_and_balanced_use_curated_coding_stack_without_catalog(
    mocker,
    fixture_catalog,
    prefer,
    effort,
    expected_stack,
):
    load_catalog = mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(["tool-use", "strong-reasoning"], prefer, effort)

    assert payload["models"] == list(expected_stack)
    assert payload["models"][:OPENROUTER_MODELS_ARRAY_MAX] == list(
        expected_stack[:OPENROUTER_MODELS_ARRAY_MAX]
    )
    assert payload["models"][0] == "openai/gpt-5.3-codex"
    assert any(model.startswith("openai/") for model in payload["models"])
    assert any(model.startswith("anthropic/") for model in payload["models"])
    assert any(model.startswith("google/") for model in payload["models"])
    assert all(":free" not in model for model in payload["models"])
    assert payload["reasoning"] == {"effort": "xhigh" if effort == "max" else effort}
    assert "model" not in payload
    assert OPENROUTER_DEFAULT_MODEL not in payload["models"]
    assert "google/gemini-2.5-flash-lite" not in payload["models"]
    load_catalog.assert_not_called()


def test_tool_use_coding_stack_honors_excluded_models(mocker, fixture_catalog):
    load_catalog = mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(
        ["tool-use"],
        "best",
        "high",
        exclude={OPENROUTER_CODING_HIGH[0]},
    )

    assert payload["models"] == list(OPENROUTER_CODING_HIGH[1:])
    load_catalog.assert_not_called()


def test_tool_use_coding_stack_errors_when_fully_excluded(mocker, fixture_catalog):
    load_catalog = mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    with pytest.raises(ProviderError, match="coding stack was fully excluded"):
        select_model_for_task(
            ["tool-use"],
            "best",
            "high",
            exclude=set(OPENROUTER_CODING_HIGH),
        )

    load_catalog.assert_not_called()


def test_tool_use_cheap_can_pick_free_tier_model(mocker, fixture_catalog):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(["tool-use"], "cheapest", "medium")

    assert payload == {"model": "free/tool-model:free", "reasoning": None}


def test_selector_drops_tilde_aliases_from_direct_selection(mocker, fixture_catalog):
    alias = openrouter_catalog.ModelEntry(
        id="~anthropic/claude-haiku-latest",
        name="Anthropic Claude Haiku Latest",
        created=500,
        context_length=200_000,
        pricing_prompt=0.001,
        pricing_completion=0.005,
        pricing_thinking=0.001,
        supports_thinking=True,
        supports_tools=True,
        supports_vision=True,
    )
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=[alias, *fixture_catalog],
    )

    payload = select_model_for_task([], "cheapest", "medium")

    assert payload["model"] != "~anthropic/claude-haiku-latest"
    assert not str(payload["model"]).startswith("~")


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["thinking"], {"cheap/thinker", "expensive/reasoner"}),
        (["tool-use"], {"expensive/reasoner", "free/tool-model:free", "tool/vision"}),
        (["vision"], {"tool/vision"}),
        (["long-context"], {"expensive/reasoner", "long/context"}),
    ],
)
def test_capability_filters_reduce_candidate_set(mocker, fixture_catalog, tags, expected):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(tags, "cheapest", "medium")

    assert payload["model"] in expected


def test_exclude_removes_named_models(mocker, fixture_catalog):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(
        ["thinking"],
        "cheapest",
        "medium",
        exclude={"cheap/thinker"},
    )

    assert payload["model"] == "expensive/reasoner"


def test_empty_filtered_result_raises_clear_error(mocker, fixture_catalog):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    with pytest.raises(ProviderError, match="tags filtered to empty"):
        select_model_for_task(["vision", "thinking"], "cheapest", "medium")
