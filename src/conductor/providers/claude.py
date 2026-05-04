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
from pathlib import Path
from typing import TYPE_CHECKING

from conductor.providers.cli_auth import (
    AuthPromptTracker,
    CapturedProcessResult,
    run_subprocess_with_live_stderr,
)
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    resolve_effort_tokens,
)
from conductor.providers.review_contract import ensure_requested_review_sentinel

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

_ORIGINAL_POPEN = subprocess.Popen

CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_REQUEST_TIMEOUT_SEC = 180.0
CLAUDE_AUTH_PROBE_TIMEOUT_SEC = 15.0
CLAUDE_CALL_FIRST_OUTPUT_TIMEOUT_SEC = 60.0
CLAUDE_EXEC_FIRST_OUTPUT_TIMEOUT_SEC = 300.0
CLAUDE_SETTING_SOURCES = "user,project,local"
CLAUDE_CLI_ENV = "CONDUCTOR_CLAUDE_CLI"
CLAUDE_LINKED_WORKTREE_ERROR = (
    "claude provider cannot guarantee cwd isolation for mutating exec sessions "
    "inside a linked git worktree"
)

# Sentinel: "caller didn't specify a timeout" vs "caller explicitly passed
# None". The constructor default applies only in the first case.
_USE_DEFAULT: object = object()


