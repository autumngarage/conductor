# {{ Title — what broke, in active voice }}

**Date:** {{ YYYY-MM-DD }}
**Type:** incident
**Trigger:** {{ T1.2 (test failed after passing) | — (human-authored) }}
**Cites:** {{ plans/<slug>, doctrine/<nnnn>, journal/<date>-<slug> }}

> {{ One-sentence summary: what broke, when, and what the user-visible impact was. }}

## Context

{{ What was in flight at the time? What was the last known-good state? }}

## Impact

{{ Who or what was affected. Duration if known. Severity. }}

## Timeline

- {{ HH:MM }} — {{ event }}
- {{ HH:MM }} — {{ event }}
- {{ HH:MM }} — resolved

## Root cause

{{ The actual cause — not the symptom. If root cause is still unknown, say so and mark this a partial post-mortem. }}

## What went well

- {{ fast detection? existing guardrail caught something? }}

## What went poorly

- {{ delayed detection? missing test? surprising coupling? }}

## Action items

- [ ] {{ guardrail test — link to PR }}
- [ ] {{ doctrine update or supersede — if the incident invalidates a prior decision }}
- [ ] {{ procedure update — if the runbook needs revising }}
