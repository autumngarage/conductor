# Bug Triage: Pattern Check Before Fix Design

Bug reports describe symptoms. Symptoms are the *result* of a root cause, not the root cause itself. When a report arrives, the first action is to understand the failure class — never to start designing the fix the report literally asks for. Building exactly what the report requested produces band-aids: the reporter saw one instance of the failure and described a fix that would handle that instance, not the class.

This is a hard prerequisite, not a preference. Every bug-fix PR must show it ran.

## When this fires

The trigger is the moment a bug enters scope. Pattern-match on:

- A GitHub issue referenced by number ("#460", "look at #459", "claim this issue")
- A `Closes #N` / `Fixes #N` / `Refs #N` reference in a brief
- A user message naming a failure observed in production ("X is broken", "exec keeps hitting the cap", "review is stalling")
- A bug-fix task arriving via `conductor exec` or any agent-delegation surface
- The phrase "fix this bug" or "let's address this"

At that moment, before designing anything, run the process below. Do not skip even when the fix "looks easy" — the easy-looking fixes are the ones that ship as band-aids.

## The process

1. **Capture the literal failure.** What was observed? What was expected? Command, provider, route, version. State it back in one sentence.

2. **Scan recent issues for related failures.** Run this exact command:

   ```bash
   gh issue list --state all --limit 30 \
     --json number,title,state,createdAt,closedAt,closedByPullRequestsReferences \
     --jq 'sort_by(.number) | reverse | .[] | "#\(.number) [\(.state)] \(.title)"'
   ```

   Skim titles for the same area (routing, exec lifecycle, telemetry, commit-boundary, etc.). Pull bodies of the 2–5 most related issues. The question to answer: is this report isolated, or part of a cluster?

3. **Name the root cause hypothesis explicitly.** Write it down in the PR body or the conversation. If multiple recent issues share a root cause, the fix must address the shared cause — not each instance separately.

4. **Choose: root-cause fix, or scoped symptom patch?** Root-cause fixes are the default. Symptom patches are allowed only under the next section — never silently.

## When a symptom patch is the right call

Time pressure, scope, or risk sometimes make a symptom patch the right scoped choice. When that's the case, the PR must:

- State explicitly in the description that it is a symptom patch.
- Name the root cause and what a proper fix would require.
- Open or link a follow-up issue tracking the root cause.

This matches **No band-aids** in [engineering-principles.md](engineering-principles.md). That principle covers *implementation*. This file covers the *triage step that happens before implementation*, so the band-aid-vs-root-cause choice is made consciously instead of by accident.

## Smell tests: signs the proposed fix is a band-aid

If the proposed fix matches any of these, stop and reconsider:

- **Adjusts a single constant.** Raises a cap, extends a timeout, adds another retry, bumps a threshold. The next failure hits the next-bigger version of the same boundary. (Recent example: PR #464 raised `--effort high` iteration cap from 60→100 to close #459; the root cause is that exec has no commit-checkpoint contract and no internal run-health awareness, not that the cap was wrong.)
- **Adds a warning instead of preventing the failure.** "Warn when X happens" lets X happen indefinitely. (Recent example: PR #466 warns when pre-commit hooks auto-stage files outside the brief — closing #460 without giving exec a real commit-boundary contract.)
- **Adds a provider-specific routing rule, classification code, or guard.** Each provider's quirks belong in its adapter, not as another shared-code branch — see CLAUDE.md "Hard-Won Lessons" #3. The recurring routing-fallback issues (#399, #410, #411, #412, #420, #423, #427, #431, #434, #438, #445, #461) are the canonical example of this anti-pattern.
- **PR title shape: "warn when…", "skip if…", "retry on…", "raise the cap…".** These shapes correlate strongly with symptom patches.
- **Touches no design surface.** Only a value, a string, or a conditional. Root-cause fixes usually touch interfaces or contracts.

## Why this is automatic, not on-demand

A reader who already noticed the band-aid pattern doesn't need this principle. The slip happens when nobody is watching for it — a tired session, a focused implementer, a bug report that "looks easy." Making the triage step a hard prerequisite catches the slip when nobody is asking the question. The operator should not have to remind us; the principle should fire on its trigger every time.
