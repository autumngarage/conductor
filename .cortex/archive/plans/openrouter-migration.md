---
Status: draft
Written: 2026-04-27
Author: claude-opus-4-7
Goal-hash: tbd
Updated-by:
  - 2026-04-27T10:00 claude-opus-4-7 (created; design after research into Cloudflare CLI options, HF, and OpenRouter :auto best practices)
Cites: principles/engineering-principles.md, .cortex/state.md
---

# OpenRouter Migration — Three-Tier Provider Architecture

> **Consolidate conductor's provider catalog into four user-facing tiers: three CLI-agentic providers (claude, codex, gemini), one hosted aggregator (OpenRouter as the catch-all for every other online model), and local (ollama, custom shells). Drop the direct kimi and deepseek HTTP adapters. The `OPENROUTER_API_KEY` becomes the single credential for everything text-only HTTP, replacing `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` (kimi) and `DEEPSEEK_API_KEY` (deepseek). Auto-mode selects models from OpenRouter's live catalog using capability filters, never hard-coded slugs.**

## Why

The user maintains separate billing relationships and credentials for kimi (Cloudflare) and deepseek (direct API), with provider-specific HTTP plumbing in two adapter files. The user-facing CLI surface today shows seven named providers (kimi, claude, codex, deepseek-chat, deepseek-reasoner, gemini, ollama); auto-mode scoring across all of them is fine but the *choice* the user has to make is more granular than necessary.

