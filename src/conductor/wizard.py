"""Interactive setup wizard for `conductor init`.

Satisfies Autumn Garage Doctrine 0002 (interactive-by-default): detects
TTY, prompts for ambiguous choices, offers a ``--yes`` escape hatch,
prints the equivalent non-interactive setup steps at the end.

Concierge flow (v0.2):
  - Walks each provider one at a time with a short description, quality
    tier, and cost profile so the user understands what they're
    configuring before they commit time to it.
  - For every provider, shows current status (CLI found? authed?
    credentials present?) explicitly rather than just "not configured".
  - Prints copy-pasteable install + login commands for every CLI-wrapped
    provider (claude, codex, gemini, ollama).
  - For API-key providers (kimi), collects the credential, offers
    Keychain / direnv / print storage, and runs an inline smoke test.
  - Offers per-provider skip at any step; --only and --remaining let
    users resume without rewalking configured providers.
  - Summary at end names what's configured, what's skipped, and what
    to do next (including the default routing preference).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Callable

from conductor import credentials
from conductor.providers import get_provider, known_providers
from conductor.providers.kimi import (
    CLOUDFLARE_ACCOUNT_ID_ENV,
    CLOUDFLARE_API_TOKEN_ENV,
)

# --------------------------------------------------------------------------- #
# Provider concierge copy — descriptions, install commands, cred URLs.
# Maintained here alongside the wizard so "add a provider" is one file, not
# three. When a provider's install path or credential source changes, this
# string is the single point of update.
# --------------------------------------------------------------------------- #


@dataclass
class _ProviderInfo:
    tagline: str
    description: str
    install_cmds: list[str]
    auth_cmds: list[str]
    credential_source_url: str | None = None


_INFO: dict[str, _ProviderInfo] = {
    "claude": _ProviderInfo(
        tagline="Anthropic's flagship reasoning model (Claude).",
        description=(
            "Strong on code review, long contexts, and tool-using agent "
            "sessions. Frontier tier; higher cost than others. Uses your "
            "Claude subscription via the `claude` CLI — no API key needed."
        ),
        install_cmds=[
            "brew install claude                          # macOS",
            "npm install -g @anthropic-ai/claude-code    # any platform",
        ],
        auth_cmds=[
            "claude /login    # opens a browser for subscription OAuth",
        ],
    ),
    "codex": _ProviderInfo(
        tagline="OpenAI's coding agent (Codex).",
        description=(
            "Strong on code review and tool use, comparable to claude at "
            "slightly lower latency. Frontier tier. Uses your ChatGPT "
            "subscription via the `codex` CLI."
        ),
        install_cmds=[
            "brew install codex                          # macOS",
            "npm install -g @openai/codex                # any platform",
        ],
        auth_cmds=[
            "codex login    # opens a browser, signs in via ChatGPT",
        ],
    ),
    "gemini": _ProviderInfo(
        tagline="Google's Gemini 2.5 Pro.",
        description=(
            "Strong on large multimodal contexts and web search. Strong "
            "tier; lower per-token cost than claude/codex. Uses the "
            "`gemini` CLI with GEMINI_API_KEY or gcloud ADC."
        ),
        install_cmds=[
            "npm install -g @google/gemini-cli",
        ],
        auth_cmds=[
            "export GEMINI_API_KEY=...                   # from aistudio.google.com",
            "# OR:",
            "gcloud auth application-default login",
        ],
        credential_source_url="https://aistudio.google.com/apikey",
    ),
    "kimi": _ProviderInfo(
        tagline="Moonshot Kimi K2.6 via Cloudflare Workers AI.",
        description=(
            "Strong on long contexts (1M tokens) and tool use. Strong "
            "tier; among the cheapest options per token. Free tier "
            "covers ~10k tokens/day. Requires a Cloudflare API token "
            "and account ID."
        ),
        install_cmds=[
            "# No install step — Conductor talks directly to Cloudflare's",
            "# Workers AI OpenAI-compatible endpoint via httpx.",
        ],
        auth_cmds=[
            "# Wizard will prompt for CLOUDFLARE_API_TOKEN and",
            "# CLOUDFLARE_ACCOUNT_ID and store them in Keychain / direnv.",
        ],
        credential_source_url="https://dash.cloudflare.com/profile/api-tokens",
    ),
    "ollama": _ProviderInfo(
        tagline="Local models via Ollama.",
        description=(
            "Runs on your machine — no cost, no network, private by "
            "default. Local tier; quality varies by model. Best for "
            "throwaway reviews, offline work, or privacy-sensitive diffs."
        ),
        install_cmds=[
            "brew install ollama                        # macOS",
            "# OR download from https://ollama.com/download",
        ],
        auth_cmds=[
            "ollama serve                               # start the daemon",
            "ollama pull qwen2.5-coder:14b              # pull a code-review model",
            "# or heavier:",
            "ollama pull llama3.3:70b                   # ~40 GB, needs ~48 GB RAM",
        ],
    ),
}


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
    return sys.stdin.isatty()


# --------------------------------------------------------------------------- #
# Main entry.
# --------------------------------------------------------------------------- #


def run_init_wizard(
    *,
    accept_defaults: bool = False,
    only: str | None = None,
    remaining: bool = False,
) -> int:
    """Walk the user through configuring every provider that needs it.

    Args:
        accept_defaults: non-interactive mode; report state without prompting.
        only: configure only this one provider; skip the rest.
        remaining: skip providers that are already configured (resume flow).

    Returns a shell exit code: 0 on success, non-zero if the user
    explicitly aborted.
    """
    interactive = _is_tty() and not accept_defaults

    _print_intro(interactive, only=only, remaining=remaining)

    names_to_walk = [only] if only else known_providers()
    outcomes: list[WizardOutcome] = []
    aborted = False

    for idx, name in enumerate(names_to_walk, start=1):
        provider = get_provider(name)
        ok, reason = provider.configured()

        if ok and remaining:
            continue

        click.echo(_section_header(name, idx, len(names_to_walk)))
        info = _INFO.get(name)
        if info:
            click.echo(f"  {info.tagline}")
            click.echo(f"  tier: {provider.quality_tier}")
            click.echo("")
            click.echo(f"  {info.description}")
            click.echo("")

        if ok:
            click.echo(
                f"  Status: ✓ already configured. "
                f"(run `conductor smoke {name}` to verify.)"
            )
            outcomes.append(WizardOutcome(name, "ok", "already configured"))
            click.echo("")
            continue

        # Not configured — enter the per-provider setup flow.
        click.echo(f"  Status: ✗ {reason}")
        click.echo("")

        if not interactive:
            click.echo(
                f"  Run `conductor init --only {name}` on a TTY for the "
                f"guided setup, or:"
            )
            _print_install_block(info, indent="    ")
            outcomes.append(
                WizardOutcome(name, "skipped", reason or "needs interactive setup")
            )
            click.echo("")
            continue

        flow: Callable[[], WizardOutcome] = _FLOWS.get(name, _default_cli_flow(name))
        try:
            outcome = flow()
        except _AbortSetup:
            aborted = True
            outcomes.append(WizardOutcome(name, "skipped", "user quit setup"))
            click.echo("")
            break
        outcomes.append(outcome)
        click.echo("")

    _print_summary(outcomes)
    _print_next_steps(outcomes)
    return 1 if aborted else 0


class _AbortSetup(Exception):  # noqa: N818  — sentinel, never caught outside this module
    """User pressed [q]uit during a provider flow — stop the walk."""


# --------------------------------------------------------------------------- #
# Header / intro formatting.
# --------------------------------------------------------------------------- #


def _print_intro(interactive: bool, *, only: str | None, remaining: bool) -> None:
    click.echo("conductor init — provider setup")
    click.echo("─" * 60)
    if only:
        click.echo(f"Configuring only: {only}")
    elif remaining:
        click.echo("Resuming setup for not-yet-configured providers.")
    else:
        click.echo(
            "I'll walk you through each provider, one at a time. For each:"
        )
        click.echo("  • description, tier, cost profile")
        click.echo("  • current status (installed? authed? credentials?)")
        click.echo("  • copy-pasteable setup commands")
        click.echo("  • inline smoke test")
        click.echo("  • or [s]kip — resume later with `conductor init --only <name>`")
    click.echo("")
    if not interactive:
        click.echo(
            "(non-interactive mode — reporting status without prompting. "
            "Pass no flags on a TTY for the concierge flow.)"
        )
        click.echo("")


def _section_header(name: str, idx: int, total: int) -> str:
    line = f"[{idx}/{total}]  {name}"
    bar = "─" * 60
    return f"{bar}\n{line}\n{bar}"


def _print_install_block(info: _ProviderInfo | None, *, indent: str = "  ") -> None:
    if info is None:
        return
    click.echo(f"{indent}Install:")
    for cmd in info.install_cmds:
        click.echo(f"{indent}  {cmd}")
    click.echo("")
    click.echo(f"{indent}Authenticate:")
    for cmd in info.auth_cmds:
        click.echo(f"{indent}  {cmd}")
    if info.credential_source_url:
        click.echo("")
        click.echo(f"{indent}Credential source: {info.credential_source_url}")


# --------------------------------------------------------------------------- #
# Per-provider flows.
# --------------------------------------------------------------------------- #


def _default_cli_flow(name: str) -> Callable[[], WizardOutcome]:
    """Shared flow for CLI-wrapped providers that don't take API keys."""

    def flow() -> WizardOutcome:
        info = _INFO.get(name)
        _print_install_block(info)
        click.echo("")
        while True:
            choice = _prompt_menu(
                options=[
                    ("t", "test now — I've installed and authed"),
                    ("s", "skip this provider"),
                    ("q", "quit setup"),
                ],
                default="s",
            )
            if choice == "s":
                return WizardOutcome(name, "skipped", "user skipped")
            if choice == "q":
                raise _AbortSetup()
            # "t": re-check configured() + smoke.
            provider = get_provider(name)
            ok, reason = provider.configured()
            if not ok:
                click.echo(f"  ✗ still not configured: {reason}")
                click.echo("  Retry the install/auth commands above, then [t]est again.")
                click.echo("")
                continue
            click.echo("  ✓ CLI detected")
            smoke_ok, smoke_reason = provider.smoke()
            if smoke_ok:
                click.echo("  ✓ smoke test passed")
                return WizardOutcome(name, "ok", "configured + smoke passed")
            click.echo(f"  ✗ smoke test failed: {smoke_reason}")
            click.echo("  → configured but not healthy; fix and [t]est again or [s]kip.")
            click.echo("")

    return flow


