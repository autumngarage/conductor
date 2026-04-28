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
import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from conductor.providers.cli_auth import AuthPromptTracker, run_subprocess_with_live_stderr
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    resolve_effort_tokens,
)

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_REQUEST_TIMEOUT_SEC = 180.0
CLAUDE_CLI_ENV = "CONDUCTOR_CLAUDE_CLI"

# Sentinel: "caller didn't specify a timeout" vs "caller explicitly passed
# None". The constructor default applies only in the first case.
_USE_DEFAULT: object = object()


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
    # Claude Sonnet 4.6 ships 1M context via the CLI's long-context mode.
    max_context_tokens = 1_000_000

    # User-facing login command surfaced in error messages and the init
    # wizard. Note: top-level `claude --help` doesn't list `auth` as a
    # subcommand, but `claude auth login` is the actual non-interactive
    # entry point (the slash variant `/login` only works inside the REPL).
    auth_login_command = "claude auth login"

    # One-liner shown under the failure reason in `conductor list`.
    fix_command = "brew install claude && claude auth login"

    def __init__(
        self,
        *,
        cli_command: str | None = None,
        timeout_sec: float = CLAUDE_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLAUDE_CLI_ENV) or "claude"
        self._timeout_sec = timeout_sec

    def _check_cli_path(self) -> tuple[bool, str | None]:
        """Cheap PATH-only check (no subprocess). Used by call()/exec()
        for the defensive guard so the hot path doesn't take an auth-probe
        round-trip on every invocation."""
        if not shutil.which(self._cli):
            configured_cli = os.environ.get(CLAUDE_CLI_ENV)
            if configured_cli:
                return False, (
                    f"{CLAUDE_CLI_ENV}={configured_cli!r} does not point to an "
                    "executable visible to this Conductor process. Set it to the "
                    "absolute Claude CLI path, update PATH for the non-interactive "
                    "agent environment, or install/auth with "
                    f"`brew install claude && {self.auth_login_command}` "
                    "(or set `ANTHROPIC_API_KEY` for non-interactive use)."
                )
            return False, (
                f"`{self._cli}` CLI not found on PATH for this Conductor process. "
                "If Claude works in your terminal, set "
                f"`{CLAUDE_CLI_ENV}=/absolute/path/to/claude` or update PATH for "
                "the non-interactive agent environment. Otherwise install with "
                "`brew install claude` and auth with "
                f"`{self.auth_login_command}` "
                "(or set `ANTHROPIC_API_KEY` for non-interactive use)."
            )
        return True, None

    def _auth_probe(self) -> tuple[bool, str | None]:
        """Verify the user is authenticated.

        Calls ``claude auth status --json``, which exits 0 in BOTH the
        authed and unauthed cases — the JSON body's ``loggedIn`` field
        is the canonical signal. Returns a structured failure reason for
        every error mode (timeout, non-JSON, missing field, loggedIn=false)
        so doctor/wizard can render a useful next step.
        """
        try:
            result = subprocess.run(
                [self._cli, "auth", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, (
                f"could not verify `{self._cli}` auth status: {e}. "
                "Update the CLI (`brew upgrade claude`) and re-run, "
                "or set `ANTHROPIC_API_KEY` for non-interactive use."
            )
        if result.returncode != 0:
            return False, (
                f"`{self._cli} auth status` exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:200]}. "
                "Older CLIs may lack the `auth` subcommand; "
                "`brew upgrade claude` and retry."
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False, (
                f"`{self._cli} auth status --json` output was not JSON: "
                f"{result.stdout[:200]!r}"
            )
        if data.get("loggedIn"):
            return True, None
        return False, (
            "not authenticated. "
            f"Run `{self.auth_login_command}` to log in via browser, "
            "`claude setup-token` for a long-lived token (subscription req'd), "
            "or set `ANTHROPIC_API_KEY` for non-interactive use."
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

    def health_probe(self, *, timeout_sec: float = 30.0) -> tuple[bool, str | None]:
        ok, reason = self._check_cli_path()
        if not ok:
            return False, reason
        try:
            result = subprocess.run(
                [self._cli, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return False, f"`{self._cli} --version` timed out after {timeout_sec:.0f}s"
        except OSError as e:
            return False, f"could not run `{self._cli} --version`: {e}"
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
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        # accepted for API parity; only codex implements stall-watchdog today
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
            live_auth_capture=True,
            session_log=session_log,
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
        timeout_sec_override: float | None | object = _USE_DEFAULT,
        resume_session_id: str | None = None,
        live_auth_capture: bool = False,
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        # Cheap PATH check only on the hot path — auth state surfaces as a
        # CLI exit failure below if the user installed but didn't log in.
        # `configured()` (with the auth probe) is the entry point that
        # `doctor`/`list`/wizard call.
        ok, reason = self._check_cli_path()
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

        if timeout_sec_override is _USE_DEFAULT:
            timeout = self._timeout_sec
        else:
            timeout = timeout_sec_override  # type: ignore[assignment]
        start = time.monotonic()
        tracker = AuthPromptTracker(self.name, session_log=session_log)
        try:
            import os as _os
            proc_env = {**_os.environ, **env_overrides} if env_overrides else None
            if live_auth_capture:
                result = run_subprocess_with_live_stderr(
                    args=args,
                    cwd=cwd,
                    env=proc_env,
                    timeout=timeout,
                    tracker=tracker,
                    popen_factory=subprocess.Popen,
                )
            else:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=proc_env,
                )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            raise ProviderError(
                f"claude CLI timed out after {elapsed:.0f}s"
            ) from e
        duration_ms = (
            result.duration_ms
            if live_auth_capture
            else int((time.monotonic() - start) * 1000)
        )

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
            auth_prompts=tracker.prompts or None,
        )
