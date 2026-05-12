"""Unit tests for the OpenRouter provider — mocked httpx, no live calls."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest
import respx

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.openrouter_model_stacks import OPENROUTER_CODING_HIGH
from conductor.providers.deepseek import DeepSeekChatProvider, DeepSeekReasonerProvider
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderExecutionError,
    ProviderHTTPError,
    UnsupportedCapability,
)
from conductor.providers.kimi import KimiProvider
from conductor.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_MAX_TOOL_ITERATIONS,
    OPENROUTER_MODELS_ARRAY_MAX,
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


def _init_clean_git_repo(path):
    env = {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, env=env, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, env=env, check=True)


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
            "cost": 0.00123,
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
    assert response.cost_usd == pytest.approx(0.00123)
    assert response.duration_ms >= 0
    assert response.raw == body


def test_call_empty_response_raises_provider_error(configured):
    body = {
        "model": OPENROUTER_DEFAULT_MODEL,
        "choices": [{"message": {"content": ""}}],
        "usage": {},
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )
        with pytest.raises(ProviderHTTPError, match="empty response content"):
            OpenRouterProvider().call("Review this.", model=OPENROUTER_DEFAULT_MODEL)


def test_call_empty_response_retries_remaining_models(configured):
    requests: list[dict] = []
    responses = [
        {
            "model": "model-a",
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        },
        {
            "model": "model-b",
            "choices": [{"message": {"content": "usable"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().call(
            "Review this.",
            models=("model-a", "model-b"),
            log_selection=False,
        )

    assert response.text == "usable"
    assert requests[0]["models"] == ["model-a", "model-b"]
    assert requests[1]["models"] == ["model-b"]
    assert response.raw["empty_response_retries"] == [
        {"reason": "empty-response", "model": "model-a"}
    ]


def test_call_none_response_retries_remaining_models(configured):
    requests: list[dict] = []
    responses = [
        {
            "model": "model-a",
            "choices": [
                {"message": {"content": None}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        },
        {
            "model": "model-b",
            "choices": [{"message": {"content": "usable"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().call(
            "Review this.",
            models=("model-a", "model-b"),
            log_selection=False,
        )

    assert response.text == "usable"
    assert requests[0]["models"] == ["model-a", "model-b"]
    assert requests[1]["models"] == ["model-b"]
    assert response.raw["empty_response_retries"] == [
        {"reason": "empty-response", "model": "model-a"}
    ]


def test_call_none_response_without_fallback_includes_response_shape(configured):
    body = {
        "model": "model-a",
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0},
    }
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OpenRouterProvider().call("Review this.", model="model-a")

    message = str(exc.value)
    assert "empty response content" in message
    assert "model=model-a" in message
    assert "content_type=NoneType" in message
    assert "finish_reason=stop" in message


@pytest.mark.parametrize(
    ("status_code", "body", "expected_reason"),
    [
        (429, "rate limit exceeded", "auth_quota"),
        (400, "invalid model request", "usage_config_error"),
        (503, "service unavailable", "provider_outage"),
    ],
)
def test_call_http_error_exposes_failure_taxonomy(
    configured,
    status_code,
    body,
    expected_reason,
):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(status_code, text=body)
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OpenRouterProvider().call("Review this.", model="model-a")

    error = exc.value
    assert error.provider == "openrouter"
    assert error.status_code == status_code
    assert error.upstream_body == body
    assert error.failure_reason == expected_reason
    assert f"upstream HTTP {status_code}" in str(error)


def test_call_malformed_json_exposes_failure_taxonomy(configured):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, text="not json")
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OpenRouterProvider().call("Review this.", model="model-a")

    assert exc.value.provider == "openrouter"
    assert exc.value.failure_reason == "malformed_response"
    assert "not JSON" in str(exc.value)


def test_call_raises_config_error_when_unconfigured(no_key):
    with pytest.raises(ProviderConfigError):
        OpenRouterProvider().call("hello")


def test_smoke_returns_true_on_well_formed_response(configured):
    body = {
        "model": OPENROUTER_DEFAULT_MODEL,
        "choices": [{"message": {"content": "pong"}}],
        "usage": {},
    }
    with respx.mock(
        base_url="https://openrouter.ai/api/v1",
        assert_all_called=False,
    ) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )
        ok, reason = OpenRouterProvider().smoke()
    assert ok is True
    assert reason is None


@pytest.mark.parametrize(
    "provider_cls,model_url",
    [
        (OpenRouterProvider, "https://openrouter.ai/api/v1/models"),
        (KimiProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekChatProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekReasonerProvider, "https://openrouter.ai/api/v1/models"),
    ],
)
def test_openrouter_family_health_probe_success(configured, provider_cls, model_url):
    with respx.mock() as router:
        router.get(model_url).mock(return_value=httpx.Response(200, json={"data": []}))
        ok, reason = provider_cls().health_probe()
    assert ok is True and reason is None


@pytest.mark.parametrize(
    "provider_cls,model_url",
    [
        (OpenRouterProvider, "https://openrouter.ai/api/v1/models"),
        (KimiProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekChatProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekReasonerProvider, "https://openrouter.ai/api/v1/models"),
    ],
)
def test_openrouter_family_health_probe_timeout(configured, provider_cls, model_url):
    with respx.mock() as router:
        router.get(model_url).mock(
            side_effect=httpx.ReadTimeout("timed out", request=httpx.Request("GET", model_url))
        )
        ok, reason = provider_cls().health_probe(timeout_sec=9)
    assert ok is False
    assert "timed out" in reason


@pytest.mark.parametrize(
    "provider_cls,model_url",
    [
        (OpenRouterProvider, "https://openrouter.ai/api/v1/models"),
        (KimiProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekChatProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekReasonerProvider, "https://openrouter.ai/api/v1/models"),
    ],
)
def test_openrouter_family_health_probe_4xx(configured, provider_cls, model_url):
    with respx.mock() as router:
        router.get(model_url).mock(return_value=httpx.Response(401, text="bad key"))
        ok, reason = provider_cls().health_probe()
    assert ok is False
    assert "HTTP 401" in reason


@pytest.mark.parametrize(
    "provider_cls,model_url",
    [
        (OpenRouterProvider, "https://openrouter.ai/api/v1/models"),
        (KimiProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekChatProvider, "https://openrouter.ai/api/v1/models"),
        (DeepSeekReasonerProvider, "https://openrouter.ai/api/v1/models"),
    ],
)
def test_openrouter_family_health_probe_network_error(
    configured, provider_cls, model_url
):
    with respx.mock() as router:
        router.get(model_url).mock(side_effect=httpx.ConnectError("refused"))
        ok, reason = provider_cls().health_probe()
    assert ok is False
    assert "network error" in reason


def test_call_recovers_from_missing_default_ca_bundle(configured, mocker):
    original_client = httpx.Client
    client_kwargs: list[dict[str, object]] = []

    def client_factory(*args, **kwargs):
        client_kwargs.append(dict(kwargs))
        if len(client_kwargs) == 1:
            raise FileNotFoundError(
                2,
                "No such file or directory",
                "/missing/certifi/cacert.pem",
            )
        return original_client(*args, **kwargs)

    mocker.patch("conductor.providers._http_client.httpx.Client", side_effect=client_factory)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": OPENROUTER_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )
        )
        response = OpenRouterProvider().call("hi", model=OPENROUTER_DEFAULT_MODEL)

    assert response.text == "ok"
    assert len(client_kwargs) == 2
    assert "verify" not in client_kwargs[0]
    assert "verify" in client_kwargs[1]


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

    with respx.mock(
        base_url="https://openrouter.ai/api/v1",
        assert_all_called=False,
    ) as router:
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


def test_call_sends_ordered_models_stack(configured):
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "google/gemini-flash-latest",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        OpenRouterProvider().call(
            "hi",
            models=(
                "google/gemini-flash-latest",
                "moonshotai/kimi-k2.6",
                "openai/gpt-5.5",
                "anthropic/claude-sonnet-4.6",
            ),
            effort="low",
        )

    assert captured["payload"] == {
        "models": [
            "google/gemini-flash-latest",
            "moonshotai/kimi-k2.6",
            "openai/gpt-5.5",
        ],
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "low"},
    }
    assert len(captured["payload"]["models"]) <= OPENROUTER_MODELS_ARRAY_MAX


def test_call_rejects_model_and_models_together(configured):
    with pytest.raises(UnsupportedCapability, match="both `model` and `models`"):
        OpenRouterProvider().call("hi", model="openai/gpt-5.5", models=("x/y",))


def test_call_without_model_invokes_selector_and_builds_payload(configured, mocker):
    selector = mocker.patch(
        "conductor.providers.openrouter.select_model_for_task",
        return_value={
            "model": OPENROUTER_DEFAULT_MODEL,
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
        "reasoning": {"effort": "medium"},
    }
    assert response.model == "google/gemini-flash-1.5"


def test_auto_router_restriction_404_gets_actionable_local_error(configured, mocker):
    mocker.patch(
        "conductor.providers.openrouter.select_model_for_task",
        return_value={
            "model": OPENROUTER_DEFAULT_MODEL,
            "plugins": [
                {
                    "id": "auto-router",
                    "allowed_models": ["qwen/qwen3.6-flash"],
                }
            ],
            "reasoning": {"effort": "medium"},
        },
    )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "message": "No models match your request and model restrictions",
                        "code": 404,
                    }
                },
            )
        )
        with pytest.raises(ProviderError) as excinfo:
            OpenRouterProvider().call("hi")

    message = str(excinfo.value)
    assert "OpenRouter provider failed locally after upstream HTTP 404" in message
    assert "request restrictions/models tried" in message
    assert "qwen/qwen3.6-flash" in message
    assert "do not derive plugins[].allowed_models from `GET /models`" in message
    assert "conductor call --with openrouter --model <model-id>" in message


def test_call_fails_locally_when_validated_shortlist_is_empty(configured, mocker):
    alias_only_catalog = [
        openrouter_catalog.ModelEntry(
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
    ]
    mocker.patch(
        "conductor.providers.openrouter_catalog.load_catalog",
        return_value=alias_only_catalog,
    )

    with respx.mock(
        base_url="https://openrouter.ai/api/v1",
        assert_all_called=False,
    ) as router:
        chat_route = router.post("/chat/completions").mock(
            return_value=httpx.Response(500, text="should not be called")
        )
        with pytest.raises(ProviderError) as excinfo:
            OpenRouterProvider().call(
                "hi",
                task_tags=["long-context", "thinking"],
                prefer="cheapest",
            )

    message = str(excinfo.value)
    assert "found no sendable models after catalog validation" in message
    assert "tags filtered to empty: ['long-context', 'thinking']" in message
    assert "configured provider: openrouter" in message
    assert "dropped invalid aliases/stale slugs" in message
    assert chat_route.call_count == 0


def test_exec_with_tools_runs_openai_tool_loop(configured, tmp_path):
    (tmp_path / "note.txt").write_text("tool loop works", encoding="utf-8")
    requests: list[dict] = []
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_read",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": "note.txt"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The file says: tool loop works",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "cost": 0.002},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Read note.txt and summarize it.",
            model="openai/gpt-5.5",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )

    assert response.text == "The file says: tool loop works"
    assert response.model == "openai/gpt-5.5"
    assert response.usage["input_tokens"] == 30
    assert response.usage["output_tokens"] == 9
    assert response.usage["tool_iterations"] == 2
    assert response.usage["iterations"][0]["cost_usd"] == pytest.approx(0.001)
    assert response.usage["iterations"][1]["cost_usd"] == pytest.approx(0.002)
    assert response.cost_usd == pytest.approx(0.003)
    assert requests[0]["tools"][0]["function"]["name"] == "Read"
    assert requests[1]["messages"][1]["tool_calls"][0]["id"] == "call_read"
    assert requests[1]["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_read",
        "name": "Read",
        "content": "tool loop works",
    }


def test_exec_without_tools_passes_timeout_to_call(configured, mocker):
    provider = OpenRouterProvider()
    call = mocker.patch.object(
        provider,
        "call",
        return_value=CallResponse(
            text="ok",
            provider="openrouter",
            model=OPENROUTER_DEFAULT_MODEL,
            duration_ms=1,
            usage={},
            raw={},
        ),
    )

    response = provider.exec(
        "Summarize this.",
        model=OPENROUTER_DEFAULT_MODEL,
        timeout_sec=7,
        max_stall_sec=3,
    )

    assert response.text == "ok"
    assert call.call_args.kwargs["timeout_sec"] == 7
    assert call.call_args.kwargs["max_stall_sec"] == 3


def test_exec_with_tools_passes_remaining_timeout(configured, tmp_path, mocker):
    provider = OpenRouterProvider()
    observed_timeouts: list[float | None] = []

    def _post_chat(payload: dict, *, timeout_sec: float | None = None) -> dict:
        observed_timeouts.append(timeout_sec)
        return {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    mocker.patch.object(provider, "_post_chat", side_effect=_post_chat)

    response = provider.exec(
        "Read and summarize.",
        model="openai/gpt-5.5",
        tools=frozenset({"Read"}),
        cwd=str(tmp_path),
        timeout_sec=60,
    )

    assert response.text == "done"
    assert len(observed_timeouts) == 1
    assert observed_timeouts[0] is not None
    assert 0 < observed_timeouts[0] <= 60


def test_exec_with_tools_empty_final_response_raises(configured, tmp_path):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "openai/gpt-5.5",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 0},
                },
            )
        )
        with pytest.raises(ProviderHTTPError, match="empty final response"):
            OpenRouterProvider().exec(
                "Review this.",
                model="openai/gpt-5.5",
                tools=frozenset({"Read"}),
                cwd=str(tmp_path),
            )


def test_exec_with_tools_empty_final_response_retries_remaining_models(
    configured, tmp_path
):
    requests: list[dict] = []
    responses = [
        {
            "model": "model-a",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        },
        {
            "model": "model-b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "final verdict",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Review this.",
            models=("model-a", "model-b"),
            tools=frozenset({"Read"}),
            cwd=str(tmp_path),
            log_selection=False,
        )

    assert response.text == "final verdict"
    assert requests[0]["models"] == ["model-a", "model-b"]
    assert requests[1]["models"] == ["model-b"]
    assert response.usage["empty_response_retries"] == [
        {"iteration": 1, "reason": "empty-response", "model": "model-a"}
    ]


def test_exec_with_tools_uses_curated_coding_stack_by_default(configured, tmp_path):
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": OPENROUTER_CODING_HIGH[0],
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "implementation complete",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Implement the change.",
            tools=frozenset({"Read", "Edit", "Write"}),
            sandbox="none",
            cwd=str(tmp_path),
        )

    assert response.model == OPENROUTER_CODING_HIGH[0]
    assert captured["payload"]["models"] == list(
        OPENROUTER_CODING_HIGH[:OPENROUTER_MODELS_ARRAY_MAX]
    )
    assert len(captured["payload"]["models"]) <= OPENROUTER_MODELS_ARRAY_MAX
    assert "model" not in captured["payload"]
    assert "openrouter/auto" not in captured["payload"]["models"]
    assert "google/gemini-2.5-flash-lite" not in captured["payload"]["models"]


def test_exec_with_tools_and_balanced_prefer_uses_curated_coding_stack(
    configured,
    tmp_path,
):
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": OPENROUTER_CODING_HIGH[0],
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "implementation complete",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Implement the change.",
            prefer="balanced",
            tools=frozenset({"Read", "Grep", "Edit", "Write", "Bash"}),
            sandbox="workspace-write",
            cwd=str(tmp_path),
        )

    assert response.model == OPENROUTER_CODING_HIGH[0]
    assert captured["payload"]["models"] == list(
        OPENROUTER_CODING_HIGH[:OPENROUTER_MODELS_ARRAY_MAX]
    )
    assert "model" not in captured["payload"]


def test_openrouter_models_array_cap_error_is_clear(configured):
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": "'models' array must have 3 items or fewer.",
                        "code": 400,
                    }
                },
            )
        )
        with pytest.raises(ProviderHTTPError) as excinfo:
            OpenRouterProvider().call(
                "hi",
                models=(
                    "openai/gpt-5.3-codex",
                    "openai/gpt-5.5",
                    "anthropic/claude-sonnet-4.6",
                    "google/gemini-3.1-pro-preview",
                ),
            )

    message = str(excinfo.value)
    assert "OpenRouter provider failed locally after upstream HTTP 400" in message
    assert "'models' array must have 3 items or fewer" in message
    assert "request restrictions/models tried" in message
    assert "openai/gpt-5.3-codex" in message


def test_exec_code_task_no_tool_calls_raises_noop_status(configured, tmp_path):
    _init_clean_git_repo(tmp_path)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "openai/gpt-5.5",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I will make a plan first.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )
        )
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement the change.",
                model="openai/gpt-5.5",
                tools=frozenset({"Read", "Edit", "Write", "Bash"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
            )

    status = exc.value.status
    assert status["state"] == "no-op"
    assert status["tool_calls"] == 0
    assert status["successful_write_tools"] == 0
    assert status["git_status_after"]["clean"] is True


def test_exec_code_task_invalid_write_args_raise_tool_error_status(
    configured, tmp_path
):
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps({"content": "changed"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I changed the files and tests passed.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 6},
        },
    ]
    requests: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement the change.",
                model="openai/gpt-5.5",
                tools=frozenset({"Write"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
            )

    status = exc.value.status
    assert status["state"] == "tool-error"
    assert status["successful_write_tools"] == 0
    assert status["tool_errors"][0]["name"] == "Write"
    assert "bad parameters" in status["tool_errors"][0]["error"]
    assert requests[1]["messages"][2]["content"].startswith("error:")


def test_exec_code_task_failed_validation_bash_raises_status(
    configured, tmp_path
):
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "path": "test_failure.py",
                                            "content": (
                                                "def test_failure():\n"
                                                "    assert False\n"
                                            ),
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_pytest",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": json.dumps({"command": "pytest -q"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Implementation complete; tests passed.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 6},
        },
    ]
    requests: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement the change and run tests.",
                model="openai/gpt-5.5",
                tools=frozenset({"Write", "Bash"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
            )

    status = exc.value.status
    assert status["state"] == "validation-failed"
    assert status["successful_write_tools"] == 1
    assert status["validation_failures"][0]["command"] == "pytest -q"
    assert status["validation_failures"][0]["exit_code"] != 0


def test_exec_code_task_iteration_cap_raises_status(configured, tmp_path):
    (tmp_path / "note.txt").write_text("still looping", encoding="utf-8")
    requests: list[dict] = []
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_read_{idx}",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": "note.txt"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        for idx in range(OPENROUTER_MAX_TOOL_ITERATIONS)
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement the change.",
                model="openai/gpt-5.5",
                tools=frozenset({"Read", "Edit"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
                max_iterations=4,
            )

    status = exc.value.status
    assert status["state"] == "iteration-cap"
    assert status["hit_iteration_cap"] is True
    assert status["iteration_cap"] == 4
    assert status["tool_calls"] == 4
    assert len(requests) == 4
    assert "Reached --max-iterations cap (4)" in str(exc.value)


def test_exec_recovers_once_from_tool_call_leak_rejection(configured, tmp_path):
    _init_clean_git_repo(tmp_path)
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_leak",
                                "type": "function",
                                "function": {
                                    "name": "Edit",
                                    "arguments": json.dumps(
                                        {
                                            "path": "README.md",
                                            "old_string": "base",
                                            "new_string": (
                                                "assistant to=functions.Edit fixed"
                                            ),
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_clean",
                                "type": "function",
                                "function": {
                                    "name": "Edit",
                                    "arguments": json.dumps(
                                        {
                                            "path": "README.md",
                                            "old_string": "base",
                                            "new_string": "fixed",
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1},
        },
    ]
    requests: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Implement the change.",
            model="openai/gpt-5.5",
            tools=frozenset({"Read", "Edit"}),
            task_tags=("code", "tool-use"),
            sandbox="none",
            cwd=str(tmp_path),
            max_iterations=2,
        )

    assert response.text == "done"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "fixed\n"
    assert response.usage["tool_error_count"] == 1
    assert response.usage["write_success_count"] == 1
    assert response.usage["hit_iteration_cap"] is False
    assert len(requests) == 3
    assert any(
        "tool-call transcript markup" in message.get("content", "")
        for message in requests[1]["messages"]
        if message.get("role") == "user"
    )


def test_exec_classifies_repeated_tool_call_leak(configured, tmp_path):
    _init_clean_git_repo(tmp_path)
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_leak_{idx}",
                                "type": "function",
                                "function": {
                                    "name": "Edit",
                                    "arguments": json.dumps(
                                        {
                                            "path": "README.md",
                                            "old_string": "base",
                                            "new_string": (
                                                "assistant to=functions.Edit fixed"
                                            ),
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        for idx in range(3)
    ]
    requests: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement the change.",
                model="openai/gpt-5.5",
                tools=frozenset({"Read", "Edit"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
                max_iterations=2,
            )

    status = exc.value.status
    assert status["state"] == "tool-call-leak"
    assert status["hit_iteration_cap"] is True
    assert status["iteration_cap"] == 2
    assert status["successful_write_tools"] == 0
    assert len(status["tool_errors"]) == 3
    assert len(requests) == 3
    assert "tool-call leak rejected" in str(exc.value)
    assert "Reached --max-iterations cap" not in str(exc.value)
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "base\n"


def test_exec_iteration_cap_reports_missing_tests(configured, tmp_path):
    _init_clean_git_repo(tmp_path)
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {"path": "app.py", "content": "value = 1\n"}
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
    ]

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=responses[0])
        )
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Implement it.\n\n## Tests\nAdd tests.",
                model="openai/gpt-5.5",
                tools=frozenset({"Write"}),
                task_tags=("code", "tool-use"),
                sandbox="none",
                cwd=str(tmp_path),
                max_iterations=1,
            )

    status = exc.value.status
    assert status["state"] == "iteration-cap"
    assert status["missing_deliverables"] == [
        {
            "kind": "tests",
            "message": "Tests requested in brief; diff did not add to tests/.",
        }
    ]
    assert "Detected unfinished items" in str(exc.value)


def test_exec_iteration_cap_does_not_require_tests_for_read_only_recommendations(
    configured, tmp_path
):
    _init_clean_git_repo(tmp_path)
    response = {
        "model": "openai/gpt-5.5",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "pyproject.toml"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=response)
        )
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                (
                    "Read-only investigation. Do not edit files.\n\n"
                    "Expected output:\n- Root cause\n"
                    "- Regression tests to add/update"
                ),
                model="openai/gpt-5.5",
                tools=frozenset({"Read"}),
                task_tags=("code", "tool-use"),
                sandbox="read-only",
                cwd=str(tmp_path),
                max_iterations=1,
            )

    status = exc.value.status
    assert status["state"] == "iteration-cap"
    assert status["missing_deliverables"] == []
    assert "diff did not add to tests/" not in str(exc.value)


def test_exec_allow_completion_stretch_runs_one_extra_turn(configured, tmp_path):
    _init_clean_git_repo(tmp_path)
    requests: list[dict] = []
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_write",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {"path": "app.py", "content": "value = 1\n"}
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I cannot add tests in the remaining turn.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 6},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            "Implement it.\n\n## Tests\nAdd tests.",
            model="openai/gpt-5.5",
            tools=frozenset({"Write"}),
            task_tags=("code", "tool-use"),
            sandbox="none",
            cwd=str(tmp_path),
            max_iterations=1,
            allow_completion_stretch=True,
        )

    assert len(requests) == 2
    assert response.usage["completion_stretched"] is True
    assert response.usage["hit_iteration_cap"] is False
    assert requests[1]["messages"][-1] == {
        "role": "user",
        "content": (
            "You're at the iteration cap. Detected unfinished: tests. "
            "Spend this final turn finishing them or surfacing why you can't."
        ),
    }


def test_exec_review_iteration_cap_gets_terminal_answer_turn(configured, tmp_path):
    (tmp_path / "note.txt").write_text("review target\n", encoding="utf-8")
    requests: list[dict] = []
    responses = [
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_read",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": "note.txt"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "model": "openai/gpt-5.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "No findings.\nCODEX_REVIEW_CLEAN",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 6},
        },
    ]

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = OpenRouterProvider().exec(
            (
                "Review note.txt. The final line must be exactly "
                "CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, or CODEX_REVIEW_BLOCKED."
            ),
            model="openai/gpt-5.5",
            tools=frozenset({"Read"}),
            task_tags=("code-review", "tool-use"),
            sandbox="none",
            cwd=str(tmp_path),
            max_iterations=1,
        )

    assert len(requests) == 2
    assert response.text == "No findings.\nCODEX_REVIEW_CLEAN"
    assert response.usage["terminal_answer_stretched"] is True
    assert response.usage["hit_iteration_cap"] is False
    assert requests[1]["messages"][-1]["role"] == "user"
    assert "Stop calling tools and give the final review answer now" in (
        requests[1]["messages"][-1]["content"]
    )


def test_exec_allow_completion_stretch_without_missing_keeps_cap(configured, tmp_path):
    (tmp_path / "note.txt").write_text("still looping", encoding="utf-8")
    requests: list[dict] = []
    response_body = {
        "model": "openai/gpt-5.5",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "note.txt"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body)

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        with pytest.raises(ProviderExecutionError) as exc:
            OpenRouterProvider().exec(
                "Read note.txt.",
                model="openai/gpt-5.5",
                tools=frozenset({"Read"}),
                sandbox="none",
                cwd=str(tmp_path),
                max_iterations=1,
                allow_completion_stretch=True,
            )

    assert len(requests) == 1
    status = exc.value.status
    assert status["hit_iteration_cap"] is True
    assert status["missing_deliverables"] == []


def test_exec_with_tools_rejects_model_and_models_together(configured):
    with pytest.raises(UnsupportedCapability, match="both `model` and `models`"):
        OpenRouterProvider().exec(
            "hi",
            model="openai/gpt-5.5",
            models=("x/y",),
            tools=frozenset({"Read"}),
            sandbox="read-only",
        )
