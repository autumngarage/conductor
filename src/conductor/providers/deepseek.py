"""DeepSeek provider — direct DeepSeek API (OpenAI-compatible).

DeepSeek ships two distinct models that map to different conductor use cases,
so they are registered as two providers (``deepseek-chat`` and
``deepseek-reasoner``) backed by one HTTP base class:

  - ``deepseek-chat``     — V3.x: cheap, fast, general/code-review.
  - ``deepseek-reasoner`` — R1: strong-reasoning with built-in chain-of-thought.

Both share one credential (``DEEPSEEK_API_KEY``) and one endpoint
(``https://api.deepseek.com/v1``). The router picks between them via the
provider-level tags.

A future Cloudflare-hosted backend (parity with how Kimi reaches CF) would
land as a backend option on this same provider, not a new identifier — same
rule documented in ``kimi.py``.

v1 scope: single-turn ``call()`` only. ``exec()`` without tools delegates
to ``call()``; with tools, raises ``UnsupportedCapability``. Tool-use
loop can be added later mirroring the kimi/ollama pattern; not in scope
for this adapter's first cut.
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

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_REQUEST_TIMEOUT_SEC = 120.0

DEEPSEEK_CHAT_MODEL = "deepseek-chat"
DEEPSEEK_REASONER_MODEL = "deepseek-reasoner"


class _DeepSeekBase:
    """Shared HTTP plumbing for DeepSeek's OpenAI-compatible endpoint.

    Subclasses set ``name``, ``default_model``, ``tags``, and the capability
    fields the router scores against.
    """

    # --- subclasses must override --- #
    name: str = ""
    default_model: str = ""
    tags: list[str] = []

    # --- capability declarations (subclasses may override) --- #
    quality_tier = "strong"
    supported_tools: frozenset[str] = frozenset()
    supported_sandboxes: frozenset[str] = frozenset({"none"})
    supports_effort = False
    effort_to_thinking: dict[str, int] = {}
    cost_per_1k_in = 0.0
    cost_per_1k_out = 0.0
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 3000
    max_context_tokens = 128_000

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout_sec: float = DEEPSEEK_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def _resolve_key(self) -> str:
        key = self._api_key or credentials.get(DEEPSEEK_API_KEY_ENV)
        if not key:
            raise ProviderConfigError(
                f"{DEEPSEEK_API_KEY_ENV} is not set. "
                "Create an API key at https://platform.deepseek.com/api_keys "
                "and set it via `conductor init` or "
                f"`export {DEEPSEEK_API_KEY_ENV}=...`."
            )
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
        }

    def configured(self) -> tuple[bool, str | None]:
        if self._api_key or credentials.get(DEEPSEEK_API_KEY_ENV):
            return True, None
        return False, (
            f"missing credential: {DEEPSEEK_API_KEY_ENV}. "
            "Set via `conductor init` or export as an env var."
        )

    def smoke(self) -> tuple[bool, str | None]:
        # Cheapest round-trip that proves auth + endpoint + model are reachable.
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
            raise ProviderHTTPError(f"network error calling DeepSeek: {e}") from e

        if resp.status_code != 200:
            raise ProviderHTTPError(
                f"DeepSeek returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"DeepSeek response was not JSON: {e}") from e

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
                f"{self.name} has no session model — each DeepSeek API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)

        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
        }

        start = time.monotonic()
        body = self._post_chat(payload)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderHTTPError(
                f"DeepSeek response missing choices[0].message.content: {body!r:.500}"
            ) from e

        # deepseek-reasoner returns its chain-of-thought in
        # ``choices[0].message.reasoning_content``; surface its length in usage
        # for observability without forcing it into ``text``.
        reasoning_content = (
            (body.get("choices") or [{}])[0].get("message", {}).get("reasoning_content")
        )
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
                "reasoning_chars": len(reasoning_content) if reasoning_content else 0,
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
        # accepted for API parity; only codex implements stall-watchdog today
        if resume_session_id:
            raise UnsupportedCapability(
                f"{self.name} has no session model — each DeepSeek API call is "
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


class DeepSeekChatProvider(_DeepSeekBase):
    """DeepSeek V3.x chat model — cheap, fast, general code-review tier."""

    name = "deepseek-chat"
    default_model = DEEPSEEK_CHAT_MODEL
    tags = ["cheap", "code-review", "tool-use"]
    fix_command = "conductor init --only deepseek-chat"

    quality_tier = "strong"
    supports_effort = False
    effort_to_thinking: dict[str, int] = {}
    # Cache-miss prices; cache hits are cheaper but unpredictable for budgeting.
    cost_per_1k_in = 0.00027
    cost_per_1k_out = 0.0011
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 2500
    max_context_tokens = 128_000


class DeepSeekReasonerProvider(_DeepSeekBase):
    """DeepSeek R1 reasoning model — strong-reasoning with built-in CoT."""

    name = "deepseek-reasoner"
    default_model = DEEPSEEK_REASONER_MODEL
    tags = ["strong-reasoning", "thinking", "code-review"]
    fix_command = "conductor init --only deepseek-reasoner"

    quality_tier = "strong"
    # R1 always emits reasoning_content; the dial is informational (DeepSeek's
    # API has no per-call thinking-budget knob today). The mapping lets
    # downstream callers reason about effort consistently across providers.
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 4_000,
        "high": 8_000,
        "max": 16_000,
    }
    cost_per_1k_in = 0.00055
    cost_per_1k_out = 0.00219
    cost_per_1k_thinking = 0.00055
    typical_p50_ms = 12_000  # reasoning models are slow
    max_context_tokens = 128_000
