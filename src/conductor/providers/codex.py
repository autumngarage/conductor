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
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path  # noqa: TC003 — runtime import so tests can patch Path.write_text
from typing import TYPE_CHECKING

from conductor import __version__ as _conductor_version
from conductor.offline_mode import _cache_dir
from conductor.orphan_detect import find_orphan_codex_processes, format_orphan_hints
from conductor.providers._startup_lock import codex_startup_lock, release_startup_lock
from conductor.providers._tool_weights import ToolBudgetCounter
from conductor.providers.cli_auth import STARTUP_LOCK_MAX_HOLD_SEC, AuthPromptTracker
from conductor.providers.interface import (
    PROVIDER_RUNTIME_STATEFUL_AGENT,
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderStalledError,
    resolve_effort_tokens,
)
from conductor.providers.review_contract import (
    ReviewOutputContractError,
    validate_requested_review_sentinel,
)
from conductor.providers.terminal_signals import (
    append_recent_text,
    detect_retriable_provider_failure,
)
from conductor.session_log import (
    SESSION_DATA_TOKEN_COUNT,
    SESSION_DATA_USAGE,
    SESSION_EVENT_SUBAGENT_MESSAGE,
    SESSION_EVENT_TOOL_CALL,
    SESSION_EVENT_USAGE,
    SESSION_USAGE_OUTPUT_TOKENS,
)

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

_LOG = logging.getLogger(__name__)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_REVIEW_MODEL = "codex-review"
CODEX_REQUEST_TIMEOUT_SEC = 180.0
CODEX_STARTUP_PROBE_TIMEOUT_SEC = 8.0
CODEX_STARTUP_PROBE_CONFIG = (
    "model_reasoning_effort=low",
)
CODEX_STARTUP_READY_EVENTS = frozenset({"thread.started", "turn.started"})
CODEX_STALL_INITIAL_EVENTS = frozenset(
    {"session.created", "thread.started", "turn.started"}
)
CODEX_STALL_BOUNDARY_EVENTS = frozenset({"turn.completed", "turn.failed"})
CODEX_STALL_TOOL_EVENTS = frozenset({"tool_use", "tool_result"})
CODEX_STALL_KNOWN_NON_PROGRESS_EVENTS = frozenset(
    {
        "error",
        "item.started",
        "session.created",
        "subagent_message",
        "thread.started",
        "turn.started",
    }
)
CODEX_STREAM_POLL_INTERVAL_SEC = 0.05
CODEX_STREAM_EXIT_READER_JOIN_SEC = 0.2

# Sentinel distinguishing "caller didn't specify a timeout" from "caller
# explicitly asked for no timeout (None)". The constructor default applies
# only in the first case; explicit None means run unbounded.
_USE_DEFAULT: object = object()

# Map symbolic effort → codex's reasoning-effort value. The CLI plumbs this
# via `-c model_reasoning_effort=<value>` as of codex-cli 0.125.0 (the older
# `--effort` flag was removed in that release). We always emit the new form;
# users on pre-0.125.0 codex will need to upgrade.
#
# Codex CLI's default tool configuration currently rejects
# `model_reasoning_effort=minimal` because built-in tools such as web_search
# and image_gen are not allowed at that reasoning tier. Conductor cannot fully
# control those implicit tools from its narrow Provider contract, so `minimal`
# dispatches at Codex's lowest compatible tier: `low`.
_EFFORT_TO_CODEX_FLAG = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",  # codex's ceiling
}

_CODEX_EXEC_RESUME_OPTION_ARITY = {
    "-c": 1,
    "--config": 1,
    "--all": 0,
    "--dangerously-bypass-approvals-and-sandbox": 0,
    "--disable": 1,
    "--enable": 1,
    "--ephemeral": 0,
    "--ignore-rules": 0,
    "--ignore-user-config": 0,
    "-i": 1,
    "--image": 1,
    "--json": 0,
    "--last": 0,
    "-m": 1,
    "--model": 1,
    "-o": 1,
    "--output-last-message": 1,
    "--skip-git-repo-check": 0,
}
_CODEX_EXEC_RESUME_DROPPED_OPTION_ARITY = {
    "--sandbox": 1,
}


