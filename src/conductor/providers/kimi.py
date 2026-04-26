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

import json
import time
from pathlib import Path

import httpx

from conductor import credentials
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)
from conductor.tools import (
    ToolExecutionError,
    ToolExecutor,
    build_tool_specs,
)

KIMI_MAX_TOOL_ITERATIONS = 10
# Stop the tool-use loop when the last-reported prompt tokens leaves
# less than this fraction of the model's context free. The next turn's
# growth tends to exceed that margin.
KIMI_CONTEXT_SAFETY_MARGIN = 0.1

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
    # Full tool-use landed in v0.3.1 (Edit/Write/Bash + workspace-write
    # sandbox). v0.3.0 shipped the read-only half. Subprocess sandbox
    # (`--sandbox strict`) lands in v0.3.2.
    supported_tools: frozenset[str] = frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    supported_sandboxes: frozenset[str] = frozenset(
        {"none", "read-only", "workspace-write", "strict"}
    )
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
    # Kimi K2.6 ships 256K context on Cloudflare Workers AI.
    max_context_tokens = 256_000

    def __init__(
        self,
        *,
        api_token: str | None = None,
        account_id: str | None = None,
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

    def configured(self) -> tuple[bool, str | None]:
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

    def smoke(self) -> tuple[bool, str | None]:
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
        model: str | None = None,
        *,
        effort: str | int = "medium",
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "kimi has no session model — each Cloudflare Workers AI call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
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
        model: str | None = None,
        *,
        effort: str | int = "medium",
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "kimi has no session model — each Cloudflare Workers AI call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
        if sandbox == "":
            sandbox = "none"

        # No tools → stateless single-turn call, sandbox must be "none".
        if not tools:
            if sandbox != "none":
                raise UnsupportedCapability(
                    f"kimi.exec() sandbox={sandbox!r} is not meaningful without "
                    "tools. Use sandbox='none' for a text-only exec, or pass "
                    "--tools with a supported tool set."
                )
            return self.call(task, model=model, effort=effort)

        # Tools requested → defence in depth on router filters.
        unknown = tools - self.supported_tools
        if unknown:
            raise UnsupportedCapability(
                f"kimi does not support tools {sorted(unknown)} "
                f"(supported: {sorted(self.supported_tools)})."
            )
        if sandbox not in self.supported_sandboxes:
            raise UnsupportedCapability(
                f"kimi does not support sandbox={sandbox!r} "
                f"(supported: {sorted(self.supported_sandboxes)})."
            )
        if sandbox == "none":
            raise UnsupportedCapability(
                "kimi.exec() with tools requires at least sandbox='read-only'."
            )

        workdir = Path(cwd) if cwd else Path.cwd()
        executor = ToolExecutor(cwd=workdir, sandbox=sandbox)
        tool_specs = build_tool_specs(tools)

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)

        messages: list[dict] = [{"role": "user", "content": task}]
        iteration = 0
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
        }
        iterations_log: list[dict] = []
        final_text = ""
        final_body: dict = {}
        hit_cap = False
        hit_context_budget = False
        context_ceiling = int(
            self.max_context_tokens * (1 - KIMI_CONTEXT_SAFETY_MARGIN)
        )

        start = time.monotonic()
        while iteration < KIMI_MAX_TOOL_ITERATIONS:
            iteration += 1
            payload: dict = {
                "model": model,
                "messages": messages,
                "tools": tool_specs,
            }
            if thinking_budget > 0:
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }

            body = self._post_chat(payload)
            final_body = body

            try:
                msg = body["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as e:
                raise ProviderHTTPError(
                    f"Cloudflare response missing choices[0].message: {body!r:.500}"
                ) from e

            usage = body.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            )
            reasoning_tokens = int(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                or 0
            )
            totals["input_tokens"] += prompt_tokens
            totals["output_tokens"] += completion_tokens
            totals["cached_tokens"] += cached_tokens
            totals["thinking_tokens"] += reasoning_tokens
            iteration_cost = (
                prompt_tokens / 1000 * self.cost_per_1k_in
                + completion_tokens / 1000 * self.cost_per_1k_out
                + reasoning_tokens / 1000 * self.cost_per_1k_thinking
            )
            iterations_log.append(
                {
                    "iteration": iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "cached_tokens": cached_tokens,
                    "cost_usd": round(iteration_cost, 6),
                }
            )

            tool_calls = msg.get("tool_calls") or []
            final_text = msg.get("content") or ""

            if not tool_calls:
                break

            # Context-budget guard: if the next turn would start near the
            # model's context ceiling, break before we make a call that 413s.
            if prompt_tokens >= context_ceiling:
                hit_context_budget = True
                break

            # Echo assistant turn (content may be None when only tool_calls).
            assistant_entry: dict = {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls,
            }
            # Moonshot/Kimi thinking variants require reasoning_content to be
            # echoed on subsequent turns for multi-turn tool calls.
            if msg.get("reasoning_content"):
                assistant_entry["reasoning_content"] = msg["reasoning_content"]
            messages.append(assistant_entry)

            for idx, call in enumerate(tool_calls):
                call_id = call.get("id") or f"call_{iteration}_{idx}"
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError as e:
                        args = None
                        result = (
                            f"error: tool `{name}` arguments were not valid JSON: {e}"
                        )
                elif isinstance(raw_args, dict):
                    args = raw_args
                    result = None
                else:
                    args = {}
                    result = None

                if args is not None:
                    try:
                        result = executor.run(name, args)
                    except ToolExecutionError as e:
                        result = f"error: {e}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result,
                    }
                )
        else:
            hit_cap = True

        if hit_cap:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: tool-use loop hit max iterations "
                f"({KIMI_MAX_TOOL_ITERATIONS}); model kept requesting tools. "
                "Re-run with a narrower task or a larger budget.]"
            )
        if hit_context_budget:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: context budget exhausted "
                f"({prompt_tokens}/{self.max_context_tokens} tokens used, "
                f"safety margin {int(KIMI_CONTEXT_SAFETY_MARGIN * 100)}%). "
                "Re-run with a narrower task or a larger-context model.]"
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return CallResponse(
            text=final_text,
            provider=self.name,
            model=final_body.get("model", model) if final_body else model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "cached_tokens": totals["cached_tokens"],
                "thinking_tokens": totals["thinking_tokens"],
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
                "tool_iterations": iteration,
                "tool_names": sorted(tools),
                "hit_iteration_cap": hit_cap,
                "hit_context_budget": hit_context_budget,
                "iterations": iterations_log,
            },
            raw=final_body,
        )
