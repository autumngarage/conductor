"""OpenRouter provider — OpenAI-compatible HTTP adapter.

PR 1 scope: a single-turn adapter that lets callers target OpenRouter
explicitly via ``--with openrouter --model <slug>``. Auto-mode selection,
catalog-driven model discovery, and tool-use orchestration are deferred to
the follow-up migration PRs.
"""

from __future__ import annotations

import time

import httpx

from conductor import credentials
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_REQUEST_TIMEOUT_SEC = 120.0
OPENROUTER_HTTP_REFERER = "https://github.com/autumngarage/conductor"
OPENROUTER_X_TITLE = "conductor"

_OPENROUTER_REASONING_EFFORTS = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


class OpenRouterProvider:
    name = "openrouter"
    default_model = OPENROUTER_DEFAULT_MODEL
    tags = [
        "long-context",
        "thinking",
        "code-review",
        "tool-use",
        "vision",
        "cheap",
        "strong-reasoning",
    ]
    fix_command = "conductor init --only openrouter"

    quality_tier = "frontier"
    supported_tools: frozenset[str] = frozenset()
    supported_sandboxes: frozenset[str] = frozenset({"none"})
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 8_000,
        "high": 24_000,
        "max": 64_000,
    }
    cost_per_1k_in = 0.005
    cost_per_1k_out = 0.015
    cost_per_1k_thinking = 0.005
    typical_p50_ms = 2500
    max_context_tokens = 200_000

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_sec: float = OPENROUTER_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def _resolve_key(self) -> str:
        key = self._api_key or credentials.get(OPENROUTER_API_KEY_ENV)
        if not key:
            raise ProviderConfigError(
                f"{OPENROUTER_API_KEY_ENV} is not set. "
                "Create an API key at https://openrouter.ai/keys and set it via "
                f"`conductor init` or `export {OPENROUTER_API_KEY_ENV}=...`."
            )
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_HTTP_REFERER,
            "X-Title": OPENROUTER_X_TITLE,
        }

    def _reasoning_payload(self, effort: str | int) -> dict[str, str] | None:
        if not isinstance(effort, str):
            return None
        mapped = _OPENROUTER_REASONING_EFFORTS.get(effort)
        if mapped is None:
            return None
        return {"effort": mapped}

    def configured(self) -> tuple[bool, str | None]:
        if self._api_key or credentials.get(OPENROUTER_API_KEY_ENV):
            return True, None
        return False, (
            f"missing credential: {OPENROUTER_API_KEY_ENV}. "
            "Set via `conductor init` or export as an env var."
        )

    def smoke(self) -> tuple[bool, str | None]:
        try:
            response = self._post_chat(
                {
                    "model": self.default_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }
            )
        except ProviderConfigError as e:
            return False, str(e)
        except ProviderHTTPError as e:
            return False, str(e)
        if "choices" not in response:
            return False, f"unexpected response shape: {sorted(response)[:5]}"
        return True, None

    def _post_chat(self, payload: dict) -> dict:
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ProviderHTTPError(f"network error calling OpenRouter: {e}") from e

        if resp.status_code != 200:
            raise ProviderHTTPError(
                f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"OpenRouter response was not JSON: {e}") from e

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
        }
        reasoning = self._reasoning_payload(effort)
        if reasoning is not None:
            payload["reasoning"] = reasoning

        start = time.monotonic()
        body = self._post_chat(payload)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderHTTPError(
                f"OpenRouter response missing choices[0].message.content: {body!r:.500}"
            ) from e

        usage = body.get("usage") or {}
        return CallResponse(
            text=text,
            provider=self.name,
            model=body.get("model", model),
            duration_ms=duration_ms,
            usage={
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ),
                "thinking_tokens": (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                ),
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            raw=body,
        )

    def exec(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
        if sandbox == "":
            sandbox = "none"

        if tools:
            raise UnsupportedCapability(
                f"{self.name} does not yet drive a tool-use loop in conductor "
                f"(supported tools: {sorted(self.supported_tools) or 'none'}). "
                "Use a provider with tool support (claude, codex, gemini, "
                "kimi, ollama) or run this task without --tools."
            )
        if sandbox != "none":
            raise UnsupportedCapability(
                f"{self.name}.exec() sandbox={sandbox!r} is not meaningful "
                "without tools. Use sandbox='none' for a text-only exec."
            )
        return self.call(task, model=model, effort=effort)