class ClaudeProvider:
    name = "claude"
    tags = ["strong-reasoning", "long-context", "tool-use", "code-review"]
    default_model = CLAUDE_DEFAULT_MODEL
    supports_native_review = True

    # Capability declarations (see interface.py)
    quality_tier = "frontier"
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    enforces_exec_tool_permissions = True
    supports_effort = True
    supports_image_attachments = False
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
        auth_probe_timeout_sec: float = CLAUDE_AUTH_PROBE_TIMEOUT_SEC,
        call_first_output_timeout_sec: float | None = (
            CLAUDE_CALL_FIRST_OUTPUT_TIMEOUT_SEC
        ),
        exec_first_output_timeout_sec: float | None = (
            CLAUDE_EXEC_FIRST_OUTPUT_TIMEOUT_SEC
        ),
        first_output_timeout_sec: float | None = None,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLAUDE_CLI_ENV) or "claude"
        self._timeout_sec = timeout_sec
        self._auth_probe_timeout_sec = auth_probe_timeout_sec
        self._call_first_output_timeout_sec = call_first_output_timeout_sec
        self._exec_first_output_timeout_sec = (
            first_output_timeout_sec
            if first_output_timeout_sec is not None
            else exec_first_output_timeout_sec
        )

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
                timeout=self._auth_probe_timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            health_ok, health_reason = self.health_probe(
                timeout_sec=self._auth_probe_timeout_sec
            )
            if health_ok:
                return True, None
            return False, (
                f"could not verify `{self._cli}` auth status: {e}. "
                f"Fallback `{self._cli} --version` probe also failed: "
                f"{health_reason or 'unknown failure'}. "
                "Update the CLI (`brew upgrade claude`) and re-run, "
                "or set `ANTHROPIC_API_KEY` for non-interactive use."
            )
        except (FileNotFoundError, OSError) as e:
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

    def review_configured(self) -> tuple[bool, str | None]:
        return self.configured()

    def review(
        self,
        task: str,
        *,
        effort: str | int = "medium",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        base: str | None = None,
        commit: str | None = None,
        uncommitted: bool = False,
        title: str | None = None,
    ) -> CallResponse:
        """Run Claude Code's native review slash command in read-only mode."""
        ok, reason = self._check_cli_path()
        if not ok:
            raise ProviderConfigError(reason or "claude not configured")

        model = self.default_model
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        review_prompt = self._build_review_prompt(
            task,
            base=base,
            commit=commit,
            uncommitted=uncommitted,
            title=title,
        )
        args = [
            self._cli,
            "-p",
            review_prompt,
            "--output-format",
            "json",
            "--model",
            model,
            "--permission-mode",
            "plan",
        ]
        if cwd is not None:
            args.extend(["--setting-sources", CLAUDE_SETTING_SOURCES])
        env_overrides = {"MAX_THINKING_TOKENS": str(thinking_budget)} if thinking_budget else {}
        timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        start = time.monotonic()
        tracker = AuthPromptTracker(self.name)
        try:
            effective_cwd = self._effective_cwd(cwd)
            proc_env = self._build_proc_env(env_overrides, effective_cwd=effective_cwd)
            result = run_subprocess_with_live_stderr(
                args=args,
                cwd=str(effective_cwd) if effective_cwd is not None else None,
                env=proc_env,
                timeout=timeout,
                max_stall_sec=max_stall_sec,
                provider_name=self.name,
                session_log=None,
                tracker=tracker,
                popen_factory=subprocess.Popen,
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            raise ProviderError(
                f"claude review timed out after {elapsed:.0f}s"
            ) from e
        duration_ms = result.duration_ms

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"claude review exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ProviderHTTPError(
                f"claude review stdout was not JSON: {result.stdout[:500]!r}"
            ) from e

        if data.get("is_error"):
            raise ProviderHTTPError(
                f"claude review returned is_error=true: "
                f"{data.get('result', '<no detail>')}"
            )

        usage = data.get("usage") or {}
        content = ensure_requested_review_sentinel(
            provider_name=self.name,
            prompt=review_prompt,
            text=data.get("result", ""),
        )
        return CallResponse(
            text=content,
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
            raw={
                **data,
                "native_review_command": "claude /review",
                "target": {
                    "base": base,
                    "commit": commit,
                    "uncommitted": uncommitted,
                    "title": title,
                },
            },
            auth_prompts=tracker.prompts or None,
        )

    @staticmethod
    def _build_review_prompt(
        task: str,
        *,
        base: str | None,
        commit: str | None,
        uncommitted: bool,
        title: str | None,
    ) -> str:
        target_lines: list[str] = []
        if base:
            target_lines.append(f"- Review changes against base branch: {base}")
        if commit:
            target_lines.append(f"- Review commit: {commit}")
        if uncommitted:
            target_lines.append("- Include staged, unstaged, and untracked changes.")
        if title:
            target_lines.append(f"- Review title: {title}")

        prompt_parts = ["/review"]
        if target_lines:
            prompt_parts.append("Review target:\n" + "\n".join(target_lines))
        prompt_parts.append(task)
        return "\n\n".join(prompt_parts)

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
        start_timeout_sec: float | None = None,
        resume_session_id: str | None = None,
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        # Claude's `--allowedTools` is fine-grained; passing an empty set
        # is effectively "no tools permitted" (single-turn).
        allowed = ",".join(sorted(tools)) if tools else None
        return self._run(
            task,
            model=model,
            effort=effort,
            allowed_tools=allowed,
            permission_mode=None,
            cwd=cwd,
            timeout_sec_override=timeout_sec,
            max_stall_sec=max_stall_sec,
            start_timeout_sec=start_timeout_sec,
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
        max_stall_sec: int | None = None,
        start_timeout_sec: float | None = None,
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
        if cwd is not None:
            # Make project/local settings resolution explicit for headless
            # nested Claude Code runs. The child still receives cwd/PWD below;
            # this flag tells the CLI to include repo-scoped settings sources.
            args.extend(["--setting-sources", CLAUDE_SETTING_SOURCES])
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
            effective_cwd = self._effective_cwd(cwd)
            proc_env = self._build_proc_env(env_overrides, effective_cwd=effective_cwd)
            first_output_timeout_sec = self._effective_first_output_timeout(
                start_timeout_sec
            )
            if session_log is not None:
                session_log.emit(
                    "provider_diagnostic",
                    {
                        "provider": self.name,
                        "check": "claude_exec_watchdogs",
                        "first_output_timeout_sec": first_output_timeout_sec,
                        "max_stall_sec": max_stall_sec,
                    },
                )
            self._emit_project_settings_diagnostic(
                effective_cwd=effective_cwd,
                sandbox_permission_mode=permission_mode,
                session_log=session_log,
            )
            self._reject_mutating_linked_worktree(
                effective_cwd=effective_cwd,
                permission_mode=permission_mode,
                session_log=session_log,
            )
            if live_auth_capture:
                result = run_subprocess_with_live_stderr(
                    args=args,
                    cwd=str(effective_cwd) if effective_cwd is not None else None,
                    env=proc_env,
                    timeout=timeout,
                    max_stall_sec=max_stall_sec,
                    first_output_timeout_sec=first_output_timeout_sec,
                    provider_name=self.name,
                    session_log=session_log,
                    tracker=tracker,
                    popen_factory=subprocess.Popen,
                )
            else:
                completed = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(effective_cwd) if effective_cwd is not None else None,
                    env=proc_env,
                )
                result = CapturedProcessResult(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            raise ProviderError(
                f"claude CLI timed out after {elapsed:.0f}s"
            ) from e
        duration_ms = result.duration_ms

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

    def _effective_cwd(self, cwd: str | None) -> Path | None:
        if cwd is None:
            return None
        return Path(cwd).expanduser().resolve(strict=False)

    def _build_proc_env(
        self,
        env_overrides: dict[str, str],
        *,
        effective_cwd: Path | None,
    ) -> dict[str, str] | None:
        if not env_overrides and effective_cwd is None:
            return None
        proc_env = {**os.environ, **env_overrides}
        if effective_cwd is not None:
            proc_env["PWD"] = str(effective_cwd)
        return proc_env

    def _reject_mutating_linked_worktree(
        self,
        *,
        effective_cwd: Path | None,
        permission_mode: str | None,
        session_log: SessionLog | None,
        ) -> None:
        if effective_cwd is None or permission_mode in (None, "plan"):
            return

        git_paths = self._git_worktree_paths(effective_cwd, session_log=session_log)
        if git_paths is None:
            return
        git_dir, git_common_dir = git_paths
        if git_dir == git_common_dir:
            return

        detail = {
            "provider": self.name,
            "check": "claude_linked_worktree_cwd",
            "cwd": str(effective_cwd),
            "git_dir": str(git_dir),
            "git_common_dir": str(git_common_dir),
            "permission_mode": permission_mode,
            "allowed": False,
        }
        if session_log is not None:
            session_log.emit("provider_diagnostic", detail)
        raise ProviderConfigError(
            f"{CLAUDE_LINKED_WORKTREE_ERROR}: cwd={effective_cwd}, "
            f"git_dir={git_dir}, git_common_dir={git_common_dir}. "
            "Run Claude against the primary checkout or route mutating "
            "worktree-isolated tasks to `--with codex`."
        )

    def _git_worktree_paths(
        self,
        cwd: Path,
        *,
        session_log: SessionLog | None,
    ) -> tuple[Path, Path] | None:
        process: subprocess.Popen[str] | None = None
        try:
            process = _ORIGINAL_POPEN(
                [
                    "git",
                    "-C",
                    str(cwd),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-dir",
                    "--git-common-dir",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired as e:
            if process is not None:
                process.kill()
                process.communicate()
            if session_log is not None:
                session_log.emit(
                    "provider_diagnostic",
                    {
                        "provider": self.name,
                        "check": "claude_linked_worktree_cwd",
                        "cwd": str(cwd),
                        "allowed": True,
                        "reason": f"git probe timed out: {e}",
                    },
                )
            return None
        except OSError as e:
            if session_log is not None:
                session_log.emit(
                    "provider_diagnostic",
                    {
                        "provider": self.name,
                        "check": "claude_linked_worktree_cwd",
                        "cwd": str(cwd),
                        "allowed": True,
                        "reason": f"git probe failed: {e}",
                    },
                )
            return None

        if process.returncode != 0:
            if session_log is not None:
                session_log.emit(
                    "provider_diagnostic",
                    {
                        "provider": self.name,
                        "check": "claude_linked_worktree_cwd",
                        "cwd": str(cwd),
                        "allowed": True,
                        "reason": "cwd is not inside a git worktree",
                        "stderr": stderr.strip()[:500],
                    },
                )
            return None

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            if session_log is not None:
                session_log.emit(
                    "provider_diagnostic",
                    {
                        "provider": self.name,
                        "check": "claude_linked_worktree_cwd",
                        "cwd": str(cwd),
                        "allowed": True,
                        "reason": "git probe returned incomplete worktree paths",
                        "stdout": stdout.strip()[:500],
                    },
                )
            return None
        return Path(lines[0]).resolve(strict=False), Path(lines[1]).resolve(strict=False)

    def _effective_first_output_timeout(
        self,
        start_timeout_sec: float | None,
    ) -> float | None:
        configured = (
            self._exec_first_output_timeout_sec
            if start_timeout_sec is None
            else start_timeout_sec
        )
        if configured is None or configured <= 0:
            return None
        return configured

    def _emit_project_settings_diagnostic(
        self,
        *,
        effective_cwd: Path | None,
        sandbox_permission_mode: str | None,
        session_log: SessionLog | None,
    ) -> None:
        if effective_cwd is None:
            return
        settings_json_path = effective_cwd / ".claude" / "settings.json"
        settings_path = effective_cwd / ".claude" / "settings.local.json"
        data = {
            "provider": self.name,
            "check": "claude_project_settings",
            "cwd": str(effective_cwd),
            "settings_json_path": str(settings_json_path),
            "settings_path": str(settings_path),
            "settings_json_exists": settings_json_path.exists(),
            "settings_local_exists": settings_path.exists(),
            "permission_mode": sandbox_permission_mode,
        }
        permission_keys: set[str] = set()
        permission_sources: list[str] = []
        for path in (settings_json_path, settings_path):
            if not path.exists():
                continue
            raw = self._read_project_settings(path)
            permissions = raw.get("permissions")
            if isinstance(permissions, dict):
                permission_sources.append(path.name)
                permission_keys.update(str(key) for key in permissions)
        data["permissions_configured"] = bool(permission_sources)
        if permission_sources:
            data["permission_sources"] = permission_sources
            data["permission_keys"] = sorted(permission_keys)
        if session_log is not None:
            session_log.emit("provider_diagnostic", data)

    def _read_project_settings(self, path: Path) -> dict:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ProviderConfigError(
                f"invalid Claude project settings JSON at {path}: "
                f"{e.msg} (line {e.lineno}, column {e.colno})"
            ) from e
        if not isinstance(raw, dict):
            raise ProviderConfigError(
                f"Claude project settings at {path} must be a JSON object"
            )
        return raw
