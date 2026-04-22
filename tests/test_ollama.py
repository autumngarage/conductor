"""Tests for the Ollama provider — mocked httpx via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from conductor.providers.interface import (
    ProviderConfigError,
    ProviderHTTPError,
)
from conductor.providers.ollama import (
    OLLAMA_BASE_URL_ENV,
    OLLAMA_DEFAULT_BASE_URL,
    OllamaProvider,
)

CHAT_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/chat"
TAGS_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/tags"


@pytest.fixture(autouse=True)
def _no_base_url_override(monkeypatch):
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)


def test_configured_true_when_server_healthy():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, reason = OllamaProvider().configured()
    assert ok is True and reason is None


def test_configured_false_when_server_unreachable():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(side_effect=httpx.ConnectError("refused"))
        ok, reason = OllamaProvider().configured()
    assert ok is False
    assert "Ollama" in reason


def test_default_model_available_true_when_pulled():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"name": "qwen2.5-coder:14b"}, {"name": "other:1b"}]},
            )
        )
        ok, reason = OllamaProvider().default_model_available()
    assert ok is True and reason is None


def test_default_model_available_false_lists_alternatives():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "qwen2.5-coder:7b"}]}
            )
        )
        ok, reason = OllamaProvider().default_model_available()
    assert ok is False
    assert "qwen2.5-coder:14b" in reason
    assert "ollama pull" in reason
    assert "qwen2.5-coder:7b" in reason  # shows locally installed alternatives


def test_default_model_available_false_when_no_models_pulled():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, reason = OllamaProvider().default_model_available()
    assert ok is False
    assert "ollama pull qwen2.5-coder:14b" in reason


def test_configured_honors_env_override(monkeypatch):
    monkeypatch.setenv(OLLAMA_BASE_URL_ENV, "http://ollama.internal:11434")
    with respx.mock() as router:
        router.get("http://ollama.internal:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, _ = OllamaProvider().configured()
    assert ok is True


def test_call_returns_normalized_response():
    body = {
        "model": "qwen2.5-coder:14b",
        "message": {"role": "assistant", "content": "hello from ollama"},
        "prompt_eval_count": 8,
        "eval_count": 3,
        "total_duration": 1_500_000_000,  # 1.5s in nanoseconds
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = OllamaProvider().call("hi")

    assert response.text == "hello from ollama"
    assert response.provider == "ollama"
    assert response.model == "qwen2.5-coder:14b"
    assert response.usage["input_tokens"] == 8
    assert response.usage["output_tokens"] == 3
    assert response.usage["cached_tokens"] is None
    assert response.usage["thinking_budget"] == 0
    assert response.duration_ms == 1500


def test_call_raises_on_unreachable_endpoint():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ProviderConfigError):
            OllamaProvider().call("hi")


def test_call_raises_on_non_200():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(500, text="oops"))
        with pytest.raises(ProviderHTTPError) as exc:
            OllamaProvider().call("hi")
    assert "500" in str(exc.value)


def test_call_raises_on_missing_content():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json={"message": {}})
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OllamaProvider().call("hi")
    assert "content" in str(exc.value)
