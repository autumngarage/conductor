"""Tests for the Ollama provider — mocked httpx via respx."""

from __future__ import annotations

import json as _json

import httpx
import pytest
import respx

from conductor.providers.interface import (
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)
from conductor.providers.ollama import (
    OLLAMA_BASE_URL_ENV,
    OLLAMA_DEFAULT_BASE_URL,
    OLLAMA_DEFAULT_MODEL,
    OLLAMA_MODEL_ENV,
    OllamaProvider,
)

CHAT_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/chat"
TAGS_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/tags"


@pytest.fixture(autouse=True)
def _no_base_url_override(monkeypatch):
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(OLLAMA_MODEL_ENV, raising=False)


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
                json={
                    "models": [
                        {"name": OLLAMA_DEFAULT_MODEL},
                        {"name": "other:1b"},
                    ]
                },
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
    assert OLLAMA_DEFAULT_MODEL in reason
    assert "ollama pull" in reason
    assert "qwen2.5-coder:7b" in reason  # shows locally installed alternatives


def test_default_model_available_false_when_no_models_pulled():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, reason = OllamaProvider().default_model_available()
    assert ok is False
    assert f"ollama pull {OLLAMA_DEFAULT_MODEL}" in reason


def test_resolved_default_model_honors_env_override(monkeypatch):
    monkeypatch.setenv(OLLAMA_MODEL_ENV, "qwen2.5-coder:14b")

    assert OllamaProvider().resolved_default_model() == "qwen2.5-coder:14b"


def test_resolved_default_model_ignores_blank_env(monkeypatch):
    monkeypatch.setenv(OLLAMA_MODEL_ENV, "  ")

    assert OllamaProvider().resolved_default_model() == OLLAMA_DEFAULT_MODEL


def test_default_model_available_uses_env_override(monkeypatch):
    monkeypatch.setenv(OLLAMA_MODEL_ENV, "qwen2.5-coder:14b")

    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "qwen2.5-coder:14b"}]}
            )
        )
        ok, reason = OllamaProvider().default_model_available()

    assert ok is True and reason is None


def test_configured_honors_env_override(monkeypatch):
    monkeypatch.setenv(OLLAMA_BASE_URL_ENV, "http://ollama.internal:11434")
    with respx.mock() as router:
        router.get("http://ollama.internal:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, _ = OllamaProvider().configured()
    assert ok is True


def test_health_probe_success():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        ok, reason = OllamaProvider().health_probe()
    assert ok is True and reason is None


def test_health_probe_timeout():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(
            side_effect=httpx.ReadTimeout(
                "timed out", request=httpx.Request("GET", TAGS_URL)
            )
        )
        ok, reason = OllamaProvider().health_probe(timeout_sec=9)
    assert ok is False
    assert "timed out" in reason


def test_health_probe_non_200():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(return_value=httpx.Response(503, text="down"))
        ok, reason = OllamaProvider().health_probe()
    assert ok is False
    assert "returned 503" in reason


def test_health_probe_network_error():
    with respx.mock() as router:
        router.get(TAGS_URL).mock(side_effect=httpx.ConnectError("refused"))
        ok, reason = OllamaProvider().health_probe()
    assert ok is False
    assert "cannot reach Ollama" in reason


def test_call_returns_normalized_response():
    body = {
        "model": OLLAMA_DEFAULT_MODEL,
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
    assert response.model == OLLAMA_DEFAULT_MODEL
    assert response.usage["input_tokens"] == 8
    assert response.usage["output_tokens"] == 3
    assert response.usage["cached_tokens"] is None
    assert response.usage["thinking_budget"] == 0
    assert response.duration_ms == 1500


def test_call_uses_env_default_model_when_model_omitted(monkeypatch):
    monkeypatch.setenv(OLLAMA_MODEL_ENV, "qwen2.5-coder:14b")
    body = {
        "model": "qwen2.5-coder:14b",
        "message": {"role": "assistant", "content": "hello from local qwen"},
    }
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=body)
        )
        response = OllamaProvider().call("hi")

    payload = _json.loads(route.calls.last.request.read())
    assert payload["model"] == "qwen2.5-coder:14b"
    assert response.model == "qwen2.5-coder:14b"


