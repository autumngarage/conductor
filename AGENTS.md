# AGENTS.md — AI Reviewer Guide for conductor

<!-- touchstone:steering:start -->

<!-- This block is generated from TOUCHSTONE.md. `touchstone update` refreshes it.
     Edit content OUTSIDE the markers; touchstone will not touch project-owned content. -->

## Touchstone — Shared Agent Steering

You are an AI agent (Claude Code, Codex, or another driving CLI) working in a Touchstone-bootstrapped project. This block is the universal contract: rules that apply on every turn, plus a routing table to deeper docs you should consult when specific triggers fire. Project-specific guidance lives outside this block in your driver's steering doc (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`).

## Agent Roles And Fallbacks

- **Driving CLI** — Claude Code, Codex, or Gemini CLI. Owns file edits, git state, tests, commits, PR creation, Conductor review invocation, and merge. Drivers are interchangeable; driver fallback is shared-contract fallback — if one is unavailable, another reads the same files and continues.
- **Conductor worker/reviewer router** — the model router used by the driving CLI for review and bounded model work. Conductor can route to Claude, Codex, Gemini, Kimi, Ollama, or other providers, and provider fallback runs across configured backends, but Conductor does not replace the driver's responsibility for the branch → PR → merge-gate review → automerge workflow.

## Engineering principles (always in mind)

Non-negotiable. Every code change is reviewed against them. Full rationale lives in `principles/engineering-principles.md`.

- **No band-aids** — fix the root cause; if patching a symptom, say so explicitly and name the root cause.
- **Keep interfaces narrow** — expose the smallest stable contract; don't leak storage shape, vendor SDKs, or workflow sequencing.
- **Derive limits from domain** — thresholds and sizes come from input/config/named constants; test at small, typical, and large scales.
- **Derive, don't persist** — compute from the source of truth; persist derived state only with documented invalidation + rebuild path.
- **No silent failures** — every exception is re-raised or logged with debug context. No `except: pass`, no swallowed errors.
- **Every fix gets a test** — bug fix includes a regression test that runs in CI and fails on the old code.
- **Think in invariants** — name and assert at least one invariant for nontrivial logic.
- **One code path** — share business logic across modes; confine mode-specific differences to adapters, config, or the I/O boundary.
- **Version your data boundaries** — when a model/algorithm/source change affects decisions, version the boundary; don't aggregate across.
- **Separate behavior changes from tidying** — never mix functional changes with broad renames, formatting sweeps, or unrelated refactors.
- **Make irreversible actions recoverable** — destructive operations need dry-run, backup, idempotency, rollback, or forward-fix plan before they run.
- **Preserve compatibility at boundaries** — public API/config/schema/CLI/hook/template changes need a compatibility or migration plan.
- **Audit weak-point classes** — find a structural bug → audit the class + add a guardrail. Use the `touchstone-audit-weak-points` skill (Claude) or read `principles/audit-weak-points.md` (other drivers).
- **Isolate file-writing subagents** — parallel workers use dedicated worktrees, slice manifests, and disjoint file ownership by default.
- **File issues for bugs** — open a GitHub issue when you find a bug, in this project or in an autumngarage tool. Don't silently work around it.
- **Escalate delivery friction upstream** — if Conductor or Touchstone causes workflow drag (excessive token burn, weak parallelization, unclear delegation ergonomics, brittle merge-gate behavior, or other agent-delivery inefficiency), file an actionable upstream issue with repro steps and impact instead of normalizing the pain.

## Never commit on the default branch

Before the first edit of a tracked file in a session, run `git branch --show-current`. If it reports the default branch (`main` or `master`), branch first with `git checkout -b <type>/<slug>` where `<type>` is `feat | fix | chore | refactor | docs`. Your unstaged changes carry over — there's no cost to switching now and a real cost to discovering at commit time. Recovery steps when it happens anyway live in `principles/git-workflow.md`.

## Required Delivery Workflow

Drive this lifecycle automatically; do not ask the user for permission at each step.

1. **Pull.** `git pull --rebase` on the default branch.
2. **Branch.** Before any edit that might become a commit.
3. **Claim issues before implementation.** If the work starts from a GitHub issue, claim it before editing or dispatching an agent: `bash scripts/claim-issue.sh <n>`. Claim every issue in a multi-issue bundle so two agents do not ship competing fixes.
4. **Change + commit.** Stage explicit file paths. Concise message. One concern per commit.
5. **Reconcile issues.** Before opening the PR, list every GitHub issue found, claimed, fixed, partially fixed, or made stale by the work. Fully fixed issues get closing trailers (`Closes-issue: #123` or `Closes #123`) so merge auto-closes them; partial/stale issues get a comment explaining the evidence or remaining gap. Do not leave fixed issues open silently.
6. **Open PR + ship through the merge gate.** `bash scripts/open-pr.sh --auto-merge` pushes, opens the PR, runs the merge-gate pipeline, squash-merges, and syncs the default branch. The required expensive gates happen at merge time: deterministic checks, Conductor LLM review/fix loop, then deterministic checks again only if Conductor changed the PR head.
7. **Clean up.** Delete the local branch if it persists.

