---
Status: superseded
Written: 2026-04-20
Author: human
Goal-hash: a4c1e8b2
Updated-by:
  - 2026-04-20T00:00 human (created; first application of Doctrine 0003 — Kimi rollout)
  - 2026-04-20T00:00 human (superseded by plans/conductor-bootstrap; Kimi becomes Conductor v0.1's integration test rather than two parallel per-tool PRs)
Cites: doctrine/0003-llm-providers-compose-by-contract, doctrine/0004-conductor-as-fourth-peer, doctrine/0002-interactive-by-default, plans/conductor-bootstrap, journal/2026-04-20-conductor-decision, https://platform.kimi.ai/docs/api/quickstart.md, https://platform.kimi.ai/docs/models.md
---

> **SUPERSEDED 2026-04-20 by [`plans/conductor-bootstrap.md`](conductor-bootstrap.md).** Adding Kimi via two parallel per-tool PRs (Touchstone hook + Sentinel `moonshot.py`) was the original plan. Same-day refinement extracted Conductor as a fourth garage peer that owns provider adapters; Kimi is now Conductor v0.1's first integration test instead. The Doctrine 0003 contract still applies (`integration/providers.md` remains the canonical reference table). Per-tool migration to Conductor — replacing today's adapters with `conductor call` shell-outs — happens in separate Sentinel/Touchstone migration plans after Conductor v0.1 ships. Reasoning: see `journal/2026-04-20-conductor-decision.md`. The Kimi research, model-selection notes, and Moonshot-specific gotchas in this plan remain useful as input to Conductor's Kimi adapter — read it for context, not for action.

# Adding LLM providers across the trio — Kimi first

> Apply Doctrine 0003 (LLM providers compose by shared contract, not shared code) to add Kimi (Moonshot AI) as a callable provider in Touchstone (reviewer cascade) and Sentinel (role router). Establish the providers reference table at `integration/providers.md` so this rollout — and every future provider addition — has a single shared source of truth. Cortex Phase C inherits the menu when synthesis ships.

## Why (grounding)

Two tools want the same provider for their own reasons:

- **Touchstone** wants reviewer choice. Today the cascade is hardcoded to `codex` (with `claude` and `gemini` configurable). Kimi is cheap (~$0.60/M in, $2.50/M out — 5–6× cheaper than Sonnet) and supports tool calling at 256k context. A reasonable choice for cost-sensitive review.
- **Sentinel** wants role-aware routing. Monitor (high-frequency) and Researcher (long-context bulk) roles benefit from Kimi's pricing and context. Reviewer or Coder may stay on Claude/Codex.

Without coordination, the two tools would diverge: different env var names, different default model IDs, different setup prompts, different smoke tests. Doctrine 0003 names the rule (per-tool implementation against a shared contract); this plan is the first application of that rule.

Kimi's specifics (verified against `platform.kimi.ai/docs/api/quickstart.md`):

- Base URL: `https://api.moonshot.ai/v1`
- Auth: `Authorization: Bearer $MOONSHOT_API_KEY`
- OpenAI SDK drop-in: `OpenAI(api_key=..., base_url="https://api.moonshot.ai/v1")`
- Models worth defaulting to: `kimi-k2.6` (256k context, multimodal, tool calling). Alternatives: `kimi-k2-thinking` (reasoning, 300-step tool calling), `kimi-k2-turbo-preview` (faster, more expensive).
- Official `kimi` CLI exists (`uv tool install --python 3.13 kimi-cli`) but is interactive-only — no `-p` / stdin / JSON output. **Not suitable for hook integration.** Both tools use the HTTP endpoint instead.

Grounds-in: `.cortex/doctrine/0003-llm-providers-compose-by-contract`.

## Approach

**Step 1 — Reference table first.** Create `autumn-garage/integration/providers.md` (a new top-level integration directory in the coordination repo). Seed it with the current providers (claude, codex, openai, gemini, local) plus the new kimi entry. This document is the single source of truth that per-tool PRs reference.

The table shape, one row per provider:

| identifier | env var | endpoint shape | base URL | default model | smoke test |
|---|---|---|---|---|---|
| `kimi` | `MOONSHOT_API_KEY` | OpenAI-compatible HTTP | `https://api.moonshot.ai/v1` | `kimi-k2.6` | `curl -sf -H "Authorization: Bearer $MOONSHOT_API_KEY" https://api.moonshot.ai/v1/models` |
| `claude` | `ANTHROPIC_API_KEY` | shell out to `claude -p` | — (CLI) | (per Claude defaults) | `claude -p "ping"` |
| `codex` | `OPENAI_API_KEY` | shell out to `codex exec` | — (CLI) | (per Codex defaults) | `codex exec "ping"` |
| `gemini` | `GEMINI_API_KEY` | shell out to `gemini -p` | — (CLI) | (per Gemini defaults) | `gemini -p "ping"` |
| `local` | `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL` | OpenAI-compatible HTTP (Ollama, LM Studio) | `$LOCAL_LLM_BASE_URL` | `$LOCAL_LLM_MODEL` | `curl -sf $LOCAL_LLM_BASE_URL/models` |

The table is the contract instance. Per-tool implementations consult it; nothing else.

**Step 2 — Touchstone PR.** Add `kimi` as a peer in the reviewer cascade (`hooks/codex-review.sh` and `.codex-review.toml` schema). Implementation calls the Moonshot HTTP endpoint via `curl` (matching how the OpenAI/openai-compatible path would work). Setup wizard inherits Doctrine 0002 — prompts for `MOONSHOT_API_KEY`, runs the smoke test, prints the equivalent flag form.

**Step 3 — Sentinel PR.** Add `src/sentinel/providers/moonshot.py`. Likely shape: thin subclass of `openai.py` overriding `base_url` and the `MOONSHOT_API_KEY` env var read. Add `ProviderName.MOONSHOT = "moonshot"` to `config/schema.py`. Wire into the router. Update `sentinel init` wizard to offer Kimi as a provider choice for any role.

**Step 4 — Dogfood in autumn-mail.** After both PRs ship, configure autumn-mail's `.sentinel/config.toml` to use Kimi for the Coder or Researcher role and run one `sentinel work --budget $5` cycle. Journal the result (cost, quality, any contract gaps surfaced).

**Step 5 — Cortex Phase C absorption (deferred).** When Cortex ships `refresh-map` / `refresh-state`, the synthesis shell-out picks the provider from the same table. No new contract negotiation needed; this is the test of whether Doctrine 0003 actually scales to a third tool.

## Success Criteria

This plan is done when all of the following hold:

1. `autumn-garage/integration/providers.md` exists with rows for claude, codex, openai, gemini, local, and kimi. Each row has all six fields populated.
2. Touchstone PR shipped: `.codex-review.toml` accepts `"kimi"` in the reviewer list; the hook script invokes Moonshot's HTTP endpoint with `MOONSHOT_API_KEY`; review feedback round-trips end-to-end on a real diff.
3. Touchstone setup wizard prompts for `MOONSHOT_API_KEY` when `kimi` is selected, runs the smoke test from the providers table verbatim, and prints the equivalent flag form.
4. Sentinel PR shipped: `src/sentinel/providers/moonshot.py` exists; `ProviderName.MOONSHOT` registered; router resolves `provider = "moonshot"` to the new adapter; tests pass.
5. Sentinel `init` wizard offers `moonshot` as a provider choice for any role with the same prompt shape used for other providers.
6. One `sentinel work` cycle on autumn-mail with at least one role set to `moonshot` completes successfully and produces a `.cortex/journal/` entry per Protocol T1.6. Cost recorded in the cycle output.
7. A journal entry in autumn-garage records the dogfood result (`2026-MM-DD-kimi-dogfood-cycle.md`), citing this plan, with verdict on whether Doctrine 0003's contract held up under real use.
8. Both per-tool PRs reference Doctrine 0003 + this plan in their PR descriptions, and `integration/providers.md` is the single source of truth they pull from for env var names, default model, and smoke test (no duplicated values).

## Work items

### Coordination repo (autumn-garage)

- [x] Create `autumn-garage/integration/providers.md` with the table above. (Shipped 2026-04-20 alongside this plan and Doctrine 0003.)
- [ ] Update `state.md` to reflect the new workstream once first per-tool PR opens.
- [ ] Open journal entry on dogfood completion (success criterion 7).
- [ ] Open separate plan(s) for the two pre-existing drifts surfaced while writing the providers table (see Follow-ups). They block clean conformance with Doctrine 0003 but not the Kimi rollout itself.

### Touchstone

- [ ] `hooks/codex-review.sh`: add `kimi` branch in the reviewer-dispatch case statement; calls Moonshot HTTP via `curl` with the model from `integration/providers.md` as default.
- [ ] `.codex-review.toml` schema: accept `"kimi"` in the `reviewers` array; document under example config.
- [ ] Setup wizard (Doctrine 0002 compliance): prompt for `MOONSHOT_API_KEY`, run smoke test, print flag form.
- [ ] README update: provider matrix gains a Kimi row.
- [ ] PR description cites Doctrine 0003 + this plan + the providers reference table.

### Sentinel

- [ ] `src/sentinel/providers/moonshot.py`: new module, likely subclassing the openai-compatible base; reads `MOONSHOT_API_KEY`; default model `kimi-k2.6`.
- [ ] `src/sentinel/config/schema.py`: add `MOONSHOT = "moonshot"` to `ProviderName` enum.
- [ ] Router registration in `providers/router.py` (or wherever providers are registered today).
- [ ] `sentinel init` wizard: include moonshot in provider choice list; prompt for env var; run smoke test (same one as Touchstone — verbatim from the providers table).
- [ ] Tests: provider instantiation, basic chat call against a mocked endpoint, env var error handling.
- [ ] README + provider docs update.
- [ ] PR description cites Doctrine 0003 + this plan + the providers reference table.

### Dogfood (autumn-mail)

- [ ] After both PRs ship, configure `.sentinel/config.toml` with at least one role set to `provider = "moonshot"`.
- [ ] Run `sentinel work --budget $5` and observe a full cycle.
- [ ] Capture: cost, completion vs failure, any contract gaps.
- [ ] Journal the result back in autumn-garage.

## Follow-ups (deferred)

- **Reconcile codex/openai identifier drift in Sentinel.** Sentinel currently labels the Codex provider `openai` (`src/sentinel/providers/openai.py`, class `OpenAIProvider`, `ProviderName.OPENAI`). Touchstone uses `codex` for the same thing. Doctrine 0003 §1 says identifiers match across tools. Fix: either rename Sentinel's identifier `openai` → `codex` (preferred — Touchstone shipped first and the identifier is more accurate), OR document the alias explicitly in `integration/providers.md` and accept it. If renaming, ship as a Sentinel-only PR with a backward-compat alias in config parsing for one minor version. Pre-existing drift, not blocking Kimi rollout — surface it now so the doctrine doesn't ship with a known unresolved violation.
- **Align `local` invocation shape across tools.** Sentinel's `local.py` calls Ollama via HTTP (`httpx` against `:11434/api/chat`) with default model `qwen2.5-coder:14b`. Touchstone's reviewer cascade `local` entry expects a user-configured shell command (e.g., `ollama run YOUR_MODEL`) per `.codex-review.toml`. Both work today but a user moving between tools sees different config knobs for "the same" provider. Fix: pick one shape (HTTP is preferred — already works for any OpenAI-compatible local server), update the lagging tool, document in `integration/providers.md`. Pre-existing drift; resolve as a sibling workstream.
- **Cortex Phase C provider integration.** When `refresh-map` / `refresh-state` ship, they pull from `integration/providers.md` for the synthesis backend choice. Verifies Doctrine 0003 scales to a third consumer without renegotiation. Resolves to a future `plans/cortex-phase-c-provider-wiring.md`.
- **Additional providers behind the same pattern.** xAI Grok, DeepSeek, Mistral, Ollama-as-first-class-local — each is a row addition to `integration/providers.md` plus per-tool PRs. No new doctrine needed unless a provider breaks one of the contract's assumptions.
- **Cost-aware default routing in Sentinel.** Once Kimi is in the menu, Sentinel could default Monitor/Researcher to Kimi based on cost-per-token if the user opts in. Resolves to a future Sentinel-only plan.

## Known limitations at exit

- **Reference table is hand-maintained.** Adding a provider means a manual edit to `integration/providers.md` before per-tool PRs can land cleanly. Acceptable because providers are added rarely. If this becomes a chokepoint, a small `garage providers list` CLI could read the table and lint per-tool configs against it.
- **No retroactive enforcement.** Existing providers (claude, codex, gemini, local) were added before Doctrine 0003. They satisfy the contract de facto but weren't subject to it during their PR. If audit reveals divergence (e.g., one tool reads `OPENAI_API_KEY` from a different env var alias the other doesn't), it's a bug to fix, not a doctrine violation.
- **Kimi's official CLI is interactive-only.** Both tools use the HTTP endpoint instead. If Moonshot ships a non-interactive CLI later (with `-p` / stdin / JSON), tools may migrate, but that's a contract revision, not a per-tool decision.

## Meta — why this plan is in autumn-garage, not in touchstone or sentinel

The same reasoning as `plans/sentinel-cortex-t16-integration.md`: a workstream that spans two or more tools is a coordination question. The implementation lands in the tool repos; the scope, contract, and success criteria live here. When an agent is dispatched to either tool's PR, that agent's brief points at this plan + Doctrine 0003 + `integration/providers.md`.
