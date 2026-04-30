"""Ollama provider — local LLM via the Ollama HTTP API.

Defaults to ``http://localhost:11434`` (Ollama's standard port). Override via
the ``OLLAMA_BASE_URL`` env var for remote Ollama servers, LM Studio
installations, or any other OpenAI/Ollama-compatible local server. The model
defaults to ``CONDUCTOR_OLLAMA_MODEL`` when set, otherwise the baked-in
``OLLAMA_DEFAULT_MODEL``.

No auth (local-only by design). This is the second HTTP-shape adapter
(alongside ``kimi``); the other three (claude, codex, gemini) shell out
to their respective CLIs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)

if TYPE_CHECKING:
    from conductor.session_log import SessionLog
from conductor.tools import (
    ToolExecutionError,
    ToolExecutor,
    build_tool_specs,
)

OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL_ENV = "CONDUCTOR_OLLAMA_MODEL"
# Default bumped 2026-04-24 from qwen2.5-coder:14b to qwen3.6:35b-a3b after
# dogfood: qwen2.5-coder emits tool calls as markdown-JSON inside content
# rather than as structured tool_calls (silent-fail for exec() with tools),
# and qwen3.6:35b-a3b is a MoE that runs ~7x faster than the 27b dense on
# low-memory-headroom Macs despite the larger total parameter count.
OLLAMA_DEFAULT_MODEL = "qwen3.6:35b-a3b"
# Bumped 2026-04-24 from 180s → 600s. Agent-loop dogfood on an M4 Max 36GB
# showed multi-turn tool-use loops with growing context routinely take
# 5-15 minutes end-to-end — even for fast MoE models — and each iteration
# can approach 180s when the prompt grows. Override per-request via the
# CONDUCTOR_OLLAMA_TIMEOUT_SEC env var.
OLLAMA_TIMEOUT_ENV = "CONDUCTOR_OLLAMA_TIMEOUT_SEC"
OLLAMA_REQUEST_TIMEOUT_SEC = 600.0
OLLAMA_MAX_TOOL_ITERATIONS = 10
OLLAMA_CONTEXT_SAFETY_MARGIN = 0.1
OLLAMA_MODEL_SELECTION_PATTERNS: tuple[str, ...] = (
    # Prefer local models that are known to be useful for Conductor's usual
    # offline delegation workload: coding, review, and tool-capable reasoning.
    "qwen3.6",
    "qwen3.5",
    "qwen3",
    "qwen2.5-coder",
    "deepseek-coder",
    "codestral",
    "coder",
    "code",
    "llama3.2",
    "llama3.1",
    "llama3",
)
OLLAMA_TOOL_MODEL_SELECTION_PATTERNS: tuple[str, ...] = (
    # qwen2.5-coder is useful for plain chat but has emitted markdown JSON
    # instead of structured tool_calls in dogfood, so tool-mode prefers known
    # chat models before falling back to it.
    "qwen3.6",
    "qwen3.5",
    "qwen3",
    "llama3.2",
    "llama3.1",
    "llama3",
    "deepseek-coder",
    "codestral",
    "coder",
    "code",
    "qwen2.5-coder",
)
OLLAMA_EMBEDDING_MODEL_PATTERNS: tuple[str, ...] = (
    "embed",
    "bge-",
    "e5-",
)


def _find_stealth_tool_call(content: str, tool_names: frozenset[str]) -> str | None:
    """Return the tool name if ``content`` contains a tool-call-shaped JSON
    block for one of ``tool_names``, otherwise None.

    The heuristic looks for a JSON object with a ``name`` field whose
    value is in ``tool_names`` — the shape every qwen-family model emits
    when its chat template fails to convert the call into a structured
    ``tool_calls`` field. Covers fenced (```json```) and bare JSON blocks.
    Deliberately cheap — one regex, one json.loads attempt per candidate;
    false positives here are not harmful because the diagnostic simply
    warns that the loop returned without executing tools.
    """
    if not content or not tool_names:
        return None
    import re

    # Look for JSON-like blocks: fenced markdown first, then any balanced
    # {...} span that happens to hold a "name" key. Nested braces (e.g.
    # {"arguments": {"path": "x"}}) defeat a simple [^{}]* match, so we
    # scan for candidate starts and walk a depth counter to find each
    # span's end. Keeps us regex-free for the nested case.
    candidates: list[str] = []
    for match in re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL
    ):
        candidates.append(match.group(1))
    if not candidates:
        for start in range(len(content)):
            if content[start] != "{":
                continue
            depth = 0
            for end in range(start, len(content)):
                ch = content[end]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        span = content[start : end + 1]
                        if '"name"' in span:
                            candidates.append(span)
                        break
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name") in tool_names:
            return obj["name"]
    return None


def _resolve_default_timeout() -> float:
    """Pick the per-request HTTP timeout at instance-init time.

    Env-override flow: ``CONDUCTOR_OLLAMA_TIMEOUT_SEC`` wins if set and
    parseable as a positive float; otherwise fall back to the baked-in
    default. Invalid values fall through rather than raising so a typo
    doesn't brick the provider.
    """
    raw = os.environ.get(OLLAMA_TIMEOUT_ENV)
    if raw:
        try:
            override = float(raw)
            if override > 0:
                return override
        except ValueError:
            pass
    return OLLAMA_REQUEST_TIMEOUT_SEC


def _clean_model_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    model = raw.strip()
    return model or None


def _model_preference_index(name: str, *, tools: bool) -> int:
    normalized = name.lower()
    patterns = (
        OLLAMA_TOOL_MODEL_SELECTION_PATTERNS
        if tools
        else OLLAMA_MODEL_SELECTION_PATTERNS
    )
    for index, pattern in enumerate(patterns):
        if pattern in normalized:
            return index
    return len(patterns)


def _is_missing_model_response(resp: httpx.Response) -> bool:
    if resp.status_code not in {400, 404}:
        return False
    text = resp.text.lower()
    return "model" in text and any(
        fragment in text
        for fragment in ("not found", "not installed", "pull", "does not exist")
    )


class OllamaProvider:
    name = "ollama"
    tags = ["cheap", "local", "offline", "code-review"]
    default_model = OLLAMA_DEFAULT_MODEL

    # Capability declarations (see interface.py)
    quality_tier = "local"
    # Tool-use runs through Conductor's HTTP tool-use loop.
    supported_tools: frozenset[str] = frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    supports_effort = False  # base ollama models don't expose a thinking dial
    effort_to_thinking: dict[str, int] = {}
    cost_per_1k_in = 0.0
    cost_per_1k_out = 0.0
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 5000  # local inference, CPU/GPU dependent
    # Conservative floor — qwen2.5-coder ships 32K; many base models run
    # 4-8K. Users on a long-context local model can override per-request.
    max_context_tokens = 32_000

    # One-liner shown under the failure reason in `conductor list`.
    fix_command = "brew install ollama && ollama serve"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        self._base_url_override = base_url
        # Resolve the timeout once per instance: explicit kwarg wins,
        # then the CONDUCTOR_OLLAMA_TIMEOUT_SEC env var, then the default.
        self._timeout_sec = (
            timeout_sec if timeout_sec is not None else _resolve_default_timeout()
        )

    def _base_url(self) -> str:
        return (
            self._base_url_override
            or os.environ.get(OLLAMA_BASE_URL_ENV)
            or OLLAMA_DEFAULT_BASE_URL
        ).rstrip("/")

    def resolved_default_model(self) -> str:
        """Return the host-local default model Conductor will send to Ollama."""
        return _clean_model_name(os.environ.get(OLLAMA_MODEL_ENV)) or self.default_model

    def _installed_model_names(self, client: httpx.Client) -> list[str]:
        resp = client.get(f"{self._base_url()}/api/tags")
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderHTTPError(
                "Ollama /api/tags response was not JSON while selecting "
                f"a fallback model: {e}"
            ) from e
        if not isinstance(data, dict):
            return []
        names: list[str] = []
        for entry in data.get("models") or []:
            if not isinstance(entry, dict):
                continue
            name = _clean_model_name(entry.get("name") or entry.get("model"))
            if name:
                names.append(name)
        return names

    def _select_installed_model(
        self,
        names: list[str],
        *,
        tools: bool,
        attempted: str,
    ) -> str | None:
        candidates = [name for name in names if name != attempted]
        candidates = [
            name
            for name in candidates
            if not any(
                pattern in name.lower()
                for pattern in OLLAMA_EMBEDDING_MODEL_PATTERNS
            )
        ]
        if not candidates:
            return None

        def key(item: tuple[int, str]) -> tuple[int, int]:
            index, name = item
            return (-_model_preference_index(name, tools=tools), -index)

        return max(enumerate(candidates), key=key)[1]

    def _post_chat_with_model_fallback(
        self,
        client: httpx.Client,
        url: str,
        payload: dict,
        *,
        explicit_model: bool,
        tools: bool,
    ) -> httpx.Response:
        """POST /api/chat, retrying once with an installed local model.

        The fallback is deliberately narrow: only implicit defaults are
        substitutable, and only when Ollama says the requested model is
        missing. An explicit --model is an exact user request and should fail
        loudly rather than being silently replaced.
        """
        resp = client.post(url, json=payload)
        if (
            resp.status_code == 200
            or explicit_model
            or not _is_missing_model_response(resp)
        ):
            return resp

        selected = self._select_installed_model(
            self._installed_model_names(client),
            tools=tools,
            attempted=str(payload.get("model") or ""),
        )
        if selected is None:
            return resp

        payload["model"] = selected
        return client.post(url, json=payload)

    def configured(self) -> tuple[bool, str | None]:
        # "Configured" here means the server responds. Ollama has no auth,
        # so there's nothing to check for credentials — the liveness of the
        # endpoint is the only meaningful signal.
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self._base_url()}/api/tags")
        except httpx.HTTPError as e:
            return False, (
                f"cannot reach Ollama at {self._base_url()}: {e}. "
                "Install with `brew install ollama`, run `ollama serve`, "
                f"and `ollama pull {self.resolved_default_model()}`."
            )
        if resp.status_code != 200:
            return False, (
                f"Ollama at {self._base_url()} returned {resp.status_code} — "
                "is the server healthy?"
            )
        return True, None

    def smoke(self) -> tuple[bool, str | None]:
        return self.configured()

    def health_probe(self, *, timeout_sec: float = 10.0) -> tuple[bool, str | None]:
        url = f"{self._base_url()}/api/tags"
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.get(url)
        except httpx.TimeoutException:
            return False, f"`GET {url}` timed out after {timeout_sec:.0f}s"
        except httpx.HTTPError as e:
            return False, (
                f"cannot reach Ollama at {self._base_url()}: {e}. "
                "Install with `brew install ollama`, run `ollama serve`, "
                f"and `ollama pull {self.resolved_default_model()}`."
            )
        if resp.status_code != 200:
            return False, (
                f"Ollama at {self._base_url()} returned {resp.status_code} — "
                "is the server healthy?"
            )
        return True, None

    def default_model_available(self) -> tuple[bool, str | None]:
        """Check whether ``default_model`` is pulled on the running daemon.

        Returns (True, None) when the model is present, (False, reason)
        otherwise. The daemon must be reachable — callers should only run
        this after ``configured()`` returns True. Returns (False, reason)
        if the API call fails, so the caller can warn without crashing.
        """
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self._base_url()}/api/tags")
        except httpx.HTTPError as e:
            return False, f"cannot query {self._base_url()}/api/tags: {e}"
        if resp.status_code != 200:
            return False, f"{self._base_url()}/api/tags returned {resp.status_code}"
        try:
            data = resp.json()
        except ValueError as e:
            return False, f"/api/tags response was not JSON: {e}"
        default_model = self.resolved_default_model()
        installed = {
            m.get("name") or m.get("model") for m in data.get("models") or []
        }
        if default_model in installed:
            return True, None
        pulled = sorted(n for n in installed if n)
        hint = f"pull with `ollama pull {default_model}`"
        if _clean_model_name(os.environ.get(OLLAMA_MODEL_ENV)):
            hint += f", change {OLLAMA_MODEL_ENV}, or pass --model"
        if pulled:
            hint += f"; locally installed: {', '.join(pulled)}"
            hint += (
                "; calls without --model will retry once with a suitable "
                "installed local model"
            )
        return False, (
            f"default model '{default_model}' is not pulled on this daemon. {hint}."
        )

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
                "ollama has no session model — each /api/chat call is stateless. "
                "To replay context, prepend the prior turns to `task`."
            )
        # Effort is a silent no-op here — base ollama models don't expose a
        # thinking dial. Tag noted in usage for observability.
        explicit_model = model is not None
        model = model or self.resolved_default_model()
        url = f"{self._base_url()}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
            "stream": False,
        }

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = self._post_chat_with_model_fallback(
                    client,
                    url,
                    payload,
                    explicit_model=explicit_model,
                    tools=False,
                )
                model = str(payload.get("model") or model)
        except httpx.HTTPError as e:
            raise ProviderConfigError(
                f"cannot reach Ollama at {self._base_url()}: {e}"
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            raise ProviderHTTPError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"Ollama response was not JSON: {e}") from e

        message = data.get("message") or {}
        text = message.get("content")
        if text is None:
            raise ProviderHTTPError(
                f"Ollama response missing message.content: {data!r:.500}"
            )

        # total_duration is reported in nanoseconds; normalize to ms.
        server_duration_ms = (data.get("total_duration") or 0) // 1_000_000 or duration_ms
        return CallResponse(
            text=text,
            provider=self.name,
            model=data.get("model", model),
            duration_ms=server_duration_ms,
            usage={
                "input_tokens": data.get("prompt_eval_count"),
                "output_tokens": data.get("eval_count"),
                "cached_tokens": None,
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": 0,  # ollama doesn't support effort
            },
            raw=data,
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
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        # accepted for API parity; only codex implements stall-watchdog today
        if resume_session_id:
            raise UnsupportedCapability(
                "ollama has no session model — each /api/chat call is stateless. "
                "To replay context, prepend the prior turns to `task`."
            )
        if not tools:
            return self.call(task, model=model, effort=effort)

        unknown = tools - self.supported_tools
        if unknown:
            raise UnsupportedCapability(
                f"ollama does not support tools {sorted(unknown)} "
                f"(supported: {sorted(self.supported_tools)})."
            )

        workdir = Path(cwd) if cwd else Path.cwd()
        executor = ToolExecutor(cwd=workdir)
        tool_specs = build_tool_specs(tools)

        explicit_model = model is not None
        model = model or self.resolved_default_model()
        url = f"{self._base_url()}/api/chat"

        messages: list[dict] = [{"role": "user", "content": task}]
        iteration = 0
        totals = {"input_tokens": 0, "output_tokens": 0}
        iterations_log: list[dict] = []
        final_text = ""
        final_body: dict = {}
        hit_cap = False
        hit_context_budget = False
        prompt_tokens = 0
        context_ceiling = int(
            self.max_context_tokens * (1 - OLLAMA_CONTEXT_SAFETY_MARGIN)
        )

        start = time.monotonic()
        while iteration < OLLAMA_MAX_TOOL_ITERATIONS:
            iteration += 1
            payload: dict = {
                "model": model,
                "messages": messages,
                "tools": tool_specs,
                "stream": False,
            }

            try:
                with httpx.Client(timeout=self._timeout_sec) as client:
                    resp = self._post_chat_with_model_fallback(
                        client,
                        url,
                        payload,
                        explicit_model=explicit_model,
                        tools=bool(tools),
                    )
                    model = str(payload.get("model") or model)
            except httpx.HTTPError as e:
                raise ProviderConfigError(
                    f"cannot reach Ollama at {self._base_url()}: {e}"
                ) from e

            if resp.status_code != 200:
                raise ProviderHTTPError(
                    f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
                )
            try:
                body = resp.json()
            except ValueError as e:
                raise ProviderHTTPError(f"Ollama response was not JSON: {e}") from e
            final_body = body

            message = body.get("message") or {}
            prompt_tokens = int(body.get("prompt_eval_count") or 0)
            completion_tokens = int(body.get("eval_count") or 0)
            totals["input_tokens"] += prompt_tokens
            totals["output_tokens"] += completion_tokens
            iterations_log.append(
                {
                    "iteration": iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": 0.0,  # ollama is free
                }
            )

            tool_calls = message.get("tool_calls") or []
            final_text = message.get("content") or ""

            if not tool_calls:
                # Silent-fail guard: some model+template combinations
                # (notably qwen2.5-coder:* in ollama) emit the tool call
                # as a markdown-JSON block inside `content` instead of
                # populating the `tool_calls` field. Without this check
                # the loop would silently return the model's prose as if
                # it were the answer — and that prose is usually a
                # hallucination since no tool actually ran.
                stealth = _find_stealth_tool_call(final_text, tools)
                if stealth:
                    final_text = (
                        "[conductor: the model "
                        f"({model}) returned a tool-call-shaped JSON block "
                        f"for `{stealth}` inside message.content instead of "
                        "the structured tool_calls field. No tool was "
                        "actually executed; the response below is the "
                        "model's unaided prose. Try a model with proper "
                        "tool-use support (e.g. qwen3.5+ or qwen3.6+, "
                        "llama3.1+) — qwen2.5-coder is known to have this "
                        "issue in ollama.]\n\n"
                    ) + final_text
                break

            if prompt_tokens >= context_ceiling:
                hit_context_budget = True
                break

            assistant_entry: dict = {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
            messages.append(assistant_entry)

            for idx, call in enumerate(tool_calls):
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments")
                # Ollama emits arguments as a dict (not a string like OpenAI).
                # Handle both shapes so mocks written either way still work.
                if isinstance(raw_args, dict):
                    args = raw_args
                    result = None
                elif isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                        result = None
                    except json.JSONDecodeError as e:
                        args = None
                        result = (
                            f"error: tool `{name}` arguments were not valid "
                            f"JSON: {e}"
                        )
                else:
                    args = {}
                    result = None

                if args is not None:
                    try:
                        result = executor.run(name, args)
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

                # Ollama does not always emit `id`; fall back to a synthesized one.
                tool_msg: dict = {
                    "role": "tool",
                    "name": name,
                    "content": result,
                }
                call_id = call.get("id")
                if call_id:
                    tool_msg["tool_call_id"] = call_id
                else:
                    tool_msg["tool_call_id"] = f"call_{iteration}_{idx}"
                messages.append(tool_msg)
        else:
            hit_cap = True

        if hit_cap:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: tool-use loop hit max iterations "
                f"({OLLAMA_MAX_TOOL_ITERATIONS}); model kept requesting tools. "
                "Re-run with a narrower task or a larger budget.]"
            )
        if hit_context_budget:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: context budget exhausted "
                f"({prompt_tokens}/{self.max_context_tokens} tokens used, "
                f"safety margin {int(OLLAMA_CONTEXT_SAFETY_MARGIN * 100)}%). "
                "Re-run with a narrower task, a larger-context local model, "
                "or override max_context_tokens.]"
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
                "cached_tokens": None,
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": 0,
                "tool_iterations": iteration,
                "tool_names": sorted(tools),
                "hit_iteration_cap": hit_cap,
                "hit_context_budget": hit_context_budget,
                "iterations": iterations_log,
            },
            raw=final_body,
        )
