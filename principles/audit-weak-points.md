# Audit One Weak-Point Class at a Time

When you find a structural bug, don't just fix the one you noticed. The same pattern is almost certainly repeated elsewhere in the codebase — and the copies you don't find will bite you later.

## The methodology

1. **Identify the pattern.** Name it precisely. Examples: "stale data contamination," "hardcoded resource list instead of registry lookup," "file-persisted state on a read-only filesystem," "hardcoded absolute values instead of ratios."

2. **Search until the reviewed surface is explicit.** Use grep, AST tools, or an exploration agent to find instances of the pattern. State what you searched (queries, tools, directories) and what you intentionally left out of scope. "Exhaustive" is unverifiable; "reviewed and bounded" is. The most dangerous instances are the ones in code paths you weren't looking at, so cast wide and make your coverage legible.

3. **Produce a ranked punch-list.** Sort by production impact, not by how easy they are to fix. The instance that silently corrupts data in a hot path matters more than the one in a rarely-used utility.

4. **Fix in tiers, and track the tail.** Start with the highest-impact instances. Don't try to fix everything in one PR — large blast radius increases review risk and rollback cost. If you split the fix across PRs, commit the ranked list somewhere durable (issue, ADR, follow-up task) so the lower-priority instances don't get abandoned. Prefer landing the guardrail (step 6) in the first PR — it stops new copies while you work through the existing ones.

5. **Reset contaminated state where filtering doesn't work.** Some derived state (trained models, accumulated statistics, cached computations) can't be "filtered" to exclude pre-fix data — it has to be rebuilt from scratch. Identify these cases and handle them explicitly.

6. **Add a guardrail.** Write a test or lint rule that catches the next instance of the pattern before it ships. This is the step that turns a one-time fix into a permanent improvement. Options:
   - AST-based test that scans for the anti-pattern
   - Lint rule (custom or built-in)
   - Integration test that exercises the failure mode
   - Import-time assertion that validates invariants

## Why this matters

Without the audit, you fix one bug but leave N copies alive. Without the guardrail, the pattern re-emerges the next time someone writes similar code. The audit + guardrail combination is what turns bug-fixing from whack-a-mole into systematic improvement.
