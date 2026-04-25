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
    troubleshoot_tips: list[str]
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
            "claude auth login         # opens a browser for subscription OAuth",
            "# OR for headless / non-interactive use:",
            "claude setup-token        # long-lived token (subscription req'd)",
            "export ANTHROPIC_API_KEY=sk-ant-...    # API-key billing",
        ],
        troubleshoot_tips=[
            "`claude auth login` opens a browser — won't work in a headless env; "
            "use `claude setup-token` or set `ANTHROPIC_API_KEY` instead.",
            "Verify with `claude auth status --json` — `loggedIn: true` means authed.",
            "Subscription status: https://claude.ai/plans — Pro or Team required.",
            "Behind a proxy? auth uses auth0; check $HTTP_PROXY / firewall rules.",
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
        troubleshoot_tips=[
            "`codex login` requires a ChatGPT Plus/Team/Enterprise subscription.",
            "Verify with `codex --version` (need >= 0.20) and `codex exec --help`.",
            "Browser fails to open? try `codex login --no-browser` and follow URL.",
            "Session expired? `codex logout && codex login` forces a fresh auth.",
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
        troubleshoot_tips=[
            "GEMINI_API_KEY must be in the current shell — restart your terminal "
            "after adding to ~/.zshrc.",
            "Validate the key: https://aistudio.google.com/apikey (regenerate if unsure).",
            "Using gcloud ADC instead? run "
            "`gcloud auth application-default print-access-token` to confirm.",
            "Free tier has a daily quota — 429 errors mean you hit the limit.",
        ],
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
        troubleshoot_tips=[
            "The token needs 'Workers AI:Read' permission — create a scoped token, "
            "not a global API key.",
            "Account ID is the hex string on the right sidebar of dash.cloudflare.com "
            "— NOT your email.",
            "Quick check: curl -H 'Authorization: Bearer $TOKEN' "
            "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/ai/models/search",
            "Free tier = 10k tokens/day; 429 errors mean you've hit the cap.",
        ],
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
        troubleshoot_tips=[
            "Daemon must be running: `ollama serve` in a separate terminal "
            "(or `brew services start ollama`).",
            "Default model is qwen2.5-coder:14b — if you don't have it, "
            "`ollama pull qwen2.5-coder:14b` (~9 GB).",
            "Check what's installed locally: `ollama list`.",
            "Connection refused on 11434? port conflict; "
            "check `lsof -iTCP:11434 -sTCP:LISTEN`.",
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
    wire_agents: str | None = None,
    patch_claude_md: str | None = None,
    patch_agents_md: str | None = None,
    patch_gemini_md: str | None = None,
    patch_claude_md_repo: str | None = None,
    wire_cursor_flag: str | None = None,
) -> int:
    """Walk the user through configuring every provider that needs it.

    Args:
        accept_defaults: non-interactive mode; report state without prompting.
        only: configure only this one provider; skip the rest.
        remaining: skip providers that are already configured (resume flow).
        wire_agents: one of "yes" / "no" / "ask" / None. Controls whether
            the wizard offers to wire conductor into detected agent tools
            (Claude Code user-scope artifacts). None means ask on TTY,
            skip on non-TTY.
        patch_claude_md: same tri-state, for the one-line ``@import`` edit
            in ``~/.claude/CLAUDE.md``. Separate from ``wire_agents`` so
            users can accept the artifacts while declining the import
            edit (and vice versa).
        patch_agents_md: same tri-state, for inlining a delegation block
            into the cwd's ``./AGENTS.md`` (the cross-tool convention used
            by Codex / Cursor / Zed). Separate from ``patch_claude_md``
            because user-scope and repo-scope patching are independent
            consents.

    Returns a shell exit code: 0 on success, non-zero if the user
    explicitly aborted.
    """
    interactive = _is_tty() and not accept_defaults

    _print_intro(interactive, only=only, remaining=remaining)

    names_to_walk = [only] if only else known_providers()
    outcomes: list[WizardOutcome] = []
    aborted = False

    # While-loop with explicit index so [b]ack can decrement. The previous
    # provider's outcome (if any) gets popped on rewind so the rewalk is
    # authoritative.
    idx = 0
    total = len(names_to_walk)
    while idx < total:
        name = names_to_walk[idx]
        provider = get_provider(name)
        ok, reason = provider.configured()

        if ok and remaining:
            idx += 1
            continue

        can_back = idx > 0

        click.echo(_section_header(name, idx + 1, total))
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
            idx += 1
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
            idx += 1
            continue

        flow = _FLOWS.get(name, _default_cli_flow(name))
        try:
            outcome = flow(can_back=can_back)
        except _AbortSetup:
            aborted = True
            outcomes.append(WizardOutcome(name, "skipped", "user quit setup"))
            click.echo("")
            break
        except _GoBack:
            # Drop the previous provider's outcome so the rewalk replaces it.
            if outcomes:
                outcomes.pop()
            idx -= 1
            click.echo("")
            continue
        outcomes.append(outcome)
        click.echo("")
        idx += 1

    # Agent wiring — only offered on an unscoped, non-aborted run. `--only`
    # narrows scope deliberately; an aborted ([q]uit) walk means the user
    # explicitly stopped, and silently writing artifacts after they quit
    # would violate that intent.
    wiring_ok = True
    if not only and not aborted:
        wiring_ok = _maybe_wire_agents(
            interactive=interactive,
            wire_agents=wire_agents,
            patch_claude_md=patch_claude_md,
            patch_agents_md=patch_agents_md,
            patch_gemini_md=patch_gemini_md,
            patch_claude_md_repo=patch_claude_md_repo,
            wire_cursor_flag=wire_cursor_flag,
        )

    _print_summary(outcomes)
    _print_next_steps(outcomes)
    if aborted or not wiring_ok:
        return 1
    return 0


class _AbortSetup(Exception):  # noqa: N818  — sentinel, never caught outside this module
    """User pressed [q]uit during a provider flow — stop the walk."""


class _GoBack(Exception):  # noqa: N818  — sentinel, never caught outside this module
    """User pressed [b]ack — rewind to the previous provider."""


# --------------------------------------------------------------------------- #
# Header / intro formatting.
# --------------------------------------------------------------------------- #


def _print_intro(interactive: bool, *, only: str | None, remaining: bool) -> None:
    from conductor.banner import (
        SUBTITLE_INIT,
        conductor_version,
        print_banner,
    )

    print_banner(SUBTITLE_INIT, conductor_version())
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


def _print_troubleshoot_tips(info: _ProviderInfo | None, *, indent: str = "  ") -> None:
    if info is None or not info.troubleshoot_tips:
        click.echo(f"{indent}(no troubleshoot tips available)")
        return
    click.echo(f"{indent}Common fixes:")
    for tip in info.troubleshoot_tips:
        click.echo(f"{indent}  • {tip}")
    click.echo("")


# --------------------------------------------------------------------------- #
# Per-provider flows.
# --------------------------------------------------------------------------- #


def _default_cli_flow(name: str) -> Callable[..., WizardOutcome]:
    """Shared flow for CLI-wrapped providers that don't take API keys."""

    def flow(*, can_back: bool = False) -> WizardOutcome:
        info = _INFO.get(name)
        _print_install_block(info)
        click.echo("")
        just_failed = False  # `[h]elp` only offered after a failure.
        while True:
            options = [
                ("t", "test now — I've installed and authed"),
                ("s", "skip this provider"),
            ]
            if just_failed:
                options.append(("h", "help — common fixes for this provider"))
            if can_back:
                options.append(("b", "back — redo the previous provider"))
            options.append(("q", "quit setup"))
            choice = _prompt_menu(options=options, default="s")
            if choice == "s":
                return WizardOutcome(name, "skipped", "user skipped")
            if choice == "q":
                raise _AbortSetup()
            if choice == "b":
                raise _GoBack()
            if choice == "h":
                _print_troubleshoot_tips(info)
                continue  # keep just_failed=True so [h] stays available
            # "t": re-check configured() + smoke.
            provider = get_provider(name)
            ok, reason = provider.configured()
            if not ok:
                click.echo(f"  ✗ still not configured: {reason}")
                click.echo("  Retry the install/auth commands above, then [t]est again.")
                click.echo("")
                just_failed = True
                continue
            click.echo("  ✓ CLI detected")
            smoke_ok, smoke_reason = provider.smoke()
            if smoke_ok:
                click.echo("  ✓ smoke test passed")
                return WizardOutcome(name, "ok", "configured + smoke passed")
            click.echo(f"  ✗ smoke test failed: {smoke_reason}")
            click.echo("  → configured but not healthy; [t]est again, [h]elp for tips, or [s]kip.")
            click.echo("")
            just_failed = True

    return flow


def _kimi_flow(*, can_back: bool = False) -> WizardOutcome:
    """API-key flow with credential collection + storage choice."""
    info = _INFO["kimi"]
    if can_back:
        # Offer [b]ack before prompting for sensitive credentials so the user
        # can bail out to the previous provider without being forced to type
        # a token or hit Ctrl-C.
        options = [
            ("c", "continue — enter credentials now"),
            ("s", "skip this provider"),
            ("b", "back — redo the previous provider"),
            ("q", "quit setup"),
        ]
        entry = _prompt_menu(options=options, default="c")
        if entry == "s":
            return WizardOutcome("kimi", "skipped", "user skipped")
        if entry == "q":
            raise _AbortSetup()
        if entry == "b":
            raise _GoBack()
        click.echo("")
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


_FLOWS: dict[str, Callable[..., WizardOutcome]] = {
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


# --------------------------------------------------------------------------- #
# Agent-integration flow — runs after the provider walk.
# --------------------------------------------------------------------------- #


def _maybe_wire_agents(
    *,
    interactive: bool,
    wire_agents: str | None,
    patch_claude_md: str | None,
    patch_agents_md: str | None = None,
    patch_gemini_md: str | None = None,
    patch_claude_md_repo: str | None = None,
    wire_cursor_flag: str | None = None,
) -> bool:
    """Offer to wire conductor into detected agent tools (Claude Code
    user-scope + repo-scope ``AGENTS.md``).

    Returns True if no wiring was attempted (skipped, declined, or no
    target detected) or if the wiring fully succeeded. Returns False if
    wiring was attempted and failed — callers propagate this into the
    process exit code so CI / scripted ``--wire-agents=yes`` runs surface
    the failure instead of silently exiting 0.
    """
    from conductor import __version__
    from conductor.agent_wiring import detect

    detection = detect()

    claude_detected = detection.claude_detected

    # Decide the top-level wire-agents decision.
    decision = wire_agents if wire_agents is not None else ("ask" if interactive else "no")
    if decision == "no":
        return True

    # Per-section "this section runs" flags. A section runs if its artifact
    # is detected OR the user explicitly forced creation via a flag.
    run_agents_md = detection.agents_md_exists or patch_agents_md == "yes"
    run_gemini_md = detection.gemini_md_exists or patch_gemini_md == "yes"
    run_claude_md_repo = (
        detection.claude_md_repo_exists or patch_claude_md_repo == "yes"
    )
    run_cursor = detection.cursor_rules_dir_exists or wire_cursor_flag == "yes"
    any_detected_or_forced = any(
        (claude_detected, run_agents_md, run_gemini_md, run_claude_md_repo, run_cursor)
    )

    if not any_detected_or_forced:
        if interactive:
            click.echo("")
            click.echo("─" * 60)
            click.echo("Agent integration")
            click.echo("─" * 60)
            click.echo(
                "  No agent tools detected in this environment "
                "(no ~/.claude/, no ./AGENTS.md, no ./GEMINI.md, no ./.cursor/)."
            )
            click.echo(
                "  (re-run `conductor init` in a repo with one of the above, "
                "or after installing Claude Code)"
            )
            click.echo("")
        return True

    results: list[bool] = []
    if claude_detected:
        results.append(
            _wire_claude_code_section(
                detection=detection,
                version=__version__,
                interactive=interactive,
                decision=decision,
                patch_claude_md=patch_claude_md,
            )
        )
    if run_agents_md:
        results.append(
            _wire_agents_md_section(
                detection=detection,
                version=__version__,
                interactive=interactive,
                decision=decision,
                patch_agents_md=patch_agents_md,
            )
        )
    if run_gemini_md:
        results.append(
            _wire_gemini_md_section(
                detection=detection,
                version=__version__,
                interactive=interactive,
                decision=decision,
                patch_gemini_md=patch_gemini_md,
            )
        )
    if run_claude_md_repo:
        results.append(
            _wire_claude_md_repo_section(
                detection=detection,
                version=__version__,
                interactive=interactive,
                decision=decision,
                patch_claude_md_repo=patch_claude_md_repo,
            )
        )
    if run_cursor:
        results.append(
            _wire_cursor_section(
                detection=detection,
                version=__version__,
                interactive=interactive,
                decision=decision,
                wire_cursor_flag=wire_cursor_flag,
            )
        )

    return all(results)


def _wire_claude_code_section(
    *,
    detection,
    version: str,
    interactive: bool,
    decision: str,
    patch_claude_md: str | None,
) -> bool:
    """Claude Code user-scope wiring (Slice A behavior, extracted)."""
    from conductor.agent_wiring import wire_claude_code

    already_wired = len(detection.managed) > 0
    click.echo("")
    click.echo("─" * 60)
    click.echo("Agent integration — Claude Code")
    click.echo("─" * 60)
    if already_wired:
        click.echo(f"  Already wired ({len(detection.managed)} managed files found).")
        click.echo("  Re-running will refresh each file to the current version.")
    else:
        click.echo("  Claude Code can delegate to other models (kimi, gemini, …)")
        click.echo("  without leaving your editor. Conductor can wire this up by")
        click.echo("  writing:")
        click.echo("")
        click.echo(f"    {detection.conductor_home}/delegation-guidance.md")
        click.echo(f"    {detection.claude_home}/commands/conductor.md   (slash: /conductor)")
        click.echo(f"    {detection.claude_home}/agents/kimi-long-context.md")
        click.echo(f"    {detection.claude_home}/agents/gemini-web-search.md")
        click.echo("")
        click.echo("  Every file carries a 'managed-by: conductor' marker and is")
        click.echo("  fully removable via `conductor init --unwire`.")
    click.echo("")

    if decision == "ask":
        prompt = "Refresh conductor integration?" if already_wired else "Wire conductor in now?"
        proceed = _prompt_menu(
            options=[
                ("y", f"yes — {prompt.lower().rstrip('?')}"),
                ("n", "no — skip (you can re-run init later)"),
            ],
            default="y",
        )
        if proceed == "n":
            click.echo("  (skipped — no files written)")
            return True

    # Decide about the CLAUDE.md @import edit.
    pcm = (
        patch_claude_md
        if patch_claude_md is not None
        else ("ask" if interactive else "no")
    )
    if pcm == "ask":
        import_line = f"@{detection.conductor_home}/delegation-guidance.md"
        click.echo("")
        click.echo("  For Claude to actually read the guidance, one line needs to go")
        click.echo(f"  into ~/.claude/CLAUDE.md:  {import_line}")
        click.echo("")
        click.echo("  Conductor can add it inside a <!-- conductor:begin --> block")
        click.echo("  so `conductor init --unwire` can remove it cleanly later.")
        click.echo("")
        choice = _prompt_menu(
            options=[
                ("y", "yes — add the line for me"),
                ("n", "no — I'll add it manually"),
            ],
            default="y",
        )
        do_patch = choice == "y"
    else:
        do_patch = pcm == "yes"

    try:
        report = wire_claude_code(version, patch_claude_md=do_patch)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        click.echo(f"  ✗ wiring failed: {exc}")
        return False

    click.echo("")
    if report.written:
        click.echo("  ✓ wrote:")
        for p in report.written:
            click.echo(f"      {p}")
    for path, reason in report.skipped:
        click.echo(f"  ⚠ skipped {path}: {reason}")
    if report.patched_claude_md:
        click.echo(f"  ✓ patched {detection.claude_user_md} (sentinel block)")
    elif not do_patch:
        import_line = f"@{detection.conductor_home}/delegation-guidance.md"
        click.echo("")
        click.echo(
            "  To activate the guidance, add this line to ~/.claude/CLAUDE.md:"
        )
        click.echo(f"      {import_line}")

    click.echo("")
    click.echo("  Try it:")
    click.echo('    Ask Claude: "summarize this README with kimi"')
    click.echo("    Or run:     /conductor kimi summarize README.md")
    click.echo("")

    # If every target file was skipped (all user-owned), treat that as a
    # failed wire — the user asked for integration and got nothing.
    return not (report.skipped and not report.written)


def _wire_sentinel_section(
    *,
    title: str,
    filename: str,
    path,
    path_exists: bool,
    already_wired: bool,
    wire_fn,
    version: str,
    interactive: bool,
    patch_flag: str | None,
) -> bool:
    """Generic prompt + wire for any sentinel-block-style patch.

    Shared by AGENTS.md, GEMINI.md, and repo-scope CLAUDE.md — all three
    are user-owned files where conductor owns only the sentinel block.
    Idempotent: the underlying ``inject_sentinel_block`` helper replaces
    an existing conductor block in place, so re-running bumps the
    version and never duplicates.
    """
    click.echo("")
    click.echo("─" * 60)
    click.echo(f"Agent integration — {title}")
    click.echo("─" * 60)
    if path_exists:
        if already_wired:
            click.echo(f"  {path} already has a conductor block.")
            click.echo("  Re-running will refresh it to the current version.")
        else:
            click.echo(f"  {path} found — conductor can inject a")
            click.echo("  delegation block (sentinel-bounded, fully removable).")
    else:
        click.echo(
            f"  No {filename} in {path.parent}. Conductor can create one"
        )
        click.echo("  containing only the delegation block — not recommended")
        click.echo(f"  unless this repo is intended to use {filename} going forward.")
    click.echo("")

    pam = patch_flag if patch_flag is not None else ("ask" if interactive else "no")
    if pam == "ask":
        verb = "refresh" if already_wired else ("patch" if path_exists else "create")
        choice = _prompt_menu(
            options=[
                ("y", f"yes — {verb} {filename}"),
                ("n", f"no — skip {filename}"),
            ],
            default="y" if path_exists else "n",
        )
        if choice == "n":
            return True
        do_patch = True
    else:
        do_patch = pam == "yes"

    if not do_patch:
        return True

    try:
        report = wire_fn(version=version)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        click.echo(f"  ✗ {filename} patch failed: {exc}")
        return False

    click.echo(f"  ✓ patched {report.path} (sentinel block)")
    click.echo("")
    return True


def _wire_agents_md_section(
    *,
    detection,
    version: str,
    interactive: bool,
    decision: str,
    patch_agents_md: str | None,
) -> bool:
    """Repo-scope AGENTS.md — Codex / Cursor / Zed shared convention."""
    from conductor.agent_wiring import wire_agents_md

    already = any(a.kind == "agents-md-import" for a in detection.managed)
    return _wire_sentinel_section(
        title="AGENTS.md (repo-scoped)",
        filename="AGENTS.md",
        path=detection.agents_md,
        path_exists=detection.agents_md_exists,
        already_wired=already,
        wire_fn=wire_agents_md,
        version=version,
        interactive=interactive,
        patch_flag=patch_agents_md,
    )


def _wire_gemini_md_section(
    *,
    detection,
    version: str,
    interactive: bool,
    decision: str,
    patch_gemini_md: str | None,
) -> bool:
    """Repo-scope GEMINI.md — Gemini CLI convention."""
    from conductor.agent_wiring import wire_gemini_md

    already = any(a.kind == "gemini-md-import" for a in detection.managed)
    return _wire_sentinel_section(
        title="GEMINI.md (repo-scoped)",
        filename="GEMINI.md",
        path=detection.gemini_md,
        path_exists=detection.gemini_md_exists,
        already_wired=already,
        wire_fn=wire_gemini_md,
        version=version,
        interactive=interactive,
        patch_flag=patch_gemini_md,
    )


def _wire_claude_md_repo_section(
    *,
    detection,
    version: str,
    interactive: bool,
    decision: str,
    patch_claude_md_repo: str | None,
) -> bool:
    """Repo-scope ./CLAUDE.md — parallel to user-scope ~/.claude/CLAUDE.md."""
    from conductor.agent_wiring import wire_claude_md_repo

    already = any(a.kind == "claude-md-repo-import" for a in detection.managed)
    return _wire_sentinel_section(
        title="CLAUDE.md (repo-scoped)",
        filename="CLAUDE.md",
        path=detection.claude_md_repo,
        path_exists=detection.claude_md_repo_exists,
        already_wired=already,
        wire_fn=wire_claude_md_repo,
        version=version,
        interactive=interactive,
        patch_flag=patch_claude_md_repo,
    )


def _wire_cursor_section(
    *,
    detection,
    version: str,
    interactive: bool,
    decision: str,
    wire_cursor_flag: str | None,
) -> bool:
    """Cursor rule at <repo>/.cursor/rules/conductor-delegation.mdc.

    Unlike the sentinel-block cases, this is a fully-managed file —
    conductor owns it whole. ``unwire`` deletes it.
    """
    from conductor.agent_wiring import wire_cursor

    already_written = any(a.kind == "cursor-rule" for a in detection.managed)

    click.echo("")
    click.echo("─" * 60)
    click.echo("Agent integration — Cursor rule (repo-scoped)")
    click.echo("─" * 60)
    rule_path = detection.cursor_rules_dir / "conductor-delegation.mdc"
    if detection.cursor_rules_dir_exists:
        if already_written:
            click.echo(f"  {rule_path} already exists (managed by conductor).")
            click.echo("  Re-running will refresh it to the current version.")
        else:
            click.echo("  Cursor rules dir found — conductor can write")
            click.echo(f"    {rule_path}")
            click.echo("  as a fully-managed rule (removable via --unwire).")
    else:
        click.echo(
            f"  No .cursor/rules/ dir in {detection.cursor_rules_dir.parent}."
        )
        click.echo("  Conductor can create it and write the delegation rule —")
        click.echo("  only useful if this repo is intended to use Cursor.")
    click.echo("")

    wc = (
        wire_cursor_flag
        if wire_cursor_flag is not None
        else ("ask" if interactive else "no")
    )
    if wc == "ask":
        verb = "refresh" if already_written else "write"
        choice = _prompt_menu(
            options=[
                ("y", f"yes — {verb} the Cursor rule"),
                ("n", "no — skip Cursor"),
            ],
            default="y" if detection.cursor_rules_dir_exists else "n",
        )
        if choice == "n":
            return True
        do_write = True
    else:
        do_write = wc == "yes"

    if not do_write:
        return True

    try:
        report = wire_cursor(version=version)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        click.echo(f"  ✗ Cursor rule write failed: {exc}")
        return False

    click.echo(f"  ✓ wrote {report.path} (managed file)")
    click.echo("")
    return True


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
        "Conductor baseline routing: prefer=balanced, effort=medium. "
        "Callers override per invocation (e.g. Touchstone's pre-push review "
        "uses prefer=best, effort=max)."
    )
    click.echo(
        "  tune: --prefer / --effort on call/exec, or CONDUCTOR_PREFER / "
        "CONDUCTOR_EFFORT env vars."
    )
    click.echo(
        "  inspect: `conductor config show` to see effective config and provenance."
    )
