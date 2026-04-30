"""OpenRouter provider — OpenAI-compatible HTTP adapter.

Supports single-turn chat plus Conductor's local tool-call loop for
``exec`` requests. OpenRouter chooses or hosts the model; Conductor still
executes local filesystem/shell tools and feeds results back to the model.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from conductor import credentials
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)
from conductor.tools import ToolExecutionError, ToolExecutor, build_tool_specs

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

from . import openrouter_catalog

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_REQUEST_TIMEOUT_SEC = 120.0
OPENROUTER_HEALTH_PROBE_TIMEOUT_SEC = 10.0
OPENROUTER_MAX_TOOL_ITERATIONS = 10
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
    supported_tools: frozenset[str] = frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
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
        return _reasoning_payload(effort)

    def _preset_model(self) -> str | None:
        return None

    def _smoke_model(self) -> str:
        return self.default_model

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
                    "model": self._smoke_model(),
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

    def health_probe(
        self, *, timeout_sec: float = OPENROUTER_HEALTH_PROBE_TIMEOUT_SEC
    ) -> tuple[bool, str | None]:
        try:
            headers = self._headers()
        except ProviderConfigError as e:
            return False, str(e)

        url = f"{self._base_url}/models"
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return False, f"`GET {url}` timed out after {timeout_sec:.0f}s"
        except httpx.HTTPError as e:
            return False, f"network error calling OpenRouter: {e}"

        if resp.status_code != 200:
            return False, f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:200]}"
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

    def _completion_target_payload(
        self,
        *,
        model: str | None,
        models: tuple[str, ...] | list[str] | None,
        effort: str | int,
        task_tags: list[str] | tuple[str, ...] | None,
        prefer: str,
        exclude: set[str] | frozenset[str] | None,
        log_selection: bool,
    ) -> tuple[dict, str]:
        if model is not None and models:
            raise UnsupportedCapability(
                "openrouter received both `model` and `models`; pass one explicit "
                "model or an ordered fallback list, not both."
            )

        selected_model = model
        ordered_models = tuple(models or ())
        preset_model = (
            self._preset_model()
            if selected_model is None and not ordered_models
            else None
        )
        payload: dict = {}
        if ordered_models:
            payload["models"] = list(ordered_models)
            selected_model = ordered_models[0]
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning
        elif preset_model is not None:
            selected_model = preset_model
            payload["model"] = selected_model
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning
        elif selected_model is None:
            selector_payload = select_model_for_task(
                task_tags=task_tags,
                prefer=prefer,
                effort=effort,
                exclude=exclude,
            )
            payload.update(selector_payload)
            selected_model = str(payload["model"])
            if payload.get("reasoning") is None:
                payload.pop("reasoning", None)
            if log_selection:
                _log_selector_choice(
                    task_tags=task_tags,
                    prefer=prefer,
                    payload=selector_payload,
                )
        else:
            payload["model"] = selected_model
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning

        return payload, selected_model

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        models: tuple[str, ...] | list[str] | None = None,
        effort: str | int = "medium",
        task_tags: list[str] | tuple[str, ...] | None = None,
        prefer: str = "balanced",
        exclude: set[str] | frozenset[str] | None = None,
        log_selection: bool = True,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )

        # Fail on missing credentials before any selector/catalog work so an
        # unconfigured provider doesn't mask the real setup error behind a
        # catalog refresh failure.
        self._resolve_key()
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        target_payload, selected_model = self._completion_target_payload(
            model=model,
            models=models,
            effort=effort,
            task_tags=task_tags,
            prefer=prefer,
            exclude=exclude,
            log_selection=log_selection,
        )
        payload: dict = {
            **target_payload,
            "messages": [{"role": "user", "content": task}],
        }

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
            model=body.get("model", selected_model),
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
        models: tuple[str, ...] | list[str] | None = None,
        effort: str | int = "medium",
        task_tags: list[str] | tuple[str, ...] | None = None,
        prefer: str = "balanced",
        exclude: set[str] | frozenset[str] | None = None,
        log_selection: bool = True,
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
        if not tools:
            return self.call(
                task,
                model=model,
                models=models,
                effort=effort,
                task_tags=task_tags,
                prefer=prefer,
                exclude=exclude,
                log_selection=log_selection,
            )

        unknown = tools - self.supported_tools
        if unknown:
            raise UnsupportedCapability(
                f"{self.name} does not support tools {sorted(unknown)} "
                f"(supported: {sorted(self.supported_tools)})."
            )

        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        effective_task_tags = tuple(task_tags or ())
        if "tool-use" not in effective_task_tags:
            effective_task_tags = (*effective_task_tags, "tool-use")
        target_payload, selected_model = self._completion_target_payload(
            model=model,
            models=models,
            effort=effort,
            task_tags=effective_task_tags,
            prefer=prefer,
            exclude=exclude,
            log_selection=log_selection,
        )

        workdir = Path(cwd) if cwd else Path.cwd()
        executor = ToolExecutor(cwd=workdir)
        tool_specs = build_tool_specs(tools)

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

        start = time.monotonic()
        while iteration < OPENROUTER_MAX_TOOL_ITERATIONS:
            iteration += 1
            payload: dict = {
                **target_payload,
                "messages": messages,
                "tools": tool_specs,
                "tool_choice": "auto",
            }
            body = self._post_chat(payload)
            final_body = body
            message = _first_message(body)

            usage = body.get("usage") or {}
            prompt_tokens = _usage_int(usage, "prompt_tokens")
            completion_tokens = _usage_int(usage, "completion_tokens")
            cached_tokens = _usage_int(
                usage.get("prompt_tokens_details") or {},
                "cached_tokens",
            )
            thinking_tokens = _usage_int(
                usage.get("completion_tokens_details") or {},
                "reasoning_tokens",
            )
            totals["input_tokens"] += prompt_tokens
            totals["output_tokens"] += completion_tokens
            totals["cached_tokens"] += cached_tokens
            totals["thinking_tokens"] += thinking_tokens
            iterations_log.append(
                {
                    "iteration": iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                    "thinking_tokens": thinking_tokens,
                }
            )

            actual_model = body.get("model")
            if (
                isinstance(actual_model, str)
                and actual_model
                and actual_model != OPENROUTER_DEFAULT_MODEL
            ):
                selected_model = actual_model
                target_payload = {"model": actual_model}
                reasoning = self._reasoning_payload(effort)
                if reasoning is not None:
                    target_payload["reasoning"] = reasoning

            tool_calls = message.get("tool_calls") or []
            final_text = _message_content_text(message)
            if not tool_calls:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": final_text,
                    "tool_calls": tool_calls,
                }
            )

            for idx, call in enumerate(tool_calls):
                name, args, result = _parse_tool_call(call)
                if result is None:
                    try:
                        result = executor.run(name, args or {})
                    except ToolExecutionError as e:
                        result = f"error: {e}"

                if session_log is not None:
                    session_log.emit(
                        "tool_call",
                        {
                            "provider": self.name,
                            "iteration": iteration,
                            "name": name,
                            "args": args,
                            "result_preview": (
                                result[:200] if isinstance(result, str) else result
                            ),
                        },
                    )

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{iteration}_{idx}",
                    "name": name,
                    "content": result,
                }
                messages.append(tool_msg)
        else:
            hit_cap = True

        if hit_cap:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: OpenRouter tool-use loop hit max iterations "
                f"({OPENROUTER_MAX_TOOL_ITERATIONS}); model kept requesting tools. "
                "Re-run with a narrower task or a larger budget.]"
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return CallResponse(
            text=final_text,
            provider=self.name,
            model=final_body.get("model", selected_model) if final_body else selected_model,
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
                "iterations": iterations_log,
            },
            raw=final_body,
        )


def select_model_for_task(
    task_tags: list[str] | tuple[str, ...] | None,
    prefer: str,
    effort: str | int,
    exclude: set[str] | frozenset[str] | None = None,
) -> dict[str, object]:
    """Select an OpenRouter model from the live catalog.

    `prefer=fastest` currently uses price as a latency proxy because the public
    catalog has no latency field. Cheaper models tend to be smaller and faster;
    a future version can swap in measured latency when a reliable source exists.
    """
    if prefer not in {"best", "balanced", "cheapest", "fastest"}:
        raise ProviderError(
            f"OpenRouter selector got unsupported prefer={prefer!r}. "
            "Use best, balanced, cheapest, or fastest."
        )

    task_tag_set = set(task_tags or [])
    exclude_set = set(exclude or ())
    catalog = openrouter_catalog.load_catalog(
        force_refresh=True,
        allow_stale_on_error=False,
    )
    catalog_ids = {entry.id for entry in catalog}
    candidates = []
    dropped = []
    for entry in catalog:
        if entry.id in exclude_set:
            continue
        if {"strong-reasoning", "thinking"} & task_tag_set and not entry.supports_thinking:
            continue
        if "tool-use" in task_tag_set and not entry.supports_tools:
            continue
        if "vision" in task_tag_set and not entry.supports_vision:
            continue
        if "long-context" in task_tag_set and entry.context_length < 100_000:
            continue
        if not _is_sendable_openrouter_model_id(entry.id, catalog_ids):
            dropped.append(entry.id)
            continue
        candidates.append(entry)

    if not candidates:
        raise ProviderError(
            _empty_selector_message(
                task_tags=task_tag_set,
                exclude=exclude_set,
                catalog=catalog,
                dropped=dropped,
            )
        )

    ranked = sorted(candidates, key=_catalog_cost_sort_key)
    if prefer in {"cheapest", "fastest"}:
        return {"model": ranked[0].id, "reasoning": None}

    shortlist = sorted(candidates, key=_catalog_recency_sort_key)[:6]
    return {
        "model": OPENROUTER_DEFAULT_MODEL,
        "plugins": [
            {
                "id": "auto-router",
                "allowed_models": [entry.id for entry in shortlist],
            }
        ],
        "reasoning": _reasoning_payload(effort),
    }


def _catalog_cost_sort_key(entry: openrouter_catalog.ModelEntry) -> tuple[float, int, str]:
    return (entry.total_price_per_1k, -entry.created, entry.id)


def _catalog_recency_sort_key(
    entry: openrouter_catalog.ModelEntry,
) -> tuple[int, float, str]:
    return (-entry.created, entry.total_price_per_1k, entry.id)


def _is_sendable_openrouter_model_id(model_id: str, catalog_ids: set[str]) -> bool:
    """Return whether ``model_id`` is valid for OpenRouter request restrictions.

    OpenRouter exposes ``~provider/family-latest`` pages as moving aliases. Those
    aliases are useful policy labels, but they have caused 404s when sent inside
    the auto-router ``allowed_models`` restriction list. The selector therefore
    only sends concrete catalog IDs, and a request-time catalog refresh drops
    slugs that no longer exist.
    """
    return model_id in catalog_ids and not model_id.startswith("~")


def _empty_selector_message(
    *,
    task_tags: set[str],
    exclude: set[str],
    catalog: list[openrouter_catalog.ModelEntry],
    dropped: list[str],
) -> str:
    catalog_ids = {model.id for model in catalog}
    sendable_examples = [
        entry.id
        for entry in catalog
        if _is_sendable_openrouter_model_id(entry.id, catalog_ids)
    ][:8]
    parts = [
        "OpenRouter selector found no sendable models after catalog validation.",
        f"tags filtered to empty: {sorted(task_tags)}",
        f"excluded models: {sorted(exclude)}",
        "configured provider: openrouter",
        f"catalog models available: {len(catalog)}",
    ]
    if sendable_examples:
        parts.append(f"available model examples: {sendable_examples}")
    if dropped:
        parts.append(f"dropped invalid aliases/stale slugs: {sorted(dropped)[:8]}")
    parts.append(
        "Broaden the request by removing tags such as --tags thinking,long-context, "
        "run `conductor models refresh`, or choose a concrete `--model` from "
        "`conductor models list`."
    )
    return " ".join(parts)


def _reasoning_payload(effort: str | int) -> dict[str, str] | None:
    if not isinstance(effort, str):
        return None
    mapped = _OPENROUTER_REASONING_EFFORTS.get(effort)
    if mapped is None:
        return None
    return {"effort": mapped}


def _log_selector_choice(
    *,
    task_tags: list[str] | tuple[str, ...] | None,
    prefer: str,
    payload: dict[str, object],
) -> None:
    tags_text = ",".join(task_tags or []) or "none"
    if payload["model"] == OPENROUTER_DEFAULT_MODEL:
        plugins = payload.get("plugins") or []
        shortlist = []
        if plugins and isinstance(plugins, list):
            first = plugins[0]
            if isinstance(first, dict):
                shortlist = list(first.get("allowed_models") or [])
        target = f"auto shortlist={shortlist}"
    else:
        target = f"model={payload['model']}"
    sys.stderr.write(
        f"[conductor] openrouter selector: tags={tags_text} prefer={prefer} -> {target}\n"
    )
    sys.stderr.flush()


def _first_message(body: dict) -> dict:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderHTTPError(
            f"OpenRouter response missing choices[0].message: {body!r:.500}"
        ) from e
    if not isinstance(message, dict):
        raise ProviderHTTPError(
            f"OpenRouter response choices[0].message was not an object: {message!r:.500}"
        )
    return message


def _message_content_text(message: dict) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _usage_int(usage: dict, key: str) -> int:
    value = usage.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_tool_call(call: dict) -> tuple[str, dict | None, str | None]:
    fn = call.get("function") or {}
    name = fn.get("name") or ""
    raw_args = fn.get("arguments")
    if isinstance(raw_args, dict):
        return name, raw_args, None
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return (
                name,
                None,
                f"error: tool `{name}` arguments were not valid JSON: {e}",
            )
        if not isinstance(args, dict):
            return (
                name,
                None,
                f"error: tool `{name}` arguments must decode to a JSON object.",
            )
        return name, args, None
    return name, {}, None
