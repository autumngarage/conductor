"""Unit tests for the Kimi provider — mocked httpx, no live calls."""

from __future__ import annotations

import json

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


# --------------------------------------------------------------------------- #
# exec() — tool-use loop (v0.3.0 / Slice A)
# --------------------------------------------------------------------------- #


def _terminal(content: str, model: str = KIMI_DEFAULT_MODEL) -> dict:
    """Build a Cloudflare/OpenAI-style chat response with no tool_calls."""
    return {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


def _tool_turn(name: str, arguments: str, call_id: str = "call_0") -> dict:
    """Build a chat response that asks the model to invoke a tool."""
    return {
        "model": KIMI_DEFAULT_MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }


def test_exec_without_tools_behaves_like_call(configured):
    body = _terminal("hello back")
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        resp = KimiProvider().exec("hi")
    assert resp.text == "hello back"
    assert "tool_iterations" not in resp.usage  # untouched call() path


def test_exec_without_tools_rejects_non_none_sandbox(configured):
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().exec("hi", sandbox="read-only")
    assert "without tools" in str(exc.value)


def test_exec_with_tools_needs_at_least_read_only(configured):
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().exec(
            "hi", tools=frozenset({"Read"}), sandbox="none"
        )
    assert "read-only" in str(exc.value)


def test_exec_rejects_unsupported_tool_set(configured):
    # Hypothetical future tool outside kimi's declared set.
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().exec(
            "hi", tools=frozenset({"Telepathy"}), sandbox="read-only"
        )
    assert "does not support" in str(exc.value)


def test_exec_rejects_unsupported_sandbox(configured):
    # `strict` is Slice C (subprocess) — not in kimi's declared sandboxes yet.
    with pytest.raises(UnsupportedCapability) as exc:
        KimiProvider().exec(
            "hi", tools=frozenset({"Read"}), sandbox="strict"
        )
    assert "does not support" in str(exc.value)


def test_exec_runs_single_tool_call_then_answers(configured, tmp_path):
    (tmp_path / "note.txt").write_text("the answer is 42")
    tool_turn = _tool_turn("Read", '{"path": "note.txt"}', call_id="call_a")
    final = _terminal("The file says 42.")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "read note.txt",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )

    assert resp.text == "The file says 42."
    assert resp.usage["tool_iterations"] == 2
    assert resp.usage["hit_iteration_cap"] is False
    assert resp.usage["tool_names"] == ["Read"]
    # Two HTTP calls total (tool turn + final answer).
    assert route.call_count == 2
    # Final request should have the tool result fed back in.
    final_request = route.calls[-1].request
    payload = json.loads(final_request.read())
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert payload["messages"][2]["content"] == "the answer is 42"
    assert payload["messages"][2]["tool_call_id"] == "call_a"


def test_exec_feeds_tool_error_back_to_model(configured, tmp_path):
    # First turn asks for a path that escapes cwd — ToolExecutor refuses;
    # we feed that error back as the tool response and the model answers.
    tool_turn = _tool_turn("Read", '{"path": "../../../etc/passwd"}')
    final = _terminal("I can't read that, so here's a summary from memory.")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "summarize passwd",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )

    # Loop ran to completion without raising; error was fed back.
    assert resp.text.startswith("I can't read that")
    payload = json.loads(route.calls[-1].request.read())
    tool_msg = [m for m in payload["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("error:")
    assert "escapes" in tool_msg["content"]


def test_exec_handles_invalid_json_args(configured, tmp_path):
    tool_turn = _tool_turn("Read", "not-json{{")
    final = _terminal("had trouble parsing args; stopping here")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "go",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert "had trouble" in resp.text
    payload = json.loads(route.calls[-1].request.read())
    tool_msg = [m for m in payload["messages"] if m["role"] == "tool"][0]
    assert "not valid JSON" in tool_msg["content"]


def test_exec_hits_iteration_cap_on_runaway_model(configured, tmp_path):
    # Model keeps requesting the same tool forever; circuit breaker fires.
    runaway = _tool_turn("Read", '{"path": "note.txt"}')
    (tmp_path / "note.txt").write_text("x")
    # Return the same tool_turn to every POST; the loop must bail at 10.
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(200, json=runaway)
        )
        resp = KimiProvider().exec(
            "never-ending",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.usage["hit_iteration_cap"] is True
    assert resp.usage["tool_iterations"] == 10
    assert "max iterations" in resp.text.lower()
    assert route.call_count == 10


def test_exec_accumulates_tokens_across_iterations(configured, tmp_path):
    (tmp_path / "a.txt").write_text("one")
    turn1 = _tool_turn("Read", '{"path": "a.txt"}', call_id="call_1")
    turn1["usage"] = {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 1},
    }
    final = _terminal("done")
    final["usage"] = {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 5},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=turn1),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "go",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.usage["input_tokens"] == 30
    assert resp.usage["output_tokens"] == 8
    assert resp.usage["cached_tokens"] == 7
    assert resp.usage["thinking_tokens"] == 3


def test_exec_includes_tool_specs_in_first_request(configured, tmp_path):
    (tmp_path / "x.txt").write_text("")
    final = _terminal("direct answer")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            return_value=httpx.Response(200, json=final)
        )
        KimiProvider().exec(
            "go",
            tools=frozenset({"Read", "Grep"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    payload = json.loads(route.calls[0].request.read())
    assert "tools" in payload
    names = sorted(t["function"]["name"] for t in payload["tools"])
    assert names == ["Grep", "Read"]
    for spec in payload["tools"]:
        assert spec["type"] == "function"
        assert "parameters" in spec["function"]


def test_exec_echoes_reasoning_content_on_multi_turn(configured, tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    tool_turn = _tool_turn("Read", '{"path": "f.txt"}', call_id="call_r")
    tool_turn["choices"][0]["message"]["reasoning_content"] = (
        "thinking about reading f.txt"
    )
    final = _terminal("done")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        KimiProvider().exec(
            "go",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    payload = json.loads(route.calls[-1].request.read())
    assistant = [m for m in payload["messages"] if m["role"] == "assistant"][0]
    assert assistant.get("reasoning_content") == "thinking about reading f.txt"


def test_exec_with_multiple_tool_calls_in_one_response(configured, tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    multi = {
        "model": KIMI_DEFAULT_MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": '{"path": "a.txt"}',
                            },
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": '{"path": "b.txt"}',
                            },
                        },
                    ],
                }
            }
        ],
        "usage": {},
    }
    final = _terminal("A and B")
    with respx.mock() as router:
        route = router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=multi),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "read both",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.text == "A and B"
    payload = json.loads(route.calls[-1].request.read())
    tool_msgs = [m for m in payload["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    ids = sorted(m["tool_call_id"] for m in tool_msgs)
    assert ids == ["c1", "c2"]
    contents = sorted(m["content"] for m in tool_msgs)
    assert contents == ["A", "B"]


def test_exec_defaults_cwd_to_current_directory(configured, monkeypatch, tmp_path):
    # Without --cwd, the executor should resolve against the process cwd.
    (tmp_path / "here.txt").write_text("present")
    monkeypatch.chdir(tmp_path)
    tool_turn = _tool_turn("Read", '{"path": "here.txt"}')
    final = _terminal("present")
    with respx.mock() as router:
        router.post(CF_CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = KimiProvider().exec(
            "read here.txt",
            tools=frozenset({"Read"}),
            sandbox="read-only",
        )
    assert resp.text == "present"
