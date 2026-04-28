---
Status: superseded
Written: 2026-04-20
Author: human
Goal-hash: 3e9d51c8
Updated-by:
  - 2026-04-20T00:00 human (created; extracted from plans/llm-provider-additions follow-ups)
  - 2026-04-20T00:00 human (superseded by plans/conductor-bootstrap; Conductor establishes canonical identifiers `ollama` (HTTP) and `local-command` (subprocess) — Option A from this plan — and consuming tools adopt them via shell-out)
Cites: doctrine/0003-llm-providers-compose-by-contract, doctrine/0004-conductor-as-fourth-peer, plans/conductor-bootstrap, journal/2026-04-20-conductor-decision, integration/providers.md
---

> **SUPERSEDED 2026-04-20 by [`plans/conductor-bootstrap.md`](conductor-bootstrap.md).** This plan opened with three options (A: split into `ollama` + `local-command`; B: HTTP-only; C: subprocess-only) and asked the user to pick. The Conductor extraction picks **Option A** by construction — Conductor v0.1 ships `ollama` (httpx HTTP, default port `:11434`) as a first-class provider; the subprocess escape hatch can be added as `local-command` in a later Conductor release if a real consumer asks for it. The semantic gap between Touchstone's and Sentinel's current `local` implementations resolves when both tools migrate to `conductor call --with ollama`, at which point neither tool implements `local` directly. The architectural reasoning in this plan (Option A's tradeoffs, why splitting beats forced convergence) remains useful — read it for the rationale Conductor's choice rests on.

# Align the `local` LLM provider semantics across Touchstone and Sentinel

> The identifier `local` means different things in the two tools. Sentinel's `local` is **Ollama via HTTP** with a default model and a fixed endpoint. Touchstone's `local` is a **generic shell command escape hatch** — `command = "ollama run YOUR_MODEL"` or any user-supplied executable that takes a prompt on stdin. Doctrine 0003 says identifiers match across tools. The two semantics aren't reconcilable by tweaking config — one of them needs to be renamed, or both need to be split into two distinct rows in the providers table. **Investigate, decide, then ship in both repos.**

## Why (grounding)

Doctrine 0003 §1 says the same identifier is used in every tool's config. Today:

- **Sentinel `local`** (`src/sentinel/providers/local.py`): hardcoded to `cli_command = "ollama"`, talks HTTP to `http://localhost:11434/api/chat`, default model `qwen2.5-coder:14b`. Tightly coupled to Ollama's endpoint shape. The provider's purpose is "run local LLMs via the Ollama daemon."
- **Touchstone `local`** (per `hooks/codex-review.config.example.toml` lines 86–94): a generic `[review.local]` section with `command = "ollama run YOUR_MODEL"` and `auth_command = ""`. The hook pipes the review prompt to the configured `command` on stdin; whatever it returns is the review output. The purpose is "let users plug in any local LLM wrapper or custom script as a reviewer."

These aren't two implementations of the same thing. They're two different abstractions sharing a name:

- Sentinel's `local` = **opinionated Ollama HTTP client.**
- Touchstone's `local` = **generic subprocess escape hatch.**

A user who configures Sentinel for Ollama and then tries to configure Touchstone the same way encounters: different config keys (`provider` vs `command`), different mental models (HTTP endpoint vs shell command), different default behaviors (Ollama-specific defaults vs none). The drift is real and predates Doctrine 0003.

This is the more interesting of the two surfaced drifts — unlike the codex/openai rename (purely cosmetic), this one is a real semantic gap that needs design work before fixing.

Grounds-in: `.cortex/doctrine/0003-llm-providers-compose-by-contract`.

## Approach

**Phase 1 — Decide the right shape (this plan, before any code).**

Three plausible end states; pick one, document the reasoning, then implement:

**Option A — Two distinct identifiers: `ollama` (HTTP, opinionated) and `local-command` (subprocess, generic).**

- Both tools support both identifiers. `ollama` is the "I run Ollama locally" path with a fixed HTTP shape and a default model; `local-command` is the "I have my own wrapper script" escape hatch.
- Pros: each identifier has one meaning; users pick the one that matches their setup; the existing semantics are preserved, just renamed.
- Cons: doubles the surface area in both tools (two new providers each); existing user configs need migration.

**Option B — Converge on HTTP only: `local` means "OpenAI-compatible HTTP at a configurable URL."**

- Sentinel's `local` already does this for Ollama. Touchstone's `local` becomes "POST to `$LOCAL_LLM_BASE_URL/v1/chat/completions` with `$LOCAL_LLM_MODEL`."
- Pros: clean, single shape; works for Ollama, LM Studio, llama.cpp server, vLLM, any OpenAI-compatible local server; matches the Kimi pattern (HTTP via OpenAI-compatible endpoint).
- Cons: removes Touchstone's "any subprocess" flexibility — users who today plug in a custom non-LLM reviewer script (e.g., a static analyzer or rule-based checker) lose that capability under the `local` name. They'd need a different mechanism (e.g., a `script` reviewer type).

**Option C — Converge on subprocess only: `local` means "run this command, pipe prompt on stdin, read response from stdout."**

- Touchstone's `local` already does this. Sentinel adopts the same shape — replaces its Ollama-specific HTTP path with a configurable command.
- Pros: maximally flexible; works with any local tool (LLM or otherwise); matches Touchstone's existing surface.
- Cons: loses Sentinel's HTTP optimizations (streaming, structured responses); subprocess-per-call is slower than persistent HTTP for high-frequency use (Sentinel's Monitor role).

