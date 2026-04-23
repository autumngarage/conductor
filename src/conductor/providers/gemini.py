"""Gemini provider — wraps Google's Gemini CLI.

Calls ``gemini -p "<prompt>" -o json --approval-mode plan [-m <model>]`` as a
subprocess and parses the JSON response. ``--approval-mode plan`` keeps the
call read-only — critical because Gemini CLI will otherwise write files
into the current directory on certain prompts.

Token usage lives under ``stats.models.<id>.tokens`` in Gemini's JSON
output; we sum across all model entries since a single call can span
multiple model hops.
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

GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"
GEMINI_REQUEST_TIMEOUT_SEC = 180.0


class GeminiProvider:
    name = "gemini"
    tags = ["long-context", "web-search", "thinking", "cheap", "code-review", "tool-use"]
    default_model = GEMINI_DEFAULT_MODEL

    # Capability declarations (see interface.py)
    quality_tier = "strong"
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    supported_sandboxes = frozenset({"read-only", "workspace-write", "none"})
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 8_000,
        "high": 16_000,
        "max": 32_000,
    }
    cost_per_1k_in = 0.00125
    cost_per_1k_out = 0.005
    cost_per_1k_thinking = 0.005
    typical_p50_ms = 1800

    def __init__(
        self,
        *,
        cli_command: str = "gemini",
        timeout_sec: float = GEMINI_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def configured(self) -> tuple[bool, str | None]:
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `npm install -g @google/gemini-cli`; "
                "first run will prompt a browser auth."
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
            approval_mode="plan",
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
        # Gemini's --approval-mode: "plan" (read-only) vs "yolo" (all writes
        # auto-approved). No finer granularity; tools set is advisory.
        approval_mode = {
            "read-only": "plan",
            "workspace-write": "yolo",
            "none": "plan",
        }.get(sandbox, "plan")
        return self._run(
            task,
            model=model,
            effort=effort,
            approval_mode=approval_mode,
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
        approval_mode: str,
        cwd: str | None = None,
        timeout_sec_override: float | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        ok, reason = self.configured()
        if not ok:
            raise ProviderConfigError(reason or "gemini not configured")

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)

        args = [
            self._cli,
            "-p",
            task,
            "-o",
            "json",
            "--approval-mode",
            approval_mode,
        ]
        if model and model != "auto":
            args.extend(["-m", model])
        if resume_session_id:
            # Gemini's --resume takes either "latest" or a positional index
            # into its own session storage; the session_id we captured may
            # be either a true ID (newer Gemini) or an opaque index. Pass
            # whatever we got — Gemini errors clearly if it can't resolve.
            args.extend(["--resume", resume_session_id])
        # Gemini CLI thinking budget support is evolving; pass via env var as
        # a forward-compatible hook. Ignored by versions that don't read it.
        import os as _os
        env_overrides: dict[str, str] = {}
        if thinking_budget:
            env_overrides["GEMINI_THINKING_BUDGET"] = str(thinking_budget)
        proc_env = {**_os.environ, **env_overrides} if env_overrides else None

        timeout = timeout_sec_override if timeout_sec_override is not None else self._timeout_sec
        start = time.monotonic()
        try:
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
                f"gemini CLI timed out after {timeout:.0f}s"
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"gemini exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        stdout = result.stdout.strip()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            if not stdout:
                raise ProviderHTTPError("gemini produced empty stdout") from None
            return CallResponse(
                text=stdout,
                provider=self.name,
                model=model,
                duration_ms=duration_ms,
                usage={
                    "input_tokens": None,
                    "output_tokens": None,
                    "cached_tokens": None,
                    "thinking_tokens": None,
                    "effort": effort if isinstance(effort, str) else None,
                    "thinking_budget": thinking_budget,
                },
                raw={"stdout": stdout},
            )

        input_tokens, output_tokens = self._sum_usage(data)
        # Gemini's JSON output may carry a session identifier under one of
        # several keys depending on CLI version (`session_id` is the
        # post-0.36 field; older builds used `conversationId` / `chatId`).
        # Best-effort extraction; None is OK when absent.
        session_id = (
            data.get("session_id")
            or data.get("conversation_id")
            or data.get("conversationId")
            or data.get("chat_id")
            or data.get("chatId")
        )
        return CallResponse(
            text=data.get("response", ""),
            provider=self.name,
            model=model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": None,
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            session_id=session_id,
            raw=data,
        )

    @staticmethod
    def _sum_usage(data: dict) -> tuple[int | None, int | None]:
        stats = data.get("stats") or {}
        models = (stats.get("models") or {}).values()
        total_input = 0
        total_output = 0
        saw_any = False
        for entry in models:
            tokens = entry.get("tokens") or {}
            if not tokens:
                continue
            saw_any = True
            total_input += tokens.get("input", 0) or 0
            total_output += tokens.get("candidates", 0) or 0
        if not saw_any:
            return None, None
        return total_input, total_output