def _kimi_flow() -> WizardOutcome:
    """API-key flow with credential collection + storage choice."""
    info = _INFO["kimi"]
    click.echo(f"  Get credentials: {info.credential_source_url}")
    click.echo("  You need two values:")
    click.echo("    1. CLOUDFLARE_API_TOKEN — API token with Workers AI:Read permission")
    click.echo("    2. CLOUDFLARE_ACCOUNT_ID — shown on the right sidebar of dash.cloudflare.com")
    click.echo("")

    missing = [
        (var, label)
        for var, label in _KIMI_CREDS
        if credentials.get(var) is None
    ]

    values: dict[str, str] = {}
    for var, label in missing:
        try:
            value = click.prompt(
                f"  {label} ({var})",
                hide_input=True,
                default="",
                show_default=False,
            )
        except click.Abort:
            # EOF on stdin (test runners, piped input) → treat as user
            # declining to provide the credential.
            value = ""
        if not value:
            click.echo(f"  {var} not provided — skipping kimi.")
            return WizardOutcome("kimi", "skipped", f"{var} not provided")
        values[var] = value

    if not values:
        # Both creds already present in env/keychain.
        provider = get_provider("kimi")
        ok, reason = provider.smoke()
        if ok:
            return WizardOutcome("kimi", "ok", "credentials already present, smoke passed")
        return WizardOutcome("kimi", "failed", f"credentials present but smoke failed: {reason}")

    storage = _prompt_menu(
        options=[
            ("keychain", "keychain — macOS Keychain (recommended, no shell-env leakage)"),
            ("envrc", "envrc — write exports to .envrc via direnv"),
            ("print", "print — show export statements, I'll store them myself"),
            ("skip", "skip — skip kimi entirely"),
        ],
    )
    if storage == "skip":
        return WizardOutcome("kimi", "skipped", "user skipped during storage choice")

    if storage == "keychain":
        try:
            for var, value in values.items():
                credentials.set_in_keychain(var, value)
            click.echo("  ✓ stored in macOS Keychain (service: conductor).")
        except RuntimeError as e:
            click.echo(f"  ✗ keychain storage failed: {e}")
            click.echo("  falling back to print-only.")
            storage = "print"

    if storage == "envrc":
        envrc_path = os.path.join(os.getcwd(), ".envrc")
        _append_envrc(envrc_path, values)
        click.echo(f"  ✓ wrote export lines to {envrc_path}")
        click.echo("    run `direnv allow` in that directory to activate.")

    if storage == "print":
        click.echo("  add these to your shell rc or .envrc:")
        for var, value in values.items():
            click.echo(f"    export {var}={value!r}")

    # Populate in-process env so the smoke test succeeds even on Keychain
    # or direnv paths where the current shell hasn't re-sourced.
    for var, value in values.items():
        os.environ[var] = value

    provider = get_provider("kimi")
    ok, reason = provider.smoke()
    if ok:
        click.echo("  ✓ smoke test passed")
        return WizardOutcome("kimi", "ok", f"stored via {storage}, smoke passed")
    click.echo(f"  ✗ smoke test failed: {reason}")
    click.echo("  credentials stored but the endpoint is not responding as expected.")
    return WizardOutcome("kimi", "failed", f"stored via {storage}, smoke failed: {reason}")


