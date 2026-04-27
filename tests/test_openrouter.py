"""Unit tests for the OpenRouter provider — mocked httpx, no live calls."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    UnsupportedCapability,
)
from conductor.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_DEFAULT_MODEL,
    OpenRouterProvider,
)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)
    # The credential resolver falls through env → key_command → keychain;
    # in dev environments where conductor's keychain entry exists, deleting
    # only the env var still resolves the key. Force the resolver to return
    # None for these unconfigured-path tests.
    from conductor import credentials as _credentials
    _orig_get = _credentials.get
    monkeypatch.setattr(
        _credentials,
        "get",
        lambda key: None if key == OPENROUTER_API_KEY_ENV else _orig_get(key),
    )


def test_configured_true_when_env_set(configured):
    ok, reason = OpenRouterProvider().configured()
    assert ok is True
    assert reason is None


def test_configured_false_when_key_missing(no_key):
    ok, reason = OpenRouterProvider().configured()
    assert ok is False
    assert OPENROUTER_API_KEY_ENV in reason


def test_call_returns_normalized_response(configured):
    body = {
        "id": "chatcmpl-abc",
        "model": "anthropic/claude-sonnet-4",
        "choices": [
            {
                "message": {"role": "assistant", "content": "4"},
                "finish_reason": "stop",
            },
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )
        response = OpenRouterProvider().call(
            "What is 2+2?",
            model="anthropic/claude-sonnet-4",
        )

    assert isinstance(response, CallResponse)
    assert response.text == "4"
    assert response.provider == "openrouter"
    assert response.model == "anthropic/claude-sonnet-4"
    assert response.usage["input_tokens"] == 7
    assert response.usage["output_tokens"] == 1
    assert response.usage["cached_tokens"] == 0
    assert response.usage["thinking_tokens"] == 3
    assert response.usage["effort"] == "medium"
    assert response.usage["thinking_budget"] == 8_000
    assert response.duration_ms >= 0
    assert response.raw == body


def test_call_raises_config_error_when_unconfigured(no_key):
    with pytest.raises(ProviderConfigError):
        OpenRouterProvider().call("hello")


def test_smoke_returns_true_on_well_formed_response(configured):
    body = {
        "model": OPENROUTER_DEFAULT_MODEL,
        "choices": [{"message": {"content": "pong"}}],
        "usage": {},
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )
        ok, reason = OpenRouterProvider().smoke()
    assert ok is True
    assert reason is None


def test_call_sends_reasoning_effort_and_openrouter_headers(configured):
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        captured["http_referer"] = request.headers.get("http-referer")
        captured["x_title"] = request.headers.get("x-title")
        return httpx.Response(
            200,
            json={
                "model": OPENROUTER_DEFAULT_MODEL,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        OpenRouterProvider().call("hi", model="anthropic/claude-sonnet-4", effort="max")

    assert captured["authorization"] == "Bearer or-test-key"
    assert captured["http_referer"] == "https://github.com/autumngarage/conductor"
    assert captured["x_title"] == "conductor"
    assert captured["payload"] == {
        "model": "anthropic/claude-sonnet-4",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "xhigh"},
    }


def test_call_without_model_invokes_selector_and_builds_payload(configured, mocker):
    selector = mocker.patch(
        "conductor.providers.openrouter.select_model_for_task",
        return_value={
            "model": OPENROUTER_DEFAULT_MODEL,
            "plugins": [
                {
                    "id": "auto-router",
                    "allowed_models": ["google/gemini-flash-1.5", "openai/gpt-5.2"],
                }
            ],
            "reasoning": {"effort": "medium"},
        },
    )
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "google/gemini-flash-1.5",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().call(
            "hi",
            task_tags=["cheap"],
            prefer="balanced",
        )

    selector.assert_called_once_with(
        task_tags=["cheap"],
        prefer="balanced",
        effort="medium",
        exclude=None,
    )
    assert captured["payload"] == {
        "model": OPENROUTER_DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "plugins": [
            {
                "id": "auto-router",
                "allowed_models": ["google/gemini-flash-1.5", "openai/gpt-5.2"],
            }
        ],
        "reasoning": {"effort": "medium"},
    }
    assert response.model == "google/gemini-flash-1.5"


def test_exec_with_tools_raises_unsupported(configured):
    with pytest.raises(UnsupportedCapability):
        OpenRouterProvider().exec("hi", tools=frozenset({"Read"}))
