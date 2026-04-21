# conductor — Claude Code Instructions

## Who You Are on This Project

Conductor is a small CLI that owns LLM provider adapters and the user-facing "pick an LLM, give it a job" surface. It exists so that Sentinel and Touchstone don't each have to implement and maintain their own provider adapters — both shell out to `conductor call` instead.

You are building the fourth peer of the Autumn Garage. The trio (Touchstone, Cortex, Sentinel) composes through file contracts, not shared code. Conductor extends that pattern: it's an independently-released CLI that other garage tools call as a subprocess, never import as a library.

"Good" looks like: a stable, narrow CLI surface (`conductor call`, `conductor list`, `conductor smoke`, `conductor init`, `conductor doctor`); provider adapters that follow the same Provider Protocol regardless of whether they wrap an HTTP API or a CLI; auto-mode routing that's documented and predictable, never magical; setup UX that satisfies Doctrine 0002 (interactive on TTY, flag-driven in CI).

The companion plan in autumn-garage names what v0.1 ships and what's deferred: `~/Repos/autumn-garage/.cortex/plans/conductor-bootstrap.md`. The doctrine that establishes Conductor's role is `~/Repos/autumn-garage/.cortex/doctrine/0004-conductor-as-fourth-peer.md`.

## Engineering Principles

@principles/engineering-principles.md
@principles/pre-implementation-checklist.md
@principles/audit-weak-points.md
@principles/documentation-ownership.md

## Git Workflow

@principles/git-workflow.md


## Current state (read this first)

@.cortex/state.md

## Cortex Protocol

@.cortex/protocol.md

### The lifecycle (drive this automatically, do not ask the user for permission at each step)

1. **Pull.** `git pull --rebase` on the default branch before starting work.
2. **Branch.** `git checkout -b <type>/<short-description>` where `<type>` is one of `feat`, `fix`, `chore`, `refactor`, `docs`.
3. **Change + commit.** Make the code change, stage explicit file paths, commit with a concise message.
4. **Ship.** `bash scripts/open-pr.sh --auto-merge` — pushes, creates the PR, runs Codex review, squash-merges, and syncs the default branch in one step.
5. **Clean up.** `git branch -D <feature-branch>` if it still exists locally.

### Housekeeping

- Concise commit messages. Logically grouped changes.
- Run `/compact` at ~50% context. Start fresh sessions for unrelated work.

### Memory Hygiene

- Treat Claude Code memory as cached guidance, not canonical truth. Before relying on a remembered command, flag, path, version, or workflow, verify it against this repo.
- Do not write memory for facts that are cheap to derive from `README.md`, `CLAUDE.md`, `AGENTS.md`, `.touchstone-config`, release docs, or the code itself.
- If memory conflicts with the repo, follow the repo and ask to audit or update the stale memory.

## Testing

```bash
bash setup.sh --deps-only          # reinstall deps
uv run pytest                      # tests
uv run ruff check src/ tests/      # lint
uv run ruff check --fix src/ tests/   # auto-fix
```

Fix failing tests before pushing. Live-API tests are gated on `RUN_LIVE_SMOKE=1` and the relevant `*_API_KEY` being set; CI runs only the mocked tests by default.

## Release & Distribution

Homebrew formula via `autumngarage/homebrew-conductor` tap (planned for v0.1.0; not yet wired). Version derived from git tag via `hatch-vcs`. Release process: tag on main (`git tag v0.X.Y`), push tag, `gh release create`, update Homebrew formula SHA. Pip install also supported (`pip install conductor`).

## Architecture

### Core idea

Conductor exposes two modes:

- **Manual:** `conductor call --with <provider> --task "..."` — caller picks the provider explicitly.
- **Auto:** `conductor call --auto --task "..." --tags <a,b,c>` — Conductor's router picks based on task tags + provider capability tags.

Both modes return the same `CallResponse` shape on stdout (text or JSON via `--json`). Consumers (Sentinel, Touchstone) shell out and read stdout; they never import Conductor as a library. This preserves Sentinel's "no Python coupling between trio tools" invariant and lets Conductor release on its own cadence.

