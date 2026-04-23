"""Claude provider — wraps the Claude Code CLI.

Calls ``claude -p "<prompt>" --output-format json`` as a subprocess and parses
the returned JSON. The user is expected to have authed via ``claude login``;
Conductor never touches the API key.

Shape shared with codex/gemini: a subprocess adapter that delegates auth to
the wrapped CLI. Compare to ``kimi``/``ollama``, which are HTTP adapters
that touch credentials directly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    resolve_effort_tokens,
)

CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_REQUEST_TIMEOUT_SEC = 180.0


class ClaudeProvider:
    name = "claude"
    tags = ["strong-reasoning", "long-context", "tool-use", "code-review"]
    default_model = CLAUDE_DEFAULT_MODEL

    # Capability declarations (see interface.py)
    quality_tier = "frontier"
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    supported_sandboxes = frozenset({"read-only", "workspace-write", "none"})
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 8_000,
        "high": 24_000,
        "max": 64_000,
    }
    # Pricing for claude sonnet as of 2026-04; maintained alongside tier.
    cost_per_1k_in = 0.003
    cost_per_1k_out = 0.015
    cost_per_1k_thinking = 0.003
    typical_p50_ms = 2500

    def __init__(
        self,
        *,
        cli_command: str = "claude",
        timeout_sec: float = CLAUDE_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def configured(self) -> tuple[bool, str | None]:
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `brew install claude` and auth with `claude login`."
            )
        return True, None

    def smoke(self) -> tuple[bool, str | None]:
        ok, reason = self.configured()
        if not ok:
            return False, reason
        try:
            result = subprocess.run(
                [self._cli, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return False, f"`{self._cli} --version` timed out"
        if result.returncode != 0:
            return False, (
                f"`{self._cli} --version` exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:200]}"
            )
        return True, None

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        resume_session_id: str | None = None,
    ) -> CallResponse:
        return self._run(
            task,
            model=model,
            effort=effort,
            allowed_tools=None,
            permission_mode=None,
            resume_session_id=resume_session_id,
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
        # Claude's `--allowedTools` is fine-grained; passing an empty set
        # is effectively "no tools permitted" (single-turn).
        allowed = ",".join(sorted(tools)) if tools else None
        # Sandbox to claude's permission model:
        #   read-only       → "plan" (no writes, no bash effects)
        #   workspace-write → "acceptEdits" (file edits auto-accepted, bash requires accept)
        #   none            → None (default interactive permissions)
        permission_mode = {
            "read-only": "plan",
            "workspace-write": "acceptEdits",
            "none": None,
        }.get(sandbox)
        return self._run(
            task,
            model=model,
            effort=effort,
            allowed_tools=allowed,
            permission_mode=permission_mode,
            cwd=cwd,
            timeout_sec_override=timeout_sec,
            resume_session_id=resume_session_id,
        )

    def _run(
        self,
        task: str,
        *,
        model: str | None,
        effort: str | int,
        allowed_tools: str | None,
        permission_mode: str | None,
        cwd: str | None = None,
        timeout_sec_override: float | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        ok, reason = self.configured()
        if not ok:
            raise ProviderConfigError(reason or "claude not configured")

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)

        args = [
            self._cli,
            "-p",
            task,
            "--output-format",
            "json",
            "--model",
            model,
        ]
        if allowed_tools is not None:
            args.extend(["--allowedTools", allowed_tools])
        if permission_mode is not None:
            args.extend(["--permission-mode", permission_mode])
        if resume_session_id:
            # Claude Code resumes a prior session via UUID. The previous
            # CallResponse.session_id is the canonical handle; the new
            # prompt layers on top of the existing conversation.
            args.extend(["--resume", resume_session_id])
        # NOTE: Claude CLI's exact flag for thinking budget is version-dependent;
        # we pass via the MAX_THINKING_TOKENS env var (safe fallback: ignored
        # by older CLI versions). Wire to a proper CLI flag when stable.
        env_overrides = {"MAX_THINKING_TOKENS": str(thinking_budget)} if thinking_budget else {}

        timeout = timeout_sec_override if timeout_sec_override is not None else self._timeout_sec
        start = time.monotonic()
        try:
            import os as _os
            proc_env = {**_os.environ, **env_overrides} if env_overrides else None
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=proc_env,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(
                f"claude CLI timed out after {timeout:.0f}s"
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"claude exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ProviderHTTPError(
                f"claude stdout was not JSON: {result.stdout[:500]!r}"
            ) from e

        if data.get("is_error"):
            raise ProviderHTTPError(
                f"claude returned is_error=true: {data.get('result', '<no detail>')}"
            )

        usage = data.get("usage") or {}
        return CallResponse(
            text=data.get("result", ""),
            provider=self.name,
            model=model,
            duration_ms=data.get("duration_ms") or duration_ms,
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cached_tokens": usage.get("cache_read_input_tokens"),
                "thinking_tokens": usage.get("thinking_tokens"),
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            cost_usd=data.get("total_cost_usd"),
            session_id=data.get("session_id"),
            raw=data,
        )
