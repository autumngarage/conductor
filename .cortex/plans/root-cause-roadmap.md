---
Status: active
Written: 2026-05-17
Author: human
Goal-hash: 7a463092
Updated-by:
  - 2026-05-17T17:09 human (created via cortex plan spawn)
Cites: doctrine/0002-audit-weak-points, doctrine/0004-engineering-principles, .cortex/journal/2026-05-17-pr-merged-1247.md, .cortex/state.md § Active plans
---

# Conductor Root-Cause Roadmap — Structural Redesigns

> **Four structural redesigns that, together, eliminate the band-aid bug classes that produced ~30 closed symptom-shaped issues in the five weeks ending 2026-05-17. The roadmap converts the conductor issue tracker from a stream of instance-shaped reports into four named failure-class threads, each with a tracking issue, a sketch of the right fix, and an acceptance signal that the next instance of the class cannot be closed with a constant-bump or a warning.**

## Why (grounding)

This plan is grounded in three durable sources:

- **[doctrine/0002-audit-weak-points](../doctrine/0002-audit-weak-points.md)** — when a structural weakness surfaces, name the pattern, audit its instances, and add a guardrail. This plan applies that doctrine to four failure classes simultaneously instead of one.
- **[doctrine/0004-engineering-principles](../doctrine/0004-engineering-principles.md)** — the "No band-aids" hard-requirement and "Audit weak-point classes" rules name what each thread is meant to prevent.
- **[journal/2026-05-17-pr-merged-1247](../journal/2026-05-17-pr-merged-1247.md)** — the T1.9 record of the bug-triage principle's merge (PR #467), which is the immediate trigger for converting the issue tracker from symptom-organized to root-cause-organized.