Do not bypass the PR/review/merge path with a direct default-branch push except through the documented emergency path in `principles/git-workflow.md`.

## Memory hygiene

- Treat AI-agent memory as cached guidance, not canonical truth. Verify a remembered command, flag, path, or version against this repo before relying on it.
- Don't write memory for facts that are cheap to derive from `README.md`, the steering files, `VERSION`, `bin/touchstone --help`, or the scripts.
- If memory mentions a command, flag, file path, version, or workflow, include the date (`YYYY-MM-DD`) and the canonical source checked.
- If memory conflicts with the repo, follow the repo and propose updating the stale memory.

## Routing table — read these when the trigger fires

| When you're about to... | Read |
|---|---|
| commit, branch, open a PR, run review, merge, recover from `no-commit-to-branch`, work with stacked PRs, or fan out worktrees | `principles/git-workflow.md` |
| understand the AI-authored change lifecycle, merge-gate review architecture, or where Conductor fits | `principles/ai-delivery-architecture.md` |
| start a non-trivial code change | `principles/pre-implementation-checklist.md` |
| understand the *why* of a daily-reminder rule | `principles/engineering-principles.md` |
| edit, write, or audit documentation | `principles/documentation-ownership.md` |
| coordinate parallel agents (subagents, worktrees, conductor swarm) | `principles/agent-swarms.md` |
| audit a structural bug class after fixing one instance | `principles/audit-weak-points.md` |
| hit a bug in an upstream tool (don't silently work around it) | `principles/file-upstream-bugs.md` |
| write a `.cortex/` artifact or see a Tier-1 trigger fire | `.cortex/protocol.md` |
| delegate to Conductor — pick a provider, write a brief, choose `--kind` / `--effort` | `~/.conductor/delegation-guidance.md` |

Claude Code agents: the Touchstone-bundled user-scoped skills (`touchstone-git-workflow`, `touchstone-pre-impl`, `cortex-protocol`, `conductor-delegation`, `touchstone-audit-weak-points`, `touchstone-agent-swarms`, `memory-audit`) provide the same routing surface as this table, with descriptions in your session header. Trust whichever surface fires first.

## Orientation

If `.cortex/state.md` exists in the project, read it at session start for the current state of in-flight work.

<!-- touchstone:steering:end -->


You are reviewing pull requests for **conductor**. Optimize your review for catching the things that bite this repo, not generic style polish.

This file is the source of truth for how AI reviewers (Codex, Claude, etc.) should think about a PR. The companion file `CLAUDE.md` is for the *author* writing the code; this file is for the *reviewer*.

---

## What to prioritize (in order)

1. **Provider contract integrity.** Every adapter must satisfy the `Provider` Protocol (`configured()`, `smoke()`, `call()`); provider quirks belong inside adapters, not in `interface.py` or the router.
2. **Routing and fallback correctness.** `auto` mode must derive choices from task tags and provider tags. Broken integrations, missing credentials, and provider failures must surface clearly, not silently fall through to another path.
3. **Credential safety.** Preserve env → `key_command` → keychain resolution. A failing `key_command` is an error, not permission to try a later source.
4. **Subprocess authority boundaries.** `conductor exec --with codex` runs unsandboxed end-to-end; `--sandbox` is deprecated/ignored. Permission profiles only make sense for providers that enforce conductor's tool whitelist.
5. **Consumer JSON compatibility.** `CallResponse` on stdout is a public contract used by Sentinel and Touchstone; shape changes need explicit compatibility handling.
6. **Generated/canonical wiring drift.** Keep provider IDs, model stacks, agent wiring, and templates in sync so subagent dispatch stays deterministic.

Style nits, formatting, and theoretical refactors are **out of scope** unless they hide a bug. Do not flag them.

---

## Specific review rules

### High-scrutiny paths

Files:

- `src/conductor/providers/interface.py` — shared adapter contract; review for interface widening, leaked vendor details, and call/smoke/configured semantic drift.
- `src/conductor/providers/__init__.py` — canonical provider identifiers; review for aliases or renames that break config, routing, or downstream callers.
- `src/conductor/router.py`, `src/conductor/router_defaults.py` — `auto` routing; review for fallback behavior that hides missing integrations or ignores task/provider tags.
- `src/conductor/credentials.py` — credential resolution; review for ordering changes and swallowed `key_command` failures.
- `src/conductor/providers/codex.py`, `src/conductor/providers/claude.py`, `src/conductor/providers/gemini.py` — subprocess/tool-whitelist adapters; review watchdogs, heartbeats, permission profiles, and sandbox claims carefully.
- `src/conductor/providers/openrouter.py` and provider-specific wrappers for Kimi, DeepSeek, and OpenRouter — shared transport with distinct provider IDs; review Moonshot/Kimi constraints such as temperature clamp and `tool_choice`.
- `src/conductor/cli.py` — CLI and stdout contract; review flag compatibility and `CallResponse` JSON changes as public API changes.
- `src/conductor/agent_wiring.py`, `src/conductor/openrouter_model_stacks.py`, `src/conductor/_agent_templates.py` — generated/canonical dispatch surfaces; review for drift across provider IDs, tags, and templates.
- `principles/`, `.cortex/doctrine/` — architectural rules; review for compatibility with the project doctrine rather than local convenience.

### Silent failures

Flag any of the following:

- New `except: pass`, `except Exception: pass`, or `except: ...` without logging.
- New `try / except` that catches a broad exception and continues without logging the exception object.
- Default values returned on error without a log line.
- Fallback behavior that masks broken state.

The rule: every exception is either re-raised or logged with enough context to debug from production logs alone.

### Tests

- Bug fixes must include a test that reproduces the original failure mode.
- Tests should use relative values (percentages, ratios) not absolute values where applicable.
- Integration tests should hit real infrastructure for critical paths (mocks have masked real bugs in the past).

---

## What NOT to flag

- Formatting, whitespace, import order — pre-commit hooks handle these.
- Type annotations on existing untyped code.
- "You could refactor this for clarity" — only if the unclarity hides a bug.
- Missing docstrings on small private functions.
- Speculative future-proofing — don't suggest abstractions for hypothetical future requirements.
- Naming preferences absent a clear convention violation.

If you find yourself writing "consider" or "you might want to" without a concrete bug or risk attached, delete the comment.

---

## Output format

1. **Summary** — one paragraph: what this PR does and your overall verdict (approve / request changes / comment).
2. **Blocking issues** — bugs or risks that must be fixed before merge. Each item: file:line, what's wrong, why it matters, suggested fix.
3. **Non-blocking observations** — things worth noting but not blocking. Keep this section short.
4. **Tests** — does this PR add tests for the changed behavior? If not, is that OK?

If there are zero blocking issues, the review is just: "LGTM."

## Current state (read this first)

@.cortex/state.md

## Cortex Protocol

@.cortex/protocol.md

<!-- conductor:begin v0.10.21 -->
## Conductor delegation

This project has [conductor](https://github.com/autumngarage/conductor)
available for delegating tasks to other LLMs from inside an agent loop.
You can shell out to it instead of trying to do everything yourself.

Quick reference:

- Quick factual/background ask:
  `conductor ask --kind research --effort minimal --brief-file /tmp/brief.md`.
- Deeper synthesis/research:
  `conductor ask --kind research --effort medium --brief-file /tmp/brief.md`.
- Code explanation or small coding judgment:
  `conductor ask --kind code --effort low --brief-file /tmp/brief.md`.
- Repo-changing implementation/debugging:
  `conductor ask --kind code --effort high --brief-file /tmp/brief.md`.
- Merge/PR/diff review:
  `conductor ask --kind review --base <ref> --brief-file /tmp/review.md`.
- Architecture/product judgment needing multiple views:
  `conductor ask --kind council --effort medium --brief-file /tmp/brief.md`.
- `conductor list` — show configured providers and their tags.

Conductor does not inherit your conversation context. For delegation,
write a complete brief with goal, context, scope, constraints, expected
output, and validation; use `--brief-file` for nontrivial `exec` tasks.
Default to `conductor ask`; use provider-specific `call` / `exec` only
when the user explicitly asks for a provider or the semantic API does not
fit.

Providers commonly worth delegating to:

- `kimi` — long-context summarization, cheap second opinions.
- `gemini` — web search, multimodal.
- `claude` / `codex` — strongest reasoning / coding agent loops.
- `ollama` — local, offline, privacy-sensitive.
- `council` kind — OpenRouter-only multi-model deliberation and synthesis.

Full delegation guidance (when to delegate, when not to, error handling):

    ~/.conductor/delegation-guidance.md
<!-- conductor:end -->
