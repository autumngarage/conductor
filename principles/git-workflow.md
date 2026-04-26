# Git Workflow

Normal code changes go through a feature branch + PR + merge. Emergency bypasses are allowed only through the documented emergency path below, and must be disclosed in the next recovery PR. This discipline catches bugs before they land on the default branch and creates an audit trail for every change, while leaving a legible escape hatch for production incidents.

## Never commit on the default branch

**This is the one rule that makes everything else work.** Every code change — including a one-line typo fix, a doc tweak, a version bump, a README edit — starts on a feature branch. Committing directly to `main` (or `master`) bypasses PR review, bypasses Codex review, bypasses the audit trail, and leaves you in a local state that's awkward to untangle without rewriting history someone else may already have pulled.

**The concrete rule for any AI or human working here:** before the first `git commit` of a session, run `git branch --show-current`. If the output is `main` or `master`, stop and branch first. `git checkout -b <type>/<slug>` preserves your staged and unstaged changes, so there's no cost to branching late — but there's real cost to committing to main.

**If you've already committed to main by accident**, don't push. Instead: `git branch <type>/<slug>` to save the work, then `git reset --hard origin/main` to restore the local default branch, then `git checkout <type>/<slug>` to continue. The commits are preserved on the new branch; main is restored to match the remote.

**If you've already pushed**, the standard ship path is broken. Don't try to rewrite history on the default branch. Disclose the slip in the next PR (see "Emergency path" below) and carry on — the commit is now part of history, and the audit trail captures what happened.

**The mechanical guardrails** that back this rule (in touchstone and every bootstrapped project):

- The `no-commit-to-branch` hook in `.pre-commit-config.yaml` is configured with `--branch main --branch master`. It runs at `pre-commit` stage and refuses the commit outright. `git commit --no-verify` bypasses it; that's the documented emergency path, not a daily shortcut.
- GitHub branch protection on the default branch requires the change to go through a PR (`required_pull_request_reviews` with `required_approving_review_count: 0`; direct pushes to `main` are rejected by the server even if the local hook was bypassed). Admin enforcement is left off so the `--no-verify` emergency path remains usable; the audit trail is the backstop.
- The Codex pre-push hook (when installed) is the last line of defense: it runs on default-branch pushes via `merge-pr.sh` and can block unsafe findings before they land.

The three layers are complementary — the local hook catches the honest mistake before it becomes a commit, branch protection catches the deliberate or hook-bypassing push at the server, and the Codex review catches the class of content we explicitly don't want on main.

## The lifecycle

1. **Pull.** `git pull --rebase` on the default branch before starting work.
2. **Branch — before any edit that might become a commit.** `git checkout -b <type>/<short-description>` where `<type>` is one of `feat`, `fix`, `chore`, `refactor`, `docs`. Do this as step one of the work, not as a cleanup step later. If you didn't branch first, `git branch --show-current` before your first commit will catch it.
3. **Loop: change → commit → push.** Each meaningful sub-task gets its own commit and push. Stage explicit file paths (not `git add -A`), write a concise message, push to the open branch. Don't batch a session's worth of changes into one commit at the end — see the "Commit and push frequency" section below.
4. **Ship.** `scripts/open-pr.sh --auto-merge` pushes, creates the PR, runs Codex review, squash-merges, deletes the remote branch, and pulls the updated default branch — all in one command. Use `scripts/open-pr.sh` (without `--auto-merge`) if you want to open the PR without merging.
5. **Clean up.** Delete the local feature branch. Run `scripts/cleanup-branches.sh` periodically for batch hygiene.

## Commit discipline

**One concern per commit.** A commit should describe a single logical change — a feature, a fix, a refactor, a doc update — not a multi-day grab bag. The diff might span many files, but it should be one coherent thought. This is the "atomic commit" principle: every commit is a self-contained unit of intent.

**Why it matters.** Atomic commits pay back continuously: they make code review legible (a reviewer can hold one idea at a time), they make `git blame` and `git log` informative ("this line exists because of fix X" beats "this line exists because of giant-batch Y"), they make `git bisect` able to pin a regression to a single change, and they make `git revert` surgical (you can undo the broken thing without losing the four good things shipped alongside).

**Concise commit messages.** Lead with *what* changed in the subject line. Use the body to explain *why* when the why isn't obvious from the diff. The PR description handles the broader narrative; commit messages are the per-step record.

**Stage explicit file paths.** Avoid `git add -A` or `git add .` — they accidentally stage sensitive files (`.env`, credentials) or large binaries. Naming files makes intent visible at the staging step.

## Commit and push frequency

**Commit at every clear stopping point.** A sub-task is complete and its tests pass — that's a commit boundary. Don't wait until "the whole feature is done." Holding hours of work in an uncommitted working tree creates four problems: (1) reviewers eventually face one giant diff instead of a sequence they can read, (2) any single mistake can lose all of it, (3) other branches can't pull your in-flight work, and (4) you lose the per-step `git log` story that future-you will rely on when debugging months later.

**Push after every commit.** Local commits are not durable. Pushing to the remote (or a personal fork) means your work survives a laptop dying or a `git reset --hard` finger-slip. On a PR branch, pushing also surfaces incremental progress to reviewers, who can comment on individual commits rather than waiting for a final blob.

**Cadence guidance.** A useful rhythm for a focused work session is something like one commit per 30–60 minutes — about as often as you'd take a sip of water. If a session goes longer than that without a commit, ask whether you've passed a clean stopping point and didn't notice. If you can describe what you just finished in one sentence, that's a commit.

