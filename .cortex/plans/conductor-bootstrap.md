---
Status: active
Written: 2026-04-20
Author: human
Goal-hash: c0n8d1c70
Updated-by:
  - 2026-04-20T00:00 human (created; v0.1 bootstrap plan for Conductor — the fourth garage peer)
Cites: doctrine/0004-conductor-as-fourth-peer, doctrine/0003-llm-providers-compose-by-contract, doctrine/0002-interactive-by-default, doctrine/0001-why-autumn-garage-exists, journal/2026-04-20-litellm-evaluated-rejected, journal/2026-04-20-conductor-decision, plans/llm-provider-additions, plans/sentinel-codex-identifier-rename, plans/local-llm-provider-alignment, integration/providers.md, https://platform.kimi.ai/docs/api/quickstart.md
---

# Conductor v0.1 — bootstrap the fourth garage peer with Kimi as first integration

> Build Conductor: a small Python CLI that owns LLM provider adapters and exposes "pick an LLM, give it a job" with manual + auto modes. Ship v0.1 with five providers (claude, codex, gemini, ollama, kimi). Kimi is the v0.1 integration test case — the first provider that requires Conductor to handle an API key directly. Once v0.1 lands, Sentinel and Touchstone migrate from per-tool adapters to `conductor call` shell-outs in subsequent plans.

## Why (grounding)

Per Doctrine 0004 (today), the garage gains a fourth peer to consolidate provider adapters and the manual/auto routing surface. Three open plans (`llm-provider-additions`, `sentinel-codex-identifier-rename`, `local-llm-provider-alignment`) collapse into Conductor's v0.1 scope.

Kimi is chosen as the v0.1 integration test case (instead of starting with the existing CLI-shellout providers) because it forces the hard cases up front:

1. It's the first API-key-touching adapter — proves the auth pattern works cleanly.
2. It's OpenAI-compatible HTTP — proves the httpx-based adapter shape (vs the shell-out adapter shape) works.
3. The earlier research surfaced specific Moonshot quirks (temperature clamp, `tool_choice="required"` removal, streaming `usage`, reasoning_content preservation) that LiteLLM gets wrong. Implementing them ourselves means handling them correctly from day one.
4. It's net-new functionality — no migration risk for existing Sentinel/Touchstone users; ship Conductor without breaking either.

Build order within v0.1: scaffold + auto router + Kimi (httpx) FIRST, then claude/codex/gemini (shell-out) and ollama (httpx) AFTER the Kimi smoke test passes. This proves the design on the harder shape before backfilling the easier ones.

Grounds-in: `.cortex/doctrine/0004-conductor-as-fourth-peer`.

## Approach

**Repo and structure.** New repo `autumngarage/conductor`. Bootstrap via Touchstone (`touchstone new conductor --type python`). Standard layout:

```
conductor/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── AGENTS.md
├── .cortex/                      # per-Doctrine-0001 single-tool decisions live here
├── src/conductor/
│   ├── __init__.py
│   ├── cli.py                    # argparse / typer; commands: call, list, smoke, init, doctor
│   ├── config.py                 # ~/.config/conductor/config.toml schema
│   ├── router.py                 # auto-mode rule-based router
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── interface.py          # Provider base class — call(task, model?) -> Response
│   │   ├── kimi.py               # httpx, MOONSHOT_API_KEY, OpenAI-compatible
│   │   ├── claude.py             # subprocess: claude -p
│   │   ├── codex.py              # subprocess: codex exec
│   │   ├── gemini.py             # subprocess: gemini -p
│   │   └── ollama.py             # httpx, http://localhost:11434/api/chat
│   └── wizard.py                 # init wizard (Doctrine 0002)
├── tests/
│   ├── test_cli.py
│   ├── test_router.py
│   ├── test_providers/
│   │   ├── test_kimi.py          # mocked httpx
│   │   ├── test_claude.py        # mocked subprocess
│   │   └── ...
│   └── test_smoke.py             # integration tests gated on env vars
└── scripts/
```

**CLI shape (v0.1):**

```bash
# Manual
conductor call --with kimi --task "summarize this README" < README.md
conductor call --with codex --task "review this diff" --json

# Auto
conductor call --auto --task "summarize this README" --tags long-context,cheap < README.md
conductor call --auto --task "review this diff" --tags code-review

# Discovery
conductor list                    # tabular: provider | configured? | smoke-passed? | default-model
conductor list --json
conductor smoke kimi              # runs the smoke test from integration/providers.md
conductor doctor                  # what's installed, what env vars set, what missing

# Setup
conductor init                    # interactive wizard, Doctrine 0002 compliance
```

