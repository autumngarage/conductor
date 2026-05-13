# Git Workflow

Normal code changes go through a feature branch + PR + merge. Emergency bypasses are allowed only through the documented emergency path below, and must be disclosed in the next recovery PR. This discipline catches bugs before they land on the default branch and creates an audit trail for every change, while leaving a legible escape hatch for production incidents.

## Never commit on the default branch

**This is the one rule that makes everything else work.** Every code change — including a one-line typo fix, a doc tweak, a version bump, a README edit — starts on a feature branch. Committing directly to `main` (or `master`) bypasses PR review, bypasses Conductor review, bypasses the audit trail, and leaves you in a local state that's awkward to untangle without rewriting history someone else may already have pulled.

**The concrete rule for any AI or human working here:** before the first edit of a tracked file in a session — `Edit`, `Write`, or any tool that mutates a file under git — run `git branch --show-current`. If the output is `main` or `master`, stop and branch first. `git checkout -b <type>/<slug>` preserves your staged and unstaged changes, so there's no cost to branching late — but there's real cost to discovering the mistake at commit time after batching several files of work.

**Why the trigger is at edit time, not commit time.** The earlier version of this rule said "check before your first commit." That phrasing reliably fails — for LLMs especially, but for humans in flow too. The actual sequence that produces the failure mode is: (1) agent reads a file on `main`, edits it; (2) edits another, and another; (3) reaches commit, the `no-commit-to-branch` hook refuses, and now the agent has to recover the accumulated work onto a new branch. The recovery is mechanically fine and documented below — but it costs more than the one `git branch --show-current` would have, and the slip leaves visible noise in the session record. The "before-commit" trigger is technically correct and practically useless: by the time the agent reaches commit, it's already on the wrong branch with work to relocate. The "before-edit" trigger moves the cost from *discovered at commit, recover* to *discovered before any work, prevent*.

**If you've already committed to main by accident**, don't push. Instead: `git branch <type>/<slug>` to save the work, then `git reset --hard origin/main` to restore the local default branch, then `git checkout <type>/<slug>` to continue. The commits are preserved on the new branch; main is restored to match the remote.

**If you've already pushed**, the standard ship path is broken. Don't try to rewrite history on the default branch. Disclose the slip in the next PR (see "Emergency path" below) and carry on — the commit is now part of history, and the audit trail captures what happened.

**The mechanical guardrails** that back this rule (in touchstone and every bootstrapped project):

- The `no-commit-to-branch` hook in `.pre-commit-config.yaml` is configured with `--branch main --branch master`. It runs at `pre-commit` stage and refuses the commit outright. `git commit --no-verify` bypasses it; that's the documented emergency path, not a daily shortcut.
- GitHub branch protection on the default branch requires the change to go through a PR (`required_pull_request_reviews` with `required_approving_review_count: 0`; direct pushes to `main` are rejected by the server even if the local hook was bypassed). Admin enforcement is left off so the `--no-verify` emergency path remains usable; the audit trail is the backstop.
- The Conductor-backed review hook (when installed) is the last line of defense: it runs on default-branch pushes via `merge-pr.sh` and can block unsafe findings before they land.

The three layers are complementary — the local hook catches the honest mistake before it becomes a commit, branch protection catches the deliberate or hook-bypassing push at the server, and the Conductor review catches the class of content we explicitly don't want on main.

## The lifecycle

1. **Pull.** `git pull --rebase` on the default branch before starting work.
2. **Branch — before any edit that might become a commit.** `git checkout -b <type>/<short-description>` where `<type>` is one of `feat`, `fix`, `chore`, `refactor`, `docs`. Do this as step one of the work, not as a cleanup step later. The check is `git branch --show-current` *before your first edit* — see "Never commit on the default branch" above for why edit time and not commit time.
3. **Check the tree before changing it.** Run `git status --short` and `git branch --show-current` before starting implementation. If the tree is dirty with unrelated user changes, do not stash them and do not auto-commit on the user's behalf. Ask how to proceed, or branch around the changes when the file surfaces are disjoint. `git stash` is hidden multi-agent state, not a coordination mechanism.
4. **Loop: change → commit → push.** Each meaningful sub-task gets its own commit and push. Stage explicit file paths (not `git add -A`), write a concise message, push to the open branch. Don't batch a session's worth of changes into one commit at the end — see the "Commit and push frequency" section below.
5. **Ship.** `scripts/open-pr.sh --auto-merge` pushes, creates the PR, runs the merge-gate pipeline, squash-merges after the gate is clean, deletes the remote branch, and pulls the updated default branch — all in one command. Use `scripts/open-pr.sh` (without `--auto-merge`) if you want to open the PR without merging. The canonical architecture is [AI Delivery Architecture](ai-delivery-architecture.md): deterministic preflight, Conductor LLM review/fix, and deterministic postflight only when the review loop changed HEAD.
6. **Clean up.** Delete the local feature branch. Run `scripts/cleanup-branches.sh` periodically for batch hygiene.