_FLOWS: dict[str, Callable[[], WizardOutcome]] = {
    "kimi": _kimi_flow,
}


# --------------------------------------------------------------------------- #
# Menu / prompt helpers.
# --------------------------------------------------------------------------- #


def _prompt_menu(
    *,
    options: list[tuple[str, str]],
    default: str | None = None,
) -> str:
    """Render a menu and return the chosen key. Re-prompts until valid.

    Accepts either the short key (e.g. "k") or the first word of the
    label (e.g. "keychain" when the label is "keychain — store securely").
    This keeps interactive muscle-memory (single-key) while tolerating
    users who type full words.

    Empty input returns ``default`` if provided; otherwise re-prompts.
    EOF on stdin (test runners, piped input) also resolves to ``default``.
    """
    aliases: dict[str, str] = {}
    for key, label in options:
        click.echo(f"  [{key}] {label}")
        aliases[key.lower()] = key
        first_word = label.split()[0].split("—")[0].strip().lower().rstrip(",.")
        if first_word:
            aliases.setdefault(first_word, key)

    valid_keys = sorted({k for k, _ in options})

    while True:
        try:
            raw = click.prompt(
                "  >",
                default=default if default is not None else "",
                show_default=False,
            )
        except click.Abort:
            # Ctrl-C / EOF: treat as default when one exists, else re-raise.
            if default is not None:
                return default
            raise
        choice = (raw or "").strip().lower()
        if not choice and default is not None:
            return default
        if choice in aliases:
            return aliases[choice]
        click.echo(f"  (pick one of: {', '.join(valid_keys)})")


