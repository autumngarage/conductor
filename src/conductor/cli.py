"""Conductor CLI — call, exec, list, smoke, doctor, init, route, config.

v0.2 surface (call/exec):
  conductor call --with <id> [--effort max] --brief "..."
  conductor call --auto [--tags a,b] [--prefer best] [--effort max] --brief "..."
  conductor exec --auto [--tools Read,Grep,Edit] --brief-file PATH

v0.1 surface (unchanged):
  conductor list [--json]
  conductor smoke [<id>] [--all] [--json]
  conductor doctor [--json]
  conductor init [--yes]

v0.2 additions:
  conductor route --tags a,b [--prefer best] [--tools X,Y] [--dry-run]
  conductor config show
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import click

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor import __version__, credentials, offline_mode
from conductor.banner import print_caller_banner
from conductor.muted_providers import (
    MutedProvidersError,
    load_muted_provider_ids,
    mute_provider_ids,
    muted_providers_file_path,
    unmute_provider_ids,
)
from conductor.profiles import ProfileError, ProfileSpec, get_profile, load_profiles
from conductor.providers import (
    QUALITY_TIERS,
    CallResponse,
    NativeReviewProvider,
    OpenRouterProvider,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderStalledError,
    UnsupportedCapability,
    get_provider,
    known_providers,
)
from conductor.providers.review_contract import (
    ReviewContextError,
    build_review_task_prompt,
)
from conductor.router import (
    VALID_PREFER_MODES,
    InvalidRouterRequest,
    NoConfiguredProvider,
    RouteDecision,
    mark_outcome,
    mark_rate_limited,
    pick,
)
from conductor.router_defaults import (
    RouterDefaultsError,
    load_home_tag_defaults,
    load_tag_defaults,
    repo_router_defaults_path,
    set_home_tag_default,
    unset_home_tag_default,
)
from conductor.semantic import (
    SEMANTIC_KINDS,
    SemanticPlan,
    plan_for,
    with_candidate_override,
)
from conductor.session_log import (
    SessionLog,
    SessionLogError,
    SessionRecord,
    find_session_record,
    latest_active_session,
    list_session_records,
)
from conductor.wizard import run_init_wizard

VALID_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
SANDBOX_DEPRECATION_WARNING = (
    "[conductor] --sandbox is deprecated and ignored; conductor exec now runs unsandboxed."
)
VALID_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")
PROFILE_PRECEDENCE_TEXT = (
    "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
)
DEFAULT_EXEC_MAX_STALL_SEC = 360
MIN_EXEC_BRIEF_CHARS = 300
GIT_RECOVERY_COMMAND_TIMEOUT_SEC = 2.0
GIT_RECOVERY_MAX_COMMITS = 5
GIT_RECOVERY_MAX_STATUS_PATHS = 8
NATIVE_REVIEW_TAG = "code-review"
DEFAULT_REVIEW_MAX_FALLBACKS = 3
DEFAULT_COUNCIL_ROUNDS = 1


@dataclass(frozen=True)
class BriefInput:
    body: str
    source: str


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _read_task(
    task: str | None,
    task_file: str | None,
    *,
    brief: str | None = None,
    brief_file: str | None = None,
) -> BriefInput:
    explicit_sources = [
        (name, value)
        for name, value in (
            ("--task", task),
            ("--brief", brief),
            ("--task-file", task_file),
            ("--brief-file", brief_file),
        )
        if value is not None
    ]
    if len(explicit_sources) > 1:
        got = ", ".join(name for name, _value in explicit_sources)
        raise click.UsageError(
            "brief source is ambiguous. Use exactly one of --brief, --brief-file, "
            f"--task, --task-file, or stdin; got {got}."
        )

    source = "stdin"
    if brief is not None:
        body = brief
        source = "--brief"
    elif task is not None:
        body = task
        source = "--task"
    elif brief_file is not None or task_file is not None:
        file_source = brief_file if brief_file is not None else task_file
        source = "--brief-file" if brief_file is not None else "--task-file"
        assert file_source is not None
        if file_source == "-":
            body = sys.stdin.read()
        else:
            try:
                body = Path(file_source).read_text(encoding="utf-8")
            except OSError as e:
                raise click.UsageError(
                    f"could not read {source} {file_source!r}: {e.strerror or e}"
                ) from e
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise click.UsageError(
            "no brief provided. Pass --brief '...', --brief-file PATH, "
            "--task '...', --task-file PATH, or pipe content on stdin."
        )

    body = body.strip()
    if not body:
        raise click.UsageError("brief is empty after stripping whitespace.")
    return BriefInput(body=body, source=source)


def _warn_if_short_exec_brief(
    brief_input: BriefInput,
    *,
    allow_short_brief: bool,
) -> None:
    if allow_short_brief or len(brief_input.body) >= MIN_EXEC_BRIEF_CHARS:
        return
    click.echo(
        "[conductor] brief is short "
        f"({len(brief_input.body)} chars). Delegated exec only sees this brief "
        "plus files it can inspect; for Claude/Codex handoffs, prefer "
        "--brief-file with goal, context, scope, constraints, expected output, "
        "and validation. Pass --allow-short-brief to silence this warning.",
        err=True,
    )


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _review_tags(raw: str | None) -> list[str]:
    tags = [NATIVE_REVIEW_TAG]
    for tag in _parse_csv(raw):
        if tag not in tags:
            tags.append(tag)
    return tags


def _resolve_layered_value(
    cli_value: str | None,
    *,
    env_key: str,
    profile_value: str | None = None,
) -> str | None:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    return profile_value


def _load_named_profile(name: str | None) -> ProfileSpec | None:
    if name is None:
        return None
    try:
        return get_profile(name)
    except ProfileError as e:
        raise click.UsageError(str(e)) from e


def _parse_effort(raw: str | None) -> str | int:
    if raw is None:
        return "medium"
    raw = raw.strip()
    if not raw:
        return "medium"
    # Integer budget override.
    if raw.lstrip("-").isdigit():
        n = int(raw)
        if n < 0:
            raise click.UsageError(f"--effort integer must be >= 0, got {n}")
        return n
    if raw not in VALID_EFFORT_LEVELS:
        hint = _closest(raw, VALID_EFFORT_LEVELS)
        raise click.UsageError(
            f"--effort={raw!r} is not valid. "
            f"Use one of: {list(VALID_EFFORT_LEVELS)} or an integer budget. "
            f"Did you mean '{hint}'?"
        )
    return raw


def _validate_tools(raw: str | None) -> frozenset[str]:
    tools = _parse_csv(raw)
    unknown = [t for t in tools if t not in VALID_TOOLS]
    if unknown:
        raise click.UsageError(
            f"--tools contains unknown tool(s): {unknown}. "
            f"Known: {list(VALID_TOOLS)}."
        )
    return frozenset(tools)


def _validate_sandbox(raw: str | None, *, warn: bool = False) -> str:
    if raw is None:
        return "none"
    if warn:
        click.echo(SANDBOX_DEPRECATION_WARNING, err=True)
    return "none"


def _validate_prefer(raw: str | None) -> str:
    if raw is None:
        return "balanced"
    if raw not in VALID_PREFER_MODES:
        hint = _closest(raw, VALID_PREFER_MODES)
        raise click.UsageError(
            f"--prefer={raw!r} is not valid. "
            f"Use one of: {list(VALID_PREFER_MODES)}. "
            f"Did you mean '{hint}'?"
        )
    return raw


def _normalize_max_stall_sec(raw: int | None) -> int | None:
    if raw is None:
        return None
    if raw < 0:
        raise click.UsageError(
            f"--max-stall-seconds must be >= 0, got {raw}."
        )
    if raw == 0:
        return None
    return raw


def _normalize_start_timeout_sec(raw: float | None) -> float | None:
    if raw is None:
        return None
    if raw < 0:
        raise click.UsageError(f"--start-timeout must be >= 0, got {raw:g}.")
    if raw == 0:
        return None
    return raw


def _apply_offline_flag(
    *, offline: bool | None, provider_id: str | None, auto: bool
) -> tuple[str | None, bool]:
    """Translate ``--offline/--no-offline`` into the routing knobs.

    Returns ``(provider_id, auto)`` with offline semantics applied:

    - ``--offline`` (True): sets the sticky offline flag and forces
      ``--with ollama`` regardless of ``--auto`` / ``--tags`` / etc.
      Auto-routing doesn't compose with a force-local directive — if the
      router filters out ollama for any reason (exclude list, unmet tool
      capability, health cooldown), silently falling through to a remote
      provider would violate the documented "force local" contract. So
      ``--offline`` unconditionally rewrites the invocation to explicit
      ollama; ``--auto`` becomes a no-op in that case. Passing
      ``--with <non-ollama>`` alongside ``--offline`` is an error.
    - ``--no-offline`` (False): clears the sticky flag, then behaves normally.
    - ``None``: no-op.
    """
    if offline is None:
        return provider_id, auto
    if offline is False:
        offline_mode.clear()
        return provider_id, auto
    # offline is True
    if provider_id and provider_id != "ollama":
        raise click.UsageError(
            f"--offline forces the local provider; --with {provider_id} "
            "contradicts it. Use one or the other."
        )
    offline_mode.set_active()
    return "ollama", False


def _closest(query: str, options: tuple[str, ...]) -> str:
    from difflib import get_close_matches

    match = get_close_matches(query, options, n=1, cutoff=0.3)
    return match[0] if match else options[0]


# Message fragments that indicate a connectivity-level failure (DNS
# resolution, TCP reset, unreachable host, etc.). These are what httpx /
# urllib / subprocess tooling surface when the network is gone — the
# airplane-mode case. Matched case-insensitively. Kept conservative: a
# false positive merely cascades a fallback that would have failed anyway,
# but a false negative means we refuse to offer the local-model swap.
_NETWORK_ERROR_SIGNALS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",       # httpx ConnectError str()
    "connect call failed",    # asyncio
    "could not resolve",      # curl / some python stacks
    "name or service not known",
    "nodename nor servname",  # macOS getaddrinfo wording
    "temporary failure in name resolution",
    "network is unreachable",
    "network is down",        # macOS airplane mode, ENETDOWN
    "no route to host",
    "no address associated",
    "no such host",
    "host is down",
    "getaddrinfo failed",
)


def _is_retryable(err: Exception) -> tuple[bool, str]:
    """Classify an error as retryable-with-fallback or fatal.

    Returns (retryable, category) — category is "rate-limit" | "5xx" |
    "timeout" | "network" | "provider-error" | "other" for health-tracking
    and fallback-UX routing purposes. "network" is separate from "timeout"
    so the offline-mode prompt can fire on the real thing (DNS/TCP failure)
    rather than on a slow-but-reachable upstream.
    """
    if isinstance(err, ProviderStalledError):
        return True, "timeout"
    msg = str(err).lower()
    if "429" in msg or "rate limit" in msg or "ratelimit" in msg:
        return True, "rate-limit"
    if any(sig in msg for sig in _NETWORK_ERROR_SIGNALS):
        return True, "network"
    if "timed out" in msg or "timeout" in msg or "stalled" in msg:
        return True, "timeout"
    # HTTP 5xx — check for " 5" preceded by "http" or a similar prefix so
    # we don't match arbitrary "5" digits. Cheap heuristic; acceptable.
    signals = ("http 5", "returned http 5", "exited 5", "overloaded")
    if any(sig in msg for sig in signals):
        return True, "5xx"
    if isinstance(err, ProviderHTTPError):
        return True, "provider-error"
    return False, "other"


def _review_failure_mode(err: Exception) -> str:
    if isinstance(err, ProviderStalledError):
        return "stall"
    _retryable, category = _is_retryable(err)
    return category


def _format_tried_providers(tried: list[tuple[str, str]]) -> str:
    return ", ".join(f"{name} ({mode})" for name, mode in tried)


def _validate_max_fallbacks(raw: int) -> int:
    if raw < 1:
        raise click.UsageError(f"--max-fallbacks must be >= 1, got {raw}")
    return raw


def _ollama_index(candidates: list) -> int | None:
    """Return the index of ollama in ``candidates`` (or None if absent)."""
    for i, c in enumerate(candidates):
        if c.name == "ollama":
            return i
    return None


def _reorder_ollama_first(candidates: list) -> bool:
    """Move ollama to the head of ``candidates``; return True if mutated."""
    idx = _ollama_index(candidates)
    if idx is None or idx == 0:
        return False
    candidates.insert(0, candidates.pop(idx))
    return True


def _stderr_is_tty() -> bool:
    """Best-effort check: are we talking to a human on stderr + stdin?

    click.confirm() prompts on stderr when ``err=True``. We also need
    stdin to be a TTY so the user can actually answer. Either one being
    non-interactive (pipes, CI, test harness) should skip the prompt.
    """
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _provider_for_preflight(provider_or_name):
    if hasattr(provider_or_name, "health_probe"):
        return provider_or_name
    return get_provider(str(provider_or_name))


def _run_exec_preflight(provider_or_name) -> tuple[bool, str | None]:
    provider = _provider_for_preflight(provider_or_name)
    return provider.health_probe()


def _review_provider_or_none(provider_or_name) -> NativeReviewProvider | None:
    provider = _provider_for_preflight(provider_or_name)
    if isinstance(provider, NativeReviewProvider):
        return provider
    return None


def _review_exclude_set(
    user_exclude: frozenset[str],
) -> tuple[frozenset[str], dict[str, str]]:
    """Return router excludes needed to keep review routing code-review-only."""
    excludes = set(user_exclude)
    reasons: dict[str, str] = {}
    for name in known_providers():
        if name in user_exclude:
            reasons[name] = "excluded by caller"
            continue
        try:
            provider = get_provider(name)
        except KeyError as e:
            excludes.add(name)
            reasons[name] = str(e)
            continue
        if NATIVE_REVIEW_TAG not in provider.tags:
            excludes.add(name)
            reasons[name] = "provider is not tagged code-review"
            continue
        ok, reason = provider.review_configured() if isinstance(
            provider, NativeReviewProvider
        ) else provider.configured()
        if not ok:
            excludes.add(name)
            reasons[name] = reason or "code-review provider is not configured"
    return frozenset(excludes), reasons


def _native_review_unavailable_message(reasons: dict[str, str]) -> str:
    if not reasons:
        return "no code-review provider is available."
    lines = ["no code-review provider is available:"]
    for name in sorted(reasons):
        lines.append(f"  - {name}: {reasons[name]}")
    return "\n".join(lines)


def _semantic_candidate_exclude_set(
    plan: SemanticPlan,
    user_exclude: frozenset[str],
) -> frozenset[str]:
    allowed = {candidate.provider for candidate in plan.candidates}
    if not allowed:
        return user_exclude
    return frozenset(set(known_providers()) - allowed) | user_exclude


def _semantic_priority(plan: SemanticPlan) -> tuple[str, ...]:
    return tuple(candidate.provider for candidate in plan.candidates)


def _format_semantic_plan_line(plan: SemanticPlan) -> str:
    stack = " > ".join(candidate.label() for candidate in plan.candidates)
    return (
        f"[conductor] ask kind={plan.kind} effort={plan.effort_bucket} "
        f"mode={plan.mode} stack={stack}"
    )


def _semantic_plan_payload(plan: SemanticPlan) -> dict[str, object]:
    return {
        "kind": plan.kind,
        "effort_bucket": plan.effort_bucket,
        "mode": plan.mode,
        "tags": list(plan.tags),
        "prefer": plan.prefer,
        "tools": sorted(plan.tools),
        "sandbox": plan.sandbox,
        "candidates": [
            {"provider": candidate.provider, "models": list(candidate.models)}
            for candidate in plan.candidates
        ],
        "council_member_models": list(plan.council_member_models),
        "council_synthesis_models": list(plan.council_synthesis_models),
    }


def _echo_preflight_failure(provider_or_name, reason: str | None) -> None:
    provider = _provider_for_preflight(provider_or_name)
    detail = reason or "provider health probe failed"
    click.echo(f"[conductor] preflight failed for {provider.name}: {detail}", err=True)
    fix = getattr(provider, "fix_command", None)
    if fix:
        click.echo(f"[conductor] try: {fix}", err=True)


def _echo_offline_hint(failed_name: str, *, silent: bool) -> None:
    """Print a hint pointing at ollama when we couldn't prompt."""
    if silent:
        return
    click.echo(
        f"[conductor] {failed_name} is unreachable and no local fallback "
        "is available for automatic switching. If you are offline, run "
        "`conductor call --with ollama --brief '...'` (or pass --offline).",
        err=True,
    )