def test_call_explicit_model_beats_env_default(monkeypatch):
    monkeypatch.setenv(OLLAMA_MODEL_ENV, "qwen2.5-coder:14b")
    body = {
        "model": "llama3.1:8b",
        "message": {"role": "assistant", "content": "hello from llama"},
    }
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=body)
        )
        response = OllamaProvider().call("hi", model="llama3.1:8b")

    payload = _json.loads(route.calls.last.request.read())
    assert payload["model"] == "llama3.1:8b"
    assert response.model == "llama3.1:8b"


def test_call_falls_back_to_installed_chat_model_when_default_missing():
    body = {
        "model": "qwen2.5-coder:14b",
        "message": {"role": "assistant", "content": "hello from fallback"},
    }
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(
                    404,
                    text=f'model "{OLLAMA_DEFAULT_MODEL}" not found, try pulling it',
                ),
                httpx.Response(200, json=body),
            ]
        )
        tags = router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "llama3.1:8b"},
                        {"name": "qwen2.5-coder:14b"},
                    ]
                },
            )
        )
        response = OllamaProvider().call("hi")

    assert tags.called
    assert route.call_count == 2
    assert _json.loads(route.calls[0].request.read())["model"] == OLLAMA_DEFAULT_MODEL
    assert (
        _json.loads(route.calls[1].request.read())["model"]
        == "qwen2.5-coder:14b"
    )
    assert response.text == "hello from fallback"
    assert response.model == "qwen2.5-coder:14b"


def test_call_explicit_missing_model_does_not_auto_replace():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(
                404, text='model "missing:latest" does not exist'
            )
        )
        tags = router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "qwen2.5-coder:14b"}]}
            )
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OllamaProvider().call("hi", model="missing:latest")

    assert "missing:latest" in str(exc.value)
    assert route.call_count == 1
    assert tags.called is False


def test_call_does_not_fall_back_to_embedding_only_model():
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(
                404,
                text=f'model "{OLLAMA_DEFAULT_MODEL}" not installed, run pull',
            )
        )
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "nomic-embed-text:latest"}]}
            )
        )
        with pytest.raises(ProviderHTTPError) as exc:
            OllamaProvider().call("hi")

    assert OLLAMA_DEFAULT_MODEL in str(exc.value)
    assert route.call_count == 1


def test_call_surfaces_invalid_tags_json_during_model_fallback():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(
                404,
                text=f'model "{OLLAMA_DEFAULT_MODEL}" not installed, run pull',
            )
        )
        router.get(TAGS_URL).mock(return_value=httpx.Response(200, text="not json"))

        with pytest.raises(ProviderHTTPError) as exc:
            OllamaProvider().call("hi")

    assert "fallback model" in str(exc.value)


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


def test_call_with_resume_session_id_raises_unsupported():
    with pytest.raises(UnsupportedCapability) as exc:
        OllamaProvider().call("hi", resume_session_id="any-id")
    assert "stateless" in str(exc.value)


def test_exec_with_resume_session_id_raises_unsupported():
    with pytest.raises(UnsupportedCapability) as exc:
        OllamaProvider().exec("hi", resume_session_id="any-id")
    assert "stateless" in str(exc.value)


def test_call_session_id_is_none_for_ollama():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {"content": "ok"},
                    "model": OLLAMA_DEFAULT_MODEL,
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
        )
        response = OllamaProvider().call("hi")
    assert response.session_id is None


# --------------------------------------------------------------------------- #
# exec() — tool-use loop (v0.3.1 / Slice B)
# --------------------------------------------------------------------------- #


def _ollama_terminal(content: str) -> dict:
    return {
        "model": OLLAMA_DEFAULT_MODEL,
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": 5,
        "eval_count": 2,
        "done": True,
    }


