"""OpenRouter provider — OpenAI-compatible HTTP adapter.

PR 1 scope: a single-turn adapter that lets callers target OpenRouter
explicitly via ``--with openrouter --model <slug>``. Auto-mode selection,
catalog-driven model discovery, and tool-use orchestration are deferred to
the follow-up migration PRs.
"""

from __future__ import annotations

import sys
import time
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

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

from . import openrouter_catalog

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_REQUEST_TIMEOUT_SEC = 120.0
OPENROUTER_HEALTH_PROBE_TIMEOUT_SEC = 10.0
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

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
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
        payload: dict = {
            "messages": [{"role": "user", "content": task}],
        }
        selected_model = model
        preset_model = self._preset_model() if selected_model is None else None
        if preset_model is not None:
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
        return self.call(
            task,
            model=model,
            effort=effort,
            task_tags=task_tags,
            prefer=prefer,
            exclude=exclude,
            log_selection=log_selection,
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
    candidates = []
    for entry in openrouter_catalog.load_catalog():
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
        candidates.append(entry)

    if not candidates:
        raise ProviderError(
            "OpenRouter selector found no models matching "
            f"tags={sorted(task_tag_set)} exclude={sorted(exclude_set)}. "
            "Run `conductor models refresh` or choose `--model` explicitly."
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