`principles/bug-triage.md` (shipped 2026-05-17 in PR #467) codifies the pattern check that runs *before* a bug-fix implementation begins: scan recent issues, name the failure class, decide root-cause-fix vs. scoped-symptom-patch consciously. Applying that principle to the existing closed-issue backlog surfaced four recurring classes that have been patched repeatedly without the underlying design being addressed. This plan names those four classes as the active roadmap and links each to a tracking issue.

The principle's smell tests (adjusts a constant, adds a warning instead of preventing, adds a provider-specific routing rule) match recent merges directly — PR #464 raised the iteration cap to close #459 (constant adjustment); PR #466 added a hook auto-stage warning to close #460 (warn-not-prevent); PR #465 added family-aware fallback ordering for #461 (provider-specific guard). Without this roadmap, the next routing or termination bug will produce the same shape of fix.

## Approach

Four threads, each owned by a single GitHub tracking issue. Each thread has its own acceptance signal expressed as "the next bug of this class cannot be closed with a band-aid." None of the threads is ready-to-implement today — each captures the design problem and a sketch of the right fix, deferring the actual phased implementation to future plans or PRs.

| Thread | Failure class | Tracking issue | Depends on |
|---|---|---|---|
| **A** | Operator-facing termination knobs (iteration cap, output cap, wall-clock cap) across exec/council/ask | [#469](https://github.com/autumngarage/conductor/issues/469) | C (for fallback summary shape), D (for checkpoint contract) |
| **B** | Provider Capability Model is too shallow → routing patches in shared code | [#472](https://github.com/autumngarage/conductor/issues/472) | independent |
| **C** | Retry/fallback cascade re-prepends raw transcript → silent cost amplifier | [#473](https://github.com/autumngarage/conductor/issues/473) | independent |
| **D** | Exec has no commit-boundary contract → scope creep, lost commits, empty final responses | [#474](https://github.com/autumngarage/conductor/issues/474) | independent |

### Dependency story

A (run-health) is the most integrative thread. Its "graduated intervention" path needs **C** (the summary shape it hands to the next provider on fallback) and **D** (the commit-boundary contract it invokes at checkpoint time) to be defined before A's interventions can write. C and D are each independently shippable but A pulls them together.

B (capability model) is structurally independent — it could ship in parallel with any of the others. It has the longest tail (one PR per migrated shared-code branch) and benefits from landing the cross-cutting smell-test guardrail first (see Work items).

### Method per thread (uniform across A–D)

1. Track in GitHub. The tracking issue is the canonical "is this class still open" answer.
2. Phase the implementation. Each thread is expected to ship over multiple PRs; shadow-mode telemetry first, then interventions, then deprecation of old knobs. No thread tries to land as a single PR.
3. Add a guardrail per `principles/audit-weak-points.md` step 6 — a CI lint or AST check that catches the next instance of the class at PR review (not after merge).

## Success Criteria

- **Plan-level**: every closed conductor issue filed between 2026-04-12 and 2026-05-17 mapping to a failure class is referenced from one of the four tracking issues (#469, #472, #473, #474) as a "Symptoms observed" link. Verifiable via `gh issue list --state closed --search "<keyword>"` against each thread's symptoms table.
- **A — run-health** (per #469): `--help` output of `conductor exec`, `conductor council`, and `conductor ask` contains no termination knobs in the default path; a council run with one failed member reports the failure structurally (no "directionally clear" framing when ≥ 30% of members failed silently).
- **B — capability model** (per #472): a CI lint scans for `provider == ` / `provider in {"…"}` patterns in `src/conductor/cli.py`, `src/conductor/semantic.py`, `src/conductor/router.py` and warns when a new occurrence is added. Existing occurrences are documented in an audit at issue-open time, and the count decreases monotonically.
- **C — fallback carry-over** (per #473): a deterministic test asserts that attempt N+1's input tokens grow sub-linearly across a 3-provider fallback chain (bounded by `O(brief_size + summary_size)`, not `O(N * brief_size)`).
- **D — commit-boundary** (per #474): an exec run where the agent edits 3 brief-scope files and the hook auto-bumps 2 unrelated files produces a commit containing only the in-scope files plus a structured warning naming the out-of-scope diff — without the agent having to remember the boundary.

## Work items

### Plan-level guardrails (do first; orthogonal to threads A–D)

- [ ] Add a CI check for the `principles/bug-triage.md` smell tests: scan PR titles/bodies for "raise the cap", "warn when", "skip if", "retry on" without an explicit "symptom patch — root cause is" disclosure. Doesn't fail the build; produces a PR review comment.
- [ ] Add the cross-cutting `provider == ` / `provider in` AST scan referenced in B's success criteria (lands once; serves B's whole tail).

### Thread A — Run-health awareness (#469)

- [ ] Phase 1 (shadow mode): implement run-health monitor inside the exec loop; log signals to delegation telemetry on every real run; no interventions yet. Validate signals against real runs for ≥ 7 days.
- [ ] Phase 2 (nudge intervention): inject "summarize where you are and what's blocking" system message when monitor crosses configured thresholds.
- [ ] Phase 3 (checkpoint commit): wire to D's commit-boundary contract; checkpoint dirty worktree at intervention time.
- [ ] Phase 4 (fallback with summary): wire to C's summary shape; hand next provider the structured summary, not raw transcript.
- [ ] Phase 5 (council honest-output contract): degraded outcomes report structurally; no "directionally clear" papering.
- [ ] Phase 6 (deprecate operator-facing termination knobs): soft-deprecate `--max-iterations`, output-token cap, wall-clock cap from default-path `--help`; one-release tail.

### Thread B — Provider Capability Model (#472)

- [ ] Audit shared-code `provider == ` / capability-shaped branches in routing/preflight; classify each as (adapter / parameterized-shared / router-global).
- [ ] Deepen `Provider.capabilities` with the fields surfaced by the audit (streaming, runtime type, model family, output-contract shape, native-extension status).
- [ ] Migrate shared-code branches to capability queries, one PR per class. Add regression tests per migration.

### Thread C — Retry/fallback carry-over (#473)

- [ ] Define `FallbackSummary` shape: brief reference, prior attempt outcomes, partial artifacts, short synthesis.
- [ ] Replace raw-transcript re-prepend with summary in `src/conductor/semantic.py` (and any sibling fallback dispatchers).
- [ ] Add cumulative-chain-cost field to delegation telemetry (closes #446's class through this mechanism).
- [ ] Add deterministic test for sub-linear input-token growth.

### Thread D — Exec commit-boundary contract (#474)

- [ ] Record brief-claimed scope at dispatch time (file globs / paths from brief).
- [ ] Inspect worktree state before agent termination; classify dirty files vs scope, commits vs scope, untracked, hook-staged.
- [ ] Move final commit boundary ownership from agent to exec (mirroring swarm's #408 contract).
- [ ] Wire to A's checkpoint phase.

## Follow-ups (deferred)

- **Bug-triage CI guardrail rollout to sibling autumn-garage repos.** The smell-test check shipped under this plan's "Plan-level guardrails" section is conductor-local in the first PR. Promotion to a doctrine-level expectation across autumn-garage belongs to a sibling plan; the trigger that spawns that plan is captured at [`journal/2026-05-17-pr-merged-1247`](../journal/2026-05-17-pr-merged-1247.md) (the principle's merge record).

## Known limitations at exit

- **The plan is the roadmap; the issues are the work.** Acceptance signals live on the tracking issues; this plan is the index. If an issue's acceptance signal changes, this plan does not automatically update — the next `cortex refresh-state` cycle won't catch divergence between this plan's tables and the issues' bodies.
- **No timeline.** Threads are dependency-ordered, not date-ordered. The plan does not commit to a quarter or month; the next issue claim and PR sequence does.
- **Symptom-patch shipping is not banned during the redesign.** Per `principles/bug-triage.md`, a documented symptom patch with a linked root-cause issue is allowed. The plan reduces the *rate* of band-aids, not to zero.
- **`conductor-blindspots.md` is not adjudicated by this plan.** That older plan (20% complete, 2026-04-24) covers a different structural surface (subprocess-adapter live smoke, subagent prompt drift, exec authority sandbox). It is not in conflict with this plan but is stale; classifying its remaining checkboxes as shipped / deferred / still-active is a separate audit and is intentionally outside this plan's scope.
- **#448 (OPENROUTER_CODING_HIGH cost-aware ordering) is not bundled.** Tactical fix, doesn't fit any of the four classes. Ships independently against the open issue, or folds into Thread B's capability-model work late if convenient. Tracked on GitHub at issue #448.

<!--
Authoring checklist (remove before committing):

- [x] Replace the Goal-hash placeholder: `cortex doctor` recomputes the hash from the H1 title (SPEC § 4.9) and tells you the correct value on first run. Copy that value into the frontmatter.
- [x] Replace every `{{ ... }}` placeholder with real content.
- [x] `## Success Criteria` must name measurable signals (SPEC § 4.3) — numeric thresholds, test/dashboard links, or path-based references like `tests/`, `doctrine/`, `journal/`, `PR #<n>`.
- [x] `## Why (grounding)` must link to doctrine/, state.md, or journal/ (SPEC § 4.1).
- [x] Every deferral in `## Follow-ups (deferred)` resolves to a successor plan, journal entry, or doctrine entry in the same commit (SPEC § 4.2).
- [x] Run `cortex doctor` — green on this plan before you commit.
-->