def _extract_review_stdout(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        raise ProviderHTTPError(
            f"codex review exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    content = result.stdout.strip()
    if not content:
        raise ProviderHTTPError(
            f"codex review produced empty stdout: {result.stderr[:500]!r}"
        )
    return content


def _codex_output_path(resume_session_id: str | None) -> Path:
    sessionish = resume_session_id or uuid.uuid4().hex
    return _cache_dir() / f"codex-exec-{sessionish}.json"


def _filter_codex_exec_resume_args(args: list[str]) -> list[str]:
    """Return argv accepted by `codex exec resume`.

    The resume subcommand has a narrower flag surface than `codex exec`.
    Keep the known accepted flags explicit so compatibility-only parent flags
    do not leak into resume and fail before the provider can do useful work.
    """
    filtered: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "-" or not item.startswith("-"):
            filtered.append(item)
            index += 1
            continue

        if item in _CODEX_EXEC_RESUME_DROPPED_OPTION_ARITY:
            value_count = _CODEX_EXEC_RESUME_DROPPED_OPTION_ARITY[item]
            _LOG.debug("dropping unsupported codex exec resume option %s", item)
            index += 1 + value_count
            continue

        accepted_value_count = _CODEX_EXEC_RESUME_OPTION_ARITY.get(item)
        if accepted_value_count is None:
            raise ProviderError(
                f"internal error: codex exec resume option is not allowlisted: {item}"
            )
        if index + accepted_value_count >= len(args):
            raise ProviderError(
                f"internal error: codex exec resume option {item} is missing a value"
            )
        filtered.append(item)
        filtered.extend(args[index + 1 : index + 1 + accepted_value_count])
        index += 1 + accepted_value_count
    return filtered


def _format_compact_count(value: int) -> str:
    """Format integer counts for operator-facing heartbeat output."""
    if value < 1_000:
        return str(value)
    if value < 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{value // 1_000}k"


def _as_token_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return None


def _codex_error_dict_message(error: dict[str, object]) -> str | None:
    detail = error.get("message")
    if not isinstance(detail, str) or not detail.strip():
        return None
    error_type = error.get("type")
    param = error.get("param")
    parts = []
    if isinstance(error_type, str) and error_type:
        parts.append(error_type)
    parts.append(detail.strip())
    if isinstance(param, str) and param:
        parts.append(f"param={param}")
    return ": ".join(parts)


def _codex_nested_error_message(message: str | dict[str, object]) -> str:
    if isinstance(message, dict):
        return _codex_error_dict_message(message) or str(message)
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return message.strip()
    error = payload.get("error")
    if not isinstance(error, dict):
        return message.strip()
    return _codex_error_dict_message(error) or message.strip()


def _codex_startup_probe_failure_detail(stdout: str, stderr: str) -> str:
    """Prefer semantic Codex error events over noisy startup NDJSON."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                return _codex_nested_error_message(message)
        if event.get("type") == "turn.failed":
            message = event.get("message") or event.get("error")
            if isinstance(message, str) and message.strip():
                return _codex_nested_error_message(message)
            if isinstance(message, dict):
                return _codex_nested_error_message(message)
            return "codex turn failed"
    return (stderr or stdout).strip()


def _codex_startup_probe_failure(reason: str | None) -> bool:
    return bool(reason and "`codex exec` startup probe" in reason)


def _codex_startup_ready_event(line: str) -> str | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        _LOG.debug("ignoring non-JSON codex startup probe stdout line: %r", line)
        return None
    if not isinstance(event, dict):
        _LOG.debug("ignoring non-object codex startup probe stdout line: %r", line)
        return None
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type in CODEX_STARTUP_READY_EVENTS:
        return event_type
    return None


def _terminate_codex_startup_probe(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    except OSError:
        _LOG.debug("failed to terminate codex startup probe", exc_info=True)


class CodexProvider:
    name = "codex"
    tags = ["strong-reasoning", "code-review", "tool-use"]
    default_model = CODEX_DEFAULT_MODEL
    supports_native_review = True

    # Capability declarations (see interface.py)
    quality_tier = "frontier"
    runtime_kind = PROVIDER_RUNTIME_STATEFUL_AGENT
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    enforces_exec_tool_permissions = False
    supports_effort = True
    supports_image_attachments = True
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

    # One-liner shown under the failure reason in `conductor list`.
    fix_command = "brew install codex && codex login"

    def __init__(
        self,
        *,
        cli_command: str = "codex",
        timeout_sec: float = CODEX_REQUEST_TIMEOUT_SEC,
        startup_probe_timeout_sec: float = CODEX_STARTUP_PROBE_TIMEOUT_SEC,
    ) -> None:
        self._cli = cli_command
        self._timeout_sec = timeout_sec
        self._startup_probe_timeout_sec = startup_probe_timeout_sec

    def endpoint_url(self) -> str | None:
        return "https://api.openai.com"

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

    def fix_command_for_reason(self, reason: str | None) -> str | None:
        if _codex_startup_probe_failure(reason):
            return None
        return self.fix_command

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

    def _startup_probe(self) -> tuple[bool, str | None]:
        """Run the same codex exec startup path used by call().

        PATH and auth probes can pass while codex wedges before it emits any
        NDJSON, notably in the CLI's models-manager startup. This bounded
        probe keeps `conductor list` aligned with the path users actually run.
        Codex readiness is forward progress: once the CLI emits a documented
        startup event, the provider is authenticated and usable even if the
        tiny probe turn has not completed yet.
        """
        with tempfile.TemporaryDirectory(prefix="conductor-codex-probe-") as tmpdir:
            output_path = Path(tmpdir) / "output.json"
            args = [
                self._cli,
                "exec",
                "-",
                "--json",
                "-o",
                str(output_path),
                "--ephemeral",
                "--sandbox",
                "danger-full-access",
            ]
            for config in CODEX_STARTUP_PROBE_CONFIG:
                args.extend(["-c", config])
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except (FileNotFoundError, OSError) as e:
                return False, f"could not run `{self._cli} exec` startup probe: {e}"

            if process.stdin is not None:
                try:
                    process.stdin.write("Reply with OK.")
                    process.stdin.close()
                except OSError:
                    _LOG.debug("failed to write codex startup probe stdin", exc_info=True)

            stdout_lines: list[str] = []
            stderr_parts: list[str] = []
            stdout_queue: queue.Queue[str | None] = queue.Queue()

            def read_stdout() -> None:
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        stdout_queue.put(line)
                finally:
                    stdout_queue.put(None)

            def read_stderr() -> None:
                assert process.stderr is not None
                stderr_parts.extend(process.stderr.readlines())

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            deadline = time.monotonic() + self._startup_probe_timeout_sec
            stdout_eof = False
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    item = stdout_queue.get(
                        timeout=min(CODEX_STREAM_POLL_INTERVAL_SEC, max(0.0, remaining))
                    )
                except queue.Empty:
                    if process.poll() is not None and stdout_eof:
                        break
                    continue
                if item is None:
                    stdout_eof = True
                    if process.poll() is not None:
                        break
                    continue
                stdout_lines.append(item)
                if _codex_startup_ready_event(item):
                    _terminate_codex_startup_probe(process)
                    return True, None

            if process.poll() is None:
                _terminate_codex_startup_probe(process)
                detail = ("".join(stderr_parts) or "".join(stdout_lines)).strip()
                reason = (
                    f"`{self._cli} exec` startup probe timed out after "
                    f"{self._startup_probe_timeout_sec:.0f}s"
                )
                if detail:
                    reason = f"{reason}: {detail[:200]}"
                return False, reason

            stdout_thread.join(timeout=CODEX_STREAM_EXIT_READER_JOIN_SEC)
            stderr_thread.join(timeout=CODEX_STREAM_EXIT_READER_JOIN_SEC)
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_parts)
            if process.returncode != 0:
                detail = _codex_startup_probe_failure_detail(stdout, stderr)
                return False, (
                    f"`{self._cli} exec` startup probe exited {process.returncode}: "
                    f"{detail[:200]}"
                )
            detail = (stderr or stdout).strip()
            reason = f"`{self._cli} exec` startup probe exited before startup events"
            if detail:
                reason = f"{reason}: {detail[:200]}"
            return False, reason
        return True, None

    def configured(self) -> tuple[bool, str | None]:
        ok, reason = self._check_cli_path()
        if not ok:
            return False, reason
        ok, reason = self._auth_probe()
        if not ok:
            return False, reason
        return self._startup_probe()

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
        """Run Codex's native code-review command."""
        ok, reason = self._check_cli_path()
        if not ok:
            raise ProviderConfigError(reason or "codex not configured")

        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        codex_effort_flag = (
            _EFFORT_TO_CODEX_FLAG.get(effort) if isinstance(effort, str) else None
        )
        review_prompt = self._build_review_prompt(
            task,
            base=base,
            commit=commit,
            uncommitted=uncommitted,
            title=title,
        )
        args = [self._cli, "review"]
        if codex_effort_flag:
            args.extend(["-c", f"model_reasoning_effort={codex_effort_flag}"])
        args.append("-")

        # `codex review` is not a streaming interface: it commonly writes no
        # stdout until the final review text. Treat max_stall_sec as the
        # attempt cap rather than a live no-output watchdog so review-gate
        # fallback budgets still bound non-streaming Codex attempts.
        timeout = self._timeout_sec if timeout_sec is None else timeout_sec
        if max_stall_sec is not None:
            timeout = min(timeout, max_stall_sec)
        start = time.monotonic()

        def run_review_once(prompt: str) -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(
                    args,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )
            except subprocess.TimeoutExpired as e:
                elapsed = time.monotonic() - start
                raise ProviderError(
                    f"codex review timed out after {elapsed:.0f}s"
                ) from e

        result = run_review_once(review_prompt)
        content = _extract_review_stdout(result)
        try:
            content = validate_requested_review_sentinel(
                provider_name=self.name,
                prompt=review_prompt,
                text=content,
            )
        except ReviewOutputContractError as e:
            print(
                "[conductor] codex review output-contract failure "
                f"({e.reason}); failing this attempt so review fallback can "
                "try the next provider",
                file=sys.stderr,
            )
            raise
        duration_ms = int((time.monotonic() - start) * 1000)

        raw: dict[str, object] = {
            "command": "codex review",
            "stderr": result.stderr,
            "target": {
                "base": base,
                "commit": commit,
                "uncommitted": uncommitted,
                "title": title,
            },
        }

        return CallResponse(
            text=content,
            provider=self.name,
            model=CODEX_REVIEW_MODEL,
            duration_ms=duration_ms,
            usage={
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            raw=raw,
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
            target_lines.append(f"- Review changes against base branch/ref: {base}")
        if commit:
            target_lines.append(f"- Review commit: {commit}")
        if uncommitted:
            target_lines.append("- Include staged, unstaged, and untracked changes.")
        if title:
            target_lines.append(f"- Review title: {title}")
        if not target_lines:
            return task
        return "Review target:\n" + "\n".join(target_lines) + "\n\n" + task

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

    def _emit_stream_event(
        self,
        raw_line: str,
        *,
        session_log: SessionLog | None,
    ) -> None:
        if session_log is None:
            return
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return

        kind = event.get("type")
        if kind == "session.created":
            session_log.set_session_id(event.get("session_id") or event.get("id"))
            return

        item = event.get("item") or {}
        item_type = item.get("type")
        if kind == "item.completed" and item_type == "agent_message":
            token_count = (
                item.get("token_count")
                or event.get("token_count")
                or event.get("output_tokens")
            )
            session_log.emit(
                SESSION_EVENT_SUBAGENT_MESSAGE,
                {
                    "provider": self.name,
                    SESSION_DATA_TOKEN_COUNT: _as_token_count(token_count),
                    "text": item.get("text", ""),
                },
            )
            return

        if kind == "turn.completed":
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                session_log.emit(
                    SESSION_EVENT_USAGE,
                    {
                        "provider": self.name,
                        SESSION_DATA_USAGE: usage,
                    },
                )
            return

        if item_type and (
            "tool" in str(item_type) or str(item_type) in {"function_call", "tool_use"}
        ):
            session_log.emit(
                SESSION_EVENT_TOOL_CALL,
                {
                    "provider": self.name,
                    "item_type": item_type,
                    "name": item.get("name") or item.get("tool_name"),
                    "args": item.get("arguments") or item.get("args"),
                },
            )

    def _codex_stall_progress_kind(self, raw_line: str) -> str | None:
        """Classify Codex JSONL events that prove the session is progressing."""
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            _LOG.debug("ignoring non-JSON codex stdout line for stall watchdog: %r", raw_line)
            return None
        if not isinstance(event, dict):
            _LOG.debug("ignoring non-object codex stdout line for stall watchdog: %r", raw_line)
            return None

        kind = event.get("type")
        if not isinstance(kind, str) or not kind:
            _LOG.debug("ignoring codex stdout event without string type: %r", event)
            return None

        item = event.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        item_type = item.get("type")
        item_type_text = str(item_type) if item_type is not None else ""

        if kind in CODEX_STALL_TOOL_EVENTS:
            return "tool"
        if item_type_text and (
            "tool" in item_type_text
            or item_type_text in {"function_call", "function_call_output"}
        ):
            return "tool"

        if kind == "item.completed" and item_type == "agent_message":
            if item.get("text"):
                return "content"
            return None
        if item_type == "agent_message" and (event.get("delta") or item.get("delta")):
            return "content"

        if kind in CODEX_STALL_BOUNDARY_EVENTS:
            return "boundary"
        if kind in CODEX_STALL_INITIAL_EVENTS:
            return "initial"

        if kind not in CODEX_STALL_KNOWN_NON_PROGRESS_EVENTS:
            _LOG.debug("codex stdout event does not reset stall watchdog: %s", kind)
        return None

    def _read_session_log_progress(
        self,
        *,
        session_log: SessionLog,
        offset: int,
    ) -> tuple[str | None, int]:
        """Summarize tool/message progress from complete NDJSON lines.

        Heartbeats report deltas since the previous heartbeat, not cumulative
        totals since process start. We therefore advance the read offset only
        after consuming complete newline-terminated records.
        """
        try:
            with session_log.log_path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                chunk = fh.read()
                end_offset = fh.tell()
        except OSError:
            return None, offset

        if not chunk:
            return (
                "[conductor] no output from codex for {silent_sec:.0f}s...",
                offset,
            )

        lines = chunk.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            incomplete = lines.pop()
            end_offset -= len(incomplete)

        tool_calls = 0
        subagent_messages = 0
        tokens_received = 0
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_name = event.get("event")
            data = event.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            if event_name == SESSION_EVENT_TOOL_CALL:
                tool_calls += 1
                continue

            if event_name == SESSION_EVENT_SUBAGENT_MESSAGE:
                subagent_messages += 1
                token_count = _as_token_count(data.get(SESSION_DATA_TOKEN_COUNT))
                if token_count is not None:
                    tokens_received += token_count
                continue

            if event_name != SESSION_EVENT_USAGE:
                continue

            usage = data.get(SESSION_DATA_USAGE) or {}
            if not isinstance(usage, dict):
                continue
            token_count = _as_token_count(usage.get(SESSION_USAGE_OUTPUT_TOKENS))
            if token_count is not None:
                tokens_received += token_count

        if tool_calls == 0 and subagent_messages == 0 and tokens_received == 0:
            return (
                "[conductor] no output from codex for {silent_sec:.0f}s"
                " · 0 tool calls, 0 tokens — possibly stalled",
                end_offset,
            )

        tool_label = "tool call" if tool_calls == 1 else "tool calls"
        message_label = (
            "subagent message" if subagent_messages == 1 else "subagent messages"
        )
        return (
            "[conductor] no output from codex for {silent_sec:.0f}s"
            f" · {tool_calls} {tool_label}"
            f" · {subagent_messages} {message_label}"
            f" · {_format_compact_count(tokens_received)} tokens received since last heartbeat",
            end_offset,
        )

    @staticmethod
    def _read_output_backstop(path: Path) -> str | None:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return content or None

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
        attachments: tuple[Path, ...] = (),
    ) -> CallResponse:
        return self._run(
            task,
            model=model,
            effort=effort,
            sandbox="danger-full-access",
            timeout_sec_override=(
                timeout_sec if timeout_sec is not None else _USE_DEFAULT
            ),
            max_stall_sec=max_stall_sec,
            resume_session_id=resume_session_id,
            attachments=attachments,
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
        session_log: SessionLog | None = None,
        attachments: tuple[Path, ...] = (),
        strict_stall: bool = False,
        max_iterations: int | None = None,
    ) -> CallResponse:
        return self._run(
            task,
            model=model,
            effort=effort,
            sandbox="danger-full-access",
            cwd=cwd,
            timeout_sec_override=timeout_sec,
            max_stall_sec=max_stall_sec,
            liveness_interval_sec=liveness_interval_sec,
            stream=True,
            resume_session_id=resume_session_id,
            session_log=session_log,
            attachments=attachments,
            strict_stall=strict_stall,
            max_iterations=max_iterations,
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
        session_log: SessionLog | None = None,
        attachments: tuple[Path, ...] = (),
        strict_stall: bool = False,
        max_iterations: int | None = None,
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
        output_path = _codex_output_path(resume_session_id)

        # Codex resume uses a subcommand: `codex exec resume <id> -`.
        # Build argv accordingly when we have a session to resume. The task
        # itself is passed via stdin (`exec -` / `exec resume <id> -`), not
        # argv — see PR openai/codex#15917 (codex 0.122.0+) for the
        # documented "primary prompt is stdin" path. argv-as-prompt has
        # three real costs: it leaks 4KB+ briefs into `ps aux`, hits
        # Windows command-line ceilings, and on long prompts is the path
        # most prone to upstream regressions in the codex CLI's argv
        # parser. Stdin is the supported path.
        if resume_session_id:
            args = [
                self._cli,
                "exec",
                "resume",
                resume_session_id,
                "-",
                "--json",
                "-o",
                str(output_path),
                "--sandbox",
                sandbox,
            ]
        else:
            args = [
                self._cli,
                "exec",
                "-",
                "--json",
                "-o",
                str(output_path),
                "--ephemeral",
                "--sandbox",
                sandbox,
            ]
        if codex_effort_flag:
            args.extend(["-c", f"model_reasoning_effort={codex_effort_flag}"])

        for attachment in attachments:
            args.extend(["-i", str(attachment)])

        if resume_session_id:
            args = _filter_codex_exec_resume_args(args)

        if timeout_sec_override is _USE_DEFAULT:
            timeout = self._timeout_sec
        else:
            # `None` means "run unbounded" (subprocess.run accepts timeout=None).
            timeout = timeout_sec_override  # type: ignore[assignment]

        if stream:
            return self._run_streaming(
                args,
                task=task,
                model=model,
                effort=effort,
                thinking_budget=thinking_budget,
                cwd=cwd,
                timeout=timeout,
                max_stall_sec=max_stall_sec,
                liveness_interval_sec=liveness_interval_sec,
                output_path=output_path,
                session_log=session_log,
                strict_stall=strict_stall,
                max_iterations=max_iterations,
            )

        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                input=task,
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
            partial_stderr = e.stderr or ""
            if isinstance(partial_stderr, bytes):
                partial_stderr = partial_stderr.decode("utf-8", errors="replace")
            elapsed = time.monotonic() - start
            raise ProviderError(
                self._failure_message(
                    f"codex CLI timed out after {elapsed:.0f}s",
                    kind="timeout",
                    elapsed_sec=elapsed,
                    command=args,
                    cwd=cwd,
                    captured_stdout=partial_stdout,
                    captured_stderr=partial_stderr,
                    prompt=task,
                )
            ) from e
        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise ProviderHTTPError(
                f"codex exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        content, input_tokens, output_tokens, session_id = self._parse_ndjson(result.stdout)
        if not content:
            content = self._read_output_backstop(output_path) or ""
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
            raw={"stdout": result.stdout, "output_path": str(output_path)},
        )

    def _run_streaming(
        self,
        args: list[str],
        *,
        task: str,
        model: str,
        effort: str | int,
        thinking_budget: int,
        cwd: str | None,
        timeout: float | None,
        max_stall_sec: int | None,
        liveness_interval_sec: float,
        output_path: Path,
        session_log: SessionLog | None,
        strict_stall: bool,
        max_iterations: int | None,
    ) -> CallResponse:
        startup_lock_context = codex_startup_lock(
            session_log=session_log,
            snapshot_provider=self._codex_startup_lock_snapshot,
        )
        startup_lock_handle = startup_lock_context.__enter__()
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=os.environ.copy(),
            )
            start = time.monotonic()
        except BaseException:
            release_startup_lock(startup_lock_handle)
            startup_lock_context.__exit__(*sys.exc_info())
            raise
        # Pipe the prompt via stdin and close — codex exec reads until EOF.
        # If write blocks (huge prompt + slow consumer), we'd hang here, but
        # codex's own ingestion is fast and the prompt fits in the pipe
        # buffer for any realistic brief size. An os.set_blocking()-based
        # async write is overkill until we see a brief that exceeds 64KB.
        assert process.stdin is not None
        try:
            process.stdin.write(task)
        finally:
            process.stdin.close()

        stream_q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        auth_tracker = AuthPromptTracker(self.name, session_log=session_log)

        def read_stdout() -> None:
            assert process.stdout is not None
            try:
                read = getattr(process.stdout, "read", None)
                while True:
                    chunk = read(1) if read is not None else process.stdout.readline()
                    if chunk == "":
                        break
                    stream_q.put(("stdout", chunk))
            except Exception as e:
                stream_q.put(("reader_error", f"stdout reader failed: {e!r}"))
            finally:
                stream_q.put(("stdout", None))

        def read_stderr() -> None:
            assert process.stderr is not None
            try:
                read = getattr(process.stderr, "read", None)
                while True:
                    chunk = read(1) if read is not None else process.stderr.readline()
                    if chunk == "":
                        break
                    stream_q.put(("stderr", chunk))
            except Exception as e:
                stream_q.put(("reader_error", f"stderr reader failed: {e!r}"))
            finally:
                stream_q.put(("stderr", None))

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        done_event = threading.Event()
        timeout_fired = threading.Event()

        def kill_on_wall_timeout() -> None:
            if timeout is None:
                return
            if done_event.wait(timeout):
                return
            timeout_fired.set()
            self._terminate_process(process)

        timeout_thread: threading.Thread | None = None
        if timeout is not None:
            timeout_thread = threading.Thread(target=kill_on_wall_timeout, daemon=True)
            timeout_thread.start()

        stdout_done = False
        stderr_done = False
        last_output = start
        last_liveness = start
        heartbeat_log_offset = 0
        session_id_emitted = False
        initial_stall_event_seen = False
        saw_output = False
        stdout_event_buffer = ""
        stderr_failure_tail = ""
        tool_budget = ToolBudgetCounter()
        hit_iteration_cap = False
        try:
            while True:
                now = time.monotonic()
                if not saw_output and now - start >= STARTUP_LOCK_MAX_HOLD_SEC:
                    release_startup_lock(startup_lock_handle)

                if process.poll() is not None and stream_q.empty():
                    break

                if timeout_fired.is_set() or (
                    timeout is not None and now - start > timeout
                ):
                    self._terminate_process(process)
                    self._join_reader_threads(stdout_thread, stderr_thread)
                    self._drain_stream_queue(
                        stream_q, stdout_parts, stderr_parts, auth_tracker
                    )
                    stdout = "".join(stdout_parts)
                    stderr = "".join(stderr_parts)
                    elapsed = time.monotonic() - start
                    raise ProviderError(
                        self._failure_message(
                            f"codex CLI timed out after {elapsed:.0f}s",
                            kind="timeout",
                            elapsed_sec=elapsed,
                            command=args,
                            cwd=cwd,
                            captured_stdout=stdout,
                            captured_stderr=stderr,
                            prompt=task,
                        )
                    )

                if max_stall_sec is not None and now - last_output > max_stall_sec:
                    self._terminate_process(process)
                    self._join_reader_threads(stdout_thread, stderr_thread)
                    self._drain_stream_queue(
                        stream_q, stdout_parts, stderr_parts, auth_tracker
                    )
                    stdout = "".join(stdout_parts)
                    stderr = "".join(stderr_parts)
                    elapsed = time.monotonic() - last_output
                    raise ProviderStalledError(
                        self._failure_message(
                            f"codex CLI stalled after {elapsed:.0f}s with no output",
                            kind="stall",
                            elapsed_sec=elapsed,
                            command=args,
                            cwd=cwd,
                            captured_stdout=stdout,
                            captured_stderr=stderr,
                            prompt=task,
                        )
                    )

                if (
                    liveness_interval_sec > 0
                    and now - last_output >= liveness_interval_sec
                    and now - last_liveness >= liveness_interval_sec
                ):
                    if session_log is not None:
                        session_log.emit(
                            "provider_silent",
                            {
                                "provider": self.name,
                                "silent_sec": round(now - last_output, 1),
                            },
                        )
                    heartbeat_template: str | None = None
                    if session_log is not None:
                        heartbeat_template, heartbeat_log_offset = (
                            self._read_session_log_progress(
                                session_log=session_log,
                                offset=heartbeat_log_offset,
                            )
                        )
                    if heartbeat_template is None:
                        heartbeat_template = (
                            "[conductor] no output from codex for {silent_sec:.0f}s..."
                        )
                    self._emit_watchdog_stderr(
                        heartbeat_template.format(silent_sec=now - last_output) + "\n"
                    )
                    last_liveness = now

                try:
                    stream_name, item = stream_q.get(
                        timeout=CODEX_STREAM_POLL_INTERVAL_SEC
                    )
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue

                if stream_name == "reader_error":
                    self._terminate_process(process)
                    self._join_reader_threads(stdout_thread, stderr_thread)
                    self._drain_stream_queue(
                        stream_q, stdout_parts, stderr_parts, auth_tracker
                    )
                    stdout = "".join(stdout_parts)
                    stderr = "".join(stderr_parts)
                    elapsed = time.monotonic() - last_output
                    detail = item or "stream reader failed"
                    self._emit_watchdog_stderr(f"[conductor] {detail}\n")
                    if session_log is not None:
                        session_log.emit(
                            "error",
                            {
                                "provider": self.name,
                                "reason": "stream_reader_failed",
                                "detail": detail,
                                "silent_sec": round(elapsed, 1),
                            },
                        )
                    raise ProviderStalledError(
                        self._failure_message(
                            f"codex CLI stream reader failed after {elapsed:.0f}s",
                            kind="stall",
                            elapsed_sec=elapsed,
                            command=args,
                            cwd=cwd,
                            captured_stdout=stdout,
                            captured_stderr=stderr,
                            prompt=task,
                        )
                    )

                if item is None:
                    if stream_name == "stdout":
                        stdout_done = True
                    else:
                        stderr_done = True
                    if stdout_done and stderr_done and process.poll() is not None:
                        break
                    continue

                release_startup_lock(startup_lock_handle)
                saw_output = True
                if stream_name == "stderr":
                    stderr_parts.append(item)
                    last_output = time.monotonic()
                    last_liveness = last_output
                    auth_tracker.observe_text(item, source="stderr")
                    stderr_failure_tail = append_recent_text(
                        stderr_failure_tail,
                        item,
                    )
                    signal = detect_retriable_provider_failure(
                        stderr_failure_tail,
                        source="stderr",
                    )
                    if signal is not None:
                        self._terminate_process(process)
                        self._join_reader_threads(stdout_thread, stderr_thread)
                        self._drain_stream_queue(
                            stream_q, stdout_parts, stderr_parts, auth_tracker
                        )
                        if session_log is not None:
                            session_log.emit(
                                "error",
                                {
                                    "provider": self.name,
                                    "reason": "provider_terminal_failure",
                                    "category": signal.category,
                                    "source": signal.source,
                                    "status_code": signal.status_code,
                                    "detail": signal.detail,
                                },
                            )
                        raise ProviderHTTPError(signal.error_message(self.name))
                    continue

                stdout_parts.append(item)
                stdout_event_buffer += item
                while "\n" in stdout_event_buffer:
                    line, stdout_event_buffer = stdout_event_buffer.split("\n", 1)
                    line = f"{line}\n"
                    auth_tracker.observe_json_line(line, source="stdout")
                    self._emit_stream_event(line, session_log=session_log)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = None
                    if isinstance(event, dict):
                        tool_budget.observe_event(event)
                        if (
                            max_iterations is not None
                            and tool_budget.weighted_total >= max_iterations
                        ):
                            hit_iteration_cap = True
                            self._emit_watchdog_stderr(
                                "[conductor] Reached --max-iterations cap "
                                f"(weighted: {tool_budget.weighted_total:.1f} / "
                                f"{max_iterations}; raw count: "
                                f"{tool_budget.raw_count}). Re-run with "
                                "--max-iterations <larger> or split the brief.\n"
                            )
                            self._terminate_process(process)
                            break
                    progress_kind = self._codex_stall_progress_kind(line)
                    if progress_kind == "initial":
                        if not initial_stall_event_seen:
                            last_output = time.monotonic()
                            last_liveness = last_output
                            initial_stall_event_seen = True
                    elif progress_kind is not None and (
                        not strict_stall or progress_kind == "tool"
                    ):
                        last_output = time.monotonic()
                        last_liveness = last_output
                    signal = detect_retriable_provider_failure(
                        line,
                        source="stdout",
                        structured_only=True,
                    )
                    if signal is not None:
                        self._terminate_process(process)
                        self._join_reader_threads(stdout_thread, stderr_thread)
                        self._drain_stream_queue(
                            stream_q, stdout_parts, stderr_parts, auth_tracker
                        )
                        if session_log is not None:
                            session_log.emit(
                                "error",
                                {
                                    "provider": self.name,
                                    "reason": "provider_terminal_failure",
                                    "category": signal.category,
                                    "source": signal.source,
                                    "status_code": signal.status_code,
                                    "detail": signal.detail,
                                },
                            )
                        raise ProviderHTTPError(signal.error_message(self.name))

                    if not session_id_emitted:
                        sid = self._extract_session_id_fast(line)
                        if sid is not None:
                            if session_log is not None:
                                session_log.set_session_id(sid)
                            self._emit_watchdog_stderr(
                                f"[conductor] codex session_id={sid}\n"
                            )
                            session_id_emitted = True
                if hit_iteration_cap:
                    break
        finally:
            release_startup_lock(startup_lock_handle)
            startup_lock_context.__exit__(None, None, None)
            done_event.set()
            if timeout_thread is not None:
                timeout_thread.join(timeout=0.1)

        if timeout_fired.is_set():
            self._terminate_process(process)
            self._join_reader_threads(stdout_thread, stderr_thread)
            self._drain_stream_queue(
                stream_q, stdout_parts, stderr_parts, auth_tracker
            )
            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)
            elapsed = time.monotonic() - start
            raise ProviderError(
                self._failure_message(
                    f"codex CLI timed out after {elapsed:.0f}s",
                    kind="timeout",
                    elapsed_sec=elapsed,
                    command=args,
                    cwd=cwd,
                    captured_stdout=stdout,
                    captured_stderr=stderr,
                    prompt=task,
                )
            )

        returncode = process.wait()
        self._join_reader_threads(
            stdout_thread,
            stderr_thread,
            timeout=CODEX_STREAM_EXIT_READER_JOIN_SEC,
        )
        self._drain_stream_queue(stream_q, stdout_parts, stderr_parts, auth_tracker)
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)

        if returncode != 0 and not hit_iteration_cap:
            raise ProviderHTTPError(
                f"codex exited {returncode}: "
                f"{(stderr or stdout).strip()[:500]}"
            )

        content, input_tokens, output_tokens, session_id = self._parse_ndjson(stdout)
        if session_log is not None:
            session_log.set_session_id(session_id)
        if not content:
            content = self._read_output_backstop(output_path) or ""
        if hit_iteration_cap:
            cap_message = (
                "[conductor] Reached --max-iterations cap "
                f"(weighted: {tool_budget.weighted_total:.1f} / {max_iterations}; "
                f"raw count: {tool_budget.raw_count}). Re-run with "
                "--max-iterations <larger> or split the brief."
            )
            content = f"{content.rstrip()}\n\n{cap_message}".strip()
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
                "tool_iterations": tool_budget.weighted_total,
                "tool_call_count": tool_budget.raw_count,
                "hit_iteration_cap": hit_iteration_cap,
            },
            session_id=session_id,
            raw={"stdout": stdout, "output_path": str(output_path)},
            auth_prompts=auth_tracker.prompts or None,
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
        *,
        timeout: float = 1.0,
    ) -> None:
        stdout_thread.join(timeout=timeout)
        stderr_thread.join(timeout=timeout)

    def _emit_watchdog_stderr(self, text: str) -> None:
        """Best-effort operator output that cannot stop watchdog checks."""

        def write() -> None:
            sys.stderr.write(text)
            sys.stderr.flush()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(timeout=0.2)

    def _drain_stream_queue(
        self,
        stream_q: queue.Queue[tuple[str, str | None]],
        stdout_parts: list[str],
        stderr_parts: list[str],
        auth_tracker: AuthPromptTracker,
    ) -> None:
        while True:
            try:
                stream_name, item = stream_q.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            if stream_name == "stdout":
                stdout_parts.append(item)
                auth_tracker.observe_json_line(item, source="stdout")
                continue
            stderr_parts.append(item)
            auth_tracker.observe_text(item, source="stderr")

    def _failure_message(
        self,
        prefix: str,
        *,
        kind: str,
        elapsed_sec: float,
        command: list[str],
        cwd: str | None,
        captured_stdout: str,
        captured_stderr: str,
        prompt: str | None = None,
    ) -> str:
        """Build a user-facing failure message + write the forensic envelope.

        Always writes the envelope (even when codex emitted zero bytes) so
        that wedges *before* `session.created` — the worst class of failure
        documented in .cortex/journal/2026-04-26-codex-exec-wedge-trace.md
        — leave the wrapping agent something to attribute the failure to.
        Pre-fix, a zero-byte hang produced no session_id, no NDJSON, and
        no diagnostic file: the wrapping agent had nothing to act on.
        """
        _, _, _, partial_session_id = self._parse_ndjson(captured_stdout)
        envelope_path = self._save_forensic_envelope(
            kind=kind,
            reason=prefix,
            elapsed_sec=elapsed_sec,
            command=command,
            cwd=cwd,
            captured_stdout=captured_stdout,
            captured_stderr=captured_stderr,
            prompt=prompt,
        )
        parts = [prefix]
        if partial_session_id:
            parts.append(
                f" (partial session_id={partial_session_id} — "
                f"resume with `conductor exec --with codex "
                f"--resume {partial_session_id} ...`"
            )
            if envelope_path is not None:
                parts.append(f"; forensic envelope: {envelope_path})")
            else:
                parts.append(")")
        elif envelope_path is not None:
            parts.append(f" (forensic envelope: {envelope_path})")
        message = "".join(parts)
        if kind == "stall":
            try:
                orphans = find_orphan_codex_processes(self._cli)
                if orphans:
                    message = message + "\n" + format_orphan_hints(orphans)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[conductor] orphan detection failed: {exc!r}\n")
        return message

    def _codex_startup_lock_snapshot(self) -> tuple[int, str]:
        orphans = find_orphan_codex_processes(self._cli)
        if not orphans:
            return 0, "no stale codex startup processes detected"
        return len(orphans), format_orphan_hints(orphans)

    def _save_forensic_envelope(
        self,
        *,
        kind: str,
        reason: str,
        elapsed_sec: float,
        command: list[str],
        cwd: str | None,
        captured_stdout: str,
        captured_stderr: str,
        prompt: str | None = None,
    ) -> Path | None:
        """Persist a structured failure envelope to the cache dir.

        Always writes when called: codex wedges that produce zero bytes
        still benefit from having `(command, cwd, version)` on disk so an
        operator or wrapping agent has *something* to pin the failure to.
        Returns the path on success, or None if the write failed. A disk
        failure MUST NOT mask the original error — the call is already
        failing; losing the forensic envelope is acceptable.
        """
        envelope = {
            "kind": kind,
            "reason": reason,
            "elapsed_sec": round(elapsed_sec, 2),
            "conductor_version": _conductor_version,
            "codex_path": shutil.which(self._cli),
            "command": command,
            # The prompt now arrives via stdin (codex exec -), so it isn't
            # in `command`. Surface it separately so an operator inspecting
            # the envelope can still correlate the wedge with the request.
            "prompt": prompt,
            "cwd": cwd,
            "captured_stdout": captured_stdout,
            "captured_stderr": captured_stderr,
        }
        try:
            cache_dir = _cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            path = cache_dir / f"codex-{os.getpid()}-{ts}.json"
            path.write_text(
                json.dumps(envelope, indent=2, default=str),
                encoding="utf-8",
            )
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