OpenRouter (with NotDiamond's `:auto` router and a public catalog API) gives access to every online model worth caring about — including frontier proprietary (Claude, GPT, Gemini), the latest Kimi K2.6, real DeepSeek V3.x and R1, plus open-weights — under one credential and one bill. For HTTP-only providers, OpenRouter is a strict superset of going direct.

The CLI providers (claude, codex, gemini) keep their value: they own session state, OAuth, and agentic exec loops with filesystem access — capabilities OpenRouter's HTTP API cannot replicate.

## Architecture: four tiers, one mental model

```
┌──────────────────────────────────────────────────────────────────┐
│  TIER 1 — CLI agentic    │  claude, codex, gemini                │
│  (subprocess providers)  │  • Filesystem-aware exec loops        │
│                          │  • Session state, OAuth, local auth    │
│                          │  • One subscription per provider      │
├──────────────────────────────────────────────────────────────────┤
│  TIER 2 — OpenRouter     │  openrouter (HTTP catch-all)          │
│  (catalog aggregator)    │  • Every online model worth using     │
│                          │  • One credential, one bill           │
│                          │  • Catalog-driven model selection     │
├──────────────────────────────────────────────────────────────────┤
│  TIER 3 — Local          │  ollama, custom shell providers       │
│                          │  • Free, offline-capable, private     │
└──────────────────────────────────────────────────────────────────┘
```

After migration the user-facing choice collapses from "which of seven providers" to "CLI agent / OpenRouter / local" — three categories for online inference, four when you count local. The router still scores all of them on tags, prefer mode, effort, etc., but the catalog of "providers it scores" is much smaller.

## Use-case routing matrix

Each row maps a task tag (or combination) to the primary route and OpenRouter's role.

| Use case | Tags | Primary | OpenRouter role |
|---|---|---|---|
| Cheap general chat / summarization | `cheap` | **openrouter** (gemini-flash, llama-4-scout class) | Top choice — burning frontier tokens here is wrong |
| Routine coding (boilerplate, simple fixes) | `coding` + `cheap` | **openrouter** (deepseek-chat, qwen3-coder class) | Top choice — strong-but-cheap coders dominate here |
| Hard reasoning at low cost | `strong-reasoning` + `cheap` | **openrouter** (deepseek-r1 class) | Top choice — R1 is dramatically cheaper than gpt-5-thinking |
| Long-context analysis (large repos) | `long-context` | **gemini** (CLI, 2M ctx, native) | Fallback — `openrouter:gemini-2.5-pro` if gemini CLI unconfigured |
| Web-grounded answers | `web-search` | **gemini** (Google Search grounding via CLI) | Alternative — `openrouter:perplexity/*` for research-shaped citations |
| Vision / multimodal | `vision` | **claude / openrouter** (depending on cost preference) | Cost-sorted vision model when budget matters |
| Agentic exec with filesystem | `tool-use` | **claude / codex** (CLI, native agent loop) | Limited — OpenRouter can use Conductor's local tool loop |
| Specialized small models | `math`, niche-language | **openrouter** | Only economical path |
| Cost-bounded jobs | any + `--max-cost` | **openrouter** | Filtered shortlist by price |
| Catch-all fallback | any | varies | Always available — one credential covers everything |

The principle: **OpenRouter is always a clean fallback**, and for cost-sensitive cases it's often the top choice. CLI providers stay top for agentic execution and provider-specific affordances (Google Search grounding, OAuth-bound subscriptions).

## Selector strategy: catalog-driven, no hard-coded slugs

OpenRouter's `:auto` model is NotDiamond under the hood. It is **quality-optimizing, not cost-optimizing** — it will pick Opus for a prompt Haiku could handle. Bare `:auto` is therefore a cost trap. The fix is a layered selector that uses the live catalog API, with `:auto` as a constrained delegate.

```python
def select_openrouter_model(task_tags, prefer, effort):
    catalog = load_or_catalog()  # GET /api/v1/models, cached 24h, refreshable
    candidates = filter_by_capabilities(catalog, task_tags)
    # tag=strong-reasoning → models with thinking=True
    # tag=long-context     → context_length >= 100k
    # tag=tool-use         → function_calling=True
    # tag=vision           → modality includes image input
    # tag=cheap            → no filter, but cost-sort dominates

    if prefer == "cheapest":
        # Bypass :auto entirely — it's quality-first
        candidates.sort(key=lambda m: m.cost_per_1k_in)
        return {"model": candidates[0].id}

    if prefer == "fastest":
        candidates.sort(key=lambda m: m.typical_latency_ms)
        return {"model": candidates[0].id}

    # Default (prefer="best" or "balanced"):
    # Delegate to :auto, but constrain its candidate set to a
    # recency-and-capability-filtered shortlist. This is the only
    # documented way to bound :auto's pool — `models: [...]` is
    # sequential failover, not constraint.
    shortlist = sorted(candidates, key=lambda m: (-m.created, m.cost_per_1k_in))[:6]
    return {
        "model": "openrouter/auto",
        "plugins": [{
            "id": "auto-router",
            "allowed_models": [m.id for m in shortlist],
        }],
        "reasoning": {"effort": effort} if effort != "minimal" else None,
    }
```

The "newer = better" heuristic in the shortlist is the key insight: frontier models almost always beat their predecessors on the same capability axis, and `created` timestamps are catalog-native data. The `tag → capability filter` map is stable code; the selected models update whenever OpenRouter adds something newer.

### Why this beats hard-coded slugs

- **No model names baked into conductor.** When DeepSeek-R2 ships, the catalog query picks it up automatically.
- **No drift on conductor releases.** A 24h cache TTL means at most one day of staleness; `conductor models refresh` flushes it.
- **Capability filters are forward-compatible.** A new "thinking-mode" model added to the catalog gets picked up by `tag=strong-reasoning` queries without code changes.
- **`:auto` is used where it's good (quality-first, no opinion) and bypassed where it's bad (cost-sensitive routing).**

### Layer 3 — small curated lists for quality calls capability flags can't capture

Some tags ("best at math", "best at code-review") aren't derivable from catalog metadata alone. For these, conductor ships a tiny versioned allowlist (target: <10 entries) clearly marked as drift-prone, refreshed via PR on conductor releases. This is the **only** place model names appear in conductor source. Everywhere else, names come from the catalog at runtime.

### User override always wins

`--with openrouter --model deepseek/deepseek-r1` short-circuits the selector. `~/.config/conductor/openrouter.toml` lets users pin their own tag→model mappings. The selector is the smart default, never a forced policy.

## Key research findings driving the design

From the OpenRouter `:auto` research dispatched 2026-04-27:

1. **`:auto` ≠ cost-optimizing.** Quality-first by NotDiamond design. We bypass it for `prefer=cheapest` and `prefer=fastest`.
2. **The constraint primitive is `plugins.allowed_models`, not `models: [...]`.** The `models` array is sequential failover; `plugins` is the candidate-set restriction.
3. **No documented "intent / hint" parameter.** A structured `[routing-context]` system message is not magic — system message content does feed the prompt classifier (it's prompt content), but explicit metadata isn't a documented input. We rely on `allowed_models` and `reasoning.effort` instead.
4. **No documented model-level price cap.** `provider.max_price` filters providers, not models. The workaround is pre-filtering `allowed_models` to sub-threshold entries.
5. **`:auto` is non-deterministic.** Same prompt may route differently as the pool/heuristics shift. Conductor logs `response.model` so observability is preserved.
6. **`reasoning.effort` exists** with `minimal|low|medium|high|xhigh` values. Whether it shifts `:auto`'s selection toward stronger reasoners is undocumented; we pass it through anyway because the chosen model honors it.
7. **Presets are a server-side feature** (`model: "@preset/<name>"`). Powerful but couples to OR's dashboard config. Skip in v1, revisit if inline requests get bloated.
8. **NotDiamond direct, Martian, RouteLLM** are credible alternatives if `:auto`'s quality-first bias becomes painful. Not v1 scope.

## Migration plan: four sequential PRs

Each PR is independently reversible.

### PR 1 — Add OpenRouter as a new provider (~400 lines)

**Scope:**
- New `src/conductor/providers/openrouter.py` — HTTP adapter, OpenAI-compatible
- Register in `providers/__init__.py`
- Add `fix_command = "conductor init --only openrouter"`
- Wizard support for `OPENROUTER_API_KEY`
- Tests with `respx` mocking `openrouter.ai/api/v1/...`
- Catalog client (separate module) with 24h cache

**Out of scope for PR 1:**
- Selector logic (Layer 2). PR 1 ships with `--model <slug>` required when calling openrouter; no auto-selection yet.
- Touching kimi or deepseek. Those continue to work as today.
- `conductor models refresh` command.

**Result after merge:** `--with openrouter --model moonshotai/kimi-k2.6` works. Existing `--with kimi` and `--with deepseek-*` paths untouched.

### PR 2 — Selector logic + `models refresh` command (~300 lines)

**Scope:**
- Implement Layer 2 selector (capability filter + cost/recency sort)
- Implement Layer 1 wrapper (`:auto` + `allowed_models` shortlist)
- New `conductor models refresh` subcommand
- Update auto-mode router to score `openrouter` as a tier-2 candidate
- Tests for selector logic

**Result after merge:** `conductor call --with openrouter --tags strong-reasoning` picks a model from the live catalog without a `--model` flag. `--auto` can pick openrouter as a candidate.

### PR 3 — Migrate kimi to OpenRouter (~400 lines, breaking change)

**Scope:**
- Refactor `kimi.py` to a thin shim that calls openrouter under the hood (model preset to whatever OR's current Kimi K2.6 slug is)
- Or: drop `kimi` as a named provider entirely; `--with kimi` becomes "unknown provider"
- Wizard detects `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` set without `OPENROUTER_API_KEY` and offers to migrate
- Doctor warns if old credentials are set without the new one
- Drop `tests/test_kimi.py` (or rewrite to test the shim, depending on the path chosen)
- Release notes call out the breaking change

**Decision required before PR 3:** soft alias (deprecation warning, still works for one release) vs. clean break (immediate "unknown provider"). Current intent: clean break, aligned with the simplification goal.

### PR 4 — Migrate deepseek-chat and deepseek-reasoner (~400 lines, breaking change)

**Scope:** symmetric to PR 3 for the two DeepSeek providers. `DEEPSEEK_API_KEY` becomes deprecated.

**Result after merge:** Conductor's named-provider list narrows to `claude`, `codex`, `gemini`, `openrouter`, `ollama`, plus user-defined custom shells. Any other model is reachable via `--with openrouter --model <slug>`.

## Decision log

- **Use OpenRouter, not Cloudflare CLI / Workers AI.** CF's catalog doesn't host the latest DeepSeek V3.x / R1 (only distill); CF's CLI doesn't have a one-shot inference subcommand. AI Gateway preserves upstream billing. OpenRouter is the only single-bill solution that includes the proprietary frontier providers and the latest open-weights together.
- **Use Hugging Face Inference Providers? No.** HF is open-weights focused — no Claude/GPT/Gemini routing. OpenRouter beats it on coverage.
- **Drop kimi and deepseek as named providers? Yes.** Aligned with the user's stated goal of "pay once, access easily." Soft aliases drift; clean break commits to the simplification.
- **Catalog query at runtime, not hard-coded slugs.** Required to deliver "always latest models" without manual updates.
- **Bypass `:auto` for cost-sensitive routing.** NotDiamond is quality-first.
- **No structured routing-hints system message.** Not documented as effective; relies on speculation. Use `allowed_models` and `reasoning.effort` instead.
- **No presets in v1.** Couples to OR's dashboard. Inline requests are portable.

## Open questions

- **Soft alias or clean break for kimi/deepseek names?** Current intent: clean break in PR 3/4. Confirm before shipping.
- **Default OpenRouter model when auto-mode picks `openrouter` with no tags?** Probably `openrouter/auto` with no `allowed_models` constraint, accepting NotDiamond's quality-first bias as the safe default.
- **How does `auto-mode router` score `openrouter` as a candidate?** It's one provider with one tier — but in practice it can serve any tier on demand. Initial proposal: tag it with the union of all relevant tags (`cheap`, `coding`, `long-context`, `strong-reasoning`, `vision`, `web-search`, etc.) and treat its `quality_tier` as `frontier` (since it can route to frontier models). Effective tier is whatever model the selector picks. This is intentionally lossy at the conductor router layer; intelligence lives in the openrouter selector.
- **Cost ceiling support (`--max-cost`)?** Not in PR 1-4. Useful future feature; mentioned in the use-case matrix but defer to a follow-up.
- **Council / multi-LLM consensus?** Separate feature, separate PR. OpenRouter makes it cheap (parallel calls, one bill) but it's a new command surface, not a routing tweak.

## Risks / sharp edges

1. **Credential migration is the breaking change.** Users with `CLOUDFLARE_API_TOKEN` / `DEEPSEEK_API_KEY` in their env need to swap to `OPENROUTER_API_KEY`. Doctor must warn loudly; release notes must call this out.
2. **OpenRouter outage = no HTTP-only inference.** Today, kimi-direct and deepseek-direct are independent failure domains. After consolidation, one OR outage takes down both. Mitigations: ollama as offline fallback, claude/codex/gemini CLIs unaffected.
3. **`:auto` non-determinism.** Same call may route to different models. Tests must not pin to specific routed models; `response.model` logging is mandatory.
4. **Pricing surprises.** `:auto` is quality-first. Without `allowed_models` constraint, calls can land on expensive models. The selector's default shortlist + `prefer` mode handling is the mitigation.
5. **Catalog drift without auto-refresh.** A 24h TTL is the right default but stale caches mean missing brand-new models. `conductor models refresh` is the manual override.

## Why this aligns with conductor's principles

- **No band-aids:** root-cause consolidation of two independent HTTP adapters and two independent billing relationships into one.
- **Keep interfaces narrow:** the user-facing surface shrinks from seven named providers to four. The `Provider` Protocol is unchanged.
- **One code path:** kimi and deepseek don't end up with two implementations (direct + via-openrouter). Direct paths are deleted.
- **No silent failures:** doctor warns on stale credentials; selector logs the chosen model; cache TTL surfaces in `conductor models` output.
- **Preserve compatibility at boundaries:** PR 3/4 are explicit breaking-change PRs with migration guidance. The `Provider` Protocol stays intact across the change.
- **Make irreversible actions recoverable:** each PR is independently revertible. The catalog cache is a derived artifact (deletable, regenerable).
