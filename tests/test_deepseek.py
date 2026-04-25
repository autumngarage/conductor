"""Unit tests for the DeepSeek providers — mocked httpx, no live calls."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from conductor.providers.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_REASONER_MODEL,
    DeepSeekChatProvider,
    DeepSeekReasonerProvider,
)
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)

CHAT_URL = f"{DEEPSEEK_BASE_URL}/chat/completions"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "ds-test-key")


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)


def test_chat_configured_true_when_env_set(configured):
    ok, reason = DeepSeekChatProvider().configured()
    assert ok is True
    assert reason is None


def test_reasoner_configured_true_when_env_set(configured):
    ok, reason = DeepSeekReasonerProvider().configured()
    assert ok is True
    assert reason is None


def test_chat_configured_false_when_key_missing(no_key):
    ok, reason = DeepSeekChatProvider().configured()
    assert ok is False
    assert DEEPSEEK_API_KEY_ENV in reason


def test_reasoner_configured_false_when_key_missing(no_key):
    ok, reason = DeepSeekReasonerProvider().configured()
    assert ok is False
    assert DEEPSEEK_API_KEY_ENV in reason


def test_chat_call_returns_normalized_response(configured):
    body = {
        "id": "chatcmpl-abc",
        "model": DEEPSEEK_CHAT_MODEL,
        "choices": [
            {"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"},
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = DeepSeekChatProvider().call("What is 2+2?")

    assert isinstance(response, CallResponse)
    assert response.text == "4"
    assert response.provider == "deepseek-chat"
    assert response.model == DEEPSEEK_CHAT_MODEL
    assert response.usage["input_tokens"] == 7
    assert response.usage["output_tokens"] == 1
    assert response.usage["cached_tokens"] == 0
    # chat doesn't support effort, so the dial resolves to 0.
    assert response.usage["thinking_budget"] == 0
    assert response.duration_ms >= 0
    assert response.raw == body


def test_reasoner_call_surfaces_reasoning_chars(configured):
    reasoning = "Let me think step by step..."
    body = {
        "model": DEEPSEEK_REASONER_MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "42",
                    "reasoning_content": reasoning,
                },
            },
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 13},
        },
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = DeepSeekReasonerProvider().call("hard question")

    assert response.text == "42"
    assert response.provider == "deepseek-reasoner"
    assert response.usage["thinking_tokens"] == 13
    assert response.usage["reasoning_chars"] == len(reasoning)
    # reasoner declares supports_effort, so the medium dial maps to a budget.
    assert response.usage["thinking_budget"] == 4_000


def test_call_uses_default_model_when_none_passed(configured):
    captured: dict[str, bytes] = {}

    def _record(request):
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "model": DEEPSEEK_CHAT_MODEL,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock() as router:
        router.post(CHAT_URL).mock(side_effect=_record)
        DeepSeekChatProvider().call("hi")

    payload = json.loads(captured["payload"])
    assert payload["model"] == DEEPSEEK_CHAT_MODEL


def test_call_raises_config_error_when_unconfigured(no_key):
    with pytest.raises(ProviderConfigError):
        DeepSeekChatProvider().call("hello")


def test_call_raises_http_error_on_non_200(configured):
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(401, text="bad key"))
        with pytest.raises(ProviderHTTPError) as exc:
            DeepSeekChatProvider().call("hi")
    assert "401" in str(exc.value)


def test_call_raises_http_error_on_network_failure(configured):
    with respx.mock() as router:
        router.post(CHAT_URL).mock(side_effect=httpx.ConnectError("dns failure"))
        with pytest.raises(ProviderHTTPError) as exc:
            DeepSeekChatProvider().call("hi")
    assert "network error" in str(exc.value).lower()


def test_smoke_returns_true_on_well_formed_response(configured):
    body = {
        "model": DEEPSEEK_CHAT_MODEL,
        "choices": [{"message": {"content": "pong"}}],
        "usage": {},
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        ok, reason = DeepSeekChatProvider().smoke()
    assert ok is True
    assert reason is None


def test_smoke_returns_false_when_unconfigured(no_key):
    ok, reason = DeepSeekChatProvider().smoke()
    assert ok is False
    assert DEEPSEEK_API_KEY_ENV in reason


def test_resume_session_id_unsupported(configured):
    with pytest.raises(UnsupportedCapability):
        DeepSeekChatProvider().call("hi", resume_session_id="abc")


def test_exec_without_tools_delegates_to_call(configured):
    body = {
        "model": DEEPSEEK_CHAT_MODEL,
        "choices": [{"message": {"content": "ok"}}],
        "usage": {},
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = DeepSeekChatProvider().exec("hi")
    assert response.text == "ok"


def test_exec_with_tools_raises_unsupported(configured):
    with pytest.raises(UnsupportedCapability):
        DeepSeekChatProvider().exec("hi", tools=frozenset({"Read"}))


def test_exec_with_sandbox_but_no_tools_raises(configured):
    with pytest.raises(UnsupportedCapability):
        DeepSeekChatProvider().exec("hi", sandbox="read-only")


def test_chat_and_reasoner_have_distinct_use_case_tags():
    """deepseek-chat targets cheap/general; deepseek-reasoner targets reasoning.
    The router uses these tags to pick the right model per task."""
    chat_tags = set(DeepSeekChatProvider().tags)
    reasoner_tags = set(DeepSeekReasonerProvider().tags)
    assert "cheap" in chat_tags
    assert "strong-reasoning" in reasoner_tags
    assert "thinking" in reasoner_tags
    # The two should not be identical — they exist to serve different routes.
    assert chat_tags != reasoner_tags


def test_authorization_header_uses_bearer_token(configured):
    captured: dict[str, str] = {}

    def _record(request):
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "model": DEEPSEEK_CHAT_MODEL,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock() as router:
        router.post(CHAT_URL).mock(side_effect=_record)
        DeepSeekChatProvider().call("hi")

    assert captured["auth"].startswith("Bearer ")
    assert "ds-test-key" in captured["auth"]
