# Codex review of conductor-blindspots plan reshaped Slice B and added Phase 0s to Slice A and C

**Date:** 2026-04-24
**Type:** decision
**Trigger:** T2.1 (decision phrased)
**Cites:** plans/conductor-blindspots, doctrine/0002-audit-weak-points, doctrine/0004-engineering-principles

> Codex's review of the first-pass blindspot remediation plan flagged five substantive findings; this entry records which were accepted, what changed in the plan, and why.

## Context

A blindspot audit on 2026-04-24 against conductor v0.4.2 surfaced ~10 candidate gaps. The first-pass plan (`.cortex/plans/conductor-blindspots.md`) picked three to ship: Slice A (subprocess-adapter live smoke in CI), Slice B (cost observability via `conductor usage`), Slice C (sandbox adversarial audit). The plan was sent for review via `codex exec --full-auto` against the repo. Codex returned `approve with changes` with five findings.

## What we decided

Accept all five Codex findings. Specifically:

1. **Replace Slice B with subagent prompt-drift testing.** Codex argued that the v0.4.0–v0.4.2 agent-wiring slices (`_agent_templates.py`, `AGENTS.md`, `GEMINI.md`, repo `CLAUDE.md`, Cursor rules) shipped in the past two weeks and have no automated check that the embedded subagent prompts mention the conductor flag surface they should know about. The "freshly-shipped, least-validated" framing wins over cost observability for the top-3 cut. Cost observability is deferred to its own follow-up plan rather than shipped under this banner with the principle gaps unresolved.

2. **Add Phase 0 to Slice A.** Codex caught that `RUN_LIVE_SMOKE=1`-gated tests for claude/codex/gemini do not exist yet — `tests/test_adapters_subprocess.py` is 305 lines of fully-mocked tests. Slice A starts by *building* the live tests, then wiring CI. The CI workflow now hard-fails when any target CLI is missing from the runner (no silent zero-coverage pass).

3. **Add Phase 0 to Slice C.** Codex caught that an attack catalog without a written sandbox contract is just behavior-testing, not invariant-validation, violating `Think in invariants`. New Phase 0 writes `.cortex/doctrine/0007-sandbox-semantics.md` defining named invariants per sandbox mode; Phase 1 attempts cite specific invariant IDs.

4. **Cost-observability deferral preserves the principle critiques.** The Slice B draft used a silent-no-op pattern for unwritable cache (mirroring `offline_mode.py`, recently shipped). Codex flagged this as a `No silent failures` violation and the persisted derived state as missing `Derive, don't persist` provenance/reconciliation. Both must be addressed when the deferred plan lands; a second follow-up tracks the same fix on `offline_mode.py` itself.

5. **Reorder accepted as A → B → C.** With B replaced (no longer adds persisted state), the original "B should not precede C" concern dissolves. A → B → C runs smallest-to-largest by scope.

## Consequences / action items

- [x] Plan updated: `.cortex/plans/conductor-blindspots.md` revised, `Updated-by` entry added.
- [ ] Cost observability: file `plans/conductor-cost-observability.md` when prioritized; carry forward the principle fixes (visible failure on unwritable cache, provenance header per JSONL record).
- [ ] `offline_mode.py` silent-no-op: small drive-by PR adding warn-once-to-stderr behavior when `set_active()` fails to write.
- [ ] Slice A implementation handed to Codex via `codex exec --full-auto`.

## Process notes (for future plan reviews)

- `codex exec --full-auto -o <file> "<prompt>"` worked once we used a tight, structured prompt and explicit output file. A first attempt with a long prose prompt hung for 41 minutes producing zero output and had to be killed; the second attempt with a 12-line structured prompt and forced output format completed in 2 minutes producing 1KB of usable feedback.
- Codex's review touched not only the plan but the recently-shipped `offline_mode.py` by analogy — useful side effect of asking it to ground critique in the engineering principles.
