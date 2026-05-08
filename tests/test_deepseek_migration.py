from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from click.testing import CliRunner

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.cli import main
from conductor.providers.deepseek import (
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_REASONER_MODEL,
    DeepSeekChatProvider,
    DeepSeekReasonerProvider,
)
from conductor.providers.openrouter import OpenRouterProvider


@pytest.fixture(autouse=True)
def _isolated_init_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)


def _model(model_id: str, *, created: int) -> openrouter_catalog.ModelEntry:
    return openrouter_catalog.ModelEntry(
        id=model_id,
        name=model_id,
        created=created,
        context_length=128_000,
        pricing_prompt=0.001,
        pricing_completion=0.002,
        pricing_thinking=None,
        supports_thinking=False,
        supports_tools=False,
        supports_vision=False,
    )


def _write_catalog_cache(path, *, model_id: str, fetched_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        f'  "fetched_at": {fetched_at},\n'
        '  "models": [\n'
        "    {\n"
        f'      "id": "{model_id}",\n'
        f'      "name": "{model_id}",\n'
        '      "created": 1800000000,\n'
        '      "context_length": 128000,\n'
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


@pytest.mark.parametrize(
    ("provider_cls", "model_slug"),
    [
        (DeepSeekChatProvider, DEEPSEEK_CHAT_MODEL),
        (DeepSeekReasonerProvider, DEEPSEEK_REASONER_MODEL),
    ],
)
def test_deepseek_providers_subclass_openrouter_and_preset_model(
    provider_cls: type[OpenRouterProvider],
    model_slug: str,
):
    assert issubclass(provider_cls, OpenRouterProvider)
    provider = provider_cls()
    assert provider.default_model == model_slug
    assert provider.fix_command == "conductor init --only openrouter"


def test_deepseek_chat_call_uses_preset_openrouter_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(
        openrouter_catalog,
        "load_catalog",
        lambda: [_model(DEEPSEEK_CHAT_MODEL, created=1_700_000_000)],
    )
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": DEEPSEEK_CHAT_MODEL,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = DeepSeekChatProvider().call("hi")

    assert response.model == DEEPSEEK_CHAT_MODEL
    assert captured["payload"] == {
        "model": DEEPSEEK_CHAT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.parametrize(
    ("provider_cls", "catalog_models", "expected_model"),
    [
        (
            DeepSeekChatProvider,
            [
                _model("deepseek/deepseek-chat", created=1_700_000_000),
                _model("deepseek/deepseek-v3.2-chat", created=1_800_000_000),
                _model("deepseek/deepseek-r1", created=1_900_000_000),
            ],
            "deepseek/deepseek-v3.2-chat",
        ),
        (
            DeepSeekReasonerProvider,
            [
                _model("deepseek/deepseek-r1", created=1_700_000_000),
                _model("deepseek/deepseek-r2", created=1_800_000_000),
                _model("deepseek/deepseek-v3.2-chat", created=1_900_000_000),
            ],
            "deepseek/deepseek-r2",
        ),
    ],
)
def test_deepseek_call_uses_newest_matching_catalog_slug(
    monkeypatch, provider_cls, catalog_models, expected_model
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(openrouter_catalog, "load_catalog", lambda: catalog_models)
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": expected_model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = provider_cls().call("hi")

    assert response.model == expected_model
    assert captured["payload"]["model"] == expected_model


def test_deepseek_uses_stale_catalog_cache_when_refresh_fails(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    cache_path = tmp_path / "openrouter-catalog.json"
    monkeypatch.setattr(openrouter_catalog, "OPENROUTER_CATALOG_CACHE_PATH", cache_path)
    cached_model = "deepseek/deepseek-v3.2-chat"
    _write_catalog_cache(
        cache_path,
        model_id=cached_model,
        fetched_at=int(time.time()) - (48 * 3600),
    )
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": cached_model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.get("/models").mock(side_effect=httpx.ConnectError("network down"))
        router.post("/chat/completions").mock(side_effect=_record)
        response = DeepSeekChatProvider().call("hi")

    assert response.model == cached_model
    assert captured["payload"]["model"] == cached_model
    assert "using stale cache" in capsys.readouterr().err


def test_init_deepseek_chat_surfaces_migration_message(mocker, monkeypatch):
    mocker.patch("conductor.wizard._is_tty", return_value=True)
    mocker.patch.object(
        DeepSeekChatProvider, "configured", lambda self: (False, "missing")
    )
    mocker.patch.object(OpenRouterProvider, "smoke", return_value=(True, None))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deprecated-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = CliRunner().invoke(
        main,
        ["init", "--only", "deepseek-chat"],
        input="or-test-key\nprint\n",
    )

    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY is deprecated" in result.output
    assert "conductor init --only openrouter" in result.output
    assert "OpenRouter API key (OPENROUTER_API_KEY)" in result.output
    assert "DeepSeek API key (DEEPSEEK_API_KEY)" not in result.output
