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

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    resolve_effort_tokens,
)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_REQUEST_TIMEOUT_SEC = 180.0

# Map symbolic effort → codex's reasoning-effort value.
# Codex natively exposes minimal|low|medium|high. The CLI plumbs this via
# `-c model_reasoning_effort=<value>` as of codex-cli 0.125.0 (the older
# `--effort` flag was removed in that release). We always emit the new
# form; users on pre-0.125.0 codex will need to upgrade.
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
    # GPT-5-codex ships 400K context via the `codex` CLI.
    max_context_tokens = 400_000

    # User-facing login command surfaced in error messages and the wizard.
    auth_login_command = "codex login"

    def __init__(
        self,
        *,
        cli_command: str = "codex",
        timeout_sec: float = CODEX_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec

    def _check_cli_path(self) -> tuple[bool, str | None]:
        """Cheap PATH-only check (no subprocess) for the call/exec hot path."""
        if not shutil.which(self._cli):
            return False, (
                f"`{self._cli}` CLI not found on PATH. "
                "Install with `npm install -g @openai/codex` "
                f"and auth with `{self.auth_login_command}` "
                "(or set `OPENAI_API_KEY` for non-interactive use)."
            )
        return True, None

    def _auth_probe(self) -> tuple[bool, str | None]:
        """Verify auth via `codex login status` (exit 0 = logged in)."""
        try:
            result = subprocess.run(
                [self._cli, "login", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, (
                f"could not verify `{self._cli}` auth status: {e}. "
                "Update the CLI and retry, or set `OPENAI_API_KEY` "
                "for non-interactive use."
            )
        if result.returncode == 0:
            return True, None
        return False, (
            "not authenticated. "
            f"Run `{self.auth_login_command}` to log in via browser, "
            f"`{self._cli} login --device-auth` for headless flow, "
            f"or `printenv OPENAI_API_KEY | {self._cli} login --with-api-key` "
            "for non-interactive use."
        )

    def configured(self) -> tuple[bool, str | None]:
        ok, reason = self._check_cli_path()
        if not ok:
            return False, reason
        return self._auth_probe()

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

    def _parse_ndjson(
        self, stdout: str
    ) -> tuple[str, int | None, int | None, str | None]:
        """Parse NDJSON events.

        Return (content, input_tokens, output_tokens, session_id).
        Codex emits a ``session.created`` event near the start with the
        session UUID; we capture it for resume support. The session ID
        survives even after ``--ephemeral`` runs (the flag controls
        persistence to ``~/.codex/sessions/`` for interactive resume,
        but the in-band ID is always emitted).
        """
        content = ""
        input_tokens: int | None = None
        output_tokens: int | None = None
        session_id: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "session.created":
                session_id = event.get("session_id") or event.get("id")
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    content = item.get("text", "")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                input_tokens = (input_tokens or 0) + (usage.get("input_tokens") or 0)
                output_tokens = (output_tokens or 0) + (usage.get("output_tokens") or 0)
        return content, input_tokens, output_tokens, session_id

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
            sandbox="read-only",
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
            resume_session_id=resume_session_id,
        )

    def _run(
        self,
        task: str,
        *,
        model: str | None,
        effort: str | int,
        sandbox: str,
        cwd: str | None = None,
        timeout_sec_override: float | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        # Cheap PATH check on the hot path; auth state surfaces as a CLI
        # exit failure below if needed. configured() (with auth probe) is
        # the entry point that doctor/list/wizard call.
        ok, reason = self._check_cli_path()
        if not ok:
            raise ProviderConfigError(reason or "codex not configured")

        model = model or self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        codex_effort_flag = (
            _EFFORT_TO_CODEX_FLAG.get(effort) if isinstance(effort, str) else None
        )

        # Codex resume uses a subcommand: `codex exec resume <id> "<prompt>"`.
        # Build argv accordingly when we have a session to resume.
        if resume_session_id:
            args = [
                self._cli,
                "exec",
                "resume",
                resume_session_id,
                task,
                "--json",
                "--sandbox",
                sandbox,
            ]
        else:
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
            args.extend(["-c", f"model_reasoning_effort={codex_effort_flag}"])

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

        content, input_tokens, output_tokens, session_id = self._parse_ndjson(
            result.stdout
        )
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
            session_id=session_id,
            raw={"stdout": result.stdout},
        )
