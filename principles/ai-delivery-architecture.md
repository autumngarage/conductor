# AI Delivery Architecture

This document owns Touchstone's end-to-end delivery architecture for AI-authored changes. Other docs should link here instead of restating the workflow in detail.

## Target Flow

```text
Human user
  |
  | asks for a change
  v
Driver AI
  |
  | reads Touchstone steering
  | - checks repo state
  | - pulls main
  | - creates a feature branch before edits
  | - inspects the relevant code
  v
Implementation
  |
  | Driver AI edits files
  | may delegate bounded work through Conductor
  | may run focused local checks while developing
  v
Commit
  |
  | stage explicit paths
  | create focused commit(s)
  v
Open PR
  |
  | push feature branch
  | create GitHub PR
  | cheap push hooks only
  | no required LLM review here
  v
Merge Gate
  |
  | 1. Deterministic preflight
  |    - format/lint/static checks
  |    - non-LLM tests
  |
  v
Conductor LLM Review / Fix Loop
  |
  | 2. LLM review happens only through Conductor
  |    - Conductor reviews the PR diff against base
  |    - Conductor may apply safe auto-fixes when allowed
  |    - fixes are committed to the PR branch
  |    - loop until Conductor returns CLEAN or BLOCKED
  |
  v
Post-Fix Deterministic Gate
  |
  | 3. If Conductor changed HEAD:
  |      run deterministic checks again on the new HEAD
  |    Else:
  |      reuse the preflight result for the unchanged HEAD
  v
Merge PR
  |
  | squash merge
  | sync local main
  | clean up branch/worktree
  v
Human user
  |
  | receives concise outcome
```

## Required Invariants

- Every change reaches `main` through a GitHub PR unless the documented emergency path is used.
- The exact commit merged has passed deterministic checks after its last mutation.
- The exact commit merged has passed Conductor review after its last mutation.
- LLM review uses Conductor as the only model access path. Driver CLIs do not call provider-specific review commands directly.
- PR creation is not the expensive gate. It should be fast enough to create reviewable work early.
- Feature-branch push is not the expensive gate. It should preserve cheap local guardrails without running full test suites or LLM review by default.
- Merge is the expensive gate. It is the one place where required deterministic checks and required Conductor review run.
- A deterministic check result may be reused only when the cache key includes the base ref, head commit, relevant config, and checker version/input boundary.

## Driver AI Responsibilities

The driver AI is Claude Code, Codex, Gemini CLI, or another AGENTS.md-native coding agent. The driver owns repo operations:

- branch before editing
- inspect and modify files
- run focused checks during implementation
- stage explicit file paths
- commit coherent changes
- open the PR
- invoke the merge gate
- explain the outcome to the user

The driver may use Conductor for bounded implementation, research, or review work, but Conductor does not own the branch-to-merge lifecycle.

## Conductor Responsibilities

Conductor is the LLM router for review and delegated model work.

- Required LLM review runs through Conductor at the merge gate.
- Conductor chooses the configured provider/model and handles provider fallback.
- Conductor may apply safe fixes only when the review mode and path policy allow it.
- Conductor findings are either fixed and committed on the PR branch, or block the merge.

Provider-specific commands such as direct Claude/Codex/Gemini review invocations are not part of the required review architecture.

## Agent Swarms And Worktrees

Parallel file-writing agents use worktrees by default.

```text
Driver AI
  |
  | decides work is parallelizable
  v
Agent swarm
  |
  | one worker per worktree
  | each worker gets an explicit file/module scope
  | workers commit only in their own worktree
  v
Driver AI integration
  |
  | integrates candidate changes into the primary PR branch
  | resolves conflicts
  | owns final checks, PR, merge gate, and cleanup
```

Rules:

- Use worktrees for file-writing parallel agents.
- Give every worker a bounded task and explicit file ownership.
- Workers must not edit outside their assigned scope.
- Workers must not revert or overwrite another worker's work.
- Workers may produce candidate changes; only the driver integrates them into the PR that enters the merge gate.
- No worker opens or merges the final PR unless the driver explicitly assigns that role.
- Clean up worktrees after merge or abandonment.

## Implementation Scope

The streamlined build should make the scripts match this architecture:

1. `open-pr.sh` creates PRs cheaply. With `--auto-merge`, it should not run PR-open advisory Conductor review.
2. Feature-branch pre-push should keep cheap guardrails only. Full validation belongs to the merge gate.
3. `merge-pr.sh` owns the required pipeline:
   - deterministic preflight for the PR base/head
   - Conductor review/fix loop
   - deterministic postflight only if the review loop changed HEAD
   - squash merge after both invariants hold
4. Review and preflight markers should key on base/head/config so repeated operations reuse valid results without hiding stale state.
5. Docs, templates, tests, and issue guidance should describe this merge-gate architecture consistently.
