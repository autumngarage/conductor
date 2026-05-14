```text
   ___                _            _
  / __\___  _ __   __| |_   _  ___| |_ ___  _ __
 / /  / _ \| '_ \ / _` | | | |/ __| __/ _ \| '__|
/ /__| (_) | | | | (_| | |_| | (__| || (_) | |
\____/\___/|_| |_|\__,_|\__,_|\___|\__\___/|_|
```

> *Pick an LLM, give it a job.*
>
> by **[Autumn Garage](https://github.com/autumngarage/autumn-garage)** · alongside [Touchstone](https://github.com/autumngarage/touchstone) · [Cortex](https://github.com/autumngarage/cortex) · [Sentinel](https://github.com/autumngarage/sentinel)

# conductor

Pick an LLM, give it a job. Manual or auto routing across providers.

**Status:** shipping. Current tap release is v0.8.7 — built-in providers for `kimi`, `openrouter`, `deepseek-chat`, `deepseek-reasoner`, `claude`, `codex`, `gemini`, and `ollama`; semantic `ask`; manual + auto routing; single-turn `call`; native `review`; multi-turn unsandboxed `exec` with tools; and agent-wiring for Claude Code, Codex, Gemini, Cursor, and repo instruction files.

DeepSeek note: `deepseek-chat` and `deepseek-reasoner` now use OpenRouter credentials. Set `OPENROUTER_API_KEY`; `DEEPSEEK_API_KEY` is deprecated. Conductor resolves the newest matching DeepSeek slug from the OpenRouter catalog and falls back to the pinned default if the catalog is unavailable.
Kimi note: `kimi` now routes through OpenRouter. Set `OPENROUTER_API_KEY`; legacy `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` are no longer used. Conductor resolves the newest matching Kimi slug from the OpenRouter catalog and falls back to the pinned default if the catalog is unavailable.

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

## Recommended credential setup

Conductor's runtime resolution order stays `env -> key_command -> keychain`. The wizard chooses a storage path that is fast to use day-to-day and keeps the secret encrypted at rest when the host supports it.

- macOS: default to `macOS Keychain`. The wizard writes the key, then immediately probe-reads it so macOS can show the first-read prompt. Click `Always Allow` and later `conductor` reads stay silent.
- Linux: if `secret-tool` is available, default to `libsecret` via `secret-tool store` plus a generated `key_command` lookup. That keeps the secret encrypted at rest without changing the runtime resolution order. If `secret-tool` is unavailable, the wizard falls back to an environment-variable export and tells you that path is not encrypted at rest.
- 1Password: available when the `op` CLI is on `PATH`. The wizard stores an `op read op://...` command in `~/.config/conductor/credentials.toml`; the secret itself never lands on disk. For zero-friction reads, set `1Password -> Settings -> Security -> Auto-Lock` to `Never`.
- CI: use environment variables from your runner or secret store, e.g. GitHub Actions repository or environment secrets mapped to `OPENROUTER_API_KEY`.
- Live subprocess smoke in GitHub Actions needs `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `GEMINI_API_KEY`; the nightly workflow skips when they are absent.

### Alternatives

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

## Quick start

```sh
# Kimi now routes through OpenRouter.
export OPENROUTER_API_KEY=sk-or-v1-...

# Manual mode: pick a specific provider
conductor call --with kimi --brief "What is 2+2?"

# Pipe content as the brief
cat README.md | conductor call --with kimi --brief "Summarize this in one sentence."

# Override the default model (default: moonshotai/kimi-k2.6)
conductor call --with kimi --model moonshotai/kimi-k2 --brief "..."

# Get the full response as JSON (for scripting)
conductor call --with kimi --brief "ping" --json

# Read-only code review uses the semantic review cascade by default
conductor review --base origin/main --brief-file /tmp/review.md

# Semantic API: say what kind of work this is and let Conductor pick
conductor ask --kind research --effort medium --brief-file /tmp/brief.md
conductor ask --kind code --effort high --brief-file /tmp/brief.md
conductor ask --kind council --effort medium --brief-file /tmp/brief.md
```

For delegation from Claude, Codex, or another agent, prefer
`--brief-file PATH` and include the goal, context, scope, constraints,
expected output, and validation. Conductor only sees the brief you pass
plus any files the delegated provider can inspect; it does not inherit
the caller's conversation context. Existing `--task` / `--task-file`
flags remain supported as compatibility aliases.
Headless orchestrators can run repo-changing work with
`conductor exec --brief-file /tmp/brief.md`.

## v0.1 scope

Shipped:

- Built-in providers: `kimi` (OpenRouter-backed HTTP preset), `openrouter`, `deepseek-chat`, `deepseek-reasoner`, `claude`, `codex`, `gemini`, and `ollama`.
- `conductor ask --kind <research|code|review|council> --effort <level>` — deterministic semantic routing. Research and low/medium code favor call-mode answer synthesis and cannot write files or open PRs; high-effort code escalates through Codex, Claude, OpenRouter tool-use exec, then Ollama; review routes to native review; council fans out through OpenRouter and synthesizes the results.
- `conductor call --with <id> --brief "..."` — manual mode for any provider.
- `conductor call --auto [--tags a,b,c] --brief "..."` — rule-based router picks the best configured provider for the task's tags.
- `conductor swarm --brief a.md --brief b.md --provider codex --max-parallel 2 --json` — first-class multi-task coding supervisor with isolated worktrees and structured per-task results.
- `conductor review --base <ref> --brief-file <path>` — code review uses the same semantic review cascade as `ask --kind review`: Codex native review, then Claude native review, then an OpenRouter hosted review prompt. Use `--with <provider>` to hard-pin one provider.
- `conductor list [--json]` — shows every provider with ready/not-ready status, default model, and capability tags.
- `conductor smoke <id>` / `conductor smoke --all [--json]` — proves a provider's auth + endpoint work (cheapest round-trip that exercises the full path).
- `conductor doctor [--json]` — diagnostic report: which providers are configured, which env vars are set, what's in the macOS Keychain.
- `conductor init [-y]` — interactive first-run wizard (TTY-detected, `--yes` for non-TTY). For providers needing credentials (`openrouter`, plus OpenRouter-backed `kimi` / `deepseek-*`), prompts, recommends macOS Keychain or Linux `secret-tool` when available, keeps 1Password available via `op read`, runs a setup verification smoke test, and prints the manual env-var fallback when no encrypted store is available.
- `conductor update [--dry-run] [--check]` — refreshes stale embedded Conductor repo integrations in the current repo and stages the refreshed paths. `--check` exits non-zero when an update is needed.
- `conductor update-all [--paths ...] [--config-file ...] [--branch ...] [--no-auto-stash]` — batch-refreshes configured consumer repos on review branches. `conductor refresh-consumers` remains as a deprecated compatibility alias and prints a one-line warning.
- `conductor refresh-on-commit` — hook-mode counterpart to `update`, installed by `conductor init` for pre-commit refresh of stale embedded repo integrations.
- Credentials resolver (`conductor.credentials`): env var first, then `key_command`, then macOS Keychain under service `conductor`.
- Offline-mode fallback: on a real connectivity failure (DNS, TCP reset, unreachable host) during `--auto` routing, Conductor prompts once to switch to the local `ollama` provider and remembers that choice for a short window. `conductor call --offline --brief "..."` is the non-interactive form — useful on a plane, in CI, or any time you want to force local. Clear the sticky flag with `--no-offline`. While the machine is online, direct `--with ollama` usage requires explicit local opt-in with `--offline` or `CONDUCTOR_ALLOW_LOCAL_ONLINE=1`; `conductor list` reports Ollama as `local/offline-only` rather than a peer hosted provider. Ollama requests use `CONDUCTOR_OLLAMA_MODEL` when set; when no explicit `--model` is passed and the requested local model is missing, Conductor queries `/api/tags` and retries once with a non-embedding installed chat model.
- Review-gate routing: `--auto` routes tagged `code-review` derive bounded provider budgets from prompt size and fallback count, so consumers do not need to guess raw timeout flags for normal review delegation. For these review-gate routes, Conductor owns provider timeout/stall budgets even when a caller accidentally forwards timeout flags. OpenRouter empty responses are retried against the remaining model stack before surfacing a provider error.

Deferred (see `autumn-garage/.cortex/plans/conductor-bootstrap.md`):

- Streaming, cost aggregation — post-v0.1. (Tool use shipped in v0.3.x.)
- LLM-based meta-routing for `--auto` (today: rule-based tag scoring).
- Native `op run` environment injection inside `conductor init`.

## Agent integration

> **What's in v0.8.7 (tap):** user-scope Claude Code wiring, repo-scope
> `AGENTS.md` / `GEMINI.md` / `CLAUDE.md` patching, Cursor rules, and
> `--unwire`. The repo-scope flags are `--patch-agents-md`,
> `--patch-gemini-md`, `--patch-claude-md-repo`, and `--wire-cursor`.

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
- Installs the `conductor-refresh` pre-commit hook by default so stale
  embed-only repo instructions refresh on commit; pass `--no-hooks` to skip.

On a TTY you get a prompt per detected file (default yes); in CI use
`--wire-agents=yes`, `--patch-claude-md=yes`, `--patch-agents-md=yes`,
`--patch-gemini-md=yes`, `--patch-claude-md-repo=yes`, and `--wire-cursor=yes`
to accept specific pieces without interaction. Everything conductor writes
is marked `managed-by: conductor vX.Y.Z`; `conductor init --unwire` removes
exactly those files and strips the sentinel blocks, preserving user content.

Diagnose wiring state anytime with `conductor doctor` (JSON shape:
`--json`).
Run `conductor update` to refresh stale embedded repo-scope wiring in the
current repo immediately. Use `conductor update-all` when you intentionally
need to walk configured consumer repos; `refresh-consumers` still works as a
deprecated alias for existing scripts. The installed pre-commit hook continues
to call `conductor refresh-on-commit`. Normal diagnostic and delegation
commands do not rewrite tracked repo-scope integration files by default. Set
`CONDUCTOR_AUTO_REFRESH_REPO_SCOPE=1` only if you intentionally want the older
ambient repo-refresh behavior.

## How Sentinel and Touchstone use it

Both consumers are expected to migrate from per-tool provider implementations to `conductor call` shell-outs. The migrations are tracked in separate plans (`autumn-garage/.cortex/plans/sentinel-conductor-migration.md` and `…/touchstone-conductor-migration.md`).

## Architecture

See `CLAUDE.md` for the full layout and the principles applied to provider adapters.

## License

MIT.
