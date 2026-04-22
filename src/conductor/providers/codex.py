"""Codex provider — wraps OpenAI's Codex CLI.

Calls ``codex exec "<prompt>" --json --ephemeral`` as a subprocess. Codex
emits NDJSON events (one JSON object per line); we scan for the
``item.completed`` event that carries the agent message and the
``turn.completed`` event that carries token usage.

Conductor uses the canonical identifier ``codex`` (the CLI's actual name).
Sentinel's existing ``OpenAIProvider`` wraps the same CLI under the
identifier ``openai`` — that drift resolves when Sentinel migrates to call
Conductor instead of implementing its own provider (see
autumn-garage `plans/sentinel-conductor-migration.md`, future).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Optional

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    resolve_effort_tokens,
)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_REQUEST_TIMEOUT_SEC = 180.0

# Map symbolic effort → codex --effort flag value.
# Codex natively exposes minimal|low|medium|high.
_EFFORT_TO_CODEX_FLAG = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",  # codex's ceiling
}


class CodexProvider:
    name = "codex"
    tags = ["strong-reasoning", "code-review", "tool-use"]
    default_model = CODEX_DEFAULT_MODEL

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
        "max": 32_000,
    }
    cost_per_1k_in = 0.010
    cost_per_1k_out = 0.040
    cost_per_1k_thinking = 0.010
    typical_p50_ms = 2000

    def __init__(
        self,
        *,
        cli_command: str = "codex",
        timeout_sec: float = CODEX_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def configured(self) -> tuple[bool, Optional[str]]:
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `npm install -g @openai/codex` and auth with `codex login`."
            )
        return True, None

    def smoke(self) -> tuple[bool, Optional[str]]:
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

    def _parse_ndjson(self, stdout: str) -> tuple[str, Optional[int], Optional[int]]:
        """Parse NDJSON events. Return (content, input_tokens, output_tokens)."""
        content = ""
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    content = item.get("text", "")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                input_tokens = (input_tokens or 0) + (usage.get("input_tokens") or 0)
                output_tokens = (output_tokens or 0) + (usage.get("output_tokens") or 0)
        return content, input_tokens, output_tokens

    def call(
        self,
        task: str,
        model: Optional[str] = None,
        *,
        effort: str | int = "medium",
    ) -> CallResponse:
        return self._run(
            task,
            model=model,
            effort=effort,
            sandbox="read-only",
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
        # Codex has two sandboxes: read-only (no fs writes), workspace-write
        # (edits allowed). "none" is ambiguous in codex; we treat it as
        # read-only since there's no meaningful "no sandbox" in codex exec.
        codex_sandbox = {
            "read-only": "read-only",
            "workspace-write": "workspace-write",
            "none": "read-only",
        }.get(sandbox, "read-only")
        # Tool filtering in codex is sandbox-based, not fine-grained.
        # `tools` is advisory for logging; sandbox does the enforcing.
        return self._run(
            task,
            model=model,
            effort=effort,
            sandbox=codex_sandbox,
            cwd=cwd,
            timeout_sec_override=timeout_sec,
        )

    def _run(
        self,
        task: str,
        *,
        model: Optional[str],
        effort: str | int,
        sandbox: str,
        cwd: Optional[str] = None,
        timeout_sec_override: Optional[float] = None,
    ) -> CallResponse:
        ok, reason = self.configured()
        if not ok:
            raise ProviderConfigError(reason or "codex not configured")

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        codex_effort_flag = (
            _EFFORT_TO_CODEX_FLAG.get(effort) if isinstance(effort, str) else None
        )

        args = [
            self._cli,
            "exec",
            task,
            "--json",
            "--ephemeral",
            "--sandbox",
            sandbox,
        ]
        if codex_effort_flag:
            args.extend(["--effort", codex_effort_flag])

        timeout = timeout_sec_override if timeout_sec_override is not None else self._timeout_sec
        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(
                f"codex CLI timed out after {timeout:.0f}s"
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"codex exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        content, input_tokens, output_tokens = self._parse_ndjson(result.stdout)
        if not content:
            raise ProviderHTTPError(
                f"codex NDJSON stream had no agent_message: {result.stdout[:500]!r}"
            )
        return CallResponse(
            text=content,
            provider=self.name,
            model=model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": None,
                "thinking_tokens": None,  # codex doesn't surface separately today
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            raw={"stdout": result.stdout},
        )
