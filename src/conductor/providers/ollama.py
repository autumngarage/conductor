"""Ollama provider — local LLM via the Ollama HTTP API.

Defaults to ``http://localhost:11434`` (Ollama's standard port). Override via
the ``OLLAMA_BASE_URL`` env var for remote Ollama servers, LM Studio
installations, or any other OpenAI/Ollama-compatible local server.

No auth (local-only by design). This is the second HTTP-shape adapter
(alongside ``kimi``); the other three (claude, codex, gemini) shell out
to their respective CLIs.
"""

from __future__ import annotations

import os
import time

import httpx

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)

OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:14b"
OLLAMA_REQUEST_TIMEOUT_SEC = 180.0


class OllamaProvider:
    name = "ollama"
    tags = ["cheap", "local", "offline", "code-review"]
    default_model = OLLAMA_DEFAULT_MODEL

    # Capability declarations (see interface.py)
    quality_tier = "local"
    # Tool-use lands in Stage 3 (HTTP-side tool-use loop).
    supported_tools: frozenset[str] = frozenset()
    supported_sandboxes: frozenset[str] = frozenset({"none"})
    supports_effort = False  # base ollama models don't expose a thinking dial
    effort_to_thinking: dict[str, int] = {}
    cost_per_1k_in = 0.0
    cost_per_1k_out = 0.0
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 5000  # local inference, CPU/GPU dependent

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_sec: float = OLLAMA_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._base_url_override = base_url
        self._timeout_sec = timeout_sec

    def _base_url(self) -> str:
        return (
            self._base_url_override
            or os.environ.get(OLLAMA_BASE_URL_ENV)
            or OLLAMA_DEFAULT_BASE_URL
        ).rstrip("/")

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
                f"and `ollama pull {self.default_model}`."
            )
        if resp.status_code != 200:
            return False, (
                f"Ollama at {self._base_url()} returned {resp.status_code} — "
                "is the server healthy?"
            )
        return True, None

    def smoke(self) -> tuple[bool, str | None]:
        return self.configured()

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
        installed = {m.get("name") for m in data.get("models") or []}
        if self.default_model in installed:
            return True, None
        pulled = sorted(n for n in installed if n)
        hint = f"pull with `ollama pull {self.default_model}`"
        if pulled:
            hint += f"; locally installed: {', '.join(pulled)}"
        return False, (
            f"default model '{self.default_model}' is not pulled on this daemon. {hint}."
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
        model = model or self.default_model
        url = f"{self._base_url()}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
            "stream": False,
        }

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(url, json=payload)
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
        timeout_sec: int = 300,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "ollama has no session model — each /api/chat call is stateless. "
                "To replay context, prepend the prior turns to `task`."
            )
        if tools:
            raise UnsupportedCapability(
                "ollama.exec() with tools is not supported in v0.2 (HTTP tool-use "
                "loop lands in Stage 3). Router should filter ollama out when "
                f"tools are requested; got tools={sorted(tools)}."
            )
        if sandbox not in ("", "none"):
            raise UnsupportedCapability(
                f"ollama.exec() sandbox={sandbox!r} not meaningful without tool-use."
            )
        return self.call(task, model=model, effort=effort)