def _maybe_echo_explicit_network_hint(provider_id: str, err: Exception) -> None:
    """On a network-category failure in explicit (--with) mode, nudge local.

    The auto-mode path has its own prompt + sticky-flag dance. Explicit mode
    can't reroute silently (the user asked for this provider specifically),
    so the most helpful thing is a one-line suggestion. No-op when the user
    already picked ollama, or when the failure isn't network-shaped.
    """
    if provider_id == "ollama":
        return
    _, category = _is_retryable(err)
    if category != "network":
        return
    click.echo(
        f"[conductor] {provider_id} looks unreachable (network error). "
        "If you are offline: `conductor call --offline --brief '...'` "
        "or `conductor call --with ollama --brief '...'`.",
        err=True,
    )


def _maybe_switch_to_ollama(
    *,
    failed: str,
    candidates: list,
    cursor: int,
    silent: bool,
) -> bool | None:
    """Ask the user whether to skip ahead to ollama, then rewrite candidates.

    Returns:
      True  — user confirmed; ``candidates`` now has ollama at ``cursor + 1``
              and later remote candidates dropped. Sticky-flag setting is the
              caller's responsibility.
      False — user declined. ``candidates`` is unchanged; the normal cascade
              continues through whatever remote candidates are left.
      None  — we couldn't prompt (non-TTY, or ollama not in the remaining
              candidates, or ollama isn't actually reachable locally). Caller
              should treat this as "the offline fallback isn't wired up right
              now" and print a hint + re-raise.
    """
    remaining_idx = _ollama_index(candidates[cursor + 1 :])
    if remaining_idx is None:
        # Ollama isn't even in the ranking — nothing to offer.
        return None
    absolute_idx = cursor + 1 + remaining_idx

    ollama = get_provider("ollama")
    ok, reason = ollama.configured()
    if not ok:
        if not silent:
            click.echo(
                f"[conductor] {failed} is unreachable and ollama is not "
                f"running locally ({reason}). Start it with `ollama serve` "
                "or re-run with a different provider.",
                err=True,
            )
        return None

    if not _stderr_is_tty():
        return None

    default_model = _provider_default_model(ollama)
    click.echo("", err=True)
    click.echo(
        f"⚠ {failed} is unreachable — you appear to be offline.",
        err=True,
    )
    try:
        answer = click.confirm(
            f"  Fall back to local model ({default_model} via ollama)?",
            default=True,
            err=True,
        )
    except click.Abort:
        return False

    if not answer:
        # The user explicitly declined the local switch. Respect that —
        # drop ollama from the remaining ranking so a silent cascade
        # doesn't route through it anyway. The normal fallback chain
        # keeps trying any other remote candidates below, and re-raises
        # the original error if none are left.
        del candidates[absolute_idx]
        return False

    # Truncate: drop any remote candidates between the current cursor and
    # ollama, and drop anything after ollama too. The user opted for local,
    # so we don't want to keep trying other remotes if ollama itself fails.
    ollama_candidate = candidates[absolute_idx]
    del candidates[cursor + 1 :]
    candidates.append(ollama_candidate)
    return True


def _invoke_with_fallback(
    decision: RouteDecision,
    *,
    mode: str,  # "call" | "exec"
    task: str,
    model: str | None,
    effort: str | int,
    tools: frozenset[str],
    sandbox: str,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    start_timeout_sec: float | None,
    silent: bool,
    resume_session_id: str | None = None,
    session_log: SessionLog | None = None,
    models_by_provider: dict[str, tuple[str, ...]] | None = None,
) -> tuple[CallResponse, list[str]]:
    """Try the decision's ranked providers in order; fallback on retryable errors.

    Returns (response, fallbacks_used). fallbacks_used is the list of
    provider names attempted before the successful one (excluding the final).

    Raises the last ProviderError if every candidate fails.

    Offline-mode integration:
      - If ``offline_mode.is_active()`` and ollama is in the ranking, ollama
        is moved to the head of the list so we try local first.
      - On the first "network"-category failure we prompt (TTY only) to
        switch to ollama, truncating the remaining remote candidates on
        acceptance. Accepting also sets the sticky offline flag so subsequent
        invocations skip straight to local for the TTL window.
    """
    last_exc: Exception | None = None
    fallbacks: list[str] = []
    candidates = list(decision.ranked)

    if offline_mode.is_active():
        if _ollama_index(candidates) is None:
            # Offline mode promises local routing. If ollama is absent from
            # the ranking (excluded, unconfigured, or filtered out by
            # tools), silently cascading to a remote provider would
            # violate that promise — and the remote will almost certainly
            # fail with a network error anyway. Surface the contradiction
            # up front instead.
            raise ProviderConfigError(
                "offline mode is active but ollama is not in the routing "
                "candidates (excluded, not configured, or filtered out by "
                "--tools). Start ollama (`ollama serve`), relax the filters, "
                "or clear the flag with --no-offline."
            )
        _reorder_ollama_first(candidates)
        if not silent:
            remaining_m = max(1, (offline_mode.seconds_remaining() + 59) // 60)
            click.echo(
                f"[conductor] offline mode active (~{remaining_m}m left) · "
                "routing → ollama. Pass --no-offline to clear.",
                err=True,
            )

    prompted_offline = False
    idx = 0
    while idx < len(candidates):
        candidate = candidates[idx]
        provider = get_provider(candidate.name)
        candidate_models = (models_by_provider or {}).get(candidate.name)
        if session_log is not None:
            session_log.bind_provider(candidate.name)
            session_log.emit(
                "provider_started",
                {
                    "provider": candidate.name,
                    "mode": mode,
                    "model": model,
                    "tools": sorted(tools),
                    "sandbox": sandbox,
                    "cwd": cwd,
                    "resume_session_id": resume_session_id,
                },
            )
        try:
            if mode == "exec":
                if isinstance(provider, OpenRouterProvider):
                    response = provider.exec(
                        task,
                        model=model,
                        models=candidate_models,
                        effort=effort,
                        task_tags=list(decision.task_tags),
                        prefer=decision.prefer,
                        log_selection=not silent,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=timeout_sec,
                        max_stall_sec=max_stall_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                    )
                else:
                    exec_kwargs = {
                        "model": model,
                        "effort": effort,
                        "tools": tools,
                        "sandbox": sandbox,
                        "cwd": cwd,
                        "timeout_sec": timeout_sec,
                        "max_stall_sec": max_stall_sec,
                        "resume_session_id": resume_session_id,
                        "session_log": session_log,
                    }
                    if candidate.name == "claude":
                        exec_kwargs["start_timeout_sec"] = start_timeout_sec
                    response = provider.exec(
                        task,
                        **exec_kwargs,
                    )
            else:
                if isinstance(provider, OpenRouterProvider):
                    response = provider.call(
                        task,
                        model=model,
                        models=candidate_models,
                        effort=effort,
                        task_tags=list(decision.task_tags),
                        prefer=decision.prefer,
                        log_selection=not silent,
                        resume_session_id=resume_session_id,
                    )
                else:
                    response = provider.call(
                        task,
                        model=model,
                        effort=effort,
                        resume_session_id=resume_session_id,
                    )
            mark_outcome(candidate.name, "success")
            if session_log is not None:
                session_log.set_session_id(response.session_id)
                session_log.emit(
                    "provider_finished",
                    {
                        "provider": response.provider,
                        "model": response.model,
                        "duration_ms": response.duration_ms,
                        "session_id": response.session_id,
                    },
                )
            return response, fallbacks
        except ProviderConfigError:
            # Config problems don't recover with a different provider using
            # the same config. Re-raise immediately.
            raise
        except UnsupportedCapability:
            # Router filter should prevent this; if it leaks through, skip.
            fallbacks.append(candidate.name)
            idx += 1
            continue
        except ProviderError as e:
            retryable, category = _is_retryable(e)
            if category == "rate-limit":
                mark_rate_limited(candidate.name)
            mark_outcome(candidate.name, category)
            last_exc = e
            if session_log is not None:
                session_log.emit(
                    "provider_failed",
                    {
                        "provider": candidate.name,
                        "category": category,
                        "error": str(e),
                    },
                )
            if not retryable:
                raise
            fallbacks.append(candidate.name)

            # First real connectivity failure in this invocation: prompt
            # (or use the sticky flag) to switch to ollama instead of
            # spraying timeouts across every remote in the ranking.
            if category == "network" and not prompted_offline:
                prompted_offline = True
                decision_flag = _maybe_switch_to_ollama(
                    failed=candidate.name,
                    candidates=candidates,
                    cursor=idx,
                    silent=silent,
                )
                if decision_flag is None:
                    # No fallback is actionable (ollama absent / not running /
                    # non-TTY). Don't silently cascade through more remotes
                    # that will also fail — surface the hint and re-raise.
                    _echo_offline_hint(candidate.name, silent=silent)
                    raise
                if decision_flag:
                    offline_mode.set_active()
                # If False (user declined), fall through to the normal
                # cascade — maybe it was a blip and claude works.

            if idx + 1 < len(candidates):
                next_name = candidates[idx + 1].name
                if not silent:
                    click.echo(
                        f"[conductor] {candidate.name} failed ({category}) · "
                        f"falling through to {next_name} (falling back → {next_name})",
                        err=True,
                    )
            idx += 1
            continue

    # Exhausted every candidate; re-raise the last error for user visibility.
    assert last_exc is not None  # at least one attempt must have happened
    raise last_exc


def _invoke_review_with_fallback(
    decision: RouteDecision,
    *,
    task: str,
    effort: str | int,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    silent: bool,
    max_fallbacks: int = DEFAULT_REVIEW_MAX_FALLBACKS,
) -> tuple[CallResponse, list[str]]:
    """Try code-review providers in route order."""
    last_exc: Exception | None = None
    fallbacks: list[str] = []
    tried: list[tuple[str, str]] = []
    candidates = list(decision.ranked[:max_fallbacks])

    for idx, candidate in enumerate(candidates):
        provider = get_provider(candidate.name)
        try:
            if isinstance(provider, NativeReviewProvider):
                response = provider.review(
                    task,
                    effort=effort,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                )
            elif isinstance(provider, OpenRouterProvider):
                response = provider.call(
                    build_review_task_prompt(
                        task,
                        base=base,
                        commit=commit,
                        uncommitted=uncommitted,
                        title=title,
                        cwd=cwd,
                        include_patch=True,
                    ),
                    effort=effort,
                    task_tags=list(decision.task_tags),
                    prefer=decision.prefer,
                    log_selection=not silent,
                )
            else:
                response = provider.call(
                    build_review_task_prompt(
                        task,
                        base=base,
                        commit=commit,
                        uncommitted=uncommitted,
                        title=title,
                        cwd=cwd,
                        include_patch=True,
                    ),
                    effort=effort,
                )
            mark_outcome(candidate.name, "success")
            tried.append((candidate.name, "success"))
            if not silent and len(tried) > 1:
                click.echo(
                    f"[conductor] review tried providers: "
                    f"{_format_tried_providers(tried)}",
                    err=True,
                )
            return response, fallbacks
        except ProviderConfigError as e:
            last_exc = e
            mark_outcome(candidate.name, "config")
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "config"))
        except UnsupportedCapability as e:
            last_exc = e
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "unsupported"))
        except ProviderError as e:
            retryable, category = _is_retryable(e)
            if category == "rate-limit":
                mark_rate_limited(candidate.name)
            mark_outcome(candidate.name, category)
            last_exc = e
            if not retryable:
                raise
            fallbacks.append(candidate.name)
            tried.append((candidate.name, _review_failure_mode(e)))
        except ReviewContextError as e:
            last_exc = e
            mark_outcome(candidate.name, "review-context")
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "review-context"))

        if idx + 1 < len(candidates) and not silent:
            click.echo(
                f"[conductor] {candidate.name} review failed ({tried[-1][1]}) · "
                f"falling back → {candidates[idx + 1].name}",
                err=True,
            )

    assert last_exc is not None
    if tried:
        trail = _format_tried_providers(tried)
        hint = (
            "increase --max-fallbacks, exclude failing providers, or run "
            "`conductor list` to check configured code-review providers"
        )
        raise ProviderError(
            "code review failed for all tried providers: "
            f"{trail}. Next step: {hint}."
        ) from last_exc
    raise last_exc


