# Cortex update check treats historical journal edits as legacy warnings

**Date:** 2026-05-11
**Type:** decision
**Trigger:** human-authored
**Cites:** GitHub issue #343, journal/2026-05-05-cortex-install-baseline

> Conductor treats the append-only warnings reported by Cortex 1.6.2 for
> pre-existing journal files as legacy known warnings, while adding a validation
> gate so generated state freshness does not drift again.

## Context

`cortex update --path .` refreshed `.cortex/state.md` from `cortex
refresh-state v0.8.2` to `v1.6.2` and rebuilt `.cortex/.index.json`. During
the post-update doctor pass, Cortex reported three append-only warnings for
old journal entries modified at commit `eccab3870bfb`:

- `.cortex/journal/2026-04-24-codex-plan-review.md`
- `.cortex/journal/2026-04-26-codex-exec-wedge-trace.md`
- `.cortex/journal/2026-04-28-pr-106-merged.md`

Those files predate this refresh. Rewriting them would violate the Cortex
append-only invariant again, so the current issue should not repair the
history in place.

## What we decided

The historical append-only warnings are documented as legacy known warnings.
The actionable fix for issue #343 is the freshness loop: `scripts/touchstone-run.sh
validate` now runs `cortex update --check --path .` via `uv run cortex` when
available, or the `cortex` executable otherwise, before the usual lint,
typecheck, build, and test profile actions.

## Consequences / action items

- [x] Refresh generated Cortex state with current CLI provenance.
- [x] Add a validation gate that fails when Cortex generated state is stale.
- [x] Document the historical append-only warnings without editing old journal
  entries in place.
