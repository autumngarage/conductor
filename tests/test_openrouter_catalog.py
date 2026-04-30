"""Tests for the OpenRouter catalog cache and parser."""

from __future__ import annotations

import time

import httpx
import pytest
import respx

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.providers.interface import ProviderHTTPError


@pytest.fixture
def catalog_response() -> dict:
    return {
        "data": [
            {
                "id": "anthropic/claude-sonnet-4.6",
                "name": "Claude Sonnet 4.6",
                "created": 1_710_000_000,
                "context_length": 200_000,
                "pricing": {
                    "prompt": "0.000003",
                    "completion": "0.000015",
                    "reasoning": "0.000004",
                },
                "supported_parameters": ["reasoning", "tools"],
                "architecture": {"modality": "text+image->text"},
            },
            {
                "id": "openai/gpt-4.1-mini",
                "name": "GPT-4.1 mini",
                "created": 1_709_000_000,
                "context_length": 64_000,
                "pricing": {
                    "prompt": "0.0000003",
                    "completion": "0.0000012",
                },
                "supported_parameters": [],
                "architecture": {"modality": "text->text"},
            },
        ]
    }


@pytest.fixture
def catalog_cache(tmp_path, monkeypatch):
    path = tmp_path / "openrouter-catalog.json"
    monkeypatch.setattr(openrouter_catalog, "OPENROUTER_CATALOG_CACHE_PATH", path)
    return path


def _write_snapshot(path, *, fetched_at: int, model_id: str = "cached/model") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        f'  "fetched_at": {fetched_at},\n'
        '  "models": [\n'
        "    {\n"
        f'      "id": "{model_id}",\n'
        '      "name": "Cached model",\n'
        '      "created": 1700000000,\n'
        '      "context_length": 32000,\n'
        '      "pricing_prompt": 0.001,\n'
        '      "pricing_completion": 0.002,\n'
        '      "pricing_thinking": null,\n'
        '      "supports_thinking": false,\n'
        '      "supports_tools": false,\n'
        '      "supports_vision": false\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def test_load_catalog_fetches_and_caches_on_cache_miss(catalog_response, catalog_cache):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(return_value=httpx.Response(200, json=catalog_response))
        snapshot = openrouter_catalog.load_catalog_snapshot()

    assert len(snapshot.models) == 2
    assert catalog_cache.exists()
    cached = openrouter_catalog.read_cached_catalog()
    assert cached is not None
    assert cached.models[0].id == "anthropic/claude-sonnet-4.6"


def test_load_catalog_uses_fresh_cache_without_http(catalog_cache):
    _write_snapshot(catalog_cache, fetched_at=int(time.time()))

    with respx.mock(base_url="https://openrouter.ai/api/v1"):
        snapshot = openrouter_catalog.load_catalog_snapshot()

    assert [model.id for model in snapshot.models] == ["cached/model"]


def test_load_catalog_refreshes_stale_cache(catalog_response, catalog_cache):
    _write_snapshot(catalog_cache, fetched_at=int(time.time()) - (48 * 3600))

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        route = router.get("/models").mock(
            return_value=httpx.Response(200, json=catalog_response)
        )
        snapshot = openrouter_catalog.load_catalog_snapshot()

    assert route.called
    assert snapshot.models[0].id == "anthropic/claude-sonnet-4.6"


def test_force_refresh_bypasses_fresh_cache(catalog_response, catalog_cache):
    _write_snapshot(catalog_cache, fetched_at=int(time.time()))

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        route = router.get("/models").mock(
            return_value=httpx.Response(200, json=catalog_response)
        )
        snapshot = openrouter_catalog.load_catalog_snapshot(force_refresh=True)

    assert route.called
    assert snapshot.models[1].id == "openai/gpt-4.1-mini"


def test_load_catalog_falls_back_to_stale_cache_on_network_error(
    capsys, catalog_cache
):
    stale_at = int(time.time()) - (48 * 3600)
    _write_snapshot(catalog_cache, fetched_at=stale_at)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(side_effect=httpx.ConnectError("network down"))
        snapshot = openrouter_catalog.load_catalog_snapshot()

    assert [model.id for model in snapshot.models] == ["cached/model"]
    assert "using stale cache" in capsys.readouterr().err


def test_load_catalog_can_require_fresh_catalog_on_network_error(catalog_cache):
    stale_at = int(time.time()) - (48 * 3600)
    _write_snapshot(catalog_cache, fetched_at=stale_at)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(side_effect=httpx.ConnectError("network down"))
        with pytest.raises(
            ProviderHTTPError,
            match="network error refreshing OpenRouter catalog",
        ):
            openrouter_catalog.load_catalog_snapshot(allow_stale_on_error=False)


def test_load_catalog_raises_without_cache_on_network_error(catalog_cache):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(side_effect=httpx.ConnectError("network down"))
        with pytest.raises(ProviderHTTPError, match="network error refreshing OpenRouter catalog"):
            openrouter_catalog.load_catalog_snapshot()


def test_read_cached_catalog_returns_none_when_missing(catalog_cache):
    assert openrouter_catalog.read_cached_catalog() is None


def test_parser_derives_capabilities_and_per_1k_pricing(
    catalog_response, catalog_cache
):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(return_value=httpx.Response(200, json=catalog_response))
        models = openrouter_catalog.load_catalog()

    sonnet = models[0]
    assert sonnet.pricing_prompt == pytest.approx(0.003)
    assert sonnet.pricing_completion == pytest.approx(0.015)
    assert sonnet.pricing_thinking == pytest.approx(0.004)
    assert sonnet.supports_thinking is True
    assert sonnet.supports_tools is True
    assert sonnet.supports_vision is True