### Touchstone CLI auto-sync

When a brew-installed `touchstone` CLI runs a write-capable command inside a Touchstone-aware project, it compares the project's recorded `.touchstone-version` with the installed Touchstone version. Minor and major version drift triggers the same `touchstone update` path before the requested command so `principles/`, `hooks/`, and `scripts/` stay current; patch-only semver drift does not auto-sync project files. Source checkouts record git SHAs instead of semver, so any SHA drift still syncs.

If the project worktree is dirty, auto-sync compares dirty paths with the planned Touchstone write set and skips only when they overlap; unrelated dirty paths are reported in one line and sync proceeds. The user's pending work is never stashed, overwritten outside that planned write set, or committed separately. `touchstone version`, help, status, diff, doctor, list, changelog, and other read-only commands do not trigger auto-sync.

When a sync attempt skips, Touchstone appends a project-local audit entry to `.git/touchstone/sync-skips.jsonl`. Later `touchstone <subcmd>` invocations warn when the project is still behind the installed Touchstone and the skip trail is persistent enough to indicate drift; the warning is informational and never blocks the subcommand.

Use `touchstone --no-auto-sync <subcommand>` to disable project auto-sync for one invocation. Set `sync_auto=false` in `.touchstone-config` to opt the project out persistently. Set `TOUCHSTONE_NO_AUTO_UPDATE=1` to disable both CLI self-update and project auto-sync. Set `TOUCHSTONE_NO_AUTO_PROJECT_SYNC=1` to keep CLI self-update enabled but disable only the per-project sync. Set `TOUCHSTONE_FORCE_OVERLAP=1` to force project sync through dirty paths that overlap planned Touchstone writes. Set `TOUCHSTONE_NO_DRIFT_WARNING=1` to suppress skipped-sync drift warnings in scripted output.

## Commit discipline

**One concern per commit.** A commit should describe a single logical change — a feature, a fix, a refactor, a doc update — not a multi-day grab bag. The diff might span many files, but it should be one coherent thought. This is the "atomic commit" principle: every commit is a self-contained unit of intent.

**Why it matters.** Atomic commits pay back continuously: they make `git blame` and `git log` informative ("this line exists because of fix X" beats "this line exists because of giant-batch Y"), they make `git bisect` able to pin a regression to a single change, they make `git revert` surgical (you can undo the broken thing without losing the four good things shipped alongside), and they let the Conductor merge review reason about one semantic change at a time instead of a tangle.

**Concise commit messages.** Lead with *what* changed in the subject line. Use the body to explain *why* when the why isn't obvious from the diff. The PR description handles the broader narrative; commit messages are the per-step record.

**Issue-closing trailers.** When a commit is meant to resolve a GitHub issue, add a body trailer such as `Closes-issue: #123` (or `Closes: #123`, `Fixes: #123`, `Refs: #123`). `scripts/open-pr.sh` scans commits unique to the branch and injects a `Closes #123` line into the PR body, so the issue auto-closes when the PR merges.

**Issue reconciliation before PR.** Treat issue state as part of delivery, not cleanup after the fact. Before opening the PR, make a short ledger of every issue you touched: fixed, partially fixed, made stale, or investigated and left open. Fixed issues must be represented by closing trailers or explicit `Closes #N` lines in the PR body. Partial fixes get `Refs #N` plus an issue comment that names what landed and what remains. Stale issues get a comment with the commit/test evidence before closing. The invariant is simple: after a merge, a reader scanning GitHub issues should not have to infer whether a shipped fix was forgotten, partial, or unrelated.

**Stage explicit file paths.** Avoid `git add -A` or `git add .` — they accidentally stage sensitive files (`.env`, credentials) or large binaries. Naming files makes intent visible at the staging step.

## Commit and push frequency

**Commit at every clear stopping point.** A sub-task is complete and its tests pass — that's a commit boundary. Don't wait until "the whole feature is done." Holding hours of work in an uncommitted working tree creates four problems: (1) the Conductor merge review faces one giant diff instead of a legible sequence, (2) any single mistake can lose all of it, (3) other branches can't pull your in-flight work, and (4) you lose the per-step `git log` story that future-you will rely on when debugging months later.

