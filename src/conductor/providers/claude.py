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
from typing import Optional

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)

CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_REQUEST_TIMEOUT_SEC = 180.0


class ClaudeProvider:
    name = "claude"
    tags = ["strong-reasoning", "long-context", "tool-use", "code-review"]
    default_model = CLAUDE_DEFAULT_MODEL

    def __init__(
        self,
        *,
        cli_command: str = "claude",
        timeout_sec: float = CLAUDE_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def configured(self) -> tuple[bool, Optional[str]]:
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `brew install claude` and auth with `claude login`."
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

    def call(self, task: str, model: Optional[str] = None) -> CallResponse:
        ok, reason = self.configured()
        if not ok:
            raise ProviderConfigError(reason or "claude not configured")

        model = model or self.default_model
        args = [
            self._cli,
            "-p",
            task,
            "--output-format",
            "json",
            "--model",
            model,
        ]
        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            raise ProviderError(
                f"claude CLI timed out after {self._timeout_sec:.0f}s"
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
            },
            cost_usd=data.get("total_cost_usd"),
            raw=data,
        )
