# AGENTS.md — AI Reviewer Guide for conductor

<!-- touchstone:shared-principles:start -->
## Shared Engineering Principles (apply these first)

These principles are touchstone-owned and shared across every project. Apply them as the **primary review criteria** before any project-specific rule below — a reviewer that lets a band-aid or a silent failure through has missed the point of this gate.

- **No band-aids** — fix the root cause; if patching a symptom, say so explicitly and name the root cause.
- **Keep interfaces narrow** — expose the smallest stable contract; don't leak storage shape, vendor SDKs, or workflow sequencing.
- **Derive limits from domain** — thresholds and sizes come from input/config/named constants; test at small, typical, and large scales.
- **Derive, don't persist** — compute from the source of truth; persist derived state only with a documented invalidation + rebuild path.
- **No silent failures** — every exception is re-raised or logged with debug context. No `except: pass`, no swallowed errors.
- **Every fix gets a test** — bug fix includes a regression test that runs in CI and fails on the old code.
- **Think in invariants** — name and assert at least one invariant for nontrivial logic.
- **One code path** — share business logic across modes; confine mode-specific differences to adapters, config, or the I/O boundary.
- **Version your data boundaries** — when a model/algorithm/source change affects decisions, version the boundary; don't aggregate across.
- **Separate behavior changes from tidying** — never mix functional changes with broad renames, formatting sweeps, or unrelated refactors.
- **Make irreversible actions recoverable** — destructive operations need a dry-run, backup, idempotency, rollback, or forward-fix plan before they run.
- **Preserve compatibility at boundaries** — public API/config/schema/CLI/hook/template changes need a compatibility or migration plan.
- **Audit weak-point classes** — when a structural bug is found, audit the class and add a guardrail; don't fix only the one instance.

Full rationale, worked examples, and the *why* behind each rule:

- `principles/engineering-principles.md`
- `principles/pre-implementation-checklist.md`
- `principles/documentation-ownership.md`
- `principles/git-workflow.md`

This block is managed by `touchstone` and refreshes on `touchstone update` / `touchstone init`. Edit content **outside** the markers to add project-specific reviewer guidance — touchstone will not touch it.
<!-- touchstone:shared-principles:end -->


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

<!-- conductor:begin v0.10.16 -->
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
