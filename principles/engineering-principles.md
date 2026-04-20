# Engineering Principles (HARD REQUIREMENTS)

These are non-negotiable. Every code change must be reviewed against them; any exception must be explicit, justified, and disclosed in the PR.

## No band-aids
Fix the root cause unless the PR explicitly documents why a symptom patch is the safer scoped change. If it's a symptom patch, say so: *"This patches the symptom. The root cause is X and fixing it properly would require Y. Which do you want?"* Undocumented symptom patches compound — a year later you have a codebase full of thin fixes and nobody remembers which ones were intentional.

## Keep interfaces narrow
Expose the smallest stable interface that lets callers do their job. Don't leak storage shape, vendor SDKs, temporary flags, or workflow sequencing across module boundaries. A deep module hides substantial complexity behind a stable contract; a shallow module exports its complexity to every caller and makes every future fix broad and risky.

## Derive limits from domain; test at scale boundaries
Derive thresholds, sizes, limits, and allocations from input, configuration, or named domain constants. Hard-code a value only when it represents a real invariant, and document why. Test behavior at small, typical, and large scales — not just the shape you developed against. Code that only works at one scale will silently misbehave at the scales you forgot.

## Derive, don't persist
Compute from the source of truth by default. Persist derived state only when recomputation is too slow, too expensive, or externally required — and when you do, document in the same commit: the source of truth, the invalidation trigger, the rebuild path, and a reconciliation check. Undocumented persisted state goes stale silently; that is the failure mode this rule prevents.

## No silent failures
Every exception is either re-raised or logged with enough context to debug from production logs alone. No `except: pass`. No swallowed errors. No default values returned on failure without a log line. Fallback behavior may continue only when it reports what failed, what was skipped, and what safety boundary still holds.

**The rule:** if something fails, the failure must be visible to someone — an operator, a log aggregator, a monitoring dashboard. A failure that nobody can see is the most dangerous kind.

## Every fix gets a test
Bug fixes must include a test that reproduces the exact failure mode, and the test must run in CI — not just locally. A bug fix without a regression test means the bug can recur silently the next time someone refactors nearby. The test should fail on the old code and pass on the new code — if it passes on both, it isn't testing the right thing.

## Think in invariants
For nontrivial logic, name at least one invariant and assert it — either in a test or as a runtime boundary check. What must always be true? What relationship between values must hold? Happy-path outputs tell you the code worked for one input; invariants tell you it can't be wrong for any input in the covered space.

## One code path
Share business logic across modes (test/prod, paper/live, dev/staging). Divergent paths drift apart silently, and bugs in one path don't surface until it's too late. If modes must differ, confine the difference to adapters, configuration, or the final I/O boundary — not a fork at the top of the pipeline.

## Version your data boundaries
When a model, algorithm, or data source changes in a way that affects decisions, rankings, persisted state, metrics, or user-visible behavior, establish a boundary (cohort, epoch, version) and ensure every downstream consumer honors it. Reads that drive decisions must not blend data across the boundary; aggregating across it dilutes signal with noise from the old regime.

## Separate behavior changes from tidying
Do not mix functional changes with broad renames, formatting sweeps, dependency churn, or unrelated refactors. If cleanup is needed, do it before or after the behavior change in a separate commit or PR. Reviewers must be able to see the semantic change without diff noise; mixed changes hide regressions and make rollback unsafe.

## Make irreversible actions recoverable
Any destructive or one-way operation must have a recovery path before it runs. Deletes, migrations, format rewrites, external side effects, and history rewrites need a dry run, backup, idempotency key, rollback plan, or forward-fix plan. A change is not safe because it passed once; it is safe when failure leaves the system in a known recoverable state.

## Preserve compatibility at boundaries
Changes to public APIs, config files, schemas, CLIs, hooks, templates, and generated artifacts must include a compatibility or migration plan. Accept old and new formats during rollout when downstream consumers may lag. Boundary breaks multiply: one local assumption becomes N downstream failures.

## Audit one weak-point class at a time
When you find a structural bug, audit the whole class — not just the one you noticed. See [audit-weak-points.md](audit-weak-points.md) for the methodology. This discipline prevents re-auditing the same code twice and catches bugs before they compound.
