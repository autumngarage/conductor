"""Tests for OpenRouter catalog-driven model selection."""

from __future__ import annotations

import pytest

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.providers.interface import ProviderError
from conductor.providers.openrouter import OPENROUTER_DEFAULT_MODEL, select_model_for_task


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


def test_prefer_best_returns_auto_with_shortlist(mocker, fixture_catalog):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task([], "best", "medium")

    assert payload["model"] == OPENROUTER_DEFAULT_MODEL
    assert payload["reasoning"] == {"effort": "medium"}
    shortlist = payload["plugins"][0]["allowed_models"]
    assert len(shortlist) == 6
    assert shortlist[0] == "recent/general"


def test_selector_drops_tilde_aliases_from_auto_shortlist(mocker, fixture_catalog):
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

    payload = select_model_for_task([], "best", "medium")

    shortlist = payload["plugins"][0]["allowed_models"]
    assert "~anthropic/claude-haiku-latest" not in shortlist
    assert all(not model.startswith("~") for model in shortlist)


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["thinking"], {"cheap/thinker", "expensive/reasoner"}),
        (["tool-use"], {"expensive/reasoner", "tool/vision"}),
        (["vision"], {"tool/vision"}),
        (["long-context"], {"expensive/reasoner", "long/context"}),
    ],
)
def test_capability_filters_reduce_candidate_set(mocker, fixture_catalog, tags, expected):
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=fixture_catalog,
    )

    payload = select_model_for_task(tags, "best", "medium")
    shortlist = set(payload["plugins"][0]["allowed_models"])

    assert shortlist == expected


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
