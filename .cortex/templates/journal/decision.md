# {{ Title — active voice, names the decision }}

**Date:** {{ YYYY-MM-DD }}
**Type:** decision
**Trigger:** {{ T1.1 | T1.4 | T1.5 | T1.8 | T2.1 | T2.2 | T2.3 | T2.4 | T2.5 | — (human-authored) }}
**Cites:** {{ plans/<slug>, doctrine/<nnnn>-<slug>, journal/<date>-<slug> }}

> {{ One-sentence summary of what was decided. }}

## Context

{{ What was the situation? What evidence or constraint prompted the decision? Cite specific files, PRs, metrics. }}

## What we decided

{{ The decision itself, stated as a claim in active voice. If multiple options were weighed, name them and say why the chosen one won. }}

## Consequences / action items

- [ ] {{ Concrete follow-up — link to issue/PR if filed }}
- [ ] {{ Guardrail test or doc update, if applicable }}

<!--
Optional flags (remove the lines that don't apply):
**failed-approach:** true   # T2.2 — journal a dead-end that taught something
**investigation:** true      # T2.3 — surprise-about-existing-code hypothesis
**inferred-invariant:** true # T2.5 — a constraint the agent is relying on
-->
