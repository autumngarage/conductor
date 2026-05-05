# {{ Plan <slug> — <old-status> → <new-status> }}

**Date:** {{ YYYY-MM-DD }}
**Type:** plan-transition
**Trigger:** T1.3
**Cites:** plans/{{ <slug> }}

> {{ One sentence: what plan changed status, from what to what, and why. }}

## Context

{{ What did the plan set out to do? Link to the plan's `Why (grounding)` section. }}

## Transition

- **From:** {{ active | blocked | deferred }}
- **To:** {{ shipped | cancelled | deferred | blocked }}
- **Reason:** {{ succeeded / deprioritized / superseded by <slug> / external blocker / scope change }}

## Outcome against success criteria

{{ Quote the plan's Success Criteria verbatim, then state met/not-met per line. If not met, say whether the criteria changed or the plan failed against them. }}

## Deferred items

- {{ item — resolved to: plans/<new-slug> | journal/<date>-<slug> | doctrine/<nnnn>-<slug> }}

(Per SPEC § 4.2, every deferred item must resolve to another Plan, Journal entry, or Doctrine entry in the same commit. No orphans.)