**Recommendation to vet with the user:** **Option A** (two identifiers). Reasoning:

- It preserves both existing capabilities. No user loses functionality.
- It cleanly matches Doctrine 0003: each identifier has one unambiguous meaning across tools.
- The "escape hatch" identifier (`local-command`) is naturally rare-use and can be a thin shim in both tools.
- The "opinionated HTTP" identifier (`ollama`) becomes the recommended default — and naturally extends to `lmstudio`, `llamacpp`, etc. as future row additions if needed (or stays generic enough to cover them all).

**Open question for the user:** does the recommendation hold, or is one of B/C preferred? B is the "force everyone to a clean shape" answer; C is the "preserve flexibility above all" answer. The recommendation favors A because Doctrine 0002 (interactive-by-default) plus the user-base of solo devs who mostly run Ollama suggests opinionated defaults are valuable, but escape hatches matter when they matter.

**Phase 2 — Implement (after the user picks a shape).**

Concrete work depends on the choice. Phase 2 work items are stubbed below for Option A; rewrite if a different option is chosen.

## Success Criteria

This plan is done when all of the following hold:

1. The user has explicitly chosen Option A, B, or C (recorded as a journal entry citing this plan).
2. `integration/providers.md` reflects the chosen shape — either updates the `local` row or replaces it with two rows (e.g., `ollama` + `local-command`).
3. Both Touchstone and Sentinel ship PRs that implement the chosen shape. Each PR cites Doctrine 0003 and this plan.
4. Existing user configs continue to work, either unchanged (if Option A keeps `local` as one of the new identifiers) or via a one-version backward-compat alias with a deprecation warning (matches the codex/openai rename pattern in `plans/sentinel-codex-identifier-rename.md`).
5. `sentinel init` and Touchstone's first-run wizard offer the new identifier(s) consistently — both tools call them the same thing and prompt for the same env vars / config keys.
6. A journal entry in autumn-garage records the alignment shipping, citing both per-tool PRs and verifying Doctrine 0003 §1 is satisfied for the local-LLM space.
7. Dogfood: configure the new shape in autumn-mail's Sentinel config and Touchstone reviewer cascade. One real cycle / push that exercises the local provider from both tools succeeds without confusion.

## Work items (Option A — provisional, rewrite if shape changes)

### Phase 1 — Decision

- [ ] User picks A / B / C. Record decision in a journal entry.
- [ ] Update this plan's Approach section to remove the alternatives and lock in the chosen shape.
- [ ] Update `integration/providers.md` with the chosen identifier(s) and their fields (env var, endpoint shape, base URL, default model, smoke test).

