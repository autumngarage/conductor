"""Interactive setup wizard for `conductor init`.

Satisfies Autumn Garage Doctrine 0002 (interactive-by-default): detects
TTY, prompts for ambiguous choices, offers a ``--yes`` escape hatch,
prints the equivalent non-interactive setup steps at the end so scripters
learn them by using the tool.

Scope of v0.1:
  - Walks each of the five known providers in turn.
  - For credential-bearing providers (kimi today; future API-key adapters
    will drop in): prompts for the required credentials when they're not
    already present, offers Keychain or direnv .envrc storage, runs the
    smoke test, reports the outcome.
  - For CLI-wrapping providers (claude/codex/gemini) and the local
    provider (ollama): checks ``configured()`` and prints the install /
    login hint if missing.
  - Never overwrites existing env vars or Keychain entries without
    explicit confirmation.

The wizard does not touch ``~/.config/conductor/config.toml`` at v0.1 —
we don't have user-level config yet. When we do (auto-mode priority
override, default tags, backend selection), the wizard grows into it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import click

from conductor import credentials
from conductor.providers import get_provider, known_providers
from conductor.providers.kimi import (
    CLOUDFLARE_ACCOUNT_ID_ENV,
    CLOUDFLARE_API_TOKEN_ENV,
)

# Each provider gets a setup recipe. For API-key providers the recipe
# lists the credentials to prompt for. For CLI-wrapped providers the
# recipe is empty (we check configured() but don't collect secrets).
_KIMI_CREDS = (
    (CLOUDFLARE_API_TOKEN_ENV, "Cloudflare API token with Workers AI read permission"),
    (CLOUDFLARE_ACCOUNT_ID_ENV, "Cloudflare account ID"),
)


@dataclass
class WizardOutcome:
    provider: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str


def _is_tty() -> bool:
    """Indirection so tests can patch `conductor.wizard._is_tty` directly;
    CliRunner swaps sys.stdin with a pipe and makes the attribute hard to
    monkey-patch in place."""
    return sys.stdin.isatty()


def run_init_wizard(*, accept_defaults: bool = False) -> int:
    """Walk the user through configuring every provider that needs it.

    Returns a shell exit code: 0 on success, non-zero if the user
    explicitly aborted.
    """
    interactive = _is_tty() and not accept_defaults

    click.echo("conductor init — interactive setup.")
    click.echo("")
    if not interactive:
        click.echo(
            "(non-interactive mode: accepting defaults / reading existing state only; "
            "pass no flags on a TTY for the full wizard)"
        )
        click.echo("")

    outcomes: list[WizardOutcome] = []

    for name in known_providers():
        provider = get_provider(name)
        click.echo(f"── {name} ──")
        ok, reason = provider.configured()
        if ok:
            click.echo("  already configured. (conductor smoke {0} to verify.)".format(name))
            outcomes.append(WizardOutcome(name, "ok", "already configured"))
            click.echo("")
            continue

        # Provider-specific recipes. Only kimi has a secret-collection
        # flow at v0.1; the others just need a CLI install + login the
        # user performs outside conductor.
        if name == "kimi":
            outcomes.append(_kimi_flow(interactive))
        else:
            click.echo(f"  not configured: {reason}")
            outcomes.append(WizardOutcome(name, "skipped", reason or "configure externally"))
        click.echo("")

    _print_summary(outcomes)
    _print_equivalent_flag_form(outcomes)
    return 0


def _kimi_flow(interactive: bool) -> WizardOutcome:
    missing = [
        (var, label)
        for var, label in _KIMI_CREDS
        if credentials.get(var) is None
    ]
    if not missing:
        return WizardOutcome("kimi", "ok", "both credentials resolved")

    if not interactive:
        click.echo("  missing credentials: " + ", ".join(v for v, _ in missing))
        click.echo("  rerun on a TTY or pass --yes with the credentials in env.")
        return WizardOutcome("kimi", "skipped", "non-interactive and credentials absent")

    click.echo(
        "  kimi routes through Cloudflare Workers AI. You need a CF API token "
        "and the account ID."
    )
    click.echo(
        "  Get a token at https://dash.cloudflare.com/profile/api-tokens "
        "(Workers AI read)."
    )

    values: dict[str, str] = {}
    for var, label in missing:
        value = click.prompt(f"  {label} ({var})", hide_input=True, default="", show_default=False)
        if not value:
            click.echo(f"  skipping — {var} not provided.")
            return WizardOutcome("kimi", "skipped", f"{var} not provided")
        values[var] = value

    storage = click.prompt(
        "  store how? [keychain / envrc / print]",
        default="keychain",
        show_default=True,
    ).strip().lower()
    if storage not in {"keychain", "envrc", "print"}:
        click.echo(f"  unknown storage {storage!r}; defaulting to print.")
        storage = "print"

    if storage == "keychain":
        try:
            for var, value in values.items():
                credentials.set_in_keychain(var, value)
            click.echo("  stored in macOS Keychain (service: conductor).")
        except RuntimeError as e:
            click.echo(f"  keychain storage failed: {e}")
            click.echo("  falling back to print-only.")
            storage = "print"

    if storage == "envrc":
        envrc_path = os.path.join(os.getcwd(), ".envrc")
        _append_envrc(envrc_path, values)
        click.echo(f"  wrote export lines to {envrc_path}. run `direnv allow` there.")

    if storage == "print":
        click.echo("  add these to your shell rc or envrc:")
        for var, value in values.items():
            click.echo(f"    export {var}={value!r}")

    # Populate in-process env so the smoke test below succeeds even on
    # keychain/envrc paths where the current shell hasn't re-sourced.
    for var, value in values.items():
        os.environ[var] = value

    provider = get_provider("kimi")
    ok, reason = provider.smoke()
    if ok:
        click.echo("  smoke test passed ✓")
        return WizardOutcome("kimi", "ok", f"stored via {storage}, smoke passed")
    click.echo(f"  smoke test FAILED: {reason}")
    return WizardOutcome("kimi", "failed", f"stored via {storage}, smoke failed: {reason}")


def _append_envrc(path: str, values: dict[str, str]) -> None:
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# Added by `conductor init`\n")
        for var, value in values.items():
            f.write(f"export {var}={value!r}\n")


def _print_summary(outcomes: list[WizardOutcome]) -> None:
    click.echo("Summary:")
    for o in outcomes:
        symbol = {"ok": "✓", "skipped": "·", "failed": "✗"}.get(o.status, "?")
        click.echo(f"  {symbol} {o.provider:<8}  {o.detail}")


def _print_equivalent_flag_form(outcomes: list[WizardOutcome]) -> None:
    click.echo("")
    click.echo("Next:")
    click.echo("  conductor list       # see what's ready")
    click.echo("  conductor smoke --all  # verify every configured provider")
    click.echo("  conductor call --auto --task \"...\"")