**Push after every commit.** Local commits are not durable. Pushing to the remote (or a personal fork) means your work survives a laptop dying or a `git reset --hard` finger-slip. On a PR branch, pushing also makes incremental work visible from another worktree or session, so a fallback agent (or future-you in a new shell) can pick up the in-flight state without rebuilding it.

**Cadence guidance.** A useful rhythm for a focused work session is something like one commit per 30–60 minutes — about as often as you'd take a sip of water. If a session goes longer than that without a commit, ask whether you've passed a clean stopping point and didn't notice. If you can describe what you just finished in one sentence, that's a commit.

**When *not* to commit.** Two cases: (1) a half-finished thought where the code is in a deliberately-broken intermediate state — squash that into a single sensible commit before pushing; (2) actively-iterating exploration where commits would just be noise — fine to keep working, but reset the timer once you've found the right shape and start committing as you build out from there.

**No checkpoint commits in review artifacts.** Local recovery commits are fine when they keep an experiment recoverable, but pushed `WIP:`, `checkpoint`, or deliberately broken commits do not belong on real review branches. If you use them locally, squash or fix them before opening the PR or marking it ready.

**Why this needs to be a rule, not a vibe.** Without an explicit cadence, "I'll commit when there's something worth committing" reliably becomes "I'll commit at the end of the day," and end-of-day commits are the ones that ship as one fat unreviewable blob. The cadence is the discipline; the discipline is what produces the legible history.

## Background reading

