"""Conductor CLI — call, exec, list, smoke, doctor, init, route, config.

v0.2 surface (call/exec):
  conductor call --with <id> [--effort max] --task "..."
  conductor call --auto [--tags a,b] [--prefer best] [--effort max] --task "..."
  conductor exec --auto [--tools Read,Grep,Edit] [--sandbox read-only] --task "..."

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
import sys
from dataclasses import asdict

import click

from conductor import __version__, credentials
from conductor.providers import (
    QUALITY_TIERS,
    CallResponse,
    ProviderConfigError,
    ProviderError,
    UnsupportedCapability,
    get_provider,
    known_providers,
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
from conductor.wizard import run_init_wizard

VALID_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
VALID_SANDBOXES = ("read-only", "workspace-write", "strict", "none")
VALID_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _read_task(task: str | None) -> str:
    if task is not None:
        body = task
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise click.UsageError(
            "no task provided. Pass --task '...' or pipe content on stdin."
        )
    body = body.strip()
    if not body:
        raise click.UsageError("task is empty after stripping whitespace.")
    return body


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


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


def _validate_sandbox(raw: str | None) -> str:
    if raw is None:
        return "none"
    if raw not in VALID_SANDBOXES:
        hint = _closest(raw, VALID_SANDBOXES)
        raise click.UsageError(
            f"--sandbox={raw!r} is not valid. "
            f"Use one of: {list(VALID_SANDBOXES)}. "
            f"Did you mean '{hint}'?"
        )
    return raw


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


def _closest(query: str, options: tuple[str, ...]) -> str:
    from difflib import get_close_matches

    match = get_close_matches(query, options, n=1, cutoff=0.3)
    return match[0] if match else options[0]


_RETRYABLE_ERROR_SIGNALS = (
    " 5",              # 5xx HTTP codes in error strings (e.g., "HTTP 503:")
    "429",             # rate-limit
    "overloaded",      # anthropic 529 wording
    "timed out",       # subprocess + httpx
    "timeout",
    "rate limit",
    "ratelimit",
    "upstream",
)


def _is_retryable(err: Exception) -> tuple[bool, str]:
    """Classify an error as retryable-with-fallback or fatal.

    Returns (retryable, category) — category is "rate-limit" | "5xx" |
    "timeout" | "other" for health-tracking purposes.
    """
    msg = str(err).lower()
    if "429" in msg or "rate limit" in msg or "ratelimit" in msg:
        return True, "rate-limit"
    if "timed out" in msg or "timeout" in msg:
        return True, "timeout"
    # HTTP 5xx — check for " 5" preceded by "http" or a similar prefix so
    # we don't match arbitrary "5" digits. Cheap heuristic; acceptable.
    signals = ("http 5", "returned http 5", "exited 5", "overloaded", "upstream")
    if any(sig in msg for sig in signals):
        return True, "5xx"
    return False, "other"


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
    timeout_sec: int,
    silent: bool,
    resume_session_id: str | None = None,
) -> tuple[CallResponse, list[str]]:
    """Try the decision's ranked providers in order; one-hop fallback on 5xx.

    Returns (response, fallbacks_used). fallbacks_used is the list of
    provider names attempted before the successful one (excluding the final).

    Raises the last ProviderError if every candidate fails.
    """
    last_exc: Exception | None = None
    fallbacks: list[str] = []

    for idx, candidate in enumerate(decision.ranked):
        provider = get_provider(candidate.name)
        try:
            if mode == "exec":
                response = provider.exec(
                    task,
                    model=model,
                    effort=effort,
                    tools=tools,
                    sandbox=sandbox,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
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
            return response, fallbacks
        except ProviderConfigError:
            # Config problems don't recover with a different provider using
            # the same config. Re-raise immediately.
            raise
        except UnsupportedCapability:
            # Router filter should prevent this; if it leaks through, skip.
            fallbacks.append(candidate.name)
            continue
        except ProviderError as e:
            retryable, category = _is_retryable(e)
            if category == "rate-limit":
                mark_rate_limited(candidate.name)
            mark_outcome(candidate.name, category)
            last_exc = e
            if not retryable:
                raise
            fallbacks.append(candidate.name)
            if idx + 1 < len(decision.ranked):
                next_name = decision.ranked[idx + 1].name
                if not silent:
                    click.echo(
                        f"[conductor] {candidate.name} failed ({category}) · "
                        f"falling back → {next_name}",
                        err=True,
                    )
            # Try next candidate.
            continue

    # Exhausted every candidate; re-raise the last error for user visibility.
    assert last_exc is not None  # at least one attempt must have happened
    raise last_exc


def _emit_call(
    response: CallResponse,
    *,
    as_json: bool,
    decision: RouteDecision | None = None,
) -> None:
    if as_json:
        payload = asdict(response)
        if decision is not None:
            payload["route"] = asdict(decision)
        click.echo(json.dumps(payload, default=str, indent=2))
    else:
        click.echo(response.text)


def _format_route_log_line(decision: RouteDecision) -> str:
    """Single-line route summary for stderr observability."""
    tags_matched = ",".join(decision.matched_tags) or "none"
    effort_str = (
        decision.effort if isinstance(decision.effort, str) else f"{decision.effort}tok"
    )
    sandbox_note = f" · sandbox={decision.sandbox}" if decision.sandbox != "none" else ""
    return (
        f"[conductor] {decision.prefer} (effort={effort_str}) → {decision.provider} "
        f"(tier: {decision.tier} · matched: {tags_matched}){sandbox_note}"
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
    for name, reason in decision.candidates_skipped:
        lines.append(f"  —  {name:<8} (skipped: {reason})")
    return lines


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


def _emit_usage_log(response: CallResponse, *, silent: bool) -> None:
    if silent:
        return
    click.echo(_format_usage_line(response), err=True)


@click.group()
@click.version_option(__version__, prog_name="conductor")
def main() -> None:
    """Pick an LLM, give it a job."""


# --------------------------------------------------------------------------- #
# call — single-turn send-a-task-to-a-provider
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--with",
    "provider_id",
    default=None,
    help="Provider identifier (kimi, claude, codex, gemini, ollama). "
    "Mutually exclusive with --auto.",
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
    help="The task / prompt. If omitted, read from stdin.",
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
    help="Suppress the default route-log line (useful for clean stdout piping).",
)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume a prior session by ID (claude/codex/gemini only). Requires --with.",
)
def call(
    provider_id: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    exclude: str | None,
    task: str | None,
    model: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
) -> None:
    """Send a task to a provider and print the response."""
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

    body = _read_task(task)
    effort_value = _parse_effort(effort)

    decision: RouteDecision | None = None
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                exclude=frozenset(_parse_csv(exclude)),
            )
        except (NoConfiguredProvider, InvalidRouterRequest) as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
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
                timeout_sec=300,
                silent=silent_route or as_json,
            )
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
    else:
        if prefer is not None:
            raise click.UsageError("--prefer is only meaningful with --auto.")
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        try:
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
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags, --prefer, --tools, --sandbox.",
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
    help=f"Sandbox mode: {' | '.join(VALID_SANDBOXES)} (default: none).",
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
    default=300,
    type=int,
    help="Wall-clock timeout in seconds (default: 300).",
)
@click.option("--task", default=None, help="The task / prompt. Reads stdin if omitted.")
@click.option("--model", default=None, help="Override the provider's default model.")
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
def exec_cmd(
    provider_id: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    sandbox: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int,
    task: str | None,
    model: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
) -> None:
    """Run a task as an agent session with tool access (exec mode)."""
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if resume_session_id and auto:
        raise click.UsageError(
            "--resume requires --with <provider> (sessions are provider-specific)."
        )

    body = _read_task(task)
    tools_set = _validate_tools(tools)
    sandbox_value = _validate_sandbox(sandbox)
    effort_value = _parse_effort(effort)

    decision: RouteDecision | None = None
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                exclude=frozenset(_parse_csv(exclude)),
            )
        except (NoConfiguredProvider, InvalidRouterRequest) as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)

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
                silent=silent_route or as_json,
            )
        except UnsupportedCapability as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
    else:
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        try:
            response = provider.exec(
                body,
                model=model,
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                resume_session_id=resume_session_id,
            )
        except UnsupportedCapability as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
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
@click.option("--sandbox", default=None, help=f"Sandbox: {' | '.join(VALID_SANDBOXES)}.")
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
    sandbox_value = _validate_sandbox(sandbox)
    effort_value = _parse_effort(effort)

    try:
        _provider, decision = pick(
            _parse_csv(tags),
            prefer=_validate_prefer(prefer),
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=frozenset(_parse_csv(exclude)),
        )
    except (NoConfiguredProvider, InvalidRouterRequest) as e:
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
    if decision.sandbox != "none":
        click.echo(f"  sandbox: {decision.sandbox}")

    click.echo("")
    click.echo("Full ranking:")
    for line in _format_route_ranking(decision):
        click.echo("  " + line.removeprefix("[conductor] "))


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
        "CONDUCTOR_WITH": os.environ.get("CONDUCTOR_WITH"),
        "CONDUCTOR_EXCLUDE": os.environ.get("CONDUCTOR_EXCLUDE"),
    }
    effective = {
        "prefer": env_overrides["CONDUCTOR_PREFER"] or "balanced",
        "effort": env_overrides["CONDUCTOR_EFFORT"] or "medium",
        "tags": _parse_csv(env_overrides["CONDUCTOR_TAGS"]),
        "with": env_overrides["CONDUCTOR_WITH"] or None,
        "exclude": _parse_csv(env_overrides["CONDUCTOR_EXCLUDE"]),
    }

    payload = {
        "version": __version__,
        "effective": effective,
        "sources": {
            key: ("env" if val is not None else "default")
            for key, val in env_overrides.items()
        },
        "known_providers": known_providers(),
    }

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"conductor v{payload['version']} — effective config")
    click.echo("")
    for key, val in effective.items():
        src = payload["sources"][f"CONDUCTOR_{key.upper()}"]
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
# list — show provider menu + configured status
# --------------------------------------------------------------------------- #


def _provider_rows() -> list[dict]:
    rows = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        rows.append(
            {
                "provider": name,
                "configured": ok,
                "reason": None if ok else reason,
                "default_model": provider.default_model,
                "tags": list(provider.tags),
                "tier": provider.quality_tier,
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
    rows = _provider_rows()
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
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "OLLAMA_BASE_URL",
)


def _diagnostic_payload() -> dict:
    providers_info = []
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
                "default_model": provider.default_model,
                "tags": list(provider.tags),
                "quality_tier": provider.quality_tier,
                "supports_effort": provider.supports_effort,
                "warnings": provider_warnings,
            }
        )

    env_info = []
    for var in _DIAGNOSTIC_ENV_VARS:
        in_env = var in os.environ
        in_keychain = credentials.keychain_has(var)
        env_info.append(
            {"name": var, "in_env": in_env, "in_keychain": in_keychain}
        )

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "providers": providers_info,
        "credentials": env_info,
        "warnings": warnings,
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
    payload = _diagnostic_payload()

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    from conductor.banner import SUBTITLE_DOCTOR, print_banner

    print_banner(SUBTITLE_DOCTOR, payload["version"])
    click.echo(
        f"{payload['platform']}  ·  python {payload['python']}"
    )
    click.echo("")
    click.echo("Providers:")
    for p in payload["providers"]:
        symbol = "✓" if p["configured"] else "✗"
        effort_note = "" if p["supports_effort"] else " (no thinking mode)"
        click.echo(
            f"  {symbol} {p['provider']:<8}  "
            f"tier={p['quality_tier']:<8}  "
            f"default={p['default_model']}{effort_note}"
        )
        if not p["configured"]:
            click.echo(f"      └─ {p['reason']}")
        for w in p.get("warnings") or []:
            click.echo(f"      ⚠ {w}")

    click.echo("")
    click.echo("Credentials (env / keychain):")
    for c in payload["credentials"]:
        in_env = "env" if c["in_env"] else "—"
        in_kc = "keychain" if c["in_keychain"] else "—"
        click.echo(f"  {c['name']:<24}  {in_env:<4}  {in_kc}")

    click.echo("")
    click.echo("Next steps:")
    not_configured = [p for p in payload["providers"] if not p["configured"]]
    if not not_configured:
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
def init(accept_defaults: bool, only: str | None, remaining: bool) -> None:
    """Interactively configure Conductor for first use."""
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
    )
    sys.exit(exit_code)


# --------------------------------------------------------------------------- #
# providers — manage user-local custom (shell-command) providers
# --------------------------------------------------------------------------- #


@main.group()
def providers() -> None:
    """Manage user-local custom providers (shell-command integrations).

    Custom providers let you register an arbitrary CLI — your own
    internal LLM wrapper, a different model's inference script, a local
    model server's CLI frontend — as a first-class Conductor provider.
    Once registered, it appears in `conductor list`, participates in
    auto-routing, and is callable via `conductor call --with <name>`.

    Custom providers are single-turn (no tool-use) and stateless (no
    resume). For CLIs that run their own agent loop internally, that
    happens inside the shell command, not through Conductor's router.
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
    if name in {"kimi", "claude", "codex", "gemini", "ollama"}:
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
    click.echo(f"Use it: conductor call --with {name} --task 'hello'")


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


@providers.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the custom-provider list as JSON.",
)
def providers_list(as_json: bool) -> None:
    """Show only custom providers (built-ins are in `conductor list`)."""
    from conductor.custom_providers import load_specs, providers_file_path

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
            }
            for s in specs
        ]
        click.echo(json.dumps(payload, indent=2))
        return

    path = providers_file_path()
    if not specs:
        click.echo("(no custom providers; register via `conductor providers add`)")
        click.echo(f"file: {path} {'(not yet created)' if not path.exists() else ''}")
        return

    click.echo(f"Custom providers ({path}):")
    click.echo("")
    for s in specs:
        click.echo(f"  {s.name}")
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