### Two physical adapter shapes

- **HTTP adapters** (e.g. `kimi`, `ollama`) talk to an OpenAI-compatible endpoint via `httpx`. These are the only adapters that touch API keys directly.
- **Subprocess adapters** (e.g. `claude`, `codex`, `gemini`) shell out to a CLI that owns its own auth. These never touch API keys.

Both shapes implement the same `Provider` Protocol (`configured()`, `smoke()`, `call()`) so the rest of Conductor doesn't care which physical shape it's calling.

### Package structure

```
src/conductor/
├── __init__.py              # __version__
├── cli.py                   # click entrypoints — `conductor call` is shipped at v0.1
├── providers/
│   ├── __init__.py          # registry: `get_provider(name)`
│   ├── interface.py         # Provider Protocol, CallResponse, error hierarchy
│   ├── kimi.py              # HTTP, MOONSHOT_API_KEY, OpenAI-compatible
│   └── (future: claude, codex, gemini, ollama)
└── (future: router.py, wizard.py, config.py)
tests/
├── test_cli.py              # CliRunner + respx — no live calls
└── test_kimi.py             # respx-mocked HTTP — no live calls
```

### v0.1 scope (in flight)

Shipped: Kimi adapter end-to-end, `conductor call --with kimi`, JSON output, helpful errors when key missing. Test suite covers happy/error paths via mocked httpx.

Deferred (see `~/Repos/autumn-garage/.cortex/plans/conductor-bootstrap.md` for the full phasing): claude/codex/gemini/ollama adapters, auto-mode router, list/smoke/doctor/init commands, streaming, tool use, cost aggregation.

## Key Files

| File | Purpose |
|------|---------|
| `src/conductor/cli.py` | CLI entrypoint (click). |
| `src/conductor/providers/interface.py` | `Provider` Protocol, `CallResponse`, error hierarchy — the contract every adapter satisfies. |
| `src/conductor/providers/kimi.py` | First adapter; HTTP via httpx; the v0.1 integration test case. |
| `src/conductor/providers/__init__.py` | `get_provider(name)` registry — single source of truth for canonical identifiers. |
| `tests/test_kimi.py` | Provider-level tests, all mocked httpx via `respx`. |
| `tests/test_cli.py` | CLI smoke tests via `CliRunner`. |

## State & Config

- **No project-level config in v0.1.** Config support (`~/.config/conductor/config.toml`) lands with the auto-mode router and `conductor init` wizard.
- **API credentials come from the environment.** Kimi reads `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` because Conductor calls Kimi K2.6 via Cloudflare Workers AI (Cloudflare added Day 0 Kimi hosting on 2026-04-20; the direct Moonshot backend is deferred as a future config option per autumn-garage journal `2026-04-21-kimi-via-cloudflare.md`). Future adapters read their own provider's standard env var — see `~/Repos/autumn-garage/integration/providers.md` for the canonical mapping.
- **No persistent state.** v0.1 is single-call: no caches, no logs, no usage aggregation. Consumers that want aggregation parse the `--json` output themselves.

## Hard-Won Lessons (v0.1 baseline — extend as we learn)

1. **Mock httpx with respx, not with `monkeypatch`-on-the-class.** The test suite uses `respx.mock(base_url=...)` so adapter code reaches the real `httpx.Client(...)` codepath; this catches signature drift between Conductor and httpx that monkey-patched mocks would silently swallow.
2. **Click's `CliRunner` attaches an empty stdin (isatty=False).** Tests that exercise "no input provided" hit the empty-task branch, not the no-stdin branch. Both are correct user errors; tests assert on the user-visible substring, not the internal branch.
3. **Provider quirks live in the adapter, never in shared code.** Kimi clamps temperature to `[0,1]`; Moonshot disallows `tool_choice="required"`. When those constraints become reachable (v0.2+ features), they belong in `kimi.py`, never in `interface.py` or the router. The Provider Protocol exists to keep the rest of Conductor ignorant of provider-specific gotchas.
