"""Unit tests for the Kimi provider — mocked httpx, no live calls."""

from __future__ import annotations

import httpx
import pytest
import respx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
)
from conductor.providers.kimi import (
    KIMI_API_KEY_ENV,
    KIMI_BASE_URL,
    KIMI_DEFAULT_MODEL,
    KimiProvider,
)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv(KIMI_API_KEY_ENV, "sk-test-key")


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.delenv(KIMI_API_KEY_ENV, raising=False)


def test_configured_true_when_env_var_set(with_key):
    ok, reason = KimiProvider().configured()
    assert ok is True
    assert reason is None


def test_configured_false_when_env_var_missing(without_key):
    ok, reason = KimiProvider().configured()
    assert ok is False
    assert KIMI_API_KEY_ENV in reason


def test_call_returns_normalized_response(with_key):
    body = {
        "id": "cmpl-abc",
        "model": "kimi-k2.6",
        "choices": [
            {"message": {"role": "assistant", "content": "4"}, "finish_reason": "stop"},
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.post("/chat/completions").mock(return_value=httpx.Response(200, json=body))
        response = KimiProvider().call("What is 2+2?")

    assert isinstance(response, CallResponse)
    assert response.text == "4"
    assert response.provider == "kimi"
    assert response.model == "kimi-k2.6"
    assert response.usage == {"input_tokens": 7, "output_tokens": 1, "cached_tokens": 0}
    assert response.duration_ms >= 0
    assert response.raw == body


def test_call_uses_default_model_when_none_passed(with_key):
    captured = {}
    with respx.mock(base_url=KIMI_BASE_URL) as router:
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

        router.post("/chat/completions").mock(side_effect=_record)
        KimiProvider().call("hi")

    import json as _json

    assert _json.loads(captured["payload"])["model"] == KIMI_DEFAULT_MODEL


def test_call_respects_model_override(with_key):
    captured = {}
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        def _record(request):
            captured["payload"] = request.read()
            return httpx.Response(
                200,
                json={
                    "model": "kimi-k2-thinking",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )

        router.post("/chat/completions").mock(side_effect=_record)
        KimiProvider().call("hi", model="kimi-k2-thinking")

    import json as _json

    assert _json.loads(captured["payload"])["model"] == "kimi-k2-thinking"


def test_call_raises_provider_config_error_when_key_missing(without_key):
    with pytest.raises(ProviderConfigError) as exc:
        KimiProvider().call("hi")
    assert KIMI_API_KEY_ENV in str(exc.value)


def test_call_raises_on_non_200(with_key):
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(401, text="invalid api key")
        )
        with pytest.raises(ProviderHTTPError) as exc:
            KimiProvider().call("hi")
    assert "401" in str(exc.value)


def test_call_raises_on_malformed_response(with_key):
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        with pytest.raises(ProviderHTTPError) as exc:
            KimiProvider().call("hi")
    assert "missing" in str(exc.value).lower()


def test_smoke_passes_on_200(with_key):
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.get("/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "kimi-k2.6"}]})
        )
        ok, reason = KimiProvider().smoke()
    assert ok is True
    assert reason is None


def test_smoke_fails_on_unauthorized(with_key):
    with respx.mock(base_url=KIMI_BASE_URL) as router:
        router.get("/models").mock(return_value=httpx.Response(401, text="nope"))
        ok, reason = KimiProvider().smoke()
    assert ok is False
    assert "401" in reason
