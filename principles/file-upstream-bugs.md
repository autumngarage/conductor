# Filing bugs as issues

When you find a bug, file a GitHub issue. Don't silently work around it. Two cases — bugs in the project you're working on, and bugs in the autumngarage tools you depend on — and the discipline is the same in both: write down what's broken so the next person doesn't trip over it.

The cost of filing an issue is two minutes. The cost of the next person rediscovering the same bug is hours.

## Bugs in this project

If you find a bug in the project you're working on, file an issue against that project — even one you can't fix right now, even one you're about to fix in the same session. The issue is the durable record; the fix commit is the resolution. A bug that lives only in conversation gets rediscovered.

When to file:

- **Discovered while doing unrelated work, not fixing now** → file an issue. Note it in the current PR description if relevant ("noticed in passing — see #123").
- **Fixing in the current session** → file the issue first, then close it via the PR with a `Closes #<n>` trailer (see `principles/git-workflow.md` for trailer conventions; `scripts/open-pr.sh` will auto-inject the closing line).
- **Suspect a bug but unsure** → file it as a question / "needs repro" issue rather than letting it sit in chat. Re-discovery later is more expensive than a wrong-flagged issue you close.
- **Hard-won lesson worth capturing** → if the bug taught a generalizable lesson, file the issue and link it from `CLAUDE.md`'s "Hard-Won Lessons" section.

`gh issue create` (no `--repo` flag — it defaults to the current project's repo). Body shape:

```
## Symptom
<what happened, with the exact error / output verbatim>

## Repro
<minimal sequence to trigger it>

## Why this matters
<one paragraph on impact / who or what it blocks>

## Suggested fixes
<cheapest first; optional but appreciated>

## Discovered
<while doing <what>, on YYYY-MM-DD>
```

Search before filing: `gh issue list --search "<keywords>"`. If a matching issue exists, comment with your repro instead of opening a duplicate.

## Bugs in autumngarage tools

If you hit what looks like a bug in an autumngarage tool — actual unexpected behavior in the tool itself, not your project's misuse of it — file the issue **upstream against the tool's repo**, not against your project. The same bug will bite the next user; logging it in the tool's repo is how the ecosystem stays sharp.

This also applies to **workflow friction** caused by the tools, even when it is not a hard crash: excessive token use, poor parallelization, unclear delegation ergonomics, brittle merge-gate behavior, or other agent-delivery inefficiency. If the pain repeats, it is an upstream product issue.

The repos:

- **touchstone** — `bin/touchstone`, the synced `scripts/`, `hooks/`, `principles/`, the bootstrap/update flow → https://github.com/autumngarage/touchstone/issues
- **conductor** — the `conductor` CLI, provider routing, `conductor exec` / `call` / `ask` / `review` → https://github.com/autumngarage/conductor/issues
- **cortex** — `.cortex/journal/`, `.cortex/doctrine/`, the Cortex Protocol → https://github.com/autumngarage/cortex/issues

`gh issue create --repo autumngarage/<tool>` with the same body shape as above. Search first: `gh issue list --repo autumngarage/<tool> --search "<keywords>"`. If a matching issue exists, comment with your repro instead of opening a duplicate.

## When NOT to file

- The bug is in your project's *use* of the tool, not the tool itself → the issue belongs against your project, not the tool.
- The bug is upstream of autumngarage (Anthropic / OpenAI / Google CLIs, `gh`, OS-level git, terminal emulators) → file with that vendor instead.
- It's a question, not a bug → open a discussion or ask in the project's chat surface rather than filing.

The point of this rule is that bugs you don't write down propagate silently. Logging them — wherever they live — is how the work compounds instead of churning.
