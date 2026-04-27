"""Smoke tests for `conductor models ...` commands."""

from __future__ import annotations

import httpx
import respx
from click.testing import CliRunner

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.cli import main


def _patch_catalog_cache(monkeypatch, tmp_path):
    path = tmp_path / "openrouter-catalog.json"
    monkeypatch.setattr(openrouter_catalog, "OPENROUTER_CATALOG_CACHE_PATH", path)
    return path


def _catalog_response() -> dict:
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
                "id": "google/gemini-flash-1.5",
                "name": "Gemini Flash 1.5",
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


def test_models_refresh_fetches_and_prints_summary(monkeypatch, tmp_path):
    cache_path = _patch_catalog_cache(monkeypatch, tmp_path)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(
            return_value=httpx.Response(200, json=_catalog_response())
        )
        result = CliRunner().invoke(main, ["models", "refresh"])

    assert result.exit_code == 0, result.output
    assert "Refreshed OpenRouter catalog at" in result.output
    assert "2 models" in result.output
    assert cache_path.exists()


def test_models_list_reads_cached_catalog(monkeypatch, tmp_path):
    _patch_catalog_cache(monkeypatch, tmp_path)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(
            return_value=httpx.Response(200, json=_catalog_response())
        )
        refresh = CliRunner().invoke(main, ["models", "refresh"])
    assert refresh.exit_code == 0, refresh.output

    result = CliRunner().invoke(main, ["models", "list"])

    assert result.exit_code == 0, result.output
    assert "models indexed, last refresh:" in result.output
    assert "anthropic/claude-sonnet-4.6" in result.output
    assert "google/gemini-flash-1.5" in result.output


def test_models_show_prints_one_model(monkeypatch, tmp_path):
    _patch_catalog_cache(monkeypatch, tmp_path)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(
            return_value=httpx.Response(200, json=_catalog_response())
        )
        refresh = CliRunner().invoke(main, ["models", "refresh"])
    assert refresh.exit_code == 0, refresh.output

    result = CliRunner().invoke(
        main, ["models", "show", "anthropic/claude-sonnet-4.6"]
    )

    assert result.exit_code == 0, result.output
    assert "Claude Sonnet 4.6" in result.output
    assert "context length: 200,000" in result.output
    assert "thinking=yes" in result.output


def test_models_list_errors_when_cache_is_missing(monkeypatch, tmp_path):
    _patch_catalog_cache(monkeypatch, tmp_path)

    result = CliRunner().invoke(main, ["models", "list"])

    assert result.exit_code != 0
    assert "Run `conductor models refresh` first" in result.output
