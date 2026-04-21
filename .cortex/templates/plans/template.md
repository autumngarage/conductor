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

{{ Cite the triggering Doctrine entry, State priority, or Journal item. Never invent context — always link. SPEC § 4.1 requires a citation to `doctrine/`, `state.md`, or `journal/`; `cortex doctor` warns when none is present. One short paragraph; name the decision or metric this plan answers to. }}

## Approach

{{ One to three paragraphs or a short bulleted sketch of how this plan gets done. Name the key modules touched, the external dependencies, and the rough shape of the work — enough that a second writer picking up the plan mid-stream can orient without rereading every citation. }}

## Success Criteria

{{ SPEC § 4.3 requires measurable signals — a numeric threshold, a test/dashboard link, or a code/path reference. Prose-only criteria fail `cortex doctor`. Keep this to 3–5 items. }}

1. {{ Concrete, measurable outcome. Example: "`cortex doctor` exits 0 on this repo's `.cortex/`." }}
2. {{ Concrete, measurable outcome with a numeric threshold or a link. }}
3. {{ Concrete, measurable outcome referencing a file, test, or PR. }}

## Work items

- [ ] {{ first concrete task — link to issue/PR when filed }}
- [ ] {{ second concrete task }}
- [ ] {{ third concrete task }}

## Follow-ups (deferred)

{{ Items moved out of scope during execution. Per SPEC § 4.2, every deferral must resolve to another Plan or Journal entry in the same commit — no orphan deferrals. }}

- {{ deferred item — resolved to: plans/<new-slug> | journal/<date>-<slug> }}

## Known limitations at exit

{{ What this plan deliberately does not solve, so a future reader knows what remains. Link forward if a follow-up plan already exists. }}

- {{ limitation — follow-up: plans/<new-slug> or journal/<date>-<slug>, if filed }}

<!--
Authoring checklist (remove before committing):

- [ ] Replace the Goal-hash placeholder: `cortex doctor` recomputes the hash from the H1 title (SPEC § 4.9) and tells you the correct value on first run. Copy that value into the frontmatter.
- [ ] Replace every `{{ ... }}` placeholder with real content.
- [ ] `## Success Criteria` must name measurable signals (SPEC § 4.3).
- [ ] `## Why (grounding)` must link to doctrine/, state.md, or journal/ (SPEC § 4.1).
- [ ] Every deferral resolves somewhere (SPEC § 4.2).
- [ ] Run `cortex doctor` — green on this plan before you commit.
-->
