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
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path  # noqa: TC003 — runtime import so tests can patch Path.write_text

from conductor.offline_mode import _cache_dir
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderStalledError,
    resolve_effort_tokens,
)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_REQUEST_TIMEOUT_SEC = 180.0

# Sentinel distinguishing "caller didn't specify a timeout" from "caller
# explicitly asked for no timeout (None)". The constructor default applies
# only in the first case; explicit None means run unbounded.
_USE_DEFAULT: object = object()

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
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        liveness_interval_sec: float = 30.0,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        codex_sandbox = {
            "read-only": "read-only",
            "workspace-write": "workspace-write",
            "none": "read-only",
        }.get(sandbox, "read-only")
        return self._run(
            task,
            model=model,
            effort=effort,
            sandbox=codex_sandbox,
            cwd=cwd,
            timeout_sec_override=timeout_sec,
            max_stall_sec=max_stall_sec,
            liveness_interval_sec=liveness_interval_sec,
            stream=True,
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
        timeout_sec_override: float | None | object = _USE_DEFAULT,
        max_stall_sec: int | None = None,
        liveness_interval_sec: float = 30.0,
        stream: bool = False,
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

        if timeout_sec_override is _USE_DEFAULT:
            timeout = self._timeout_sec
        else:
            # `None` means "run unbounded" (subprocess.run accepts timeout=None).
            timeout = timeout_sec_override  # type: ignore[assignment]

        if stream:
            return self._run_streaming(
                args,
                model=model,
                effort=effort,
                thinking_budget=thinking_budget,
                cwd=cwd,
                timeout=timeout,
                max_stall_sec=max_stall_sec,
                liveness_interval_sec=liveness_interval_sec,
            )

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
            # Recover the session_id (if any) from whatever NDJSON the codex
            # CLI managed to emit before we killed it. Without this the user
            # has nothing to `--resume` from after a timeout.
            partial_stdout = e.stdout or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", errors="replace")
            elapsed = time.monotonic() - start
            raise ProviderError(
                self._message_with_partial_session_id(
                    f"codex CLI timed out after {elapsed:.0f}s",
                    partial_stdout,
                )
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

    def _run_streaming(
        self,
        args: list[str],
        *,
        model: str,
        effort: str | int,
        thinking_budget: int,
        cwd: str | None,
        timeout: float | None,
        max_stall_sec: int | None,
        liveness_interval_sec: float,
    ) -> CallResponse:
        start = time.monotonic()
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )

        stdout_q: queue.Queue[str | None] = queue.Queue()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            try:
                while True:
                    line = process.stdout.readline()
                    if line == "":
                        break
                    stdout_q.put(line)
            finally:
                stdout_q.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = process.stderr.readline()
                if chunk == "":
                    break
                stderr_parts.append(chunk)

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        stdout_done = False
        last_output = start
        last_liveness = start
        session_id_emitted = False
        while True:
            now = time.monotonic()
            if timeout is not None and now - start > timeout:
                self._terminate_process(process)
                self._join_reader_threads(stdout_thread, stderr_thread)
                self._drain_stdout_queue(stdout_q, stdout_parts)
                stdout = "".join(stdout_parts)
                elapsed = time.monotonic() - start
                raise ProviderError(
                    self._message_with_partial_session_id(
                        f"codex CLI timed out after {elapsed:.0f}s",
                        stdout,
                    )
                )

            if max_stall_sec is not None and now - last_output > max_stall_sec:
                self._terminate_process(process)
                self._join_reader_threads(stdout_thread, stderr_thread)
                self._drain_stdout_queue(stdout_q, stdout_parts)
                stdout = "".join(stdout_parts)
                elapsed = time.monotonic() - last_output
                raise ProviderStalledError(
                    self._message_with_partial_session_id(
                        f"codex CLI stalled after {elapsed:.0f}s with no output",
                        stdout,
                    )
                )

            if (
                liveness_interval_sec > 0
                and now - last_output >= liveness_interval_sec
                and now - last_liveness >= liveness_interval_sec
            ):
                sys.stderr.write(
                    f"[conductor] no output from codex for {now - last_output:.0f}s...\n"
                )
                sys.stderr.flush()
                last_liveness = now

            try:
                item = stdout_q.get(timeout=0.05)
            except queue.Empty:
                if stdout_done and process.poll() is not None:
                    break
                continue

            if item is None:
                stdout_done = True
                if process.poll() is not None:
                    break
                continue

            stdout_parts.append(item)
            last_output = time.monotonic()
            last_liveness = last_output

            if not session_id_emitted:
                sid = self._extract_session_id_fast(item)
                if sid is not None:
                    sys.stderr.write(f"[conductor] codex session_id={sid}\n")
                    sys.stderr.flush()
                    session_id_emitted = True

        returncode = process.wait()
        self._join_reader_threads(stdout_thread, stderr_thread)
        self._drain_stdout_queue(stdout_q, stdout_parts)
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)

        if returncode != 0:
            raise ProviderHTTPError(
                f"codex exited {returncode}: "
                f"{(stderr or stdout).strip()[:500]}"
            )

        content, input_tokens, output_tokens, session_id = self._parse_ndjson(stdout)
        if not content:
            raise ProviderHTTPError(
                f"codex NDJSON stream had no agent_message: {stdout[:500]!r}"
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
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            session_id=session_id,
            raw={"stdout": stdout},
        )

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
                process.wait()

    def _join_reader_threads(
        self,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
    ) -> None:
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

    def _drain_stdout_queue(
        self,
        stdout_q: queue.Queue[str | None],
        stdout_parts: list[str],
    ) -> None:
        while True:
            try:
                item = stdout_q.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                stdout_parts.append(item)

    def _message_with_partial_session_id(self, prefix: str, stdout: str) -> str:
        _, _, _, partial_session_id = self._parse_ndjson(stdout)
        log_path = self._save_forensic_log(stdout)
        parts = [prefix]
        if partial_session_id:
            parts.append(
                f" (partial session_id={partial_session_id} — "
                f"resume with `conductor exec --with codex "
                f"--resume {partial_session_id} ...`"
            )
            if log_path is not None:
                parts.append(f"; raw NDJSON saved to {log_path})")
            else:
                parts.append(")")
        elif log_path is not None:
            parts.append(f" (raw NDJSON saved to {log_path})")
        return "".join(parts)

    def _save_forensic_log(self, stdout: str) -> Path | None:
        """Persist captured NDJSON to the cache dir on failure.

        Returns the path on success, or None if there was nothing to save
        or the write failed. A failure here MUST NOT propagate — the call
        is already failing for a different reason; losing the forensic
        log is acceptable, but masking the original error with a disk
        error is not.
        """
        if not stdout.strip():
            return None
        try:
            cache_dir = _cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            path = cache_dir / f"codex-{os.getpid()}-{ts}.ndjson"
            path.write_text(stdout, encoding="utf-8")
            return path
        except OSError:
            return None

    @staticmethod
    def _extract_session_id_fast(line: str) -> str | None:
        """Cheap substring filter + JSON parse for the session.created event.

        Called on every line in the streaming read loop, so the substring
        check matters: full json.loads on every NDJSON event would parse
        events we never care about (item.started, turn.completed, etc.).
        """
        if "session.created" not in line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") != "session.created":
            return None
        return event.get("session_id") or event.get("id")
