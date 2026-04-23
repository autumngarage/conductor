"""Unit tests for the Kimi provider — mocked httpx, no live calls."""

from __future__ import annotations

import httpx
import pytest
import respx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)
from conductor.providers.kimi import (
    CLOUDFLARE_ACCOUNT_ID_ENV,
    CLOUDFLARE_API_TOKEN_ENV,
    KIMI_DEFAULT_MODEL,
    KimiProvider,
)

TEST_ACCOUNT_ID = "acct-test-1234"
CF_CHAT_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{TEST_ACCOUNT_ID}"
    "/ai/v1/chat/completions"
)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, TEST_ACCOUNT_ID)


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(CLOUDFLARE_ACCOUNT_ID_ENV, TEST_ACCOUNT_ID)


@pytest.fixture
def no_account(monkeypatch):
    monkeypatch.setenv(CLOUDFLARE_API_TOKEN_ENV, "cf-test-token")
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)


@pytest.fixture
def nothing_set(monkeypatch):
    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)


def test_configured_true_when_both_env_vars_set(configured):
    ok, reason = KimiProvider().configured()
    assert ok is True
    assert reason is None


def test_configured_false_when_token_missing(no_token):
    ok, reason = KimiProvider().configured()
    assert ok is False
    assert CLOUDFLARE_API_TOKEN_ENV in reason


def test_configured_false_when_account_missing(no_account):
    ok, reason = KimiProvider().configured()
    assert ok is False
    assert CLOUDFLARE_ACCOUNT_ID_ENV in reason


def test_call_returns_normalized_response(configured):
    body = {
        "id": "chatcmpl-abc",
        "model": KIMI_DEFAULT_MODEL,
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
        router.post(CF_CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = KimiProvider().call("What is 2+2?")

    assert isinstance(response, CallResponse)
    assert response.text == "4"
    assert response.provider == "kimi"
    assert response.model == KIMI_DEFAULT_MODEL
    assert response.usage["input_tokens"] == 7
    assert response.usage["output_tokens"] == 1
    assert response.usage["cached_tokens"] == 0
    assert response.usage["effort"] == "medium"
    assert response.usage["thinking_budget"] == 4_000
    assert response.duration_ms >= 0
    assert response.raw == body


def test_call_uses_default_model_when_none_passed(configured):
    captured = {}
    with respx.mock() as router:
        def _record(request):
            captured["payload"] = request.read()
            return httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )

        router.post(CF_CHAT_URL).mock(side_effect=_record)
        KimiProvider().call("hi")

    import json as _json

    assert _json.loads(captured["payload"])["model"] == KIMI_DEFAULT_MODEL


def test_call_respects_model_override(configured):
    captured = {}
    with respx.mock() as router:
        def _record(request):
            captured["payload"] = request.read()
            return httpx.Response(
                200,
                json={
                    "model": "@cf/moonshotai/kimi-k2.5",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )

        router.post(CF_CHAT_URL).mock(side_effect=_record)
        KimiProvider().call("hi", model="@cf/moonshotai/kimi-k2.5")

    import json as _json

    assert _json.loads(captured["payload"])["model"] == "@cf/moonshotai/kimi-k2.5"


def test_call_includes_bearer_auth_header(configured):
    seen = {}
    with respx.mock() as router:
        def _record(request):
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )

        router.post(CF_CHAT_URL).mock(side_effect=_record)
        KimiProvider().call("hi")

    assert seen["authorization"] == "Bearer cf-test-token"


def test_call_raises_provider_config_error_when_token_missing(no_token):
    with pytest.raises(ProviderConfigError) as exc:
        KimiProvider().call("hi")
    assert CLOUDFLARE_API_TOKEN_ENV in str(exc.value)


def test_call_raises_provider_config_error_when_account_missing(no_account):
    with pytest.raises(ProviderConfigError) as exc:
        KimiProvider().call("hi")
    assert CLOUDFLARE_ACCOUNT_ID_ENV in str(exc.value)


def test_call_raises_on_non_200(configured):
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(401, text="invalid token")
        )
        with pytest.raises(ProviderHTTPError) as exc:
            KimiProvider().call("hi")
    assert "401" in str(exc.value)


def test_call_raises_on_malformed_response(configured):
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        with pytest.raises(ProviderHTTPError) as exc:
            KimiProvider().call("hi")
    assert "missing" in str(exc.value).lower()


def test_smoke_passes_on_minimal_chat_completion(configured):
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "p"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
        ok, reason = KimiProvider().smoke()
    assert ok is True
    assert reason is None


def test_smoke_fails_on_unauthorized(configured):
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(return_value=httpx.Response(401, text="nope"))
        ok, reason = KimiProvider().smoke()
    assert ok is False
    assert "401" in reason


def test_call_with_resume_session_id_raises_unsupported(configured):
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().call("hi", resume_session_id="any-id")
    assert "stateless" in str(exc.value)


def test_exec_with_resume_session_id_raises_unsupported(configured):
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().exec("hi", resume_session_id="any-id")
    assert "stateless" in str(exc.value)


def test_call_session_id_is_none_for_kimi(configured):
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
        response = KimiProvider().call("hi")
    assert response.session_id is None


def test_smoke_fails_when_not_configured(nothing_set):
    ok, reason = KimiProvider().smoke()
    assert ok is False
    assert CLOUDFLARE_API_TOKEN_ENV in reason or CLOUDFLARE_ACCOUNT_ID_ENV in reason
