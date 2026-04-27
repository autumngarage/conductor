from __future__ import annotations

import json

import httpx
import pytest
import respx
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.deepseek import (
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_REASONER_MODEL,
    DeepSeekChatProvider,
    DeepSeekReasonerProvider,
)
from conductor.providers.openrouter import OpenRouterProvider


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
