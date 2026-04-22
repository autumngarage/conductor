"""Kimi (Moonshot AI K2.6) provider — hosted on Cloudflare Workers AI.

Conductor calls Kimi K2.6 via Cloudflare's OpenAI-compatible endpoint rather
than Moonshot's own api.moonshot.ai. Cloudflare added native Kimi K2.6 hosting
on 2026-04-20 with Day 0 support from Moonshot, which gives us:

  - one credential surface (Cloudflare API token + account ID) that will serve
    future CF-hosted models too, without the user ever creating a Moonshot
    account;
  - lowest-latency inference (CF's edge network);
  - unified billing on the Cloudflare bill.

Endpoint:
  POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions

Auth:
  Authorization: Bearer {CLOUDFLARE_API_TOKEN}

Model ID is Cloudflare's namespaced form: ``@cf/moonshotai/kimi-k2.6``.

Quirks (inherited from Moonshot; may or may not be enforced by the Cloudflare
frontend — test before sending):
  - ``temperature`` expected in [0, 1] per Moonshot's spec (OpenAI allows [0, 2]).
    v0.1 doesn't expose temperature so this isn't reachable; when a future
    version does, clamp before sending.
  - ``tool_choice="required"`` not supported by Moonshot.
  - Multi-turn tool calls with thinking variants require echoing
    ``reasoning_content`` back on subsequent turns.

If a future consumer needs the direct Moonshot backend (own API key, own
billing), that lands as a second backend option on this provider — not a
new provider identifier. ``kimi`` is the model family; the backend is a
detail of how Conductor reaches it.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from conductor import credentials
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)

CLOUDFLARE_API_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
CLOUDFLARE_ACCOUNT_ID_ENV = "CLOUDFLARE_ACCOUNT_ID"
KIMI_DEFAULT_MODEL = "@cf/moonshotai/kimi-k2.6"
KIMI_BASE_URL_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
)
KIMI_REQUEST_TIMEOUT_SEC = 120.0


class KimiProvider:
    name = "kimi"
    tags = ["long-context", "cheap", "vision", "code-review"]
    default_model = KIMI_DEFAULT_MODEL

    # Capability declarations (see interface.py)
    quality_tier = "strong"
    # Tool-use lands in Stage 3 (HTTP-side tool-use loop).
    # Until then kimi.exec() with non-empty tools raises UnsupportedCapability.
    supported_tools: frozenset[str] = frozenset()
    supported_sandboxes: frozenset[str] = frozenset({"none"})
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 4_000,
        "high": 8_000,
        "max": 16_000,
    }
    cost_per_1k_in = 0.00015
    cost_per_1k_out = 0.00075
    cost_per_1k_thinking = 0.00015
    typical_p50_ms = 3500

    def __init__(
        self,
        *,
        api_token: Optional[str] = None,
        account_id: Optional[str] = None,
        base_url_template: str = KIMI_BASE_URL_TEMPLATE,
        timeout_sec: float = KIMI_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._api_token = api_token
        self._account_id = account_id
        self._base_url_template = base_url_template
        self._timeout_sec = timeout_sec

    def _resolve_token(self) -> str:
        token = self._api_token or credentials.get(CLOUDFLARE_API_TOKEN_ENV)
        if not token:
            raise ProviderConfigError(
                f"{CLOUDFLARE_API_TOKEN_ENV} is not set. "
                "Create a Cloudflare API token with Workers AI read permission "
                "at https://dash.cloudflare.com/profile/api-tokens "
                "and set it via `conductor init` or "
                f"`export {CLOUDFLARE_API_TOKEN_ENV}=...`."
            )
        return token

    def _resolve_account_id(self) -> str:
        account_id = self._account_id or credentials.get(CLOUDFLARE_ACCOUNT_ID_ENV)
        if not account_id:
            raise ProviderConfigError(
                f"{CLOUDFLARE_ACCOUNT_ID_ENV} is not set. "
                "Find your account ID on the right sidebar of any zone page in "
                "https://dash.cloudflare.com/ (or run `wrangler whoami`) "
                "and set it via `conductor init` or "
                f"`export {CLOUDFLARE_ACCOUNT_ID_ENV}=...`."
            )
        return account_id

    def _base_url(self) -> str:
        return self._base_url_template.format(account_id=self._resolve_account_id())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_token()}",
            "Content-Type": "application/json",
        }

    def configured(self) -> tuple[bool, Optional[str]]:
        missing = []
        if not (self._api_token or credentials.get(CLOUDFLARE_API_TOKEN_ENV)):
            missing.append(CLOUDFLARE_API_TOKEN_ENV)
        if not (self._account_id or credentials.get(CLOUDFLARE_ACCOUNT_ID_ENV)):
            missing.append(CLOUDFLARE_ACCOUNT_ID_ENV)
        if missing:
            return False, (
                f"missing credential(s): {', '.join(missing)}. "
                "Set via `conductor init` or export as env vars."
            )
        return True, None

    def smoke(self) -> tuple[bool, Optional[str]]:
        # No /models endpoint on CF's Workers AI OpenAI-compat surface today;
        # a 1-token chat completion is the cheapest round-trip that proves
        # auth + account + model are all reachable.
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
                    f"{self._base_url()}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ProviderHTTPError(f"network error calling Cloudflare: {e}") from e

        if resp.status_code != 200:
            raise ProviderHTTPError(
                f"Cloudflare returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"Cloudflare response was not JSON: {e}") from e

    def call(
        self,
        task: str,
        model: Optional[str] = None,
        *,
        effort: str | int = "medium",
    ) -> CallResponse:
        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)

        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
        }
        # Moonshot/Kimi exposes reasoning via `reasoning_content` in streaming;
        # the non-streaming contract accepts a thinking budget hint through
        # the `thinking` object per OpenAI-ish convention where supported.
        if thinking_budget > 0:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

        start = time.monotonic()
        body = self._post_chat(payload)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderHTTPError(
                f"Cloudflare response missing choices[0].message.content: {body!r:.500}"
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
        model: Optional[str] = None,
        *,
        effort: str | int = "medium",
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: Optional[str] = None,
        timeout_sec: int = 300,
    ) -> CallResponse:
        if tools:
            raise UnsupportedCapability(
                "kimi.exec() with tools is not supported in v0.2 (HTTP tool-use loop "
                "lands in Stage 3). Router should filter kimi out when tools are "
                f"requested; got tools={sorted(tools)}."
            )
        if sandbox not in ("", "none"):
            raise UnsupportedCapability(
                f"kimi.exec() sandbox={sandbox!r} not meaningful without tool-use; "
                "use sandbox='none' or filter kimi from routing when sandbox required."
            )
        # No tools, no sandbox → equivalent to single-turn call.
        return self.call(task, model=model, effort=effort)
