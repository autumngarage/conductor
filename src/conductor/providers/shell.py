"""Shell-command custom provider.

Users register their own providers via ``conductor providers add --shell
'<cmd>'``. The shell command reads the prompt on stdin (default) or as
an argv positional (with ``--accepts argv``), and its stdout is the
response.

This is deliberately simple. Custom providers:

- Do not support tool-use. ``exec()`` with a non-empty tool set raises
  ``UnsupportedCapability``. Shell-command providers are single-turn by
  contract; if you have a custom CLI that runs a full agent loop
  internally (e.g. your company's internal Copilot wrapper), it can
  still be registered here — the loop just happens inside the command,
  not through Conductor's router.

- Do not support session resume. Each call is stateless from Conductor's
  perspective. Applications needing multi-turn context should prepend
  prior turns to the prompt themselves.

- Do not expose thinking budgets. ``effort`` is accepted and silently
  no-ops, matching the ollama contract.

- Declare tier, tags, and cost at registration time. Those feed the
  router exactly like built-in providers.

Authentication is the user's problem. Whatever credentials the shell
command needs (API keys, SSO tokens, etc.) must be present in the
environment the command runs in. Conductor doesn't attempt to scope or
scrub env vars — the shell command inherits the invoking shell's env.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    UnsupportedCapability,
)

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

SHELL_PROVIDER_TIMEOUT_SEC = 180.0

# Sentinel: distinguishes "caller didn't specify a timeout" from "caller
# explicitly passed None" (no timeout, run unbounded).
_USE_DEFAULT: object = object()

AcceptsMode = Literal["stdin", "argv"]


@dataclass(frozen=True)
class ShellProviderSpec:
    """Persisted configuration for a single custom shell-command provider.

    Stored under ``~/.config/conductor/providers.toml`` as one entry per
    ``[[providers]]`` table. See ``conductor.custom_providers`` for the
    file-format glue.
    """

    name: str
    shell: str
    accepts: AcceptsMode = "stdin"
    tags: tuple[str, ...] = ()
    quality_tier: str = "local"
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    typical_p50_ms: int = 3000


class ShellProvider:
    """Conductor provider backed by a user-supplied shell command.

    Instances are built from a ``ShellProviderSpec`` loaded at CLI startup
    from the user's custom-providers TOML file.
    """

    # Capability declarations that are the same for every shell provider.
    supported_tools: frozenset[str] = frozenset()
    supports_effort: bool = False
    supports_image_attachments: bool = False
    effort_to_thinking: dict[str, int] = {}  # noqa: RUF012
    cost_per_1k_thinking: float = 0.0

    # User-defined shell commands have no canonical install/auth recipe —
    # the user wrote the command, so no automated fix line in `conductor list`.
    fix_command: str | None = None

    def __init__(
        self,
        spec: ShellProviderSpec,
        *,
        timeout_sec: float = SHELL_PROVIDER_TIMEOUT_SEC,
    ) -> None:
        self._spec = spec
        self._timeout_sec = timeout_sec

    # --- identity / declared capability ------------------------------------ #

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def default_model(self) -> str:
        # Custom providers don't have a model menu — the shell command is
        # the "model." Expose it so `conductor list` has something useful
        # to show in the default-model column.
        return self._spec.shell

    @property
    def tags(self) -> list[str]:
        return list(self._spec.tags)

    @property
    def quality_tier(self) -> str:
        return self._spec.quality_tier

    @property
    def cost_per_1k_in(self) -> float:
        return self._spec.cost_per_1k_in

    @property
    def cost_per_1k_out(self) -> float:
        return self._spec.cost_per_1k_out

    @property
    def typical_p50_ms(self) -> int:
        return self._spec.typical_p50_ms

    # --- liveness checks --------------------------------------------------- #

    def configured(self) -> tuple[bool, str | None]:
        binary = self._binary()
        if not binary:
            return False, (
                f"custom provider `{self._spec.name}` has an empty shell command. "
                "Re-register with `conductor providers add --name ... --shell '<cmd>'`."
            )
        if not shutil.which(binary):
            return False, (
                f"`{binary}` (used by custom provider `{self._spec.name}`) not found on "
                f"PATH. Ensure the command exists in the shell env this CLI runs in."
            )
        return True, None

    def smoke(self) -> tuple[bool, str | None]:
        # Cheapest possible — just confirm the binary exists. A real
        # round-trip would cost tokens / time; the user can use
        # `conductor call --with <name> --brief "ping"` for that.
        return self.configured()

    def health_probe(self, *, timeout_sec: float = 30.0) -> tuple[bool, str | None]:
        del timeout_sec
        return self.configured()

    def _binary(self) -> str:
        parts = shlex.split(self._spec.shell)
        return parts[0] if parts else ""

    # --- core operations --------------------------------------------------- #

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                f"custom provider `{self._spec.name}` is stateless — no session "
                "to resume. To replay context, prepend prior turns to `task`."
            )
        ok, reason = self.configured()
        if not ok:
            raise ProviderConfigError(reason or f"{self._spec.name} not configured")

        return self._invoke(task, effort=effort)

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
        if tools:
            raise UnsupportedCapability(
                f"custom provider `{self._spec.name}` (shell-command) does not support "
                "tool-use. If your command runs its own agent loop, omit --tools and "
                "call the provider directly; the tool calls happen inside your command, "
                "not through Conductor's router."
            )
        if resume_session_id:
            raise UnsupportedCapability(
                f"custom provider `{self._spec.name}` is stateless — no session to resume."
            )
        # No Conductor-managed tools; custom commands run their own process.
        return self._invoke(task, effort=effort, timeout_override=timeout_sec, cwd=cwd)

    # --- subprocess plumbing ---------------------------------------------- #

    def _invoke(
        self,
        task: str,
        *,
        effort: str | int,
        timeout_override: float | None | object = _USE_DEFAULT,
        cwd: str | None = None,
    ) -> CallResponse:
        argv = shlex.split(self._spec.shell)
        stdin_bytes: bytes | None = None
        if self._spec.accepts == "stdin":
            stdin_bytes = task.encode("utf-8")
        else:  # argv
            argv = [*argv, task]

        # Sentinel branch: caller passed an explicit float | None when not _USE_DEFAULT.
        timeout: float | None = (
            self._timeout_sec
            if timeout_override is _USE_DEFAULT
            else timeout_override  # type: ignore[assignment]
        )
        start = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                input=stdin_bytes,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            raise ProviderError(
                f"custom provider `{self._spec.name}` timed out after {elapsed:.0f}s"
            ) from e
        except FileNotFoundError as e:
            raise ProviderConfigError(
                f"custom provider `{self._spec.name}` failed to start: {e}"
            ) from e

        duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            stderr_tail = (result.stderr or b"").decode("utf-8", errors="replace")[-500:]
            raise ProviderHTTPError(
                f"custom provider `{self._spec.name}` exited {result.returncode}: "
                f"{stderr_tail.strip() or '(no stderr)'}"
            )

        text = (result.stdout or b"").decode("utf-8", errors="replace").rstrip("\n")
        if not text:
            raise ProviderHTTPError(
                f"custom provider `{self._spec.name}` produced empty stdout"
            )

        return CallResponse(
            text=text,
            provider=self._spec.name,
            model=self._spec.shell,
            duration_ms=duration_ms,
            usage={
                "input_tokens": None,
                "output_tokens": None,
                "cached_tokens": None,
                "thinking_tokens": None,
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": 0,
            },
            raw={"stdout": text, "returncode": result.returncode},
        )