- [Commit Often, Perfect Later, Publish Once — Git Best Practices](https://sethrobertson.github.io/GitBestPractices/) (Seth Robertson) — the canonical "commit early, commit often" essay.
- [Trunk-Based Development](https://trunkbaseddevelopment.com/) — the practice that frequent small commits enable at scale (Google, Facebook, et al.).
- The autumn-garage convention is closer to "tiny PRs to main" than "long-lived feature branches" — short branches, frequent commits, fast review.

## Conductor merge review (optional, recommended)

If the project has AI review configured (see `.touchstone-review.toml` for policy and the `conductor-review` hook in `.pre-commit-config.yaml` for the entry point), the required LLM review belongs to the merge gate. Legacy `.codex-review.toml` configs and `codex-review` hooks still work as compatibility names. The hook delegates model access to Conductor, so the reviewer may be Claude, Codex, Gemini, a local model, or another configured provider. Feature-branch pushes should stay cheap; the expensive path is `scripts/open-pr.sh --auto-merge`: open PR → deterministic preflight → Conductor review/fix loop → deterministic postflight when needed → squash-merge → branch deleted. There is no separate required PR-open advisory review in the core architecture.

**AI review is advisory.** It does not replace deterministic checks (lint, tests, type checking). It catches semantic bugs and policy violations that automated tools miss; it does not guarantee correctness.

**Fail-open by default.** When the review infrastructure fails — timeout, missing Conductor CLI, no provider configured, or unparseable reviewer output — the hook allows the push rather than blocking it. This is the correct trade-off: a Conductor outage during a critical week should not freeze all merges. The cost is that the AI safety net is absent during those events, so each fail-open event is made explicitly visible:

- A `[fail-open:<code>]` line is written to stderr naming exactly why the safety net opened.
- A structured entry is appended to `~/.touchstone-review-log` for audit and skip-rate monitoring.

The fail-open taxonomy codes are:

| Code | Cause |
|------|-------|
| `FAIL_OPEN_TIMEOUT` | Reviewer exceeded an explicit `CODEX_REVIEW_TIMEOUT` / `timeout` budget |
| `FAIL_OPEN_PARSE_ERROR` | Reviewer output contained no valid sentinel line |
| `FAIL_OPEN_DEPENDENCY_MISSING` | Conductor CLI not found on PATH |
| `FAIL_OPEN_PROVIDER_UNAVAILABLE` | Conductor installed but no provider configured |
| `FAIL_OPEN_REVIEWER_ERROR` | Reviewer crashed or returned non-zero |

To make infra failures fatal instead, set `on_error = "fail-closed"` in `.touchstone-review.toml`.

Behavior:
- `merge-pr.sh` invokes `scripts/conductor-review.sh`, which routes LLM review through Conductor against the diff vs the default branch
- Auto-fixes only low-risk findings (typos, missing imports, missing null checks, adding logging to empty exception handlers, named constants for unexplained magic numbers); anything that changes business logic or retry/error-handling semantics is reported as a finding for the author to address before merge
- Blocks merge for unsafe findings (high-scrutiny paths)
- Loops up to `max_iterations` times (default 3)
- Fails open on infra errors with a visible `[fail-open:<code>]` stderr line and an audit log entry (see codes above), unless the project config sets `on_error = "fail-closed"`

### Scope-aware preflight

Merge gates run deterministic preflight in diff mode:
`bash lib/preflight.sh --diff origin/<default-branch>`. The invariant is that
preflight checks the files changed by the PR, not the whole project, unless
`--all-files` is explicitly passed.

In diff mode, shellcheck, shfmt, markdownlint, and actionlint receive only
changed files with matching extensions or paths. Touchstone's own self-tests
remain full-project because Touchstone changes propagate to downstream
projects. In non-Touchstone projects, project-level validate commands are not
run in diff mode unless changed `tests/test-*.sh` files can be executed
directly; use `bash lib/preflight.sh --all-files` for repo-wide audits.
`TOUCHSTONE_NO_PREFLIGHT=1` remains the emergency escape hatch.

If the reviewer itself wedges after the branch has already recorded a clean review iteration, use `scripts/merge-pr.sh <pr-number> --bypass-with-disclosure="<reason>"` instead of dropping to raw `gh pr merge`. The bypass refuses fresh branches, prints a visible warning, comments on the PR with the reason, and adds a `Reviewer-bypass: <reason>` trailer to the squash commit when GitHub accepts the supplied merge body. This is for a stalled reviewer gate on an already-reviewed branch, not for bypassing substantive findings.

Prefer a different model or provider for AI review than the one that authored the change, when Conductor has one available. Deterministic checks — format, lint, typecheck, tests, and project-specific validators — still run before AI review; model diversity complements those checks, it does not replace them.

If the project enables GitHub merge queue, `open-pr.sh --auto-merge` should enqueue the reviewed PR instead of bypassing the queue. Never use `--admin` to skip required checks or queue policy. Treat queue removal or repeated queue failure as a blocker that needs diagnosis before retrying.

## Periodic branch hygiene

```bash
scripts/cleanup-branches.sh              # dry-run first
scripts/cleanup-branches.sh --execute    # actually delete merged branches
```

The cleanup script never deletes the default branch, the current branch, branches checked out in worktrees, or branches with unique unmerged commits. Ancestor-merged branches are deleted with `git branch -d` as defense in depth (git refuses unmerged work). Squash-merged branches — the common case with `open-pr.sh --auto-merge`, where the commits on your feature branch aren't ancestors of the default branch but their changes are already applied — are detected via tree equivalence: every file the branch changed relative to the merge-base must match the default branch's current content. This uniformly handles squash, rebase, and cherry-pick shapes, and correctly rejects the add-then-revert case (where history-based patch-id lookups would false-positive on the add commit). Once equivalence is confirmed, the branch is force-deleted with `git branch -D`.

## Stacked PRs (and why they usually aren't worth it)

A stacked PR is a PR whose base branch is another open PR's branch instead of the default branch. The goal: split a large change into a chain where each step is reviewable on its own, with the child's diff narrowed to "only the new commits on top of the parent." `open-pr.sh --base <parent-branch>` opens one.

**The gotcha that orphans them.** `gh pr merge --squash` (the `--auto-merge` default) rewrites the parent's history into a single squash commit on the default branch — which means the child branch no longer traces to anything upstream. GitHub notices the orphan and **closes the child PR** instead of rebasing it onto the new default branch. The child's code is not lost (the branch still exists on remote), but the PR is marked closed-without-merge and any review discussion on it is effectively abandoned. You've seen this fire before (sentinel PRs #49/#50/#51 on 2026-04-16).

**What to do.**

- **First preference: bundle.** When the user says "ship it all," default to one PR with all the commits. The Conductor merge review reasons more cleanly about one coherent story than a chain; mergers prefer one squash over orchestrating a chain in order.
- **If you must stack:** drop `--auto-merge` on the whole chain. Merge each PR by hand in order, using **merge commit** or **rebase merge** (never squash) for the parent so the child's branch still traces to something on main. `open-pr.sh` will warn if you pass `--base <branch>` + `--auto-merge` together — take the warning seriously.
- **Recover an orphaned child**: re-open the work as a fresh PR against current `main` (the lineage is lost but the diff usually still applies). If the parent's squashed content is already on main, the child's diff is just the child-only changes — which is usually what you wanted anyway.

## Claiming issues before agent dispatch

Before spawning a coding agent — Claude Code subagent, Conductor `exec` worker, Codex CLI, or any other — to work on a GitHub issue, **claim it first**. Set the assignee, post a one-line dispatch comment, then spawn the agent. The cost is ten seconds per issue; the cost of skipping it is two agents picking up the same issue and shipping competing PRs.

**The mechanical steps.**

```bash
bash scripts/claim-issue.sh <n>
```

Under the hood this uses the same GitHub API flow (claim + dispatch comment), equivalent to:

```bash
gh issue edit <n> --add-assignee @me
gh issue comment <n> --body "Wave N Lane X dispatched. Branch \`<branch>\`, worktree at \`<path>\`. <agent type> implementing. PR will land via \`open-pr.sh --auto-merge\`."
```

Then start the agent. Not after.

**Why this is a rule.**

Without it, three failure modes recur in agent-driven workflows:

1. **Duplicate work.** Two agents (or two collaborators, or a future you in another session) pick up the same issue from the open queue and ship competing PRs. The first to merge wins; the second rebases into conflict or closes orphaned. Both burned budget.
2. **No in-progress signal.** A reader scanning open issues can't tell which are actively being worked vs which are dormant. Triage decays — a two-week-old "open" issue might be thirty seconds from a PR or completely abandoned, with no way to tell from outside the thread.
3. **Lost lineage on bypass.** If the PR that closes the issue ends up using `merge-pr.sh --bypass-with-disclosure` or hits review-tooling drama, the dispatch comment is the only record on the issue thread that ties the work back to a specific lane, agent, branch, and worktree. That breadcrumb matters when something goes wrong months later.

The discipline is small. The recovery from skipping it is large.

**When to unassign.**

If you decide not to ship — the work turns out to be wrong, the approach pivots, the agent stalls and you stand it down — unassign with `gh issue edit <n> --remove-assignee @me` and post a "stood down — <reason>" comment. Leaving stale assignments is worse than no assignment at all; readers will assume the issue is being worked when it isn't.

**When this rule does NOT apply.**

- **Issues you're proposing or analyzing, not implementing.** You're researching whether something is even worth doing — no claim. Claim only when an agent (or you) is actively starting implementation.
- **Drive-by fixes during unrelated work.** A one-line typo fix on the way to something else doesn't need a claim — but if it warrants its own commit, it warrants a `Closes-issue:` trailer at minimum.

**For multi-issue bundles.**

When one lane closes multiple issues (e.g., Wave 1's Lane A bundling shfmt + markdownlint + actionlint into one branch), claim and comment on all of them with the same lane / branch reference. The dispatch comment becomes the per-issue audit thread; the bundling is visible from any of them.

**Deterministic enforcement.**

Three layers back the convention so a missed claim doesn't reach merge silently:

- **`scripts/claim-issue.sh`** is the canonical claim path. It does the claim + dispatch comment in one step, and detects races (another assignee appeared between the API read and write — back off, exit non-zero so the dispatching agent knows not to spawn a worker). Use it instead of raw `gh issue edit` when an agent is about to start work.
- **`scripts/open-pr.sh`** runs `scripts/issue-claim-check.sh` locally before creating a new PR and before auto-merging an existing PR. If the PR body closes an open issue that is not assigned to the PR author, it fails before spending merge-gate review time.
- **`.github/workflows/issue-claim-check.yml`** runs on every `pull_request` open/edit/synchronize. It parses `Closes #N` / `Fixes #N` / `Resolves #N` / `Closes-issue: #N` from the PR body, fetches each open referenced issue, and fails the check if the PR author is not in the issue's assignees. The failure posts a comment on the PR explaining what to fix.

The local check is the fast path: it stops the common mistake before a PR exists or before merge review runs. The CI check is the hard backstop: even if an agent bypasses the local script, a PR that tries to close an unclaimed issue fails its checks and won't auto-merge.

**Bypass token: `[skip-claim-check]`.**

For documented exemptions (drive-by typo fix, true emergency, sandbox PR you don't intend to merge), put the literal token `[skip-claim-check]` somewhere in the PR body. The CI check sees the token and skips with a workflow-run note, leaving an audit trail. Like other bypasses (`git push --no-verify`, `merge-pr.sh --bypass-with-disclosure`), this is a documented escape hatch — not a daily shortcut.

## Parallel work with worktrees

File-writing subagents must use isolated worktrees unless explicitly waived. The default is isolation; flat shared-checkout fan-out is the exception.

The default for a single driver is one branch at a time in the main checkout. When you have N genuinely independent tasks — changes that touch disjoint files and don't logically depend on each other — `git worktree` lets them run concurrently without stepping on each other. The common case is an AI assistant being asked to "do these three things in parallel"; the right move is three branches in three worktrees, not three half-done edits interleaved on one branch.

For the full fan-out playbook — slice manifests, file ownership, parent orchestration, concurrency caps, `.worktreeinclude`, and cleanup rules — see [agent-swarms.md](agent-swarms.md). This section defines the git workflow default; the swarm guide defines the operating model.

**The primitive.** From the main checkout, `git worktree add ../<project>-<slug> -b <type>/<slug>` creates a second working tree on a new branch, sharing the same `.git`. Work in it the same way you'd work anywhere — the only difference is that the main checkout stays free to run tests, start another worktree, or keep serving the user's questions while the other tasks run.

**For AI subagents.** When delegating to a subagent that supports worktree isolation (e.g. Claude Code's `Agent` tool with `isolation: "worktree"`), prefer it for any task that writes files. The subagent gets its own checkout, can't clobber siblings, and the worktree is discarded automatically if the agent made no changes. The parent session stays on the base checkout.

**Rules that make it actually parallel.**

- **Disjoint file sets.** If two concurrent tasks touch the same file, they're not parallel — they're a merge conflict delivered on two branches. Before launching, name the file surface each task owns; if they overlap, sequence them.
- **No coordination in flight.** Each worktree ships via its own `scripts/open-pr.sh --auto-merge` when the slice is independently shippable, or reports back to a parent-owned aggregate PR when the feature only makes sense as a unit. If task B needs something from task A's PR before it can merge, that's stacked work — see the stacked-PR section above and run them sequentially instead.
- **Each agent burns its own budget.** Five parallel agents use roughly 5× the tokens and 5× the CPU of one. Start with 2–3 concurrent worktrees, observe, and scale from there. Practitioners report the comfortable cap without heavy orchestration is around 5–6.

**Gotchas.**

- **Untracked files don't follow.** `.env`, local config, and built artifacts live in the working tree, not in `.git`. If the task depends on any of them, copy them into the new worktree after `git worktree add` (or make the setup step recreate them from an example file).
- **Shared `.git`.** Don't run destructive git ops (`git gc --prune=now`, `git worktree remove --force`) while a sibling worktree has uncommitted work — the shared object store is the same object store.
- **Disk cost.** Each worktree is a full working tree. Not an issue for a small repo; matters for large monorepos with generated artifacts.

**Cleanup.** Two paths, pick whichever fits the moment:

- **Inline (preferred for fire-and-forget).** Pass `--cleanup-worktree` alongside `--auto-merge` to `scripts/open-pr.sh`. After the PR squash-merges, the helper removes the current feature worktree itself by invoking `git worktree remove` from the default-branch worktree. The worktree is gone before the script returns, so there's nothing to come back to. Failures here are reported as warnings — the merge already happened, cleanup is best-effort.
- **Deferred sweep.** From the main checkout, run `scripts/cleanup-worktrees.sh` (dry-run by default) to preview and `--execute` to remove clean merged-or-equivalent worktrees. Use this when several worktrees accumulated across sessions, or when the inline cleanup couldn't run (dirty tree, etc.).

Do not substitute `rm -rf <worktree-dir>` for `git worktree remove <path>`.
Deleting only the directory can leave stale Git worktree metadata behind; Git
may still treat the missing path as owning the branch and refuse later branch
deletes, checkouts, or merge cleanup. If that already happened, run
`git worktree prune` from a remaining checkout to drop records for missing
paths, then retry the blocked command.

`scripts/cleanup-branches.sh` already refuses to delete branches currently checked out in worktrees, so it won't fight you — but it also won't remove the worktree directories themselves; that is what `cleanup-worktrees.sh` and the inline `--cleanup-worktree` flag are for.

## Emergency path

If a production bug requires immediate action and can't wait for the PR cycle, push directly with `git push --no-verify`. The next PR must include an "Emergency-bypass disclosure" section explaining what was bypassed and why. The convention — not the tooling — is what keeps the discipline.

Do not use `git push --no-verify` for a wedged Conductor merge review when the PR path is otherwise healthy. Use `scripts/merge-pr.sh --bypass-with-disclosure="<reason>"` so the bypass remains in the PR and merge audit trail.
