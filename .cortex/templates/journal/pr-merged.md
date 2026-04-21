# PR #{{ nnn }} merged — {{ short title }}

**Date:** {{ YYYY-MM-DD }}
**Type:** pr-merged
**Trigger:** T1.9
**Cites:** plans/{{ <slug> }}, journal/{{ <date>-<slug> }}, doctrine/{{ <nnnn>-<slug> }}
**Merge-commit:** {{ full sha }}
**Branch:** {{ <type>/<slug> }}

> {{ One sentence: what shipped, and which plan or decision it closes out. }}

## What shipped

{{ Bulleted list of the user-visible or protocol-visible changes in this PR. Not a diff summary — a changes-to-the-project summary. }}

## Closes / advances

- **Plans:** {{ plan <slug> → shipped | plan <slug> advanced (specify which Work items) }}
- **Doctrine:** {{ new: doctrine/<nnnn> | supersedes: doctrine/<mmmm> | none }}
- **Journal linkage:** {{ entries written during this branch that this merge ratifies }}

## Follow-ups (deferred to future work)

- [ ] {{ item — resolved to: plans/<new-slug> | journal/<date>-<slug> }}

(Per SPEC § 4.2, deferred items must resolve somewhere in the same commit as the merge note.)

## What we'd do differently

{{ Optional — fill when the PR cycle itself surfaced a process lesson (not a code lesson; those go in decision or incident entries). Omit if nothing. }}