def _invoke_council(
    plan: SemanticPlan,
    *,
    task: str,
    effort: str | int,
    timeout_sec: int | None,
    rounds: int,
    silent: bool,
) -> CallResponse:
    """Run a deterministic OpenRouter council request.

    Council is intentionally OpenRouter-only: it fans out to the policy's
    member model stack, then asks a synthesis model to reconcile disagreements.
    """
    if plan.candidates[0].provider != "openrouter":
        raise ProviderConfigError(
            "council policy invariant violated: council must route through openrouter."
        )
    if not plan.council_member_models:
        raise ProviderConfigError(
            "council policy invariant violated: no member models configured."
        )

    provider = get_provider("openrouter")
    if not isinstance(provider, OpenRouterProvider):
        raise ProviderConfigError(
            "provider registry invariant violated: openrouter is not OpenRouterProvider."
        )
    if timeout_sec is not None:
        provider = OpenRouterProvider(timeout_sec=float(timeout_sec))

    member_responses: list[CallResponse] = []
    member_prompt = _council_member_prompt(task, rounds=rounds)
    for idx, model in enumerate(plan.council_member_models, start=1):
        if not silent:
            click.echo(f"[conductor] council member {idx}: {model}", err=True)
        response = provider.call(
            member_prompt,
            model=model,
            effort=effort,
            task_tags=list(plan.tags),
            prefer=plan.prefer,
            log_selection=False,
        )
        member_responses.append(response)

    synthesis_models = plan.council_synthesis_models or plan.council_member_models[:1]
    if not silent:
        click.echo(
            "[conductor] council synthesis: " + ",".join(synthesis_models),
            err=True,
        )
    synthesis = provider.call(
        _council_synthesis_prompt(task, member_responses),
        models=synthesis_models,
        effort=effort,
        task_tags=list(plan.tags),
        prefer=plan.prefer,
        log_selection=False,
    )

    raw = {
        **(synthesis.raw or {}),
        "conductor_council": {
            "member_models": [response.model for response in member_responses],
            "requested_member_models": list(plan.council_member_models),
            "requested_synthesis_models": list(synthesis_models),
            "rounds": rounds,
            "member_usage": [response.usage for response in member_responses],
            "member_duration_ms": [response.duration_ms for response in member_responses],
        },
    }
    usage = {
        **synthesis.usage,
        "council_members": len(member_responses),
        "council_rounds": rounds,
    }
    cost_usd = synthesis.cost_usd
    if cost_usd is not None and all(
        response.cost_usd is not None for response in member_responses
    ):
        cost_usd += sum(response.cost_usd or 0 for response in member_responses)
    return replace(synthesis, usage=usage, cost_usd=cost_usd, raw=raw)


def _council_member_prompt(task: str, *, rounds: int) -> str:
    return (
        "You are one member of a multi-model council. Give an independent, "
        "critical answer to the request below. Name assumptions, risks, and "
        "where another strong model might disagree. Do not claim consensus.\n\n"
        f"Council rounds requested: {rounds}\n\n"
        "Request:\n"
        f"{task}"
    )


def _council_synthesis_prompt(task: str, member_responses: list[CallResponse]) -> str:
    sections = []
    for i, response in enumerate(member_responses, start=1):
        sections.append(
            f"## Member {i}: {response.model}\n\n{response.text.strip()}"
        )
    return (
        "You are synthesizing a multi-model council. Compare the independent "
        "responses, call out meaningful disagreements, resolve them when the "
        "evidence supports it, and give the final answer. Preserve uncertainty "
        "instead of flattening it into false consensus.\n\n"
        "Original request:\n"
        f"{task}\n\n"
        "Council member responses:\n\n"
        + "\n\n".join(sections)
    )


def _emit_call(
    response: CallResponse,
    *,
    as_json: bool,
    decision: RouteDecision | None = None,
    semantic_plan: SemanticPlan | None = None,
    auth_prompts: list[dict] | None = None,
) -> None:
    if as_json:
        payload = asdict(response)
        effective_auth_prompts = auth_prompts or response.auth_prompts
        if effective_auth_prompts:
            payload["auth_prompts"] = effective_auth_prompts
        else:
            payload.pop("auth_prompts", None)
        if decision is not None:
            payload["route"] = asdict(decision)
        if semantic_plan is not None:
            payload["semantic"] = _semantic_plan_payload(semantic_plan)
        click.echo(json.dumps(payload, default=str, indent=2))
    else:
        click.echo(response.text)


def _emit_provider_error(err: ProviderError, *, as_json: bool) -> None:
    payload = getattr(err, "error_response", None)
    if as_json and isinstance(payload, dict):
        click.echo(json.dumps(payload, default=str, indent=2))
        return
    click.echo(f"conductor: {err}", err=True)


def _collect_session_auth_prompts(session_log: SessionLog | None) -> list[dict] | None:
    if session_log is None:
        return None
    try:
        lines = session_log.log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    prompts: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "auth_prompt":
            continue
        data = event.get("data") or {}
        provider = data.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        key = (provider, data.get("url"))
        if key in seen:
            continue
        seen.add(key)
        prompts.append(data)
    return prompts or None