**Auto-mode router (v0.1, rule-based):**

Each provider declares capability tags in its module: `kimi.tags = ["long-context", "cheap", "tool-use", "vision"]`. Each call carries task tags via `--tags a,b,c`. The router scores providers as `len(set(provider.tags) & set(task.tags))` and picks the highest scorer that's `configured == True` and `smoke-passed == True`. Ties broken by a configured priority list in `~/.config/conductor/config.toml`. Document the rule clearly in the docstring; don't pretend it's smart.

**Provider interface:**

```python
# src/conductor/providers/interface.py
from dataclasses import dataclass
from typing import Optional, Protocol

@dataclass
class CallResponse:
    text: str
    provider: str
    model: str
    usage: dict           # {"input_tokens": N, "output_tokens": N, "cached_tokens": N}
    duration_ms: int
    cost_usd: Optional[float]   # None if pricing unknown
    raw: dict             # full upstream response for debugging

class Provider(Protocol):
    name: str             # canonical identifier (claude, codex, kimi, ...)
    tags: list[str]       # capability tags for auto-mode
    default_model: str

    def configured(self) -> tuple[bool, Optional[str]]:
        """Return (is_configured, reason_if_not). Checks env vars / CLI presence."""
    def smoke(self) -> tuple[bool, Optional[str]]:
        """Run the provider's smoke test. Cheap call to /v1/models or equivalent."""
    def call(self, task: str, model: Optional[str] = None) -> CallResponse:
        """Make the actual call. Returns response or raises."""
```

**Kimi adapter (the v0.1 integration test):**

- httpx-based, OpenAI-compatible. Endpoint: `https://api.moonshot.ai/v1/chat/completions`.
- Reads `MOONSHOT_API_KEY` from env. No fallback to `OPENAI_API_KEY` (the research flagged that as a footgun).
- Default model `kimi-k2.6`. Allow override via `--model`.
- Handles the documented Moonshot quirks: temperature clamped to [0,1], no `tool_choice="required"`, streaming `usage` requires `stream_options={"include_usage": true}` (deferred — v0.1 is non-streaming).
- For the multi-turn tool-call path: not in v0.1 scope (single-turn `chat.completions` only). Document that future tool-use support must echo `reasoning_content` back per LiteLLM #21672.
- Smoke test: `GET https://api.moonshot.ai/v1/models` with auth header. Verify 200 + JSON.
- Capability tags: `["long-context", "cheap", "tool-use", "vision"]`.

**Setup UX (Doctrine 0002 compliance):**

`conductor init` prompts:
1. Which providers to configure now? (multi-select from claude, codex, gemini, ollama, kimi)
2. For each selected: prompt for env var if not present; offer to write to `~/.zshrc` or print export line; run smoke test; print result.
3. Auto-mode default tags? (skip in v0.1 — defer to user editing `~/.config/conductor/config.toml`)
4. Print equivalent flag-form at end.
5. Save `~/.config/conductor/config.toml` with the chosen defaults.

**Distribution:**

- Brew tap `autumngarage/homebrew-conductor`. Formula points at GitHub Releases.
- Release flow modeled on Sentinel's (since both are Python CLIs).
- v0.1.0 tagged when all Success Criteria below pass.

## Success Criteria

This plan is done when all of the following hold:

