# conductor

Pick an LLM, give it a job. Manual or auto routing across providers.

**Status:** shipping. Five provider adapters (kimi, claude, codex, gemini, ollama), manual + auto routing, single-turn `call` + multi-turn `exec` with tools and sandboxes, agent-wiring for Claude Code / Codex / Cursor / Gemini CLI. See the Homebrew tap for the latest released version.

Conductor is the fourth peer in the [Autumn Garage](https://github.com/autumngarage/autumn-garage) tool family alongside [Touchstone](https://github.com/autumngarage/touchstone), [Cortex](https://github.com/autumngarage/cortex), and [Sentinel](https://github.com/autumngarage/sentinel). It owns the LLM provider adapters and the user-facing "pick an LLM, give it a job" surface so that Sentinel and Touchstone don't each have to.

## Install

```sh
brew install autumngarage/conductor/conductor
```

Same pattern as the other Autumn Garage peers:

```sh
brew install autumngarage/touchstone/touchstone   # pre-push code review
brew install autumngarage/cortex/cortex           # project memory
brew install autumngarage/sentinel/sentinel       # autonomous agent cycles
```

Then walk the setup wizard:

```sh
conductor init       # credentials + optional agent-tool wiring
```

### Alternatives

```sh
# Dev install from a clone
git clone https://github.com/autumngarage/conductor
cd conductor
bash setup.sh
uv sync

# Or via pip
pip install conductor
```

## Quick start

```sh
# Conductor calls Kimi K2.6 via Cloudflare Workers AI. Get a token at
#   https://dash.cloudflare.com/profile/api-tokens   (needs Workers AI read)
# and find your account ID in the Cloudflare dashboard sidebar.
export CLOUDFLARE_API_TOKEN=cf-...
export CLOUDFLARE_ACCOUNT_ID=...

# Manual mode: pick a specific provider
conductor call --with kimi --task "What is 2+2?"

# Pipe content as the task
cat README.md | conductor call --with kimi --task "Summarize this in one sentence."

# Override the default model (default: @cf/moonshotai/kimi-k2.6)
conductor call --with kimi --model @cf/moonshotai/kimi-k2.5 --task "..."

# Get the full response as JSON (for scripting)
conductor call --with kimi --task "ping" --json
```

## v0.1 scope

Shipped:

- Five provider adapters: `kimi` (Cloudflare Workers AI HTTP), `claude` / `codex` / `gemini` (CLI shell-out), `ollama` (local HTTP).
- `conductor call --with <id> --task "..."` — manual mode for any provider.
- `conductor call --auto [--tags a,b,c] --task "..."` — rule-based router picks the best configured provider for the task's tags.
- `conductor list [--json]` — shows every provider with ready/not-ready status, default model, and capability tags.
- `conductor smoke <id>` / `conductor smoke --all [--json]` — proves a provider's auth + endpoint work (cheapest round-trip that exercises the full path).
- `conductor doctor [--json]` — diagnostic report: which providers are configured, which env vars are set, what's in the macOS Keychain.
- `conductor init [-y]` — interactive first-run wizard (TTY-detected, `--yes` for non-TTY). For providers needing credentials (today just `kimi`), prompts, offers macOS Keychain / direnv `.envrc` / print-only storage, runs the smoke test, prints the equivalent non-interactive setup.
- Credentials resolver (`conductor.credentials`): env var first, then macOS Keychain under service `conductor`.

Deferred (see `autumn-garage/.cortex/plans/conductor-bootstrap.md`):

- Streaming, cost aggregation — post-v0.1. (Tool use shipped in v0.3.x.)
- LLM-based meta-routing for `--auto` (today: rule-based tag scoring).
- 1Password (`op run`) storage backend for `conductor init`.

## Agent integration (v0.4.x)

`conductor init` detects which agent tools you have installed (Claude Code,
Codex, Cursor, Gemini CLI, Zed — anything that reads `AGENTS.md` /
`CLAUDE.md` / `GEMINI.md`, or looks at `.cursor/rules/`) and offers to wire
conductor in so those agents can delegate to other LLMs without you
hand-authoring any prompts:

- Writes `~/.conductor/delegation-guidance.md` (canonical guidance) and
  appropriate user-scope artifacts (slash command + subagents for Claude Code).
- Injects a self-contained delegation block into any agent instruction
  files present in your repo (`AGENTS.md`, `GEMINI.md`, `CLAUDE.md`).
  Existing user content is preserved — the block is bounded by
  `<!-- conductor:begin -->` markers.
- Writes a Cursor rule at `.cursor/rules/conductor-delegation.mdc` if
  the directory exists.

On a TTY you get a prompt per detected file (default yes); in CI use
`--wire-agents=yes`, `--patch-claude-md=yes`, `--patch-agents-md=yes`,
`--patch-gemini-md=yes`, `--patch-claude-md-repo=yes`, and `--wire-cursor=yes`
to accept specific pieces without interaction. Everything conductor writes
is marked `managed-by: conductor vX.Y.Z`; `conductor init --unwire` removes
exactly those files and strips the sentinel blocks, preserving user content.

Diagnose wiring state anytime with `conductor doctor` (JSON shape:
`--json`).

## How Sentinel and Touchstone use it

Once Conductor v0.1 ships, both consumers migrate from per-tool provider implementations to `conductor call` shell-outs. That migration lands in separate plans (`autumn-garage/.cortex/plans/sentinel-conductor-migration.md` and `…/touchstone-conductor-migration.md`) and is not part of v0.1.

## Architecture

See `CLAUDE.md` for the full layout and the principles applied to provider adapters.

## License

MIT.