def _git_stdout(cwd: str, args: list[str], *, errors: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_RECOVERY_COMMAND_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        errors.append(f"`git {' '.join(args)}` failed: {e}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_stall_recovery_lines(cwd: str | None) -> list[str]:
    if not cwd:
        return []

    errors: list[str] = []
    root = _git_stdout(cwd, ["rev-parse", "--show-toplevel"], errors=errors)
    if not root:
        if errors:
            return [f"git recovery unavailable: {errors[0]}"]
        return []

    lines = [f"repo: {root}"]

    branch = _git_stdout(cwd, ["branch", "--show-current"], errors=errors)
    if not branch:
        short_head = _git_stdout(cwd, ["rev-parse", "--short", "HEAD"], errors=errors)
        branch = f"detached at {short_head}" if short_head else "unknown"
    lines.append(f"branch: {branch}")

    upstream = _git_stdout(
        cwd,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        errors=errors,
    )
    if upstream:
        lines.append(f"upstream: {upstream}")
        commits = _git_stdout(
            cwd,
            ["log", "--oneline", f"{upstream}..HEAD", "--"],
            errors=errors,
        )
        commit_lines = commits.splitlines() if commits else []
        if commit_lines:
            first = commit_lines[0]
            suffix = "" if len(commit_lines) == 1 else f"; latest: {first}"
            lines.append(f"unpushed commits: {len(commit_lines)}{suffix}")
        else:
            lines.append("unpushed commits: none")
    else:
        lines.append("upstream: none configured")
        commits = _git_stdout(
            cwd,
            ["log", "--oneline", f"-{GIT_RECOVERY_MAX_COMMITS}", "--"],
            errors=errors,
        )
        if commits:
            first = commits.splitlines()[0]
            lines.append(f"recent local commit: {first}")

    status = _git_stdout(cwd, ["status", "--porcelain"], errors=errors)
    if status:
        changed = status.splitlines()
        lines.append(f"working tree: {len(changed)} changed path(s)")
        for entry in changed[:GIT_RECOVERY_MAX_STATUS_PATHS]:
            lines.append(f"changed: {entry}")
        if len(changed) > GIT_RECOVERY_MAX_STATUS_PATHS:
            hidden = len(changed) - GIT_RECOVERY_MAX_STATUS_PATHS
            lines.append(f"changed: ... {hidden} more")
    elif status == "":
        lines.append("working tree: clean")

    open_pr_script = Path(root) / "scripts" / "open-pr.sh"
    if open_pr_script.exists():
        lines.append(
            "hint: inspect the diff, then run `bash scripts/open-pr.sh --auto-merge` "
            "if the branch is ready"
        )
    else:
        lines.append("hint: inspect with `git status`, then resume or finish the branch")
    return lines


def _maybe_echo_stall_recovery_hint(err: ProviderError, *, cwd: str | None) -> None:
    if not isinstance(err, ProviderStalledError):
        return
    lines = _git_stall_recovery_lines(cwd)
    if not lines:
        return
    click.echo("", err=True)
    click.echo("Recoverable git state:", err=True)
    for line in lines:
        click.echo(f"  - {line}", err=True)


def _format_route_log_line(decision: RouteDecision) -> str:
    """Single-line route summary for stderr observability."""
    tags_matched = ",".join(decision.matched_tags) or "none"
    effort_str = (
        decision.effort if isinstance(decision.effort, str) else f"{decision.effort}tok"
    )
    return (
        f"[conductor] {decision.prefer} (effort={effort_str}) → {decision.provider} "
        f"(tier: {decision.tier} · matched: {tags_matched})"
    )


def _format_usage_line(response: CallResponse) -> str:
    """Token + cost summary for stderr observability."""
    usage = response.usage or {}
    tok_in = usage.get("input_tokens")
    tok_out = usage.get("output_tokens")
    tok_think = usage.get("thinking_tokens")

    parts = [f"{response.duration_ms / 1000:.1f}s"]
    if tok_in:
        parts.append(f"{tok_in:,} tok in")
    if tok_think:
        parts.append(f"{tok_think:,} tok thinking")
    if tok_out:
        parts.append(f"{tok_out:,} tok out")
    if response.cost_usd is not None:
        parts.append(f"${response.cost_usd:.4f}")
    return "[conductor] " + " · ".join(parts)


def _format_route_ranking(decision: RouteDecision) -> list[str]:
    """Verbose ranking table for --verbose-route."""
    lines = [f"[conductor] route decision (prefer={decision.prefer}, effort={decision.effort}):"]
    if decision.tag_default_considered:
        for tag, provider, status in decision.tag_default_considered:
            picked_note = ""
            if decision.provider == provider and status == "applied":
                picked_note = ", picked"
            lines.append(
                f"[conductor] tag_default: {tag} → {provider} ({status}{picked_note})"
            )
    for i, c in enumerate(decision.ranked, start=1):
        marker = " ← picked" if i == 1 else ""
        tags = ",".join(c.matched_tags) or "none"
        lines.append(
            f"  {i}. {c.name:<8} "
            f"(tier={c.tier}[{c.tier_rank}] "
            f"tags=+{c.tag_score}:{tags} "
            f"cost≈${c.cost_score:.4f}/1k "
            f"p50={c.latency_ms}ms"
            f"){marker}"
        )
    shadow_names = {c.name for c in decision.unconfigured_shadow}
    for c in decision.unconfigured_shadow:
        tags = ",".join(c.matched_tags) or "none"
        lines.append(
            f"  ?  {c.name:<8} "
            f"(tier={c.tier}[{c.tier_rank}] "
            f"tags=+{c.tag_score}:{tags} "
            f"cost≈${c.cost_score:.4f}/1k "
            f"p50={c.latency_ms}ms"
            f") ← would rank if installed: {c.unconfigured_reason}"
        )
    # Don't duplicate unconfigured providers in the skipped list — they
    # already appear (with scores) in the shadow block above. Other skip
    # reasons (excluded, missing tools, health) still show.
    for name, reason in decision.candidates_skipped:
        if name in shadow_names:
            continue
        lines.append(f"  —  {name:<8} (skipped: {reason})")
    return lines


def _format_shadow_hint(decision: RouteDecision) -> str | None:
    """Return a stderr advisory if an unconfigured provider outranks the winner.

    Returns None when the unconfigured-shadow ranking is empty (no provider
    we couldn't actually call would have been preferable) or when the top
    shadow candidate's score isn't strictly higher than the picked provider's.
    Equal scores resolve in favor of the configured provider — there's no
    reason to nag the user about a tie.

    The advisory exists because auto-mode falling back silently to the only
    configured provider hides the cost of missing integrations. Surfacing
    this at call-time turns "I didn't know codex wasn't installed" into
    "I see codex would be a better fit; here's how to install it."
    """
    if not decision.unconfigured_shadow or not decision.ranked:
        return None
    top_shadow = decision.unconfigured_shadow[0]
    winner = decision.ranked[0]
    if top_shadow.combined_score <= winner.combined_score:
        return None
    reason = top_shadow.unconfigured_reason or "not configured"
    return (
        f"[conductor] heads-up: `{top_shadow.name}` would rank above "
        f"`{winner.name}` if configured — {reason} "
        f"(run `conductor list` for the fix)"
    )


def _emit_route_log(
    decision: RouteDecision,
    *,
    verbose: bool,
    silent: bool,
) -> None:
    if silent:
        return
    if verbose:
        for line in _format_route_ranking(decision):
            click.echo(line, err=True)
    else:
        click.echo(_format_route_log_line(decision), err=True)
    hint = _format_shadow_hint(decision)
    if hint is not None:
        click.echo(hint, err=True)


def _emit_usage_log(response: CallResponse, *, silent: bool) -> None:
    if silent:
        return
    click.echo(_format_usage_line(response), err=True)


def _start_exec_session_log(
    *,
    log_file: str | None,
    resume_session_id: str | None,
) -> SessionLog:
    try:
        return SessionLog(
            path=Path(log_file).expanduser() if log_file else None,
            session_id=resume_session_id,
        )
    except SessionLogError as e:
        raise click.ClickException(str(e)) from e


def _emit_session_route_decision(
    session_log: SessionLog | None,
    decision: RouteDecision,
) -> None:
    if session_log is None:
        return
    session_log.emit(
        "route_decision",
        {
            "provider": decision.provider,
            "prefer": decision.prefer,
            "effort": decision.effort,
            "thinking_budget": decision.thinking_budget,
            "task_tags": list(decision.task_tags),
            "matched_tags": list(decision.matched_tags),
            "tools_requested": list(decision.tools_requested),
            "sandbox": decision.sandbox,
            "tag_default_applied": decision.tag_default_applied,
            "tag_default_considered": [
                {"tag": tag, "provider": provider, "status": status}
                for tag, provider, status in decision.tag_default_considered
            ],
            "ranked": [asdict(candidate) for candidate in decision.ranked],
        },
    )


def _emit_session_usage(
    session_log: SessionLog | None,
    response: CallResponse,
) -> None:
    if session_log is None:
        return
    session_log.emit(
        "usage",
        {
            "provider": response.provider,
            "model": response.model,
            "session_id": response.session_id,
            "usage": response.usage,
            "cost_usd": response.cost_usd,
            "duration_ms": response.duration_ms,
        },
    )


def _tail_record(record: SessionRecord) -> None:
    offset = 0
    current_path = record.log_path
    current_status = record.status
    while True:
        if current_path.exists():
            with current_path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                chunk = fh.read()
                if chunk:
                    click.echo(chunk, nl=False)
                offset = fh.tell()
        if current_status != "running":
            return
        time.sleep(0.1)
        refreshed = find_session_record(record.session_id) or find_session_record(record.run_id)
        if refreshed is None:
            return
        current_path = refreshed.log_path
        current_status = refreshed.status


def _openrouter_catalog_or_exit() -> openrouter_catalog.CatalogSnapshot:
    try:
        snapshot = openrouter_catalog.read_cached_catalog()
    except ProviderHTTPError as e:
        raise click.ClickException(str(e)) from e
    if snapshot is None:
        raise click.ClickException(
            "OpenRouter catalog cache not found. Run `conductor models refresh` first."
        )
    return snapshot


def _model_capabilities(model: openrouter_catalog.ModelEntry) -> str:
    caps = []
    if model.supports_thinking:
        caps.append("thinking")
    if model.supports_tools:
        caps.append("tools")
    if model.supports_vision:
        caps.append("vision")
    return ",".join(caps) or "-"


def _maybe_emit_agent_wiring_notice(ctx: click.Context) -> None:
    if ctx.invoked_subcommand in {None, "init"}:
        return
    if os.environ.get("CONDUCTOR_AGENT_WIRING_NOTICE") == "0":
        return

    try:
        from conductor.agent_wiring import (
            agent_wiring_notice,
            should_emit_agent_wiring_notice,
        )

        include_missing = bool(getattr(sys.stderr, "isatty", lambda: False)())
        notice = agent_wiring_notice(
            current_version=__version__,
            include_missing=include_missing,
        )
        if notice is None:
            return
        key, message = notice
        if should_emit_agent_wiring_notice(key):
            click.echo(message, err=True)
    except Exception:
        # Advisory wiring freshness checks must never break the command the
        # user actually asked conductor to run.
        return


@click.group()
@click.version_option(__version__, prog_name="conductor")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Pick an LLM, give it a job."""
    _maybe_emit_agent_wiring_notice(ctx)


# --------------------------------------------------------------------------- #
# ask — semantic intent API
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--kind",
    required=True,
    type=click.Choice(SEMANTIC_KINDS),
    help="Semantic work category: research, code, review, or council.",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option("--cwd", default=None, help="Repository working directory for code/review.")
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help="Wall-clock timeout in seconds for review/exec/council provider calls.",
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help="Kill streaming exec/review providers after this many silent seconds. Set 0 to disable.",
)
@click.option("--base", default=None, help="For review: compare changes against this ref.")
@click.option("--commit", default=None, help="For review: review one commit.")
@click.option(
    "--uncommitted",
    is_flag=True,
    default=False,
    help="For review: review staged, unstaged, and untracked changes.",
)
@click.option("--title", default=None, help="For review: optional review title.")
@click.option("--task", default=None, help="The task / prompt. Alias: --brief.")
@click.option(
    "--task-file",
    default=None,
    help="Read task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option("--brief", default=None, help="Delegation brief / prompt.")
@click.option(
    "--brief-file",
    default=None,
    help="Read the delegation brief from a UTF-8 file. Use '-' to read stdin.",
)
@click.option("--log-file", default=None, help="For exec: write structured NDJSON events.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--verbose-route", is_flag=True, default=False)
@click.option("--silent-route", is_flag=True, default=False)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local ollama for supported semantic kinds. --no-offline clears it.",
)
@click.option(
    "--preflight/--no-preflight",
    "preflight",
    default=True,
    help="For exec: run a provider health probe before forwarding the task.",
)
@click.option(
    "--allow-short-brief",
    is_flag=True,
    default=False,
    help="Suppress the short-brief warning when semantic code routes to exec.",
)
def ask(
    kind: str,
    effort: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    log_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    offline: bool | None,
    preflight: bool,
    allow_short_brief: bool,
) -> None:
    """Run a task through Conductor's deterministic semantic routing matrix."""
    effort_value = _parse_effort(effort)
    plan = plan_for(kind, effort_value)
    max_stall_sec = _normalize_max_stall_sec(max_stall_sec)

    if offline is False:
        offline_mode.clear()
    elif offline is True:
        if kind == "council":
            raise click.UsageError(
                "council always routes through OpenRouter; --offline contradicts it."
            )
        if kind == "review":
            raise click.UsageError("review uses native review modes; --offline is not supported.")
        offline_mode.set_active()
        plan = with_candidate_override(plan, provider="ollama")

    if kind == "council" and plan.candidates[0].provider != "openrouter":
        raise click.UsageError("council always routes through OpenRouter.")

    review_target_count = sum(1 for value in (base, commit, uncommitted) if value)
    if review_target_count > 1:
        raise click.UsageError("use only one of --base, --commit, or --uncommitted.")

    brief_input = _read_task(task, task_file, brief=brief, brief_file=brief_file)
    if plan.mode == "exec":
        _warn_if_short_exec_brief(brief_input, allow_short_brief=allow_short_brief)
    body = brief_input.body

    if not (silent_route or as_json):
        click.echo(_format_semantic_plan_line(plan), err=True)

    if plan.mode == "council":
        try:
            response = _invoke_council(
                plan,
                task=body,
                effort=effort_value,
                timeout_sec=timeout_sec,
                rounds=DEFAULT_COUNCIL_ROUNDS,
                silent=silent_route or as_json,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
        _emit_usage_log(response, silent=silent_route or as_json)
        _emit_call(response, as_json=as_json, semantic_plan=plan)
        return

    if plan.mode == "review":
        review_exclude, review_reasons = _review_exclude_set(
            frozenset()
        )
        try:
            _provider, decision = pick(
                list(plan.tags),
                prefer=plan.prefer,
                effort=effort_value,
                exclude=review_exclude,
                shadow=True,
            )
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {_native_review_unavailable_message(review_reasons)}", err=True)
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        try:
            response, _fallbacks = _invoke_review_with_fallback(
                decision,
                task=body,
                effort=effort_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                base=base,
                commit=commit,
                uncommitted=uncommitted,
                title=title,
                silent=silent_route or as_json,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
        _emit_usage_log(response, silent=silent_route or as_json)
        _emit_call(response, as_json=as_json, decision=decision, semantic_plan=plan)
        return

    tools_set = plan.tools
    sandbox_value = plan.sandbox
    exclude_set = _semantic_candidate_exclude_set(plan, frozenset())
    try:
        provider, decision = pick(
            list(plan.tags),
            prefer=plan.prefer,
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=exclude_set,
            priority=_semantic_priority(plan),
            shadow=True,
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)

    session_log: SessionLog | None = None
    if plan.mode == "exec":
        session_log = _start_exec_session_log(log_file=log_file, resume_session_id=None)
        _emit_session_route_decision(session_log, decision)
    print_caller_banner(decision.provider, silent=silent_route or as_json)
    _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
    if plan.mode == "exec" and preflight:
        provider_obj = _provider_for_preflight(provider)
        ok, reason = provider_obj.health_probe()
        if not ok:
            if session_log is not None:
                session_log.bind_provider(provider_obj.name)
                session_log.emit(
                    "provider_failed",
                    {"provider": provider_obj.name, "error": reason or "preflight failed"},
                )
                session_log.mark_finished()
            _echo_preflight_failure(provider_obj, reason)
            sys.exit(2)

    models_by_provider = {
        candidate.provider: candidate.models
        for candidate in plan.candidates
        if candidate.models
    }
    try:
        response, _fallbacks = _invoke_with_fallback(
            decision,
            mode=plan.mode,
            task=body,
            model=None,
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                start_timeout_sec=None,
                silent=silent_route or as_json,
                session_log=session_log,
                models_by_provider=models_by_provider,
        )
    except UnsupportedCapability as e:
        if session_log is not None:
            session_log.emit("provider_failed", {"error": str(e)})
            session_log.mark_finished()
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderConfigError as e:
        if session_log is not None:
            session_log.emit("provider_failed", {"error": str(e)})
            session_log.mark_finished()
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderError as e:
        if session_log is not None:
            session_log.mark_finished()
        click.echo(f"conductor: {e}", err=True)
        _maybe_echo_stall_recovery_hint(e, cwd=cwd)
        sys.exit(1)

    _emit_usage_log(response, silent=silent_route or as_json)
    _emit_session_usage(session_log, response)
    if session_log is not None:
        session_log.mark_finished()
    _emit_call(
        response,
        as_json=as_json,
        decision=decision,
        semantic_plan=plan,
        auth_prompts=_collect_session_auth_prompts(session_log),
    )


# --------------------------------------------------------------------------- #
# call — single-turn send-a-task-to-a-provider
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--with",
    "provider_id",
    default=None,
    help=(
        "Provider identifier "
        "(kimi, claude, codex, deepseek-chat, deepseek-reasoner, gemini, ollama, openrouter). "
        "Mutually exclusive with --auto."
    ),
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags, --prefer, and configured providers.",
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags for --auto routing (e.g. 'long-context,cheap').",
)
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)} (default: balanced).",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget "
    "(default: medium).",
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option(
    "--task",
    default=None,
    help="The task / prompt. Reads stdin if omitted.\n"
    "For delegation, prefer --brief or --brief-file.\n"
    "For long briefs, prefer --brief-file or stdin to keep the prompt\n"
    "out of `ps aux`.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read the task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Delegation brief / prompt. Alias for --task with clearer intent.",
)
@click.option(
    "--brief-file",
    default=None,
    help="Read the delegation brief from a UTF-8 file. Use '-' to read stdin.",
)
@click.option(
    "--model",
    default=None,
    help="Override the provider's default model.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON (with routing info when --auto).",
)
@click.option(
    "--verbose-route",
    is_flag=True,
    default=False,
    help="Print the full routing decision (ranking table) to stderr.",
)
@click.option(
    "--silent-route",
    is_flag=True,
    default=False,
    help="Suppress the route-log line and caller-attribution banner "
    "(useful for clean stdout piping).",
)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume a prior session by ID (claude/codex/gemini only). Requires --with.",
)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local (ollama) routing and set the sticky offline "
    "flag. --no-offline: clear the sticky flag before running.",
)
def call(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    exclude: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    model: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
    offline: bool | None,
) -> None:
    """Send a task to a provider and print the response."""
    explicit_prefer = prefer
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")
    provider_id, auto = _apply_offline_flag(
        offline=offline, provider_id=provider_id, auto=auto
    )
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if resume_session_id and auto:
        raise click.UsageError(
            "--resume requires --with <provider> (sessions are provider-specific)."
        )

    # When --with is used with --exclude, it's a contradiction:
    if provider_id and exclude and provider_id in _parse_csv(exclude):
        raise click.UsageError(
            f"--with {provider_id} and --exclude {exclude} contradict each other."
        )

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
    )
    body = brief_input.body
    effort_value = _parse_effort(effort)

    decision: RouteDecision | None = None
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                exclude=frozenset(_parse_csv(exclude)),
                shadow=True,
            )
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)

        try:
            response, _fallbacks = _invoke_with_fallback(
                decision,
                mode="call",
                task=body,
                model=model,
                effort=effort_value,
                tools=frozenset(),
                sandbox="none",
                cwd=None,
                timeout_sec=None,
                max_stall_sec=None,
                start_timeout_sec=None,
                silent=silent_route or as_json,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
    else:
        if explicit_prefer is not None and provider_id != "openrouter":
            raise click.UsageError("--prefer is only meaningful with --auto.")
        # Earlier guard `if not auto and not provider_id: raise` makes this
        # narrowing safe; the assert documents it for mypy and future readers.
        assert provider_id is not None
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        print_caller_banner(provider_id, silent=silent_route or as_json)
        try:
            if isinstance(provider, OpenRouterProvider):
                response = provider.call(
                    body,
                    model=model,
                    effort=effort_value,
                    task_tags=_parse_csv(tags),
                    prefer=_validate_prefer(prefer),
                    log_selection=not (silent_route or as_json),
                    resume_session_id=resume_session_id,
                )
            else:
                response = provider.call(
                    body,
                    model=model,
                    effort=effort_value,
                    resume_session_id=resume_session_id,
                )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except UnsupportedCapability as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            _maybe_echo_explicit_network_hint(provider_id, e)
            sys.exit(1)

    if auto and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _emit_call(response, as_json=as_json, decision=decision)


# --------------------------------------------------------------------------- #
# review — first-class read-only code review
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--with",
    "provider_id",
    default=None,
    help="Native review provider identifier (codex, claude, gemini).",
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick among configured native review providers.",
)
@click.option(
    "--tags",
    default=None,
    help=(
        "Comma-separated review tags. code-review is always included for this "
        "subcommand."
    ),
)
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)} (default: best).",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option("--cwd", default=None, help="Repository working directory.")
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help="Wall-clock timeout in seconds for the native review command.",
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill streaming review providers if they produce no output for this "
        "many seconds. Set 0 to disable."
    ),
)
@click.option(
    "--max-fallbacks",
    default=DEFAULT_REVIEW_MAX_FALLBACKS,
    type=int,
    help=(
        "Maximum code-review providers to try in total for --auto review "
        f"(default: {DEFAULT_REVIEW_MAX_FALLBACKS})."
    ),
)
@click.option(
    "--base",
    default=None,
    help="Review changes against this base branch/ref.",
)
@click.option(
    "--commit",
    default=None,
    help="Review the changes introduced by one commit.",
)
@click.option(
    "--uncommitted",
    is_flag=True,
    default=False,
    help="Review staged, unstaged, and untracked changes.",
)
@click.option(
    "--title",
    default=None,
    help="Optional review title passed to providers that support it.",
)
@click.option(
    "--task",
    default=None,
    help="Review instructions. Reads stdin if omitted. Alias: --brief.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read review instructions from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Review instructions. Alias for --task.",
)
@click.option(
    "--brief-file",
    default=None,
    help="Read review instructions from a UTF-8 file. Use '-' to read stdin.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON (with routing info when --auto).",
)
@click.option(
    "--verbose-route",
    is_flag=True,
    default=False,
    help="Print the full native-review routing decision to stderr.",
)
@click.option(
    "--silent-route",
    is_flag=True,
    default=False,
    help="Suppress route-log output and caller-attribution banner.",
)
def review(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    max_fallbacks: int,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
) -> None:
    """Run a read-only code review through a provider's native review mode."""
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")

    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if provider_id and exclude and provider_id in _parse_csv(exclude):
        raise click.UsageError(
            f"--with {provider_id} and --exclude {exclude} contradict each other."
        )
    review_target_count = sum(1 for value in (base, commit, uncommitted) if value)
    if review_target_count > 1:
        raise click.UsageError("use only one of --base, --commit, or --uncommitted.")

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
    )
    body = brief_input.body
    effort_value = _parse_effort(effort)
    max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
    max_fallbacks = _validate_max_fallbacks(max_fallbacks)
    prefer_value = _validate_prefer(prefer) if prefer is not None else "best"

    decision: RouteDecision | None = None
    if auto:
        user_exclude = frozenset(_parse_csv(exclude))
        review_exclude, review_reasons = _review_exclude_set(user_exclude)
        try:
            _provider, decision = pick(
                _review_tags(tags),
                prefer=prefer_value,
                effort=effort_value,
                exclude=review_exclude,
                shadow=True,
            )
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {_native_review_unavailable_message(review_reasons)}", err=True)
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        try:
            response, _fallbacks = _invoke_review_with_fallback(
                decision,
                task=body,
                effort=effort_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                base=base,
                commit=commit,
                uncommitted=uncommitted,
                title=title,
                silent=silent_route or as_json,
                max_fallbacks=max_fallbacks,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
    else:
        assert provider_id is not None
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        review_provider = _review_provider_or_none(provider)
        if review_provider is None:
            raise click.UsageError(
                f"provider {provider_id!r} does not expose native code review."
            )
        print_caller_banner(provider_id, silent=silent_route or as_json)
        ok, reason = review_provider.review_configured()
        if not ok:
            click.echo(f"conductor: {reason or 'native review is not configured'}", err=True)
            sys.exit(2)
        try:
            response = review_provider.review(
                body,
                effort=effort_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                base=base,
                commit=commit,
                uncommitted=uncommitted,
                title=title,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)

    if auto and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _emit_call(response, as_json=as_json, decision=decision)


# --------------------------------------------------------------------------- #
# exec — multi-turn agent session with tool access
# --------------------------------------------------------------------------- #


@main.command(name="exec")
@click.option(
    "--with",
    "provider_id",
    default=None,
    help="Provider identifier. Mutually exclusive with --auto.",
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags, --prefer, and --tools.",
)
@click.option("--tags", default=None, help="Comma-separated task tags.")
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)}.",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option(
    "--tools",
    default=None,
    help=f"Comma-separated tool set: {','.join(VALID_TOOLS)}.",
)
@click.option(
    "--sandbox",
    default=None,
    help="Deprecated and ignored; exec always runs unsandboxed.",
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option(
    "--cwd",
    default=None,
    help="Working directory for file operations.",
)
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help=(
        "Wall-clock timeout in seconds. Default: no timeout — agent sessions "
        "can run as long as they need. Set explicitly (e.g. --timeout 600) "
        "for CI or unattended runs that must bound runtime."
    ),
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill the underlying provider if it produces no output for this many "
        "seconds. Default: 360, just past codex's 5-minute internal websocket "
        "idle (openai/codex#17003) so codex gets one retry attempt before "
        "conductor kills it. Set 0 to disable."
    ),
)
@click.option(
    "--start-timeout",
    "start_timeout_sec",
    default=None,
    type=float,
    help=(
        "Startup watchdog in seconds for providers that may cold-load before "
        "their first byte. Set 0 to disable. After first output, "
        "--max-stall-seconds is the only stall watchdog."
    ),
)
@click.option(
    "--task",
    default=None,
    help="The task / prompt. Reads stdin if omitted.\n"
    "For delegation, prefer --brief or --brief-file.\n"
    "For long briefs, prefer --brief-file or stdin to keep the prompt\n"
    "out of `ps aux`.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read the task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Delegation brief / prompt. Alias for --task with clearer intent.",
)
@click.option(
    "--brief-file",
    default=None,
    help="Read the delegation brief from a UTF-8 file. Use '-' to read stdin.",
)
@click.option("--model", default=None, help="Override the provider's default model.")
@click.option(
    "--log-file",
    default=None,
    help=(
        "Write structured NDJSON progress events to PATH. Defaults to "
        "~/.cache/conductor/sessions/<session_id>.ndjson."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON.",
)
@click.option("--verbose-route", is_flag=True, default=False)
@click.option("--silent-route", is_flag=True, default=False)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume a prior session by ID (claude/codex/gemini only). Requires --with.",
)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local (ollama) routing and set the sticky offline "
    "flag. --no-offline: clear the sticky flag before running.",
)
@click.option(
    "--preflight/--no-preflight",
    "preflight",
    default=True,
    help="Run a provider health probe before forwarding the task.",
)
@click.option(
    "--allow-short-brief",
    is_flag=True,
    default=False,
    help=(
        "Suppress the short-brief warning for exec delegation. Use when the "
        "brief is intentionally tiny or all context is supplied through files."
    ),
)
def exec_cmd(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    sandbox: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    start_timeout_sec: float | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    model: str | None,
    log_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
    offline: bool | None,
    preflight: bool,
    allow_short_brief: bool,
) -> None:
    """Run a task as an agent session with tool access (exec mode)."""
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    sandbox = _resolve_layered_value(
        sandbox,
        env_key="CONDUCTOR_SANDBOX",
        profile_value=profile_spec.sandbox if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")
    provider_id, auto = _apply_offline_flag(
        offline=offline, provider_id=provider_id, auto=auto
    )
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if resume_session_id and auto:
        raise click.UsageError(
            "--resume requires --with <provider> (sessions are provider-specific)."
        )

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
    )
    _warn_if_short_exec_brief(
        brief_input,
        allow_short_brief=allow_short_brief,
    )
    body = brief_input.body
    tools_set = _validate_tools(tools)
    sandbox_value = _validate_sandbox(sandbox, warn=sandbox is not None)
    effort_value = _parse_effort(effort)
    max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
    start_timeout_sec = _normalize_start_timeout_sec(start_timeout_sec)

    decision: RouteDecision | None = None
    session_log: SessionLog | None = None
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                exclude=frozenset(_parse_csv(exclude)),
                shadow=True,
            )
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        session_log = _start_exec_session_log(
            log_file=log_file,
            resume_session_id=resume_session_id,
        )
        _emit_session_route_decision(session_log, decision)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        if preflight:
            # `provider` may arrive as a Provider object (real `pick()` return)
            # or as a string (test fixtures, and any caller passing the name).
            # Resolve once via the helper so downstream `.name` access is safe.
            provider_obj = _provider_for_preflight(provider)
            ok, reason = provider_obj.health_probe()
            if not ok:
                if session_log is not None:
                    session_log.bind_provider(provider_obj.name)
                    session_log.emit(
                        "provider_failed",
                        {"provider": provider_obj.name, "error": reason or "preflight failed"},
                    )
                    session_log.mark_finished()
                _echo_preflight_failure(provider_obj, reason)
                sys.exit(2)

        try:
            response, _fallbacks = _invoke_with_fallback(
                decision,
                mode="exec",
                task=body,
                model=model,
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                start_timeout_sec=start_timeout_sec,
                silent=silent_route or as_json,
                resume_session_id=resume_session_id,
                session_log=session_log,
            )
        except UnsupportedCapability as e:
            if session_log is not None:
                session_log.emit("provider_failed", {"error": str(e)})
                session_log.mark_finished()
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderConfigError as e:
            if session_log is not None:
                session_log.emit("provider_failed", {"error": str(e)})
                session_log.mark_finished()
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            if session_log is not None:
                session_log.mark_finished()
            _emit_provider_error(e, as_json=as_json)
            _maybe_echo_stall_recovery_hint(e, cwd=cwd)
            sys.exit(1)
    else:
        if prefer is not None and provider_id != "openrouter":
            raise click.UsageError("--prefer is only meaningful with --auto.")
        # Same narrowing as in `call()` — the early guard rejects the case
        # where neither --auto nor --with was passed.
        assert provider_id is not None
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        session_log = _start_exec_session_log(
            log_file=log_file,
            resume_session_id=resume_session_id,
        )
        session_log.bind_provider(provider_id)
        print_caller_banner(provider_id, silent=silent_route or as_json)
        if preflight:
            ok, reason = _run_exec_preflight(provider)
            if not ok:
                session_log.emit(
                    "provider_failed",
                    {"provider": provider_id, "error": reason or "preflight failed"},
                )
                session_log.mark_finished()
                _echo_preflight_failure(provider, reason)
                sys.exit(2)
        try:
            session_log.emit(
                "provider_started",
                {
                    "provider": provider_id,
                    "mode": "exec",
                    "model": model,
                    "tools": sorted(tools_set),
                    "sandbox": sandbox_value,
                    "cwd": cwd,
                    "resume_session_id": resume_session_id,
                },
            )
            if isinstance(provider, OpenRouterProvider):
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    task_tags=_parse_csv(tags),
                    prefer=_validate_prefer(prefer),
                    log_selection=not (silent_route or as_json),
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                )
            else:
                exec_kwargs = {
                    "model": model,
                    "effort": effort_value,
                    "tools": tools_set,
                    "sandbox": sandbox_value,
                    "cwd": cwd,
                    "timeout_sec": timeout_sec,
                    "max_stall_sec": max_stall_sec,
                    "resume_session_id": resume_session_id,
                    "session_log": session_log,
                }
                if provider_id == "claude":
                    exec_kwargs["start_timeout_sec"] = start_timeout_sec
                response = provider.exec(
                    body,
                    **exec_kwargs,
                )
        except UnsupportedCapability as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderConfigError as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            _emit_provider_error(e, as_json=as_json)
            _maybe_echo_stall_recovery_hint(e, cwd=cwd)
            _maybe_echo_explicit_network_hint(provider_id, e)
            sys.exit(1)
        session_log.set_session_id(response.session_id)
        session_log.emit(
            "provider_finished",
            {
                "provider": response.provider,
                "model": response.model,
                "duration_ms": response.duration_ms,
                "session_id": response.session_id,
            },
        )

    if auto and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _emit_session_usage(session_log, response)
    if session_log is not None:
        session_log.mark_finished()
    _emit_call(
        response,
        as_json=as_json,
        decision=decision,
        auth_prompts=_collect_session_auth_prompts(session_log),
    )


