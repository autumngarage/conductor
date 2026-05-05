---
Status: active
Written: {{ YYYY-MM-DD }}
Author: human
Goal-hash: (recompute with cortex doctor)
Updated-by:
  - {{ YYYY-MM-DDTHH:MM human (created) }}
Cites: {{ doctrine/<nnnn>-<slug>, state.md § <section>, journal/<date>-<slug> }}
---

# {{ Plan Title — active voice, names the effort }}

> **{{ One-sentence bold claim: what this plan ships, and why it matters now. }}**

## Why (grounding)

{{ TODO: cite the triggering grounding source. SPEC requires a link into a durable layer; cortex doctor warns until you replace this placeholder with a real citation. See the authoring checklist at the bottom for the supported citation shapes. }}

## Approach

{{ One to three paragraphs or a short bulleted sketch of how this plan gets done. Name the key modules touched, the external dependencies, and the rough shape of the work — enough that a second writer picking up the plan mid-stream can orient without rereading every citation. }}

## Success Criteria

{{ TODO: replace each bullet below with a concrete, measurable signal — see the authoring checklist at the bottom of this file for examples. Prose-only criteria fail cortex doctor validation. }}

- {{ TODO: concrete, measurable outcome }}
- {{ TODO: concrete, measurable outcome }}
- {{ TODO: concrete, measurable outcome }}

## Work items

- [ ] {{ first concrete task — link to issue/PR when filed }}
- [ ] {{ second concrete task }}
- [ ] {{ third concrete task }}

## Follow-ups (deferred)

{{ TODO: items moved out of scope during execution. SPEC § 4.2 requires every deferral to resolve to another Plan, Journal entry, or Doctrine entry in the same commit — no orphan deferrals. Cortex doctor's orphan-deferral check ships in the v0.3.0 release. }}

- {{ TODO: deferred item — resolve to a successor plan, a journal entry, or a doctrine entry }}

## Known limitations at exit

{{ What this plan deliberately does not solve, so a future reader knows what remains. Link forward if a follow-up plan already exists. }}

- {{ limitation — follow-up: TODO when filed }}

<!--
Authoring checklist (remove before committing):

- [ ] Replace the Goal-hash placeholder: `cortex doctor` recomputes the hash from the H1 title (SPEC § 4.9) and tells you the correct value on first run. Copy that value into the frontmatter.
- [ ] Replace every `{{ ... }}` placeholder with real content.
- [ ] `## Success Criteria` must name measurable signals (SPEC § 4.3) — numeric thresholds, test/dashboard links, or path-based references like `tests/`, `doctrine/`, `journal/`, `PR #<n>`.
- [ ] `## Why (grounding)` must link to doctrine/, state.md, or journal/ (SPEC § 4.1).
- [ ] Every deferral in `## Follow-ups (deferred)` resolves to a successor plan, journal entry, or doctrine entry in the same commit (SPEC § 4.2).
- [ ] Run `cortex doctor` — green on this plan before you commit.
-->
