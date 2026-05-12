```text
   ___                _            _
  / __\___  _ __   __| |_   _  ___| |_ ___  _ __
 / /  / _ \| '_ \ / _` | | | |/ __| __/ _ \| '__|
/ /__| (_) | | | | (_| | |_| | (__| || (_) | |
\____/\___/|_| |_|\__,_|\__,_|\___|\__\___/|_|
```

[![Release](https://img.shields.io/github/v/release/autumngarage/conductor?label=release&color=informational)](https://github.com/autumngarage/conductor/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Homebrew](https://img.shields.io/badge/brew-autumngarage%2Fconductor-orange)](https://github.com/autumngarage/homebrew-conductor)

> **Pick an LLM, give it a job. Manual or auto routing across providers.**
>
> *The voice* of the **[Autumn Garage](https://github.com/autumngarage/autumn-garage)** quartet, alongside [Touchstone](https://github.com/autumngarage/touchstone) · [Cortex](https://github.com/autumngarage/cortex) · [Sentinel](https://github.com/autumngarage/sentinel).

# Conductor

Conductor is a capability-aware router across LLM providers. It owns the adapter layer and the user-facing "pick an LLM, give it a job" surface so other tools (Sentinel, Touchstone, your own scripts) don't each have to.

Built-in providers: `kimi`, `openrouter`, `deepseek-chat`, `deepseek-reasoner`, `claude`, `codex`, `gemini`, and `ollama`. Three modes:

- **`conductor ask`** — semantic routing. Say what *kind* of work this is and how much *effort* you want; Conductor picks.
- **`conductor call --with <provider>`** — manual. Pin a specific provider.
- **`conductor call --auto [--tags ...]`** — rule-based router by capability tag.

Plus first-class `conductor review` for code review and `conductor exec` for multi-turn tool-using sessions.

## Install

```sh
brew install autumngarage/conductor/conductor
conductor init       # credentials wizard + optional agent-tool wiring
```

Or from source:

```sh
# Dev install from a clone
git clone https://github.com/autumngarage/conductor
cd conductor
bash setup.sh
uv sync

# Or via pip directly from the repo (the bare name `conductor` on PyPI
# is an unrelated project — use the git URL explicitly):
pip install git+https://github.com/autumngarage/conductor
```

### Credential setup

Runtime resolution order: `env → key_command → keychain`. The wizard picks a storage path that's fast day-to-day and keeps the secret encrypted at rest where the host supports it.

- **macOS** — defaults to macOS Keychain. The wizard writes the key, then probe-reads it so macOS shows the first-read prompt. Click `Always Allow` and later reads stay silent.
- **Linux** — uses `libsecret` via `secret-tool` when available (encrypted at rest). Falls back to an environment-variable export when not.
- **1Password** — available when the `op` CLI is on `PATH`. The wizard stores an `op read op://...` command in `~/.config/conductor/credentials.toml`; the secret itself never lands on disk.
- **CI** — use environment variables from your runner or secret store, e.g. GitHub Actions secrets mapped to `OPENROUTER_API_KEY`.

**Provider notes:**

- `kimi` and `deepseek-*` route through OpenRouter. Set `OPENROUTER_API_KEY`; legacy `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` and `DEEPSEEK_API_KEY` are deprecated. Conductor resolves the newest matching slug from the OpenRouter catalog and falls back to a pinned default if the catalog is unavailable.

## Quickstart

```sh
export OPENROUTER_API_KEY=sk-or-v1-...

# Manual: pick a specific provider
conductor call --with kimi --brief "What is 2+2?"

# Pipe content as the brief
cat README.md | conductor call --with kimi --brief "Summarize this in one sentence."

# Get the full response as JSON (for scripting)
conductor call --with kimi --brief "ping" --json

# Read-only code review uses the semantic review cascade by default
conductor review --base origin/main --brief-file /tmp/review.md

# Semantic API: say what kind of work this is and let Conductor pick
conductor ask --kind research --effort medium --brief-file /tmp/brief.md
conductor ask --kind code --effort high --brief-file /tmp/brief.md
conductor ask --kind council --effort medium --brief-file /tmp/brief.md
```

For delegation from Claude, Codex, or another agent, prefer `--brief-file PATH` and include the goal, context, scope, constraints, expected output, and validation. Conductor only sees the brief you pass plus any files the delegated provider can inspect; it does not inherit the caller's conversation context. Existing `--task` / `--task-file` flags remain supported as compatibility aliases.

## Commands

- **`conductor ask --kind <research|code|review|council> --effort <level>`** — deterministic semantic routing. Research and low/medium code favor call-mode answer synthesis and cannot write files or open PRs; high-effort code escalates through Codex, Claude, OpenRouter tool-use exec, then Ollama; review routes to the semantic review cascade; council fans out through OpenRouter and synthesizes the results.
- **`conductor call --with <id> --brief "..."`** — manual mode for any provider.
- **`conductor call --auto [--tags a,b,c] --brief "..."`** — rule-based router picks the best configured provider for the task's tags.
- **`conductor swarm --brief a.md --brief b.md --provider codex --max-parallel 2 --json`** — first-class multi-task coding supervisor with isolated worktrees and structured per-task results.
- **`conductor review --base <ref> --brief-file <path>`** — code review uses the same semantic review cascade as `ask --kind review`: Codex native review, then Claude native review, then an OpenRouter hosted review prompt. Use `--with <provider>` to hard-pin one provider.
- **`conductor exec --with <provider> --tools Read,Grep,Edit --brief-file <path>`** — multi-turn agent session with tool use.
- **`conductor list [--json]`** — shows every provider with ready/not-ready status, default model, and capability tags.
- **`conductor smoke <id>`** / **`conductor smoke --all`** — proves a provider's auth + endpoint work (cheapest round-trip that exercises the full path).
- **`conductor doctor [--json]`** — diagnostic report: which providers are configured, which env vars are set, what's in the macOS Keychain.
- **`conductor init [-y]`** — interactive first-run wizard (TTY-detected, `--yes` for non-TTY). Recommends macOS Keychain or Linux `secret-tool` when available, keeps 1Password available via `op read`, runs a setup verification smoke test.
- **`conductor update [--dry-run] [--check]`** — refreshes stale embedded Conductor repo integrations in the current repo and stages the refreshed paths.
- **`conductor update-all [--paths ...] [--config-file ...] [--branch ...] [--no-auto-stash]`** — batch-refreshes configured consumer repos on review branches.
- **`conductor refresh-on-commit`** — hook-mode counterpart to `update`, installed by `conductor init` for pre-commit refresh of stale embedded repo integrations.

### Smart routing

- **Credentials resolver** (`conductor.credentials`): env var first, then `key_command`, then macOS Keychain under service `conductor`.
- **Offline-mode fallback**: on a real connectivity failure (DNS, TCP reset, unreachable host) during `--auto` routing, Conductor prompts once to switch to the local `ollama` provider and remembers that choice for a short window. `conductor call --offline --brief "..."` is the non-interactive form — useful on a plane, in CI, or any time you want to force local. Clear the sticky flag with `--no-offline`.
- **Review-gate routing**: `--auto` routes tagged `code-review` derive bounded provider budgets from prompt size and fallback count, so consumers don't have to guess raw timeout flags. Conductor owns provider timeout/stall budgets for these routes even when a caller accidentally forwards timeout flags. OpenRouter empty responses are retried against the remaining model stack before surfacing a provider error.

## Agent integration

`conductor init` detects which agent tools you have installed (Claude Code, Codex, Cursor, Gemini CLI, Zed — anything that reads `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`, or looks at `.cursor/rules/`) and offers to wire Conductor in so those agents can delegate to other LLMs without you hand-authoring any prompts:

- Writes `~/.conductor/delegation-guidance.md` (canonical guidance) and appropriate user-scope artifacts (slash command + subagents for Claude Code).
- Injects a self-contained delegation block into any agent instruction files present in your repo (`AGENTS.md`, `GEMINI.md`, `CLAUDE.md`). Existing user content is preserved — the block is bounded by `<!-- conductor:begin -->` markers.
- Writes a Cursor rule at `.cursor/rules/conductor-delegation.mdc` if the directory exists.
- Installs the `conductor-refresh` pre-commit hook by default so stale embed-only repo instructions refresh on commit; pass `--no-hooks` to skip.

On a TTY you get a prompt per detected file (default yes); in CI use `--wire-agents=yes`, `--patch-claude-md=yes`, `--patch-agents-md=yes`, `--patch-gemini-md=yes`, `--patch-claude-md-repo=yes`, and `--wire-cursor=yes` to accept specific pieces without interaction. Everything Conductor writes is marked `managed-by: conductor vX.Y.Z`; `conductor init --unwire` removes exactly those files and strips the sentinel blocks, preserving user content.

Diagnose wiring state anytime with `conductor doctor` (JSON shape: `--json`). Run `conductor update` to refresh stale embedded repo-scope wiring in the current repo immediately. Use `conductor update-all` when you intentionally need to walk configured consumer repos.

## The quartet

Conductor is the voice every other tool that needs an LLM speaks through:

- **[Touchstone](https://github.com/autumngarage/touchstone)** — scaffolding + pre-push AI review gate. *The ground.*
- **[Cortex](https://github.com/autumngarage/cortex)** — portable file-format protocol for project memory. *The spine.*
- **[Sentinel](https://github.com/autumngarage/sentinel)** — autonomous assess→plan→delegate→review loop. *The hands.*
- **Conductor** *(this tool)* — capability-aware router across LLM providers. *The voice.*

Each tool installs independently and composes through **file contracts, never code imports**. Touchstone's pre-push review hook calls `conductor review`; Sentinel's coder, reviewer, planner, and researcher roles all shell out to `conductor`; both consumers store zero provider API keys. See [autumn-garage](https://github.com/autumngarage/autumn-garage) for the coordination repo.

```sh
brew install autumngarage/touchstone/touchstone   # pre-push code review
brew install autumngarage/cortex/cortex           # project memory
brew install autumngarage/sentinel/sentinel       # autonomous agent cycles
brew install autumngarage/conductor/conductor     # provider routing (this tool)
```

## Architecture

See [`CLAUDE.md`](./CLAUDE.md) for the full layout and the principles applied to provider adapters.

## Status

Production-ready and shipped via Homebrew. Latest release: [GitHub Releases](https://github.com/autumngarage/conductor/releases). Run `conductor doctor` to see provider readiness on your machine.

## License

MIT