# --------------------------------------------------------------------------- #
# route — dry-run the router and print what would happen
# --------------------------------------------------------------------------- #


@main.command()
@click.option("--tags", default=None, help="Comma-separated task tags.")
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)}.",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option("--tools", default=None, help="Comma-separated tool set.")
@click.option("--sandbox", default=None, help="Deprecated and ignored.")
@click.option("--exclude", default=None, help="Comma-separated providers to exclude.")
@click.option("--json", "as_json", is_flag=True, default=False)
def route(
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    sandbox: str | None,
    exclude: str | None,
    as_json: bool,
) -> None:
    """Dry-run the router: show which provider would be picked and why.

    Makes no upstream calls. Used for sanity-checking config + routing
    before a real `call` or `exec`.
    """
    tools_set = _validate_tools(tools)
    sandbox_value = _validate_sandbox(sandbox, warn=sandbox is not None)
    effort_value = _parse_effort(effort)

    try:
        _provider, decision = pick(
            _parse_csv(tags),
            prefer=_validate_prefer(prefer),
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=frozenset(_parse_csv(exclude)),
            shadow=True,
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        if as_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            click.echo(f"conductor: {e}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(asdict(decision), default=str, indent=2))
        return

    click.echo(f"→ would pick: {decision.provider}")
    click.echo(
        f"  tier: {decision.tier}"
        f"  ·  prefer: {decision.prefer}"
        f"  ·  effort: {decision.effort}"
        f" (thinking budget: {decision.thinking_budget} tokens)"
    )
    if decision.matched_tags:
        click.echo(f"  matched tags: {','.join(decision.matched_tags)}")
    if decision.tools_requested:
        click.echo(f"  tools requested: {','.join(decision.tools_requested)}")
    click.echo("")
    click.echo("Full ranking:")
    for line in _format_route_ranking(decision):
        click.echo("  " + line.removeprefix("[conductor] "))


# --------------------------------------------------------------------------- #
# router defaults — manage persistent tag-default preferences
# --------------------------------------------------------------------------- #


@main.group(name="router")
def router_cmd() -> None:
    """Manage persistent auto-router defaults."""


@router_cmd.group(name="defaults")
def router_defaults_cmd() -> None:
    """Inspect or edit tag → provider defaults for auto-routing."""


@router_defaults_cmd.command(name="list")
def router_defaults_list() -> None:
    """Print the effective tag-default mappings."""
    try:
        home_defaults = load_home_tag_defaults()
        effective = load_tag_defaults()
        repo_path = repo_router_defaults_path()
        repo_defaults = {}
        if repo_path.exists():
            repo_merged = load_tag_defaults(cwd=repo_path.parent.parent)
            repo_defaults = {
                tag: provider
                for tag, provider in repo_merged.items()
                if home_defaults.get(tag) != provider or tag not in home_defaults
            }
    except RouterDefaultsError as e:
        raise click.ClickException(str(e)) from e

    if not effective:
        click.echo("(no router defaults)")
        return

    for tag, provider in sorted(effective.items()):
        if tag in repo_defaults:
            source = "repo override"
        elif tag in home_defaults:
            source = "home"
        else:
            source = "effective"
        click.echo(f"{tag} = {provider} ({source})")


@router_defaults_cmd.command(name="set")
@click.argument("tag")
@click.argument("provider")
def router_defaults_set(tag: str, provider: str) -> None:
    """Write a home-level tag-default mapping."""
    if provider not in known_providers():
        raise click.UsageError(
            f"unknown provider {provider!r}. Known providers: {', '.join(known_providers())}."
        )
    try:
        path = set_home_tag_default(tag, provider)
    except RouterDefaultsError as e:
        raise click.UsageError(str(e)) from e
    click.echo(f"set {tag} → {provider} in {path}")


@router_defaults_cmd.command(name="unset")
@click.argument("tag")
def router_defaults_unset(tag: str) -> None:
    """Remove a home-level tag-default mapping."""
    try:
        path, existed = unset_home_tag_default(tag)
    except RouterDefaultsError as e:
        raise click.UsageError(str(e)) from e
    if existed:
        click.echo(f"unset {tag} from {path}")
        return
    click.echo(f"no router default for {tag} in {path}")


# --------------------------------------------------------------------------- #
# config — show effective configuration
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("subcommand", type=click.Choice(["show"]))
@click.option("--json", "as_json", is_flag=True, default=False)
def config(subcommand: str, as_json: bool) -> None:
    """Inspect conductor configuration (currently: `show` only)."""
    if subcommand != "show":
        raise click.UsageError(f"unknown config subcommand: {subcommand}")

    # Effective config is derived from env vars (no config file in v0.2 yet).
    env_overrides = {
        "CONDUCTOR_PREFER": os.environ.get("CONDUCTOR_PREFER"),
        "CONDUCTOR_EFFORT": os.environ.get("CONDUCTOR_EFFORT"),
        "CONDUCTOR_TAGS": os.environ.get("CONDUCTOR_TAGS"),
        "CONDUCTOR_SANDBOX": os.environ.get("CONDUCTOR_SANDBOX"),
        "CONDUCTOR_WITH": os.environ.get("CONDUCTOR_WITH"),
        "CONDUCTOR_EXCLUDE": os.environ.get("CONDUCTOR_EXCLUDE"),
    }
    effective = {
        "prefer": env_overrides["CONDUCTOR_PREFER"] or "balanced",
        "effort": env_overrides["CONDUCTOR_EFFORT"] or "medium",
        "tags": _parse_csv(env_overrides["CONDUCTOR_TAGS"]),
        "sandbox": env_overrides["CONDUCTOR_SANDBOX"] or "none",
        "with": env_overrides["CONDUCTOR_WITH"] or None,
        "exclude": _parse_csv(env_overrides["CONDUCTOR_EXCLUDE"]),
    }

    sources: dict[str, str] = {
        key: ("env" if val is not None else "default")
        for key, val in env_overrides.items()
    }
    payload = {
        "version": __version__,
        "effective": effective,
        "sources": sources,
        "known_providers": known_providers(),
    }

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"conductor v{payload['version']} — effective config")
    click.echo("")
    for key, val in effective.items():
        src = sources[f"CONDUCTOR_{key.upper()}"]
        if isinstance(val, list):
            val_str = ",".join(val) or "(none)"
        elif val is None:
            val_str = "(unset)"
        else:
            val_str = val
        click.echo(f"  {key:<8} = {val_str:<20}  (from: {src})")
    click.echo("")
    click.echo(f"Known providers: {', '.join(payload['known_providers'])}")
    click.echo("Run `conductor list` for per-provider configured status.")


# --------------------------------------------------------------------------- #
# profiles — inspect built-in + user-defined defaults
# --------------------------------------------------------------------------- #


@main.group(name="profiles")
def profiles_cmd() -> None:
    """Inspect named profiles for call/exec defaults."""


@profiles_cmd.command(name="list")
def profiles_list() -> None:
    """List built-in and user-defined profiles."""
    try:
        profiles = load_profiles()
    except ProfileError as e:
        raise click.UsageError(str(e)) from e

    for name in sorted(profiles):
        spec = profiles[name]
        click.echo(
            f"{name:<12} "
            f"prefer={spec.prefer or '-'} "
            f"effort={spec.effort or '-'} "
            f"tags={spec.tags or '-'} "
            f"sandbox={spec.sandbox or '-'} "
            f"[{spec.source}]"
        )


@profiles_cmd.command(name="show")
@click.argument("name")
def profiles_show(name: str) -> None:
    """Show one profile and the precedence rules around it."""
    try:
        spec = get_profile(name)
    except ProfileError as e:
        raise click.UsageError(str(e)) from e

    click.echo(f"{spec.name} [{spec.source}]")
    click.echo(f"  prefer   = {spec.prefer or '(unset)'}")
    click.echo(f"  effort   = {spec.effort or '(unset)'}")
    click.echo(f"  tags     = {spec.tags or '(unset)'}")
    click.echo(f"  sandbox  = {spec.sandbox or '(unset)'}")
    click.echo("")
    click.echo(PROFILE_PRECEDENCE_TEXT)


# --------------------------------------------------------------------------- #
# list — show provider menu + configured status
# --------------------------------------------------------------------------- #


def _provider_rows() -> list[dict]:
    muted = set(load_muted_provider_ids(known=set(known_providers())))
    rows = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        rows.append(
            {
                "provider": name,
                "configured": ok,
                "reason": None if ok else reason,
                # Copy-pasteable shell one-liner that takes the user from
                # "not configured" to "configured". None for providers
                # without a canonical recipe (e.g. user-defined shell
                # providers).
                "fix_command": (
                    None if ok else getattr(provider, "fix_command", None)
                ),
                "default_model": _provider_default_model(provider),
                "tags": list(provider.tags),
                "tier": provider.quality_tier,
                "muted": name in muted,
            }
        )
    return rows


@main.command(name="list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the provider list as JSON.",
)
def list_cmd(as_json: bool) -> None:
    """Show every known provider and whether it's configured."""
    try:
        rows = _provider_rows()
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return

    name_w = max(len("PROVIDER"), max(len(r["provider"]) for r in rows))
    model_w = max(len("DEFAULT MODEL"), max(len(r["default_model"]) for r in rows))
    tier_w = max(len("TIER"), max(len(r["tier"]) for r in rows))
    header = (
        f"{'PROVIDER':<{name_w}}  "
        f"{'READY':<5}  "
        f"{'TIER':<{tier_w}}  "
        f"{'DEFAULT MODEL':<{model_w}}  TAGS"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        ready = "yes" if r["configured"] else "no"
        tags = ",".join(r["tags"])
        click.echo(
            f"{r['provider']:<{name_w}}  "
            f"{ready:<5}  "
            f"{r['tier']:<{tier_w}}  "
            f"{r['default_model']:<{model_w}}  "
            f"{tags}"
        )
        if not r["configured"] and r["reason"]:
            click.echo(f"{'':<{name_w}}  {'':<5}  └─ {r['reason']}")
        if not r["configured"] and r["fix_command"]:
            click.echo(f"{'':<{name_w}}  {'':<5}  → fix: {r['fix_command']}")


# --------------------------------------------------------------------------- #
# smoke — run one or all providers' smoke tests
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("provider_id", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    default=False,
    help="Run smoke tests for every configured provider.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit results as JSON.",
)
def smoke(provider_id: str | None, run_all: bool, as_json: bool) -> None:
    """Prove a provider's auth + endpoint actually work."""
    if provider_id and run_all:
        raise click.UsageError("pass a provider id OR --all, not both.")
    if not provider_id and not run_all:
        raise click.UsageError("pass a provider id or --all.")

    if provider_id:
        if provider_id not in known_providers():
            raise click.UsageError(
                f"unknown provider {provider_id!r}; known: {known_providers()}"
            )
        targets = [provider_id]
    else:
        targets = [
            name for name in known_providers()
            if get_provider(name).configured()[0]
        ]

    results = []
    any_failed = False
    for name in targets:
        provider = get_provider(name)
        ok, reason = provider.smoke()
        results.append({"provider": name, "ok": ok, "reason": reason})
        if not ok:
            any_failed = True

    if as_json:
        click.echo(json.dumps(results, indent=2))
    else:
        if not results:
            click.echo("no configured providers to smoke-test.")
        for r in results:
            symbol = "✓" if r["ok"] else "✗"
            click.echo(f"{symbol} {r['provider']}")
            if not r["ok"] and r["reason"]:
                click.echo(f"  {r['reason']}")

    if any_failed:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# doctor — diagnostic report (install + env + keychain)
# --------------------------------------------------------------------------- #


_DIAGNOSTIC_ENV_VARS = (
    "OLLAMA_BASE_URL",
    "CONDUCTOR_OLLAMA_MODEL",
    "OPENROUTER_API_KEY",
)

_HTTP_PROVIDER_CREDENTIAL_ENV_VARS = {
    "deepseek-chat": "OPENROUTER_API_KEY",
    "deepseek-reasoner": "OPENROUTER_API_KEY",
    "kimi": "OPENROUTER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _credential_fingerprint(value: str) -> str:
    """Return a non-secret fingerprint for a resolved credential."""
    if len(value) <= 4:
        return value
    return f"{value[:-4]}...{value[-4:]}"


def _provider_default_model(provider: object) -> str:
    resolver = getattr(provider, "resolved_default_model", None)
    if callable(resolver):
        return str(resolver())
    return str(getattr(provider, "default_model", ""))


def _active_credential_row(provider: object, *, configured: bool) -> dict | None:
    """Summarize the credential Conductor would use for one provider.

    Only configured providers get a row. The doctor report remains derived
    from the same configured() gate the router uses, while this helper adds
    the provider-specific credential detail that was previously missing.
    """
    provider_name = getattr(provider, "name", None)
    if not configured or not isinstance(provider_name, str):
        return None

    if provider_name == "ollama":
        detail = "no credential (local)"
        return {
            "provider": provider_name,
            "kind": "local",
            "source": "local",
            "env_var": None,
            "fingerprint": None,
            "detail": detail,
        }

    # CLI-backed providers own auth inside the external CLI session; Conductor
    # does not resolve or persist a secret for them directly.
    if hasattr(provider, "auth_login_command"):
        cli_name = getattr(provider, "_cli", provider_name)
        detail = f"OAuth via `{cli_name}` CLI session (no env var)"
        return {
            "provider": provider_name,
            "kind": "cli_session",
            "source": "cli_session",
            "env_var": None,
            "fingerprint": None,
            "detail": detail,
        }

    env_var = _HTTP_PROVIDER_CREDENTIAL_ENV_VARS.get(provider_name)
    if env_var is None:
        return None

    value, source = credentials.resolve_with_source(env_var)
    if value is None or source is None:
        return None

    fingerprint = _credential_fingerprint(value)
    detail = f"{env_var} ({source}, {fingerprint})"
    return {
        "provider": provider_name,
        "kind": "env_var",
        "source": source,
        "env_var": env_var,
        "fingerprint": fingerprint,
        "detail": detail,
    }


def _diagnostic_payload() -> dict:
    muted_list = load_muted_provider_ids(known=set(known_providers()))
    muted = set(muted_list)
    providers_info = []
    active_credentials = []
    warnings: list[dict] = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        provider_warnings: list[str] = []

        # Provider-specific health probes: daemon up but default model missing,
        # token nearly expired, etc. Kept in the CLI layer so each provider's
        # core interface stays minimal.
        if ok and hasattr(provider, "default_model_available"):
            model_ok, model_reason = provider.default_model_available()
            if not model_ok:
                provider_warnings.append(model_reason or "default model unavailable")
                warnings.append(
                    {"provider": name, "level": "warning", "message": model_reason}
                )

        providers_info.append(
            {
                "provider": name,
                "configured": ok,
                "reason": None if ok else reason,
                "fix_command": (
                    None if ok else getattr(provider, "fix_command", None)
                ),
                "default_model": _provider_default_model(provider),
                "tags": list(provider.tags),
                "quality_tier": provider.quality_tier,
                "supports_effort": provider.supports_effort,
                "warnings": provider_warnings,
                "muted": name in muted,
            }
        )
        active = _active_credential_row(provider, configured=ok)
        if active is not None:
            active_credentials.append(active)

    env_info = []
    key_commands = credentials.load_key_commands()
    for var in _DIAGNOSTIC_ENV_VARS:
        in_env = var in os.environ
        in_keychain = credentials.keychain_has(var)
        has_key_command = var in key_commands
        if in_env:
            source = "env"
        elif has_key_command:
            source = "key_command"
        elif in_keychain:
            source = "keychain"
        else:
            source = None
        env_info.append(
            {
                "name": var,
                "in_env": in_env,
                "in_keychain": in_keychain,
                "has_key_command": has_key_command,
                "source": source,
            }
        )

    openrouter_value, _ = credentials.resolve_with_source("OPENROUTER_API_KEY")
    legacy_kimi_detected = any(
        (
            var in os.environ
            or var in credentials.load_key_commands()
            or credentials.keychain_has(var)
        )
        for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
    )

    if "DEEPSEEK_API_KEY" in os.environ and openrouter_value is None:
        warnings.append(
            {
                "provider": "deepseek-chat",
                "level": "warning",
                "message": (
                    "DEEPSEEK_API_KEY is deprecated for deepseek-chat and "
                    "deepseek-reasoner. Set OPENROUTER_API_KEY and run "
                    "`conductor init --only openrouter`."
                ),
            }
        )
    if legacy_kimi_detected and openrouter_value is None:
        warnings.append(
            {
                "provider": "kimi",
                "level": "warning",
                "message": (
                    "kimi now routes through OpenRouter; CLOUDFLARE_* credentials "
                    "are no longer used. Set OPENROUTER_API_KEY and run "
                    "`conductor init --only openrouter`."
                ),
            }
        )

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "providers": providers_info,
        "muted": muted_list,
        "credentials": env_info,
        "active_credentials": active_credentials,
        "agent_integration": _agent_integration_payload(),
        "warnings": warnings,
    }


def _agent_integration_payload() -> dict:
    """Summarize the state of agent-integration wiring (see agent_wiring.py)."""
    from conductor.agent_wiring import detect

    detection = detect()
    kinds = {a.kind for a in detection.managed}
    return {
        "claude_detected": detection.claude_detected,
        "claude_cli_on_path": detection.claude_cli_on_path,
        "claude_home": str(detection.claude_home),
        "claude_home_exists": detection.claude_home_exists,
        "conductor_home": str(detection.conductor_home),
        "agents_md_path": str(detection.agents_md),
        "agents_md_exists": detection.agents_md_exists,
        "agents_md_wired": "agents-md-import" in kinds,
        "gemini_md_path": str(detection.gemini_md),
        "gemini_md_exists": detection.gemini_md_exists,
        "gemini_md_wired": "gemini-md-import" in kinds,
        "claude_md_repo_path": str(detection.claude_md_repo),
        "claude_md_repo_exists": detection.claude_md_repo_exists,
        "claude_md_repo_wired": "claude-md-repo-import" in kinds,
        "cursor_rules_dir": str(detection.cursor_rules_dir),
        "cursor_rules_dir_exists": detection.cursor_rules_dir_exists,
        "cursor_rule_wired": "cursor-rule" in kinds,
        "managed_files": [
            {"path": str(a.path), "kind": a.kind, "version": a.version}
            for a in detection.managed
        ],
    }


@main.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit diagnostic report as JSON.",
)
def doctor(as_json: bool) -> None:
    """Diagnose what's configured, what's missing, and where to look."""
    try:
        payload = _diagnostic_payload()
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    from conductor.banner import SUBTITLE_DOCTOR, print_banner

    print_banner(SUBTITLE_DOCTOR, payload["version"])
    click.echo(
        f"{payload['platform']}  ·  python {payload['python']}"
    )
    click.echo("")
    configured = [p for p in payload["providers"] if p["configured"]]
    unconfigured = [
        p for p in payload["providers"] if not p["configured"] and not p["muted"]
    ]
    active = [p for p in payload["providers"] if not p["muted"]]
    muted = payload["muted"]

    def _provider_line(p: dict) -> None:
        symbol = "✓" if p["configured"] else "✗"
        effort_note = "" if p["supports_effort"] else " (no thinking mode)"
        click.echo(
            f"    {symbol} {p['provider']:<8}  "
            f"tier={p['quality_tier']:<8}  "
            f"default={p['default_model']}{effort_note}"
        )
        if p["configured"]:
            click.echo(
                f"        Verify end-to-end: conductor smoke {p['provider']}"
            )
        if not p["configured"]:
            click.echo(f"        └─ {p['reason']}")
            if p.get("fix_command"):
                click.echo(f"        → fix: {p['fix_command']}")
        for w in p.get("warnings") or []:
            click.echo(f"        ⚠ {w}")

    click.echo(
        f"Providers ({len([p for p in configured if not p['muted']])}/{len(active)} active, "
        f"{len(muted)} muted):"
    )
    if configured:
        click.echo("  Configured:")
        for p in configured:
            _provider_line(p)
    if unconfigured:
        if configured:
            click.echo("")
        click.echo("  Available (not configured):")
        for p in unconfigured:
            _provider_line(p)
    if muted:
        click.echo("")
        click.echo(f"  Muted: {', '.join(muted)}")

    click.echo("")
    click.echo("Credentials (active source per env-var):")
    for c in payload["credentials"]:
        source_label = {
            "env": "✓ env",
            "key_command": "✓ key_command (secret manager)",
            "keychain": "✓ keychain",
            None: "—",
        }.get(c["source"], "—")
        click.echo(f"  {c['name']:<24}  {source_label}")

    if payload["warnings"]:
        click.echo("")
        click.echo("Warnings:")
        for warning in payload["warnings"]:
            click.echo(f"  ⚠ {warning['message']}")

    click.echo("")
    click.echo("Active credentials (per provider):")
    for row in payload["active_credentials"]:
        click.echo(f"  {row['provider']:<14} {row['detail']}")

    click.echo("")
    click.echo("Agent integration:")
    ai = payload["agent_integration"]

    _repo_kinds = {
        "agents-md-import", "gemini-md-import",
        "claude-md-repo-import", "cursor-rule",
    }
    user_managed = [f for f in ai["managed_files"] if f["kind"] not in _repo_kinds]
    if not ai["claude_detected"]:
        click.echo("  Claude Code:  not detected")
    elif not user_managed:
        click.echo("  Claude Code:  detected, not wired (run `conductor init`)")
    else:
        click.echo(f"  Claude Code:  wired — {len(user_managed)} user-scope files")
        for f in user_managed:
            version_note = f" v{f['version']}" if f["version"] else ""
            click.echo(f"    {f['kind']:<18}  {f['path']}{version_note}")

    def _repo_line(
        label: str,
        file_kind: str,
        exists_key: str,
        wired_key: str,
        path_key: str,
    ) -> None:
        if not ai[exists_key] and not ai[wired_key]:
            click.echo(f"  {label}  no {label.split(':')[0].strip()} in current directory")
            return
        if ai[wired_key]:
            entry = next(
                (f for f in ai["managed_files"] if f["kind"] == file_kind),
                None,
            )
            version_note = f" v{entry['version']}" if entry and entry["version"] else ""
            click.echo(f"  {label}  wired — {ai[path_key]}{version_note}")
        else:
            # The file itself is loaded normally by its host agent; only
            # Conductor's per-repo delegation block is missing. Spell that out
            # so "present but not wired" doesn't read as "the file is broken".
            click.echo(
                f"  {label}  no Conductor delegation block — {ai[path_key]}"
            )
            click.echo(
                "                (file still loads normally for its agent; "
                "Conductor would add per-repo"
            )
            click.echo(
                "                routing hints via `conductor init`.)"
            )

    _repo_line("AGENTS.md:   ", "agents-md-import",
               "agents_md_exists", "agents_md_wired", "agents_md_path")
    _repo_line("GEMINI.md:   ", "gemini-md-import",
               "gemini_md_exists", "gemini_md_wired", "gemini_md_path")
    _repo_line("CLAUDE.md:   ", "claude-md-repo-import",
               "claude_md_repo_exists", "claude_md_repo_wired", "claude_md_repo_path")

    # Cursor is a fully-managed file inside a conventional directory, not a
    # sentinel-block patch. Its detection story is "does .cursor/rules/ exist".
    if not ai["cursor_rules_dir_exists"] and not ai["cursor_rule_wired"]:
        click.echo("  Cursor:       no .cursor/rules/ in current directory")
    elif ai["cursor_rule_wired"]:
        entry = next(
            (f for f in ai["managed_files"] if f["kind"] == "cursor-rule"),
            None,
        )
        version_note = f" v{entry['version']}" if entry and entry["version"] else ""
        click.echo(f"  Cursor:       rule wired{version_note}")
    else:
        click.echo(
            "  Cursor:       no Conductor rule in .cursor/rules/ "
            "(run `conductor init` to add one)"
        )

    click.echo("")
    click.echo("Next steps:")
    not_configured = [
        p for p in payload["providers"] if not p["configured"] and not p["muted"]
    ]
    if not not_configured:
        if payload["muted"]:
            click.echo(
                "  all remaining providers are either configured or muted. "
                "try `conductor smoke --all`."
            )
        else:
            click.echo("  everything is configured. try `conductor smoke --all`.")
    else:
        click.echo("  run `conductor init` to configure missing providers interactively,")
        click.echo("  or set the env vars listed above and re-run `conductor doctor`.")


# --------------------------------------------------------------------------- #
# init — interactive setup wizard
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--yes",
    "-y",
    "accept_defaults",
    is_flag=True,
    default=False,
    help="Accept all defaults without prompting (non-TTY friendly).",
)
@click.option(
    "--only",
    default=None,
    help="Configure only the named provider (skips others).",
)
@click.option(
    "--remaining",
    is_flag=True,
    default=False,
    help="Resume setup with only the not-yet-configured providers.",
)
@click.option(
    "--wire-agents",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Wire conductor into detected agent tools (Claude Code today). "
    "Default: yes for unscoped init; pass no to opt out.",
)
@click.option(
    "--patch-claude-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Add the delegation-guidance @import line to ~/.claude/CLAUDE.md. "
    "Default: yes when Claude Code is detected; pass no to opt out.",
)
@click.option(
    "--patch-agents-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject a conductor delegation block into ./AGENTS.md "
    "(Codex / Cursor / Zed convention). Default: yes for unscoped init.",
)
@click.option(
    "--patch-gemini-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject a conductor delegation block into ./GEMINI.md "
    "(Gemini CLI convention). Default: yes when GEMINI.md exists.",
)
@click.option(
    "--patch-claude-md-repo",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject @import into repo-scope ./CLAUDE.md (parallel to "
    "--patch-claude-md for user-scope). Default: yes when CLAUDE.md exists.",
)
@click.option(
    "--wire-cursor",
    "wire_cursor_flag",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Write a managed Cursor rule at .cursor/rules/conductor-delegation.mdc. "
    "Default: yes when .cursor/rules/ exists.",
)
@click.option(
    "--unwire",
    is_flag=True,
    default=False,
    help="Remove every conductor-managed agent integration artifact "
    "(user-scope + repo-scope sentinel blocks + Cursor rule) and exit.",
)
def init(
    accept_defaults: bool,
    only: str | None,
    remaining: bool,
    wire_agents: str | None,
    patch_claude_md: str | None,
    patch_agents_md: str | None,
    patch_gemini_md: str | None,
    patch_claude_md_repo: str | None,
    wire_cursor_flag: str | None,
    unwire: bool,
) -> None:
    """Interactively configure Conductor for first use."""
    if unwire:
        wiring_flags = (
            wire_agents, patch_claude_md, patch_agents_md,
            patch_gemini_md, patch_claude_md_repo, wire_cursor_flag,
        )
        if only or remaining or any(f is not None for f in wiring_flags):
            raise click.UsageError(
                "--unwire can't be combined with provider or wiring flags."
            )
        sys.exit(_run_unwire())

    if only and remaining:
        raise click.UsageError("--only and --remaining are mutually exclusive.")
    if only and only not in known_providers():
        raise click.UsageError(
            f"unknown provider {only!r}; known: {known_providers()}"
        )
    exit_code = run_init_wizard(
        accept_defaults=accept_defaults,
        only=only,
        remaining=remaining,
        wire_agents=wire_agents,
        patch_claude_md=patch_claude_md,
        patch_agents_md=patch_agents_md,
        patch_gemini_md=patch_gemini_md,
        patch_claude_md_repo=patch_claude_md_repo,
        wire_cursor_flag=wire_cursor_flag,
    )
    sys.exit(exit_code)


def _run_unwire() -> int:
    """Remove every managed agent-integration artifact. Returns an exit code."""
    from conductor.agent_wiring import unwire

    report = unwire()
    if not report.removed and not report.skipped:
        click.echo("No conductor-managed agent integration files found.")
        return 0

    if report.removed:
        click.echo("Removed:")
        for p in report.removed:
            click.echo(f"  {p}")
    if report.skipped:
        click.echo("")
        click.echo("Skipped (not conductor-managed):")
        for path, reason in report.skipped:
            click.echo(f"  {path}  — {reason}")
    return 0


# --------------------------------------------------------------------------- #
# sessions — inspect structured exec logs
# --------------------------------------------------------------------------- #


@main.group()
def sessions() -> None:
    """Inspect structured session logs for `conductor exec`."""


@sessions.command("list")
def sessions_list() -> None:
    """List known session logs with their latest status."""
    records = list_session_records()
    if not records:
        click.echo("(no session logs)")
        return

    click.echo(
        "SESSION ID                           STATUS    UPDATED                      PROVIDER"
    )
    click.echo("--------------------------------------------------------------------------------------")
    for record in reversed(records):
        provider = record.provider or "-"
        click.echo(
            f"{record.session_id:<36}  "
            f"{record.status:<8}  "
            f"{record.updated_at:<27}  "
            f"{provider}"
        )


@sessions.command("tail")
@click.argument("session_id", required=False)
def sessions_tail(session_id: str | None) -> None:
    """Print a session log and follow it while the session is running."""
    if session_id is None:
        record = latest_active_session()
        if record is None:
            click.echo("no active session")
            return
    else:
        record = find_session_record(session_id)
        if record is None:
            raise click.ClickException(f"unknown session {session_id!r}")

    _tail_record(record)


# --------------------------------------------------------------------------- #
# models — inspect and refresh the OpenRouter catalog cache
# --------------------------------------------------------------------------- #


@main.group()
def models() -> None:
    """Inspect and refresh the cached OpenRouter model catalog."""


@models.command("refresh")
def models_refresh() -> None:
    """Fetch the live OpenRouter catalog and rewrite the local cache."""
    try:
        snapshot = openrouter_catalog.load_catalog_snapshot(force_refresh=True)
    except ProviderHTTPError as e:
        raise click.ClickException(str(e)) from e

    click.echo(
        f"Refreshed OpenRouter catalog at "
        f"{openrouter_catalog.format_timestamp(snapshot.fetched_at)}"
    )
    click.echo(
        f"  {len(snapshot.models)} models · cache TTL "
        f"{openrouter_catalog.cache_ttl_hours()}h · written to "
        f"{openrouter_catalog.display_cache_path()}"
    )


@models.command("list")
def models_list() -> None:
    """Print the cached OpenRouter catalog summary."""
    snapshot = _openrouter_catalog_or_exit()
    click.echo(
        f"{len(snapshot.models)} models indexed, last refresh: "
        f"{openrouter_catalog.format_timestamp(snapshot.fetched_at)}"
    )
    click.echo(
        f"  cache TTL {openrouter_catalog.cache_ttl_hours()}h · "
        f"cache file {openrouter_catalog.display_cache_path()}"
    )
    click.echo("")

    sorted_models = sorted(snapshot.models, key=lambda model: model.id)
    if not sorted_models:
        click.echo("(catalog cache is empty — run `conductor models refresh` to populate)")
        return
    id_w = max(len("MODEL"), max(len(model.id) for model in sorted_models))
    ctx_w = max(len("CTX"), max(len(f"{model.context_length:,}") for model in sorted_models))
    header = (
        f"{'MODEL':<{id_w}}  {'CTX':>{ctx_w}}  {'IN/1K':>10}  "
        f"{'OUT/1K':>10}  CAPS"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for model in sorted_models:
        click.echo(
            f"{model.id:<{id_w}}  "
            f"{model.context_length:>{ctx_w},}  "
            f"{model.pricing_prompt:>10.6f}  "
            f"{model.pricing_completion:>10.6f}  "
            f"{_model_capabilities(model)}"
        )


@models.command("show")
@click.argument("slug")
def models_show(slug: str) -> None:
    """Print one cached OpenRouter model's parsed details."""
    snapshot = _openrouter_catalog_or_exit()
    model = next((entry for entry in snapshot.models if entry.id == slug), None)
    if model is None:
        raise click.ClickException(
            f"OpenRouter model {slug!r} was not found in the local cache. "
            "Run `conductor models refresh`."
        )

    thinking_price = (
        "n/a"
        if model.pricing_thinking is None
        else f"{model.pricing_thinking:.6f} USD / 1k"
    )
    click.echo(model.id)
    click.echo(f"  name: {model.name}")
    click.echo(f"  created: {openrouter_catalog.format_timestamp(model.created)}")
    click.echo(f"  context length: {model.context_length:,}")
    click.echo(f"  prompt price: {model.pricing_prompt:.6f} USD / 1k")
    click.echo(f"  completion price: {model.pricing_completion:.6f} USD / 1k")
    click.echo(f"  thinking price: {thinking_price}")
    click.echo(
        "  capabilities: "
        f"thinking={'yes' if model.supports_thinking else 'no'} · "
        f"tools={'yes' if model.supports_tools else 'no'} · "
        f"vision={'yes' if model.supports_vision else 'no'}"
    )


# --------------------------------------------------------------------------- #
# providers — manage user-local custom (shell-command) providers
# --------------------------------------------------------------------------- #


@main.group()
def providers() -> None:
    """Manage user-local provider state (custom integrations + muting).

    Custom providers let you register an arbitrary CLI — your own
    internal LLM wrapper, a different model's inference script, a local
    model server's CLI frontend — as a first-class Conductor provider.
    Once registered, it appears in `conductor list`, participates in
    auto-routing, and is callable via `conductor call --with <name>`.

    Custom providers are single-turn (no tool-use) and stateless (no
    resume). For CLIs that run their own agent loop internally, that
    happens inside the shell command, not through Conductor's router.

    Muting is persistent: muted providers are hidden from doctor's
    "Available" section and excluded from auto-routing until unmuted.
    """


@providers.command("add")
@click.option(
    "--name",
    required=True,
    help="Identifier used for --with and auto-routing. Must be unique, not a built-in name.",
)
@click.option(
    "--shell",
    required=True,
    help="The shell command to run. First token must be on PATH (shutil.which). "
    "Supports quoted arguments via standard shell quoting.",
)
@click.option(
    "--accepts",
    type=click.Choice(["stdin", "argv"]),
    default="stdin",
    show_default=True,
    help="How the prompt reaches the command. `stdin`: piped on stdin (default). "
    "`argv`: appended as the last positional argument.",
)
@click.option(
    "--tags",
    default="",
    help="Comma-separated capability tags for auto-routing (e.g. 'code-review,offline').",
)
@click.option(
    "--tier",
    type=click.Choice(list(QUALITY_TIERS)),
    default="local",
    show_default=True,
    help="Quality tier for prefer=best scoring.",
)
@click.option(
    "--cost-per-1k-in",
    type=float,
    default=0.0,
    help="Input cost in USD per 1,000 tokens (for prefer=cheapest scoring).",
)
@click.option(
    "--cost-per-1k-out",
    type=float,
    default=0.0,
    help="Output cost in USD per 1,000 tokens.",
)
@click.option(
    "--typical-p50-ms",
    type=int,
    default=3000,
    show_default=True,
    help="Typical p50 latency in milliseconds (for prefer=fastest scoring).",
)
def providers_add(
    name: str,
    shell: str,
    accepts: str,
    tags: str,
    tier: str,
    cost_per_1k_in: float,
    cost_per_1k_out: float,
    typical_p50_ms: int,
) -> None:
    """Register a custom shell-command provider."""
    from conductor.custom_providers import CustomProviderError, add_spec
    from conductor.providers.shell import ShellProviderSpec

    try:
        spec = ShellProviderSpec(
            name=name,
            shell=shell,
            accepts=accepts,  # type: ignore[arg-type]
            tags=tuple(t.strip() for t in tags.split(",") if t.strip()),
            quality_tier=tier,
            cost_per_1k_in=cost_per_1k_in,
            cost_per_1k_out=cost_per_1k_out,
            typical_p50_ms=typical_p50_ms,
        )
    except (TypeError, ValueError) as e:
        raise click.UsageError(f"invalid provider spec: {e}") from e

    # Guard against shadowing built-ins — the loader does the same check
    # when reading the file, but catching it here gives a friendlier error
    # before the file is touched.
    if name in {
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
    }:
        raise click.UsageError(
            f"`{name}` is a built-in provider identifier. Pick a different name."
        )

    try:
        path = add_spec(spec)
    except CustomProviderError as e:
        raise click.UsageError(str(e)) from e

    click.echo(f"==> registered custom provider `{name}`")
    click.echo(f"    shell:   {shell}")
    click.echo(f"    accepts: {accepts}")
    click.echo(f"    tier:    {tier}")
    if spec.tags:
        click.echo(f"    tags:    {', '.join(spec.tags)}")
    click.echo(f"    file:    {path}")
    click.echo("")
    click.echo(f"Try it: conductor smoke {name}")
    click.echo(f"Use it: conductor call --with {name} --brief 'hello'")


@providers.command("remove")
@click.argument("name")
def providers_remove(name: str) -> None:
    """Remove a custom provider by name."""
    from conductor.custom_providers import remove_spec

    path, removed = remove_spec(name)
    if not removed:
        click.echo(f"conductor: no custom provider `{name}` (check {path})", err=True)
        sys.exit(1)
    click.echo(f"==> removed custom provider `{name}` from {path}")


@providers.command("mute")
@click.argument("names", nargs=-1, required=True)
def providers_mute(names: tuple[str, ...]) -> None:
    """Persistently mute one or more providers."""
    try:
        path, added = mute_provider_ids(list(names), known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.UsageError(str(e)) from e

    if added:
        click.echo(f"==> muted: {', '.join(added)}")
    else:
        click.echo("==> no changes; all requested providers were already muted")
    click.echo(f"    file: {path}")


@providers.command("unmute")
@click.argument("names", nargs=-1, required=True)
def providers_unmute(names: tuple[str, ...]) -> None:
    """Remove one or more providers from the persistent mute list."""
    try:
        path, removed = unmute_provider_ids(list(names), known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.UsageError(str(e)) from e

    if removed:
        click.echo(f"==> unmuted: {', '.join(removed)}")
    else:
        click.echo("==> no changes; none of the requested providers were muted")
    click.echo(f"    file: {path}")


@providers.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the custom-provider list as JSON.",
)
def providers_list(as_json: bool) -> None:
    """Show persistent muted state plus registered custom providers."""
    from conductor.custom_providers import load_specs, providers_file_path

    try:
        muted = load_muted_provider_ids(known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e

    specs = load_specs()
    if as_json:
        payload = [
            {
                "name": s.name,
                "shell": s.shell,
                "accepts": s.accepts,
                "tags": list(s.tags),
                "tier": s.quality_tier,
                "cost_per_1k_in": s.cost_per_1k_in,
                "cost_per_1k_out": s.cost_per_1k_out,
                "typical_p50_ms": s.typical_p50_ms,
                "muted": s.name in muted,
            }
            for s in specs
        ]
        click.echo(json.dumps(payload, indent=2))
        return

    path = providers_file_path()
    muted_path = muted_providers_file_path()
    click.echo(
        "Muted providers: "
        + (", ".join(muted) if muted else "(none)")
    )
    click.echo(f"file: {muted_path} {'(not yet created)' if not muted_path.exists() else ''}")
    click.echo("")

    if not specs:
        click.echo("(no custom providers; register via `conductor providers add`)")
        click.echo(f"file: {path} {'(not yet created)' if not path.exists() else ''}")
        return

    click.echo(f"Custom providers ({path}):")
    click.echo("")
    for s in specs:
        muted_note = "  [muted]" if s.name in muted else ""
        click.echo(f"  {s.name}{muted_note}")
        click.echo(f"    shell:    {s.shell}")
        click.echo(f"    accepts:  {s.accepts}")
        click.echo(f"    tier:     {s.quality_tier}")
        if s.tags:
            click.echo(f"    tags:     {', '.join(s.tags)}")
        if s.cost_per_1k_in or s.cost_per_1k_out:
            click.echo(
                f"    cost:     ${s.cost_per_1k_in}/1k in, ${s.cost_per_1k_out}/1k out"
            )


if __name__ == "__main__":
    main()
