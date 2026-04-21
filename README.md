# conductor

Pick an LLM, give it a job. Manual or auto routing across providers.

**Status:** v0.1 in flight. Kimi adapter shipped as the integration test case for the Conductor architecture. Other providers (claude, codex, gemini, ollama) and auto-mode routing land in subsequent phases.

Conductor is the fourth peer in the [Autumn Garage](https://github.com/autumngarage/autumn-garage) tool family alongside [Touchstone](https://github.com/autumngarage/touchstone), [Cortex](https://github.com/autumngarage/cortex), and [Sentinel](https://github.com/autumngarage/sentinel). It owns the LLM provider adapters and the user-facing "pick an LLM, give it a job" surface so that Sentinel and Touchstone don't each have to.

## Install

```sh
# Clone + dev install
git clone https://github.com/autumngarage/conductor
cd conductor
bash setup.sh
uv sync
```

Brew tap (`autumngarage/conductor/conductor`) ships with v0.1.0.

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

- `conductor call --with kimi --task "..."` — manual mode for the Kimi (Moonshot AI) provider via OpenAI-compatible HTTP at `api.moonshot.ai/v1`.
- `--task` flag or stdin for input. `--json` for structured output. `--model` to override the default.
- Test suite covers the happy path, missing key, non-200 responses, and malformed responses with mocked httpx.

Deferred to subsequent phases (see `autumn-garage/.cortex/plans/conductor-bootstrap.md`):

- Adapters for `claude`, `codex`, `gemini` (CLI shell-out) and `ollama` (HTTP).
- Auto mode (`--auto` with rule-based routing on task tags + provider capabilities).
- Discovery commands: `conductor list`, `conductor smoke <id>`, `conductor doctor`.
- Interactive setup wizard: `conductor init` (per Doctrine 0002).
- Streaming, tool use, cost aggregation — all post-v0.1.

## How Sentinel and Touchstone use it

Once Conductor v0.1 ships, both consumers migrate from per-tool provider implementations to `conductor call` shell-outs. That migration lands in separate plans (`autumn-garage/.cortex/plans/sentinel-conductor-migration.md` and `…/touchstone-conductor-migration.md`) and is not part of v0.1.

## Architecture

See `CLAUDE.md` for the full layout and the principles applied to provider adapters.

## License

MIT.
