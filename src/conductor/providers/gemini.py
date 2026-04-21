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
from typing import Optional

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)

GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"
GEMINI_REQUEST_TIMEOUT_SEC = 180.0


class GeminiProvider:
    name = "gemini"
    tags = ["long-context", "web-search", "thinking", "cheap"]
    default_model = GEMINI_DEFAULT_MODEL

    def __init__(
        self,
        *,
        cli_command: str = "gemini",
        timeout_sec: float = GEMINI_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def configured(self) -> tuple[bool, Optional[str]]:
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `npm install -g @google/gemini-cli`; "
                "first run will prompt a browser auth."
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
            raise ProviderConfigError(reason or "gemini not configured")

        model = model or self.default_model
        args = [
            self._cli,
            "-p",
            task,
            "-o",
            "json",
            "--approval-mode",
            "plan",
        ]
        if model and model != "auto":
            args.extend(["-m", model])

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
                f"gemini CLI timed out after {self._timeout_sec:.0f}s"
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"gemini exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        # Gemini CLI usually returns JSON but occasionally emits plain text
        # for very short prompts. Fall back to treating stdout as the
        # content when JSON parse fails.
        stdout = result.stdout.strip()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            if not stdout:
                raise ProviderHTTPError("gemini produced empty stdout")
            return CallResponse(
                text=stdout,
                provider=self.name,
                model=model,
                duration_ms=duration_ms,
                usage={
                    "input_tokens": None,
                    "output_tokens": None,
                    "cached_tokens": None,
                },
                raw={"stdout": stdout},
            )

        input_tokens, output_tokens = self._sum_usage(data)
        return CallResponse(
            text=data.get("response", ""),
            provider=self.name,
            model=model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": None,
            },
            raw=data,
        )

    @staticmethod
    def _sum_usage(data: dict) -> tuple[Optional[int], Optional[int]]:
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
