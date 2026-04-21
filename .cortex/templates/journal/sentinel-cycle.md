# Sentinel cycle {{ YYYY-MM-DD-HHMM }} — {{ outcome summary }}

**Date:** {{ YYYY-MM-DD }}
**Type:** sentinel-cycle
**Trigger:** T1.6
**Cites:** .sentinel/runs/{{ YYYY-MM-DD-HHMM }}.md, plans/{{ <slug> }}

> {{ One sentence: what the cycle attempted and the outcome. }}

## Cycle inputs

- **Lens:** {{ lens name or goal statement }}
- **Plans consulted:** {{ list from .cortex/plans/*.md at cycle start }}
- **Doctrine consulted:** {{ list loaded into the cycle's context }}
- **Recent Journal:** {{ entries the cycle read for continuity }}

## Work performed

{{ Bulleted list of changes the cycle made — file paths, PR numbers if opened, tests added. }}

## Outcome

- {{ PR opened: #nnn — status }}
- {{ Tests added/changed: <count> }}
- {{ Blockers hit: <description> — or "none" }}

## Carry-forward for next cycle

{{ What state does the next cycle need to know about? Open loops, partial migrations, pending reviews. This section is the continuity substrate for the next Sentinel run. }}