def _ollama_tool_turn(name: str, arguments: dict, call_id: str | None = None) -> dict:
    call: dict = {
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }
    if call_id is not None:
        call["id"] = call_id
    return {
        "model": OLLAMA_DEFAULT_MODEL,
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [call],
        },
        "prompt_eval_count": 3,
        "eval_count": 4,
        "done": False,
    }


def test_exec_without_tools_delegates_to_call():
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {"content": "just text"},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
        )
        resp = OllamaProvider().exec("hi")
    assert resp.text == "just text"
    assert "tool_iterations" not in resp.usage


def test_exec_rejects_non_none_sandbox_without_tools():
    with pytest.raises(UnsupportedCapability) as exc:
        OllamaProvider().exec("hi", sandbox="read-only")
    assert "without tools" in str(exc.value)


def test_exec_with_tools_requires_at_least_read_only():
    with pytest.raises(UnsupportedCapability) as exc:
        OllamaProvider().exec(
            "hi", tools=frozenset({"Read"}), sandbox="none"
        )
    assert "read-only" in str(exc.value)


def test_exec_rejects_unsupported_tool():
    with pytest.raises(UnsupportedCapability) as exc:
        OllamaProvider().exec(
            "hi", tools=frozenset({"Telepathy"}), sandbox="read-only"
        )
    assert "does not support" in str(exc.value)


def test_exec_runs_single_tool_call_then_answers(tmp_path):
    (tmp_path / "note.txt").write_text("the answer is 42")
    tool_turn = _ollama_tool_turn("Read", {"path": "note.txt"}, call_id="c1")
    final = _ollama_terminal("The file says 42.")
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = OllamaProvider().exec(
            "read note.txt",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )

    assert resp.text == "The file says 42."
    assert resp.usage["tool_iterations"] == 2
    assert resp.usage["hit_iteration_cap"] is False
    assert route.call_count == 2

    final_request = route.calls[-1].request
    payload = _json.loads(final_request.read())
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert payload["messages"][2]["content"] == "the answer is 42"


def test_exec_keeps_installed_fallback_model_after_tool_retry(tmp_path):
    (tmp_path / "note.txt").write_text("fallback model persisted")
    tool_turn = _ollama_tool_turn("Read", {"path": "note.txt"}, call_id="c1")
    tool_turn["model"] = "llama3.1:8b"
    final = _ollama_terminal("The fallback model saw the file.")
    final["model"] = "llama3.1:8b"

    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(
                    404,
                    text=f'model "{OLLAMA_DEFAULT_MODEL}" not found, try pulling it',
                ),
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        router.get(TAGS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen2.5-coder:14b"},
                        {"name": "llama3.1:8b"},
                    ]
                },
            )
        )
        resp = OllamaProvider().exec(
            "read note.txt",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )

    payloads = [_json.loads(call.request.read()) for call in route.calls]
    assert [payload["model"] for payload in payloads] == [
        OLLAMA_DEFAULT_MODEL,
        "llama3.1:8b",
        "llama3.1:8b",
    ]
    assert resp.text == "The fallback model saw the file."
    assert resp.model == "llama3.1:8b"