**When *not* to commit.** Two cases: (1) a half-finished thought where the code is in a deliberately-broken intermediate state — squash that into a single sensible commit before pushing, or use `git stash` to set it aside; (2) actively-iterating exploration where commits would just be noise — fine to keep working, but reset the timer once you've found the right shape and start committing as you build out from there.

**Why this needs to be a rule, not a vibe.** Without an explicit cadence, "I'll commit when there's something worth committing" reliably becomes "I'll commit at the end of the day," and end-of-day commits are the ones that ship as one fat unreviewable blob. The cadence is the discipline; the discipline is what produces the legible history.

## Background reading

- [Commit Often, Perfect Later, Publish Once — Git Best Practices](https://sethrobertson.github.io/GitBestPractices/) (Seth Robertson) — the canonical "commit early, commit often" essay.
- [Trunk-Based Development](https://trunkbaseddevelopment.com/) — the practice that frequent small commits enable at scale (Google, Facebook, et al.).
- The autumn-garage convention is closer to "tiny PRs to main" than "long-lived feature branches" — short branches, frequent commits, fast review.

## Codex merge review (optional, recommended)

If the project has Codex review configured (see `.codex-review.toml` for policy and the `codex-review` hook in `.pre-commit-config.yaml` for the entry point), a pre-push hook gates default-branch pushes (including squash-merges via `merge-pr.sh`). The mechanism is `stages: [pre-push]` in `.pre-commit-config.yaml`; it skips feature-branch pushes and only activates when the push target is the default branch. **The reviewer is the merge gate** — `scripts/open-pr.sh --auto-merge` is the standard ship path: open PR → reviewer runs → squash-merge → branch deleted, all in one command, no extra approval step.

Behavior:
- Runs `codex exec --full-auto` against the diff vs the default branch
- Auto-fixes only low-risk findings (typos, missing imports, missing null checks, adding logging to empty exception handlers, named constants for unexplained magic numbers); anything that changes business logic or retry/error-handling semantics is reported as a finding for the author to address in another commit before merge
- Blocks the push for unsafe findings (high-scrutiny paths)
- Loops up to `max_iterations` times (default 3)
- Gracefully skips if the Codex CLI isn't installed, printing a visible "review skipped" line so the missing safety boundary isn't silent

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

- **First preference: bundle.** When the user says "ship it all," default to one PR with all the commits. Reviewers prefer one coherent story over a chain; mergers prefer one squash over orchestrating a chain in order.
- **If you must stack:** drop `--auto-merge` on the whole chain. Merge each PR by hand in order, using **merge commit** or **rebase merge** (never squash) for the parent so the child's branch still traces to something on main. `open-pr.sh` will warn if you pass `--base <branch>` + `--auto-merge` together — take the warning seriously.
- **Recover an orphaned child**: re-open the work as a fresh PR against current `main` (the lineage is lost but the diff usually still applies). If the parent's squashed content is already on main, the child's diff is just the child-only changes — which is usually what you wanted anyway.

## Parallel work with worktrees

The default is one branch at a time in the main checkout. When you have N genuinely independent tasks — changes that touch disjoint files and don't logically depend on each other — `git worktree` lets them run concurrently without stepping on each other. The common case is an AI assistant being asked to "do these three things in parallel"; the right move is three branches in three worktrees, not three half-done edits interleaved on one branch.

**The primitive.** From the main checkout, `git worktree add ../<project>-<slug> -b <type>/<slug>` creates a second working tree on a new branch, sharing the same `.git`. Work in it the same way you'd work anywhere — the only difference is that the main checkout stays free to run tests, start another worktree, or keep serving the user's questions while the other tasks run.

**For AI subagents.** When delegating to a subagent that supports worktree isolation (e.g. Claude Code's `Agent` tool with `isolation: "worktree"`), prefer it for any task that writes files. The subagent gets its own checkout, can't clobber siblings, and the worktree is discarded automatically if the agent made no changes. The parent session stays on the base checkout.

**Rules that make it actually parallel.**

- **Disjoint file sets.** If two concurrent tasks touch the same file, they're not parallel — they're a merge conflict delivered on two branches. Before launching, name the file surface each task owns; if they overlap, sequence them.
- **No coordination in flight.** Each worktree ships via its own `scripts/open-pr.sh --auto-merge`. PRs are independent because their branches are independent. If task B needs something from task A's PR before it can merge, that's stacked work — see the stacked-PR section above and run them sequentially instead.
- **Each agent burns its own budget.** Five parallel agents use roughly 5× the tokens and 5× the CPU of one. Start with 2–3 concurrent worktrees, observe, and scale from there. Practitioners report the comfortable cap without heavy orchestration is around 5–6.

**Gotchas.**

- **Untracked files don't follow.** `.env`, local config, and built artifacts live in the working tree, not in `.git`. If the task depends on any of them, copy them into the new worktree after `git worktree add` (or make the setup step recreate them from an example file).
- **Shared `.git`.** Don't run destructive git ops (`git gc --prune=now`, `git worktree remove --force`) while a sibling worktree has uncommitted work — the shared object store is the same object store.
- **Disk cost.** Each worktree is a full working tree. Not an issue for a small repo; matters for large monorepos with generated artifacts.

**Cleanup.** After the PR merges, `git worktree remove <path>` from the main checkout to drop the directory. `scripts/cleanup-branches.sh` already refuses to delete branches currently checked out in worktrees, so it won't fight you — but it also won't remove the worktree directories themselves; that step is manual.

## Emergency path

If a production bug requires immediate action and can't wait for the PR cycle, push directly with `git push --no-verify`. The next PR must include an "Emergency-bypass disclosure" section explaining what was bypassed and why. The convention — not the tooling — is what keeps the discipline.