def _append_envrc(path: str, values: dict[str, str]) -> None:
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# Added by `conductor init`\n")
        for var, value in values.items():
            f.write(f"export {var}={value!r}\n")


# --------------------------------------------------------------------------- #
# Summary + next steps.
# --------------------------------------------------------------------------- #


def _print_summary(outcomes: list[WizardOutcome]) -> None:
    click.echo("─" * 60)
    click.echo("Summary")
    click.echo("─" * 60)
    ok_names = [o.provider for o in outcomes if o.status == "ok"]
    skipped_names = [o.provider for o in outcomes if o.status == "skipped"]
    failed_names = [o.provider for o in outcomes if o.status == "failed"]
    click.echo(f"  Configured: {', '.join(ok_names) if ok_names else '(none)'}")
    click.echo(f"  Skipped:    {', '.join(skipped_names) if skipped_names else '(none)'}")
    if failed_names:
        click.echo(f"  Failed:     {', '.join(failed_names)}")


def _print_next_steps(outcomes: list[WizardOutcome]) -> None:
    click.echo("")
    click.echo("Next steps:")
    click.echo("  conductor list           # see providers + quality tiers")
    click.echo("  conductor smoke --all    # verify configured providers")

    skipped = [o.provider for o in outcomes if o.status == "skipped"]
    if skipped:
        click.echo(f"  conductor init --only {skipped[0]}   # resume any skipped provider")

    click.echo("")
    click.echo(
        "Default routing preference: prefer=balanced, effort=medium. "
        "Override with --prefer / --effort on conductor call/exec, "
        "or set CONDUCTOR_PREFER / CONDUCTOR_EFFORT env vars."
    )