def test_exec_handles_ollama_string_args(tmp_path):
    # Ollama nominally emits arguments as dict, but the provider accepts
    # either shape — round-trip stringified JSON just in case.
    (tmp_path / "f.txt").write_text("hello")
    tool_turn = _ollama_tool_turn("Read", {"path": "f.txt"}, call_id="c1")
    tool_turn["message"]["tool_calls"][0]["function"]["arguments"] = (
        '{"path": "f.txt"}'
    )
    final = _ollama_terminal("got it")
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = OllamaProvider().exec(
            "go",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.text == "got it"


def test_exec_tool_error_feeds_back(tmp_path):
    tool_turn = _ollama_tool_turn(
        "Read", {"path": "../../../etc/passwd"}, call_id="x"
    )
    final = _ollama_terminal("refused, summarizing from memory")
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        resp = OllamaProvider().exec(
            "read secret",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert "refused" in resp.text
    payload = _json.loads(route.calls[-1].request.read())
    tool_msg = [m for m in payload["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("error:")


def test_exec_iteration_cap(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    loop = _ollama_tool_turn("Read", {"path": "f.txt"})
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=loop)
        )
        resp = OllamaProvider().exec(
            "never",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.usage["hit_iteration_cap"] is True
    assert resp.usage["tool_iterations"] == 10
    assert route.call_count == 10
    assert "max iterations" in resp.text.lower()


def test_exec_workspace_write_runs_edit(tmp_path):
    (tmp_path / "a.py").write_text("hello world\n")
    tool_turn = _ollama_tool_turn(
        "Edit",
        {
            "path": "a.py",
            "old_string": "world",
            "new_string": "galaxy",
        },
        call_id="c",
    )
    final = _ollama_terminal("done")
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=tool_turn),
                httpx.Response(200, json=final),
            ]
        )
        OllamaProvider().exec(
            "swap world→galaxy",
            tools=frozenset({"Edit"}),
            sandbox="workspace-write",
            cwd=str(tmp_path),
        )
    assert (tmp_path / "a.py").read_text() == "hello galaxy\n"


def test_exec_session_id_raises():
    with pytest.raises(UnsupportedCapability):
        OllamaProvider().exec("hi", resume_session_id="abc")


def test_exec_hits_context_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(OllamaProvider, "max_context_tokens", 100)
    (tmp_path / "f.txt").write_text("x")
    turn = _ollama_tool_turn("Read", {"path": "f.txt"}, call_id="c1")
    turn["prompt_eval_count"] = 95
    with respx.mock() as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=turn)
        )
        resp = OllamaProvider().exec(
            "long",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert resp.usage["hit_context_budget"] is True
    assert route.call_count == 1
    assert "context budget" in resp.text.lower()


def test_exec_reports_per_iteration_log(tmp_path):
    (tmp_path / "a.txt").write_text("one")
    turn1 = _ollama_tool_turn("Read", {"path": "a.txt"}, call_id="c1")
    turn1["prompt_eval_count"] = 10
    turn1["eval_count"] = 3
    final = _ollama_terminal("done")
    final["prompt_eval_count"] = 20
    final["eval_count"] = 5
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(200, json=turn1),
                httpx.Response(200, json=final),
            ]
        )
        resp = OllamaProvider().exec(
            "go",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    iters = resp.usage["iterations"]
    assert len(iters) == 2
    assert iters[0]["prompt_tokens"] == 10
    assert iters[1]["prompt_tokens"] == 20


def test_exec_accepts_strict_sandbox(tmp_path):
    final = _ollama_terminal("direct answer")
    with respx.mock() as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=final)
        )
        resp = OllamaProvider().exec(
            "read a.txt",
            tools=frozenset({"Read"}),
            sandbox="strict",
            cwd=str(tmp_path),
        )
    assert resp.text == "direct answer"


# --------------------------------------------------------------------------- #
# v0.3.3 — local LLM ergonomics (timeout env override, stealth tool-call guard)
# --------------------------------------------------------------------------- #


def test_default_model_points_at_tool_use_capable_qwen():
    # Guardrail: the default must be a model that actually emits structured
    # tool_calls in ollama. Bumped from qwen2.5-coder:14b (silent-fail) on
    # 2026-04-24 after dogfood. If someone rolls this back, fail loud.
    assert "qwen" in OLLAMA_DEFAULT_MODEL
    assert "qwen2.5-coder" not in OLLAMA_DEFAULT_MODEL


def test_timeout_defaults_to_baked_in_when_env_unset(monkeypatch):
    from conductor.providers.ollama import OLLAMA_REQUEST_TIMEOUT_SEC

    monkeypatch.delenv("CONDUCTOR_OLLAMA_TIMEOUT_SEC", raising=False)
    provider = OllamaProvider()
    assert provider._timeout_sec == OLLAMA_REQUEST_TIMEOUT_SEC


