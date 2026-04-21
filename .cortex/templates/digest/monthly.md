---
Generated: {{ YYYY-MM-DDTHH:MM:SS-TZ }}
Generator: cortex digest monthly v{{ CLI version }}
Sources:
  - .cortex/journal/ (entries: {{ YYYY-MM-01 .. YYYY-MM-DD }})
  - .cortex/plans/*.md (active: {{ count }})
  - .sentinel/runs/ ({{ count }} cycles, if present)
Corpus: {{ N }} Journal entries, {{ K }} Plans, {{ M }} Sentinel cycles
Omitted:
  - {{ journal/<slug> — reason (e.g., marked noisy, wip-debugging) }}
Incomplete: []
Conflicts-preserved:
  - {{ "<topic>" — journal/<date-a> argues X; journal/<date-b> argues Y }}
Spec: 0.3.1
Depth: 0   # monthly digests cite raw Journal only, never other digests
---

# {{ Month Year }} digest

**Type:** digest
**Period:** {{ YYYY-MM-01 to YYYY-MM-DD }}

> {{ One-paragraph narrative arc for the month. What were the dominant themes? What shifted? What's newly uncertain? Cite 2–3 load-bearing entries inline. }}

## Key decisions

- **{{ theme }}** — {{ one-sentence summary }}. Source: journal/{{ <date>-<slug> }}.
- **{{ theme }}** — {{ summary }}. Source: journal/{{ <date>-<slug> }}.

## Incidents

- **{{ title }}** — {{ one-sentence summary with impact }}. Source: journal/{{ <date>-<slug> }}.

## Plans advanced

- plans/{{ <slug> }}: {{ status change or work items completed }}.

## Doctrine changes

- doctrine/{{ <nnnn>-<slug> }}: {{ new | superseded by <mmmm> }}.

## Open threads going into next period

- {{ unresolved question, cited to its latest journal entry }}
