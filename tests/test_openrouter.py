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
    UnsupportedCapability,
)
from conductor.providers.kimi import KimiProvider
from conductor.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_MAX_TOOL_ITERATIONS,
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
            models=("google/gemini-flash-latest", "moonshotai/kimi-k2.6"),
            effort="low",
        )

    assert captured["payload"] == {
        "models": ["google/gemini-flash-latest", "moonshotai/kimi-k2.6"],
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "low"},
    }


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
    assert captured["payload"]["models"] == list(OPENROUTER_CODING_HIGH)
    assert "model" not in captured["payload"]
    assert "openrouter/auto" not in captured["payload"]["models"]
    assert "google/gemini-2.5-flash-lite" not in captured["payload"]["models"]


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
            )

    status = exc.value.status
    assert status["state"] == "iteration-cap"
    assert status["hit_iteration_cap"] is True
    assert status["tool_calls"] == OPENROUTER_MAX_TOOL_ITERATIONS
    assert len(requests) == OPENROUTER_MAX_TOOL_ITERATIONS


def test_exec_with_tools_rejects_model_and_models_together(configured):
    with pytest.raises(UnsupportedCapability, match="both `model` and `models`"):
        OpenRouterProvider().exec(
            "hi",
            model="openai/gpt-5.5",
            models=("x/y",),
            tools=frozenset({"Read"}),
            sandbox="read-only",
        )
