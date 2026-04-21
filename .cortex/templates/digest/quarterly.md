---
Generated: {{ YYYY-MM-DDTHH:MM:SS-TZ }}
Generator: cortex digest quarterly v{{ CLI version }}
Sources:
  - .cortex/journal/digest-{{ YYYY-MM }}.md ({{ 3 months }})
  - .cortex/journal/ (raw entries cited directly; see Corpus)
  - .cortex/plans/ (active + shipped in period)
Corpus: {{ N }} monthly digests, {{ K }} raw Journal entries cited directly (>=5 required by SPEC § 5.3)
Omitted: []
Incomplete: []
Conflicts-preserved:
  - {{ "<topic>" — digest/<month-a> concluded X; digest/<month-b> concluded Y; resolution: ... }}
Spec: 0.3.1
Depth: 1   # quarterly digests may cite monthly digests (depth 1 max per SPEC § 5.3)
---

# Q{{ N }} {{ YYYY }} digest

**Type:** digest
**Period:** {{ YYYY-MM-01 to YYYY-MM-DD }}

> {{ One-paragraph narrative arc for the quarter. What's the story of these three months? What was believed at the start and has since shifted? What Doctrine emerged or was superseded? }}

## Doctrine changes this quarter

- **doctrine/{{ <nnnn>-<slug> }}** ({{ new | superseded <mmmm> }}) — {{ one-sentence claim }}. Emerged from {{ journal/<date>-<slug> + 2 others }}.

## Dominant themes

### {{ theme 1 }}

{{ 2-4 sentences. Cite at least one monthly digest AND at least one raw journal entry. }}

- Evidence: digest/{{ <month> }}.md § <section>, journal/{{ <date>-<slug> }}, journal/{{ <date>-<slug> }}.

### {{ theme 2 }}

{{ 2-4 sentences with citations as above. }}

## Incidents and reversals

- **{{ title }}** — {{ summary }}. Source: journal/{{ <date>-<slug> }} (raw). Followed up in {{ journal/<date>-<slug> }}.

## What's unresolved going into next quarter

- {{ question with citation to its latest journal or digest entry }}

---

**Audit note.** This digest is subject to `cortex doctor --audit-digests` random-sample claim verification (SPEC § 5.4). At least five raw Journal entries must be cited directly, not just through monthly digests (SPEC § 5.3).