1. **Repo exists.** `autumngarage/conductor` exists on GitHub. Bootstrapped via `touchstone new conductor --type python`. Brew tap exists.
2. **CLI installs and runs.** `brew install autumngarage/conductor/conductor` works on macOS. `conductor --version` reports `v0.1.0`. `conductor --help` shows all commands documented.
3. **Kimi adapter works end-to-end.** With `MOONSHOT_API_KEY` set: `conductor call --with kimi --task "What is 2+2?"` returns a response from `kimi-k2.6` on stdout. `conductor smoke kimi` reports OK. `conductor list` shows kimi as configured.
4. **All five providers implemented.** claude, codex, gemini, ollama, kimi each have an adapter. Each has a smoke test. Each handles the "not configured" case gracefully (informative error, no traceback).
5. **Auto-mode picks correctly.** `conductor call --auto --task "..." --tags long-context,cheap` picks kimi (or ollama if kimi not configured). `--tags code-review` picks codex (or claude). The router's logic is documented and tested.
6. **`conductor init` wizard works.** Fresh user with no config can run `conductor init`, configure at least one provider, and successfully run `conductor call` against it. Doctrine 0002 compliance: TTY-detected, `--yes` flag, prints flag-form at end, reversible on Ctrl-C.
7. **`conductor doctor` is useful.** Run on a partially-configured system, output lists every provider with: installed (yes/no), env var set (yes/no/with-name), smoke test (passed/failed/not-run), default model. Includes installation hints for missing pieces.
8. **JSON output is stable.** `conductor call --json` returns the `CallResponse` shape documented in this plan. `conductor list --json` returns a list of provider records. Schema documented in README.
9. **Tests pass.** `pytest` green. Includes: mocked httpx tests for kimi/ollama; mocked subprocess tests for claude/codex/gemini; router unit tests; CLI integration tests (no network); a smoke-test suite gated on env vars (only runs when `RUN_LIVE_SMOKE=1` and the relevant `*_API_KEY` is set).
10. **Documentation.** README explains: what Conductor is (one paragraph), the manual/auto mode distinction, every command with one example, how to add a provider (for future contributors), and how Sentinel/Touchstone consume it (forward references — the integration plans land separately).
11. **autumn-garage updated.** `integration/providers.md` notes that Conductor is now the canonical source for provider identifiers + capability tags. State.md reflects Conductor's existence.
12. **Three sibling plans annotated.** `plans/llm-provider-additions.md`, `plans/sentinel-codex-identifier-rename.md`, and `plans/local-llm-provider-alignment.md` each get a status note: "Superseded by Conductor v0.1 — see plans/conductor-bootstrap.md."

## Work items

### Phase 1 — Repo + scaffolding

