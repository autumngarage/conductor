# Touchstone script-sync help bug filed upstream

**Date:** 2026-06-06
**Type:** decision
**Trigger:** T2.2, T2.3
**Cites:** journal/2026-06-06-flat-rate-first-conductor-vision, doctrine/0007-flat-rate-first-delivery-control-plane, https://github.com/autumngarage/touchstone/issues/444
**failed-approach:** true
**investigation:** true

> Running `scripts/open-pr.sh --help` exposed a Touchstone script-sync bug; the
> durable Conductor vision remains owned by Cortex doctrine and the roadmap.

## Context

While preparing the flat-rate-first vision docs, `bash scripts/open-pr.sh
--help` was run to inspect the PR helper. Because the project-local Touchstone
scripts were stale, the script-sync guard ran before any help handling,
committed a Touchstone update on the feature branch, re-executed
`scripts/open-pr.sh --help`, and continued into the normal push/PR flow. The
command was manually terminated while it was still in local pre-push validation.
No PR was created.

The Touchstone update also rewrote `principles/ai-delivery-architecture.md`
from the upstream template, removing the Conductor-specific lines that had been
added there in the previous commit. That shows the architecture file is not the
right canonical owner for Conductor-specific product vision in this repo unless
the upstream Touchstone template also changes.

## What we decided

The flat-rate-first vision is canonical in
`doctrine/0007-flat-rate-first-delivery-control-plane.md` and operationalized
in `plans/root-cause-roadmap.md` Thread B. `principles/ai-delivery-architecture.md`
remains useful context, but it should not own Conductor-specific provider
economics because Touchstone project sync may overwrite it.

The upstream Touchstone bug is filed as autumngarage/touchstone#444.

## Consequences / action items

- [x] File the Touchstone script-sync bug upstream with repro and impact.
- [x] Keep the Conductor-specific vision in Cortex-owned doctrine and plan files.
- [ ] Fix Touchstone so read-only/help invocations do not mutate, commit, push,
      or re-execute into delivery flow.
