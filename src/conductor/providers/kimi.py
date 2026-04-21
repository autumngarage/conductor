"""Kimi (Moonshot AI) provider — OpenAI-compatible HTTP at api.moonshot.ai.

This is the first Conductor provider that touches an API key directly. Every
other adapter at v0.1 either shells out to a CLI that owns its own auth
(claude, codex, gemini) or talks to a local endpoint with no auth (ollama).
Moonshot has no scriptable CLI — the official `kimi` CLI is interactive-only —
so the OpenAI-compatible HTTP path is the only viable shape.

Quirks vs. vanilla OpenAI Chat Completions, per Moonshot's migration guide
(https://platform.kimi.ai/docs/guide/migrating-from-openai-to-kimi):

  - `temperature` is clamped to [0, 1] (OpenAI accepts [0, 2]). The CLI
    surface doesn't expose temperature today, so we don't enforce this — but
    a future v0.2 that adds `--temperature` MUST clamp before sending.
  - `tool_choice="required"` is not supported. v0.1 doesn't issue tool calls
    so this isn't reachable.
  - Streaming responses omit `usage` unless the request includes
    `stream_options={"include_usage": true}`. v0.1 is non-streaming so the
    `usage` block is always present in the response body.
  - Multi-turn tool calls with thinking models (kimi-k2-thinking) require
    echoing the assistant's `reasoning_content` back on subsequent turns or
    the API rejects with 400. v0.1 doesn't support tool use; if/when it
    does, see LiteLLM #21672 for the canonical writeup of the failure mode.

The default model `kimi-k2.6` is set per autumn-garage/integration/providers.md.
Override per-call via the `model` argument.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
)

KIMI_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_DEFAULT_MODEL = "kimi-k2.6"
KIMI_API_KEY_ENV = "MOONSHOT_API_KEY"
KIMI_REQUEST_TIMEOUT_SEC = 120.0


class KimiProvider:
    name = "kimi"
    tags = ["long-context", "cheap", "tool-use", "vision"]
    default_model = KIMI_DEFAULT_MODEL

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = KIMI_BASE_URL,
        timeout_sec: float = KIMI_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def _resolve_key(self) -> str:
        key = self._api_key or os.environ.get(KIMI_API_KEY_ENV)
        if not key:
            raise ProviderConfigError(
                f"{KIMI_API_KEY_ENV} is not set. "
                "Get a key from https://platform.moonshot.ai/console/api-keys "
                f"and export {KIMI_API_KEY_ENV}=... in your shell."
            )
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
        }

    def configured(self) -> tuple[bool, Optional[str]]:
        if self._api_key or os.environ.get(KIMI_API_KEY_ENV):
            return True, None
        return False, f"{KIMI_API_KEY_ENV} is not set"

    def smoke(self) -> tuple[bool, Optional[str]]:
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.get(
                    f"{self._base_url}/models",
                    headers=self._headers(),
                )
        except ProviderConfigError as e:
            return False, str(e)
        except httpx.HTTPError as e:
            return False, f"network error: {e}"

        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        try:
            data = resp.json()
        except ValueError:
            return False, "response was not JSON"
        if "data" not in data:
            return False, f"unexpected response shape: {sorted(data)[:5]}"
        return True, None

    def call(self, task: str, model: Optional[str] = None) -> CallResponse:
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
        }
        start = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ProviderHTTPError(f"network error calling Kimi: {e}") from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            raise ProviderHTTPError(
                f"Kimi returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"Kimi response was not JSON: {e}") from e

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderHTTPError(
                f"Kimi response missing choices[0].message.content: {body!r:.500}"
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
            },
            raw=body,
        )
