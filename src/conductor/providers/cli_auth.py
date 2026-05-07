"""Helpers for surfacing interactive CLI auth prompts during exec runs."""

from __future__ import annotations

import contextlib
import json
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.providers._startup_lock import release_startup_lock
from conductor.providers.interface import (
    ProviderHTTPError,
    ProviderStalledError,
    ProviderStartupStalledError,
)
from conductor.providers.terminal_signals import (
    append_recent_text,
    detect_retriable_provider_failure,
)

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

_AUTH_URL_RE = re.compile(r"https://[^\s'\"<>]+")
_AUTH_FALLBACK_PROVIDER = {
    "codex": "claude",
    "claude": "codex",
    "gemini": "claude",
}
_AUTH_TEXT_SIGNALS = (
    "please visit",
    "open this url",
    "open the following url",
    "complete authentication in your browser",
    "complete the login in your browser",
    "waiting for oauth",
    "login --with-api-key",
)


@dataclass(frozen=True)
class CapturedProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


def _format_stall_seconds(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _extract_url(text: str) -> str | None:
    match = _AUTH_URL_RE.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(".,)")


def _iter_scalar_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        for value in obj.values():
            found.extend(_iter_scalar_strings(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_iter_scalar_strings(value))
    return found


def _extract_event_url(event: dict[str, Any]) -> str | None:
    for key in (
        "url",
        "auth_url",
        "oauth_url",
        "verification_uri",
        "verification_url",
        "login_url",
    ):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in _iter_scalar_strings(event):
        url = _extract_url(value)
        if url is not None:
            return url
    return None


class AuthPromptTracker:
    """Detect provider-auth prompts, mirror them to stderr, and remember them."""

    def __init__(
        self,
        provider: str,
        *,
        session_log: SessionLog | None = None,
    ) -> None:
        self._provider = provider
        self._session_log = session_log
        self._seen: set[tuple[str, str | None]] = set()
        self.prompts: list[dict] = []

    def observe_text(self, text: str, *, source: str) -> None:
        notice = self._detect_from_text(text, source=source)
        if notice is not None:
            self._record(notice)

    def observe_json_line(self, line: str, *, source: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        notice = self._detect_from_event(event, source=source)
        if notice is not None:
            self._record(notice)

    def _detect_from_text(self, text: str, *, source: str) -> dict | None:
        raw = text.strip()
        if not raw:
            return None
        lowered = raw.lower()
        url = _extract_url(raw)
        if url is not None and "/oauth" in url.lower():
            return self._build_notice(url=url, source=source)
        if url is not None and any(signal in lowered for signal in _AUTH_TEXT_SIGNALS):
            return self._build_notice(url=url, source=source)
        if "login --with-api-key" in lowered:
            return self._build_notice(url=url, source=source)
        return None

    def _detect_from_event(self, event: dict[str, Any], *, source: str) -> dict | None:
        if str(event.get("type") or "") != "auth_required":
            return None
        return self._build_notice(url=_extract_event_url(event), source=source)

    def _build_notice(self, *, url: str | None, source: str) -> dict:
        notice = {
            "provider": self._provider,
            "message": "provider is waiting for OAuth completion",
            "source": source,
        }
        if url is not None:
            notice["url"] = url
        return notice

    def _record(self, notice: dict) -> None:
        key = (notice["provider"], notice.get("url"))
        if key in self._seen:
            return
        self._seen.add(key)
        self.prompts.append(notice)
        self._emit_stderr(notice)
        if self._session_log is not None:
            self._session_log.emit("auth_prompt", notice)

    def _emit_stderr(self, notice: dict) -> None:
        provider = notice["provider"]
        fallback = _AUTH_FALLBACK_PROVIDER.get(provider, "claude")
        sys.stderr.write(
            f"[conductor] auth required for {provider} — "
            "provider is waiting for OAuth completion\n"
        )
        url = notice.get("url")
        if url:
            sys.stderr.write(f"[conductor] complete the flow at: {url}\n")
        else:
            sys.stderr.write(
                "[conductor] complete the flow in the provider auth prompt "
                "(no URL captured)\n"
            )
        sys.stderr.write(
            "[conductor] rerun this exec after auth, or use "
            f"`--with {fallback}` for now\n"
        )
        sys.stderr.flush()


def run_subprocess_with_live_stderr(
    *,
    args: list[str],
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float | None,
    max_stall_sec: float | None = None,
    first_output_timeout_sec: float | None = None,
    provider_name: str | None = None,
    session_log: SessionLog | None = None,
    tracker: AuthPromptTracker,
    popen_factory,
    startup_lock: contextlib.AbstractContextManager | None = None,
) -> CapturedProcessResult:
    """Run a subprocess while inspecting stderr for auth prompts live."""

    start = time.monotonic()
    lock_context = startup_lock or contextlib.nullcontext()
    with lock_context as startup_lock_handle:
        process = popen_factory(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )

        parts: dict[str, list[str]] = {"stdout": [], "stderr": []}
        stream_done = {"stdout": False, "stderr": False}
        stream_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(name: str, pipe) -> None:
            try:
                read = getattr(pipe, "read", None)
                while True:
                    chunk = read(1) if read is not None else pipe.readline()
                    if chunk == "":
                        break
                    stream_q.put((name, chunk))
            finally:
                stream_q.put((name, None))

        stdout_thread = threading.Thread(
            target=read_stream, args=("stdout", process.stdout), daemon=True
        )
        stderr_thread = threading.Thread(
            target=read_stream, args=("stderr", process.stderr), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        last_output = start
        saw_output = False
        provider_label = provider_name or str(args[0])
        stderr_tail = ""
        try:
            while True:
                now = time.monotonic()
                if timeout is not None and now - start > timeout:
                    _terminate_process(process)
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    _drain_stream_queue(stream_q, parts, tracker)
                    raise subprocess.TimeoutExpired(
                        cmd=args,
                        timeout=timeout,
                        output="".join(parts["stdout"]),
                        stderr="".join(parts["stderr"]),
                    )

                if (
                    first_output_timeout_sec is not None
                    and not saw_output
                    and stream_q.empty()
                    and now - start > first_output_timeout_sec
                ):
                    _terminate_process(process)
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    _drain_stream_queue(stream_q, parts, tracker)
                    elapsed = time.monotonic() - start
                    reason = (
                        "no_initial_provider_output_within_"
                        f"{_format_stall_seconds(first_output_timeout_sec)}s"
                    )
                    if session_log is not None:
                        session_log.emit(
                            "error",
                            {
                                "provider": provider_label,
                                "reason": reason,
                                "phase": "first_output",
                                "last_event": "provider_started",
                                "silent_sec": round(elapsed, 1),
                            },
                        )
                    raise ProviderStartupStalledError(
                        provider=provider_label,
                        timeout_sec=first_output_timeout_sec,
                    )

                if (
                    max_stall_sec is not None
                    and stream_q.empty()
                    and now - last_output > max_stall_sec
                ):
                    _terminate_process(process)
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    _drain_stream_queue(stream_q, parts, tracker)
                    elapsed = time.monotonic() - last_output
                    last_event = (
                        "provider_output"
                        if parts["stdout"] or parts["stderr"]
                        else "provider_started"
                    )
                    reason = (
                        "no_provider_response_within_"
                        f"{_format_stall_seconds(max_stall_sec)}s"
                    )
                    if session_log is not None:
                        session_log.emit(
                            "error",
                            {
                                "provider": provider_label,
                                "reason": reason,
                                "last_event": last_event,
                                "silent_sec": round(elapsed, 1),
                            },
                        )
                    raise ProviderStalledError(
                        f"{provider_label} CLI stalled after "
                        f"{elapsed:.0f}s with no output"
                    )

                try:
                    stream_name, item = stream_q.get(timeout=0.05)
                except queue.Empty:
                    if all(stream_done.values()) and process.poll() is not None:
                        break
                    continue

                if item is None:
                    stream_done[stream_name] = True
                    if all(stream_done.values()) and process.poll() is not None:
                        break
                    continue

                if not saw_output:
                    release_startup_lock(startup_lock_handle)
                parts[stream_name].append(item)
                saw_output = True
                last_output = time.monotonic()
                if stream_name != "stderr":
                    continue

                tracker.observe_text(item, source="stderr")
                stderr_tail = append_recent_text(stderr_tail, item)
                signal = detect_retriable_provider_failure(
                    stderr_tail,
                    source="stderr",
                )
                if signal is not None:
                    _terminate_process(process)
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    _drain_stream_queue(stream_q, parts, tracker)
                    if session_log is not None:
                        session_log.emit(
                            "error",
                            {
                                "provider": provider_label,
                                "reason": "provider_terminal_failure",
                                "category": signal.category,
                                "source": signal.source,
                                "status_code": signal.status_code,
                                "detail": signal.detail,
                            },
                        )
                    raise ProviderHTTPError(signal.error_message(provider_label))

            returncode = process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            _drain_stream_queue(stream_q, parts, tracker)
            return CapturedProcessResult(
                returncode=returncode,
                stdout="".join(parts["stdout"]),
                stderr="".join(parts["stderr"]),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        finally:
            release_startup_lock(startup_lock_handle)


def _drain_stream_queue(
    stream_q: queue.Queue[tuple[str, str | None]],
    parts: dict[str, list[str]],
    tracker: AuthPromptTracker,
) -> None:
    while True:
        try:
            stream_name, item = stream_q.get_nowait()
        except queue.Empty:
            return
        if item is None:
            continue
        parts[stream_name].append(item)
        if stream_name == "stderr":
            tracker.observe_text(item, source="stderr")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
            process.wait()