### Phase 2 — Sentinel repo (assumes Option A)

- [ ] Rename `src/sentinel/providers/local.py` → `src/sentinel/providers/ollama.py`. Keep the HTTP shape; rename class `LocalProvider` → `OllamaProvider`; rename `ProviderName.LOCAL` → `ProviderName.OLLAMA`; keep `LOCAL` as a deprecated alias.
- [ ] Add `src/sentinel/providers/local_command.py`: subprocess-based provider that runs a configured shell command, pipes the prompt to stdin, reads response from stdout. Mirror Touchstone's existing shape.
- [ ] Add `ProviderName.LOCAL_COMMAND = "local-command"`.
- [ ] Update `sentinel init` wizard to offer `ollama` (recommended) and `local-command` (escape hatch).
- [ ] Tests for both providers; backward-compat tests for `provider = "local"` mapping to `ollama` with a deprecation warning.

### Phase 2 — Touchstone repo (assumes Option A)

- [ ] Add an `ollama` reviewer in `hooks/codex-review.sh`: HTTP POST to `$LOCAL_LLM_BASE_URL/v1/chat/completions` (default `http://localhost:11434/v1/chat/completions`) using the model from `integration/providers.md`.
- [ ] Rename the existing `local` reviewer cascade entry to `local-command` (since it's the subprocess one). Keep `local` as a deprecated alias that resolves to `local-command` with a stderr warning.
- [ ] Update `.codex-review.toml` schema and example config to document both `ollama` and `local-command` in the `[review]` section.
- [ ] Update Touchstone's setup wizard / first-run prompts to offer both.
- [ ] Self-tests for both reviewer paths.

### Phase 2 — Coordination repo (autumn-garage)

- [ ] Update `integration/providers.md`: replace the `local` row with `ollama` and `local-command` rows. Bump "Last update" date.
- [ ] Update `plans/llm-provider-additions.md` Follow-ups: mark the local-alignment item as resolved by this plan.
- [ ] Journal the alignment shipping.
- [ ] Update `state.md` once both per-tool PRs are open.

## Follow-ups (deferred)

- **Other local-LLM ecosystems.** Once `ollama` is established as an HTTP-via-OpenAI-compatible identifier, consider whether LM Studio, vLLM, llama.cpp server need their own identifiers or can share `ollama` (the API surface is OpenAI-compatible; only the default port differs). Resolves to a future row addition discussion in `integration/providers.md`.
- **Streaming support for local providers.** Sentinel's HTTP path could stream responses (Ollama supports it); Touchstone's hook path doesn't need streaming today. If streaming becomes desirable for Touchstone (long reviews), revisit. Not in scope here.
- **Authentication for local providers.** Today both tools assume local LLMs need no auth. If that changes (e.g., a self-hosted vLLM behind a token), the providers table gains an env var field for `local-command` / `ollama`. Resolves to a row update when needed.

## Known limitations at exit (assumes Option A)

- **Two-tool migration.** Both Touchstone and Sentinel ship breaking-with-deprecation-alias renames at roughly the same time. Coordination matters: ship Sentinel and Touchstone PRs in the same week so users don't see one tool with the new names and the other with the old. Consider a shared dogfood cycle on autumn-mail to verify before either tool tags a release.
- **The escape-hatch path stays niche.** `local-command` is rare-use by design. Documentation effort should not overweight it.
- **Doctrine 0003 §1 doesn't strictly require renaming `local`** — it requires identifiers match across tools. If the user picks Option B or C, the rename pattern changes. Phase 2 work items are written for Option A; rewrite if needed.

## Meta — why this plan is in autumn-garage

Same reasoning as the sibling plans (`llm-provider-additions.md`, `sentinel-codex-identifier-rename.md`). The work spans two tools; coordination lives here; implementation lands in the tool repos. The first phase (deciding the shape) is purely coordination — no per-tool work happens until that's locked.
