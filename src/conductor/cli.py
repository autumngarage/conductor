"""Conductor CLI — call, list, smoke, doctor, init.

v0.1 surface:
  conductor call --with <id> --task "..."             # manual
  conductor call --auto [--tags a,b,c] --task "..."   # router picks
  conductor list [--json]                             # providers + status
  conductor smoke [<id>] [--all] [--json]             # health check
  conductor doctor [--json]                           # diagnostic report
  conductor init [--yes]                              # interactive setup
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from typing import Optional

import click

from conductor import __version__, credentials
from conductor.providers import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    get_provider,
    known_providers,
)
from conductor.router import NoConfiguredProvider, RouteDecision, pick
from conductor.wizard import run_init_wizard


def _read_task(task: Optional[str]) -> str:
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


def _parse_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _emit_call(
    response: CallResponse,
    *,
    as_json: bool,
    decision: Optional[RouteDecision] = None,
) -> None:
    if as_json:
        payload = asdict(response)
        if decision is not None:
            payload["route"] = asdict(decision)
        click.echo(json.dumps(payload, default=str, indent=2))
    else:
        click.echo(response.text)


@click.group()
@click.version_option(__version__, prog_name="conductor")
def main() -> None:
    """Pick an LLM, give it a job."""


# ---------------------------------------------------------------------------
# call — send a task to a provider
# ---------------------------------------------------------------------------


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
    help="Let the router pick based on --tags and configured providers.",
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags for --auto routing "
    "(e.g. 'long-context,cheap'). Ignored in --with mode.",
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
def call(
    provider_id: Optional[str],
    auto: bool,
    tags: Optional[str],
    task: Optional[str],
    model: Optional[str],
    as_json: bool,
) -> None:
    """Send a task to a provider and print the response."""
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")

    body = _read_task(task)

    decision: Optional[RouteDecision] = None
    if auto:
        try:
            provider, decision = pick(_parse_tags(tags))
        except NoConfiguredProvider as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
    else:
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e

    try:
        response = provider.call(body, model=model)
    except ProviderConfigError as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderError as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(1)

    _emit_call(response, as_json=as_json, decision=decision)


# ---------------------------------------------------------------------------
# list — show provider menu + configured status
# ---------------------------------------------------------------------------


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

    # Plain text table — avoid a rich dependency for core output so scripting
    # consumers can grep reliably.
    name_w = max(len("PROVIDER"), max(len(r["provider"]) for r in rows))
    model_w = max(len("DEFAULT MODEL"), max(len(r["default_model"]) for r in rows))
    header = f"{'PROVIDER':<{name_w}}  {'READY':<5}  {'DEFAULT MODEL':<{model_w}}  TAGS"
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        ready = "yes" if r["configured"] else "no"
        tags = ",".join(r["tags"])
        click.echo(
            f"{r['provider']:<{name_w}}  {ready:<5}  {r['default_model']:<{model_w}}  {tags}"
        )
        if not r["configured"] and r["reason"]:
            click.echo(f"{'':<{name_w}}  {'':<5}  └─ {r['reason']}")


# ---------------------------------------------------------------------------
# smoke — run one or all providers' smoke tests
# ---------------------------------------------------------------------------


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
def smoke(provider_id: Optional[str], run_all: bool, as_json: bool) -> None:
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


# ---------------------------------------------------------------------------
# doctor — diagnostic report (install + env + keychain)
# ---------------------------------------------------------------------------


_DIAGNOSTIC_ENV_VARS = (
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "OLLAMA_BASE_URL",
)


def _diagnostic_payload() -> dict:
    providers_info = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        providers_info.append(
            {
                "provider": name,
                "configured": ok,
                "reason": None if ok else reason,
                "default_model": provider.default_model,
                "tags": list(provider.tags),
            }
        )

    env_info = []
    for var in _DIAGNOSTIC_ENV_VARS:
        in_env = var in os.environ
        in_keychain = credentials.keychain_has(var)
        env_info.append(
            {
                "name": var,
                "in_env": in_env,
                "in_keychain": in_keychain,
            }
        )

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "providers": providers_info,
        "credentials": env_info,
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

    click.echo(f"conductor v{payload['version']}  ·  {payload['platform']}  ·  python {payload['python']}")
    click.echo("")
    click.echo("Providers:")
    for p in payload["providers"]:
        symbol = "✓" if p["configured"] else "✗"
        click.echo(f"  {symbol} {p['provider']:<8}  default={p['default_model']}")
        if not p["configured"]:
            click.echo(f"      └─ {p['reason']}")

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


# ---------------------------------------------------------------------------
# init — interactive setup wizard (Doctrine 0002 compliant)
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--yes",
    "-y",
    "accept_defaults",
    is_flag=True,
    default=False,
    help="Accept all defaults without prompting (non-TTY friendly).",
)
def init(accept_defaults: bool) -> None:
    """Interactively configure Conductor for first use."""
    exit_code = run_init_wizard(accept_defaults=accept_defaults)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