def test_timeout_env_override_wins(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_OLLAMA_TIMEOUT_SEC", "45")
    provider = OllamaProvider()
    assert provider._timeout_sec == 45.0


def test_timeout_explicit_kwarg_beats_env(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_OLLAMA_TIMEOUT_SEC", "45")
    provider = OllamaProvider(timeout_sec=12.0)
    assert provider._timeout_sec == 12.0


def test_timeout_env_invalid_falls_back_to_default(monkeypatch):
    from conductor.providers.ollama import OLLAMA_REQUEST_TIMEOUT_SEC

    monkeypatch.setenv("CONDUCTOR_OLLAMA_TIMEOUT_SEC", "abc")
    assert OllamaProvider()._timeout_sec == OLLAMA_REQUEST_TIMEOUT_SEC


def test_timeout_env_non_positive_falls_back_to_default(monkeypatch):
    from conductor.providers.ollama import OLLAMA_REQUEST_TIMEOUT_SEC

    monkeypatch.setenv("CONDUCTOR_OLLAMA_TIMEOUT_SEC", "-5")
    assert OllamaProvider()._timeout_sec == OLLAMA_REQUEST_TIMEOUT_SEC


def test_stealth_tool_call_fenced_markdown_detected():
    from conductor.providers.ollama import _find_stealth_tool_call

    content = '```json\n{"name": "Read", "arguments": {"path": "a.txt"}}\n```'
    assert _find_stealth_tool_call(content, frozenset({"Read"})) == "Read"


def test_stealth_tool_call_plain_json_detected():
    from conductor.providers.ollama import _find_stealth_tool_call

    content = 'Here you go: {"name": "Grep", "arguments": {"pattern": "x"}}'
    assert _find_stealth_tool_call(content, frozenset({"Grep"})) == "Grep"


def test_stealth_tool_call_ignored_when_name_not_in_tool_set():
    from conductor.providers.ollama import _find_stealth_tool_call

    content = '```json\n{"name": "Telepathy", "arguments": {}}\n```'
    assert _find_stealth_tool_call(content, frozenset({"Read"})) is None


def test_stealth_tool_call_ignored_when_no_tools_param():
    from conductor.providers.ollama import _find_stealth_tool_call

    content = '{"name": "Read"}'
    assert _find_stealth_tool_call(content, frozenset()) is None


def test_stealth_tool_call_ignored_on_plain_prose():
    from conductor.providers.ollama import _find_stealth_tool_call

    content = "The function _resolve_in_cwd defends against path traversal."
    assert _find_stealth_tool_call(content, frozenset({"Read"})) is None


def test_exec_warns_on_stealth_tool_call(tmp_path):
    # Simulate qwen2.5-coder-style silent fail: content contains the
    # tool call as markdown JSON, but tool_calls field is empty. The
    # loop should inject a visible diagnostic ahead of the prose.
    stealth_body = {
        "model": OLLAMA_DEFAULT_MODEL,
        "message": {
            "role": "assistant",
            "content": (
                '```json\n{"name": "Read", "arguments": '
                '{"path": "src/conductor/tools/registry.py"}}\n```\n\n'
                "_resolve_in_cwd blocks path traversal."
            ),
            # No tool_calls field — the silent fail pattern.
        },
        "prompt_eval_count": 10,
        "eval_count": 20,
        "done": True,
    }
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=stealth_body))
        resp = OllamaProvider().exec(
            "summarize _resolve_in_cwd",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert "[conductor:" in resp.text
    assert "tool-call-shaped" in resp.text
    assert "Read" in resp.text
    # Original model prose still present after the diagnostic.
    assert "_resolve_in_cwd" in resp.text


def test_exec_does_not_warn_on_clean_no_tool_response(tmp_path):
    # When the model legitimately answers without tool-use, don't spam
    # the user with a false-positive diagnostic.
    clean = _ollama_terminal("direct answer without tools")
    with respx.mock() as router:
        router.post(CHAT_URL).mock(return_value=httpx.Response(200, json=clean))
        resp = OllamaProvider().exec(
            "what is 2+2",
            tools=frozenset({"Read"}),
            sandbox="read-only",
            cwd=str(tmp_path),
        )
    assert "[conductor:" not in resp.text
    assert resp.text == "direct answer without tools"