- [ ] `touchstone new conductor --type python` — scaffold the repo locally.
- [ ] Create the GitHub repo `autumngarage/conductor` (private or public — decide with user).
- [ ] Initial push: scaffolding + `pyproject.toml` + `README.md` placeholder + `CLAUDE.md` (matching the trio's CLAUDE.md style) + `AGENTS.md` + `.cortex/` initialized via `cortex init`.
- [ ] Set up brew tap `autumngarage/homebrew-conductor` (model on `homebrew-sentinel`).
- [ ] CI: GitHub Actions for pytest on macOS + Linux.

### Phase 2 — Kimi adapter (the integration test)

- [ ] `src/conductor/providers/interface.py` — Provider protocol, CallResponse dataclass.
- [ ] `src/conductor/providers/kimi.py` — httpx implementation, MOONSHOT_API_KEY auth, default model `kimi-k2.6`, smoke test, capability tags. Handles temperature clamping; no streaming in v0.1.
- [ ] `src/conductor/cli.py` — `conductor call --with kimi --task "..."` end-to-end. argparse or typer.
- [ ] `tests/test_providers/test_kimi.py` — mocked httpx, covers: success case, missing env var, smoke test, model override.
- [ ] Manual end-to-end: with real `MOONSHOT_API_KEY`, run `conductor call --with kimi --task "ping"` and observe a real response. Journal the result.

### Phase 3 — Remaining adapters

- [ ] `claude.py` — shell out to `claude -p`. Mirror Sentinel's existing `claude.py` pattern.
- [ ] `codex.py` — shell out to `codex exec`. (Note: NOT `openai.py` — Conductor uses canonical identifier `codex`. Doctrine 0003.)
- [ ] `gemini.py` — shell out to `gemini -p`.
- [ ] `ollama.py` — httpx against `http://localhost:11434/api/chat`. Allow base URL override via `OLLAMA_BASE_URL` env var.
- [ ] Tests for each (mocked subprocess / httpx).
- [ ] `conductor list` and `conductor doctor` show all five.

### Phase 4 — Auto-mode router

- [ ] `src/conductor/router.py` — rule-based router as described in Approach.
- [ ] Each provider module declares its `tags` list.
- [ ] `conductor call --auto --tags a,b,c --task "..."` works end-to-end.
- [ ] `tests/test_router.py` — covers: tag matching, configured filter, smoke-passed filter, tie-break, no-match error.
- [ ] Document the rule in `conductor --help` output AND the README.

### Phase 5 — Setup wizard + smoke commands

- [ ] `src/conductor/wizard.py` — interactive `conductor init` per Doctrine 0002.
- [ ] `conductor smoke <id>` and `conductor smoke --all`.
- [ ] `conductor doctor` rich output.
- [ ] `~/.config/conductor/config.toml` schema + parser.
- [ ] `tests/test_init_scenarios.py` — covers TTY/non-TTY, `--yes`, partial config, Ctrl-C cleanup.

### Phase 6 — Distribution + docs

- [ ] README: usage, examples, provider matrix, integration notes for Sentinel/Touchstone consumers.
- [ ] CHANGELOG entry for v0.1.0.
- [ ] Tag and release v0.1.0. Update brew formula. Verify `brew install` works.
- [ ] Smoke-test the brew install on a clean macOS user.

### Phase 7 — autumn-garage updates

- [ ] Update `autumn-garage/README.md` install block to include Conductor.
- [ ] Update `autumn-garage/.cortex/state.md` to reflect Conductor v0.1 shipping.
- [ ] Update `integration/providers.md` to note Conductor is now the source of truth for identifiers; bump "Last update" date.
- [ ] Annotate the three sibling plans (`llm-provider-additions`, `sentinel-codex-identifier-rename`, `local-llm-provider-alignment`) with "Superseded by Conductor v0.1" status notes.
- [ ] Journal entry: Conductor v0.1 shipped, with link to the GitHub release and notes on what was easier/harder than expected.

## Follow-ups (deferred)

- **Sentinel migration plan.** Once Conductor v0.1 is live, open `plans/sentinel-conductor-migration.md` — replace `src/sentinel/providers/{claude,openai,gemini,local}.py` with a thin shell-out to `conductor call`. Default to `--auto`; user overrides per-role go to `--with <id>`. This is where the codex/openai rename and the local/ollama alignment actually land — when Sentinel stops implementing those adapters at all.
- **Touchstone migration plan.** Once Sentinel migration is proven, open `plans/touchstone-conductor-migration.md` — extend the reviewer cascade to support `auto` as a valid entry that resolves via `conductor call --auto`. Existing `claude`/`codex`/`gemini` entries can also migrate to going through Conductor for consistency.
- **Cortex Phase C synthesis** picks up Conductor as the synthesis backend choice instead of shelling directly to `claude -p`. Resolves to a future `plans/cortex-phase-c-conductor-wiring.md`.
- **Streaming support in Conductor.** Deferred from v0.1. Useful for long reviews, long synthesis. Resolves to a future `plans/conductor-streaming.md` if there's pull from the consumers.
- **LLM-based meta-routing for `--auto`.** Replace the rule-based router with a small LLM call that picks the provider given the task description. Higher quality, more expensive per call. Only worth it if rule-based proves insufficient. Resolves to a future plan.
- **Cost tracking and budgets.** Conductor surfaces `usage` and (when known) `cost_usd` per call. Aggregating into a budget enforcement layer is its own concern; defer until a consumer asks for it.
- **Tool-use (function-calling) support.** v0.1 is single-turn chat completions only. Multi-turn tool calling adds complexity (especially for Kimi reasoning models — reasoning_content preservation per LiteLLM #21672). Resolves to a future plan when a consumer needs it.

## Known limitations at exit (v0.1)

- **Single-turn only.** No tool-use, no streaming, no multi-call agents. Conductor v0.1 is "send a prompt, get a response."
- **Auto-mode is naive.** Tag matching with simple set intersection. Will pick wrong sometimes. The remedy is to update tags or override with `--with`, not to make the router smarter in v0.1.
- **No retries.** A failed call returns the error to the caller. Conductor doesn't try a fallback provider. Sentinel's role-router does this today; if Conductor needs it, it lands in v0.2.
- **One config file location.** `~/.config/conductor/config.toml`. No per-project config in v0.1. Add later if needed.
- **No telemetry, no usage reporting, no cost dashboards.** Output JSON includes `usage` per call; aggregation is the consumer's problem.
- **Brew install only on macOS.** Linux supported via pip. Windows is not supported.

## Meta — why this plan is in autumn-garage

Conductor itself will have its own `.cortex/` for single-tool decisions once it's bootstrapped. This plan lives in autumn-garage because:

1. The decision to *create* Conductor is cross-tool (it changes the Sentinel + Touchstone integration story). That's coordination, not single-tool work.
2. Kimi-as-first-integration-test affects the autumn-garage Kimi rollout plan, which lives here.
3. The three sibling plans being superseded all live here.

Once Conductor v0.1 is shipped and its own `.cortex/` is healthy, future Conductor-internal decisions move there. Future Sentinel-and-Touchstone-and-Conductor coordination decisions stay here.
