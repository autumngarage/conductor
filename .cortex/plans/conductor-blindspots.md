---
Status: active
Written: 2026-04-24
Author: claude-opus-4-7
Goal-hash: pending
Updated-by:
  - 2026-04-24T16:35 claude-opus-4-7 (created; first-pass plan after blindspot audit)
  - 2026-04-24T20:55 claude-opus-4-7 (revised after Codex review: replaced Slice B "cost observability" with "subagent prompt-drift testing"; added Phase 0 to Slice A and Slice C; deferred cost observability)
Cites: doctrine/0002-audit-weak-points, doctrine/0004-engineering-principles, .cortex/state.md, .cortex/journal/2026-04-24-codex-plan-review.md
---

# Conductor Blindspot Remediation — Top 3

> **Three load-bearing gaps surfaced by audit and a Codex review of the first-pass plan, scoped into shippable slices: subprocess-adapter live-path drift, freshly-shipped agent-wiring prompt drift, and sandbox security. Each is independent, each ships as its own PR, and each replaces an unverified assumption in conductor's posture with a measurable signal.**

## Why (grounding)

Doctrine 0002 (Audit One Weak-Point Class at a Time) requires that when we surface a structural weakness, we name the pattern, audit its instances, and add a guardrail — not just patch what we noticed. A blindspot audit completed 2026-04-24 against the v0.4.2 codebase surfaced ~10 candidate gaps; the first-pass plan picked three slices for remediation. A Codex review of that draft (transcript at `.cortex/journal/2026-04-24-codex-plan-review.md`) flagged that **the freshest, least-validated surface in the repo is the v0.4.0–v0.4.2 agent-wiring code** (`_agent_templates.py`, AGENTS.md, GEMINI.md, repo CLAUDE.md, Cursor rules) and that *prompt-drift testing for that surface should outrank cost observability* in the top-3 cut. This revision accepts that critique.

The selection criteria, applied uniformly:

- **Subprocess-adapter live smoke** — highest silent-breakage risk: `claude` / `codex` / `gemini` CLIs are owned by third parties whose argument surfaces drift; mocked tests prove our wrapper, not the live call.
- **Subagent prompt-drift testing** — highest *recent-shipping* risk: three slices of agent wiring landed in the past two weeks (v0.4.0/4.1/4.2) and have no automated check that a real LLM, given each subagent prompt, actually invokes conductor with the right flags. As discussed in journal 2026-04-24-llm-as-router-client, the LLM-above-conductor is the de-facto semantic router; if the prompts don't enumerate the flag surface, routing quality silently degrades.
- **Sandbox security** — highest single-incident risk: `exec` runs LLM-chosen `Bash` under `workspace-write` / `strict`, and we have no formal sandbox contract — let alone an adversarial test suite — proving the sandbox holds.

Cost observability was the first-pass third slice. Codex's review noted it adds persisted derived state that needs explicit provenance and visible failure handling per `Derive, don't persist` and `No silent failures`, and that the absence of those in the draft was a band-aid pattern carried forward from `offline_mode.py`. The slice is deferred to its own follow-up plan rather than shipped under this remediation banner with the principle gaps unresolved.

## Approach

Three slices, ordered A → B → C by ship-momentum (smallest first), each in its own PR so review surfaces stay narrow.

### Slice A — Subprocess-adapter live smoke in CI

**Phase 0 (build the test surface).** Codex review caught that the `RUN_LIVE_SMOKE=1` gate on `tests/test_adapters_subprocess.py` does not exist yet — all 305 lines of that file are mocked. So Slice A starts by *writing* the live tests, not just wiring them. Add `RUN_LIVE_SMOKE=1`-gated cases that run a 1-token round-trip against each of `claude`, `codex`, and `gemini` and assert the response shape matches what the adapter expects to parse. Skip cleanly when the corresponding CLI is not installed or `RUN_LIVE_SMOKE` is unset.

**Phase 1 (CI workflow).** A nightly GitHub Actions job that installs the three CLIs (or fails the run if any is missing — no silent zero-coverage pass) and runs `RUN_LIVE_SMOKE=1 pytest tests/test_adapters_subprocess.py -k live`. Failures open an issue tagged `live-smoke-failure`; passes log a daily heartbeat. Intentionally narrow: this only proves "the CLI still accepts the args we're passing," not "the model still gives good answers."

Touches: `.github/workflows/`, `tests/test_adapters_subprocess.py`.

Rough size: medium. ~250 lines (Phase 0 tests) + ~150 lines of YAML.

### Slice B — Subagent prompt-drift testing

The agent-wiring slices (v0.4.0–v0.4.2) write `_agent_templates.py`-defined system prompts into Claude Code, Cursor, and Gemini configs. These prompts teach an outer LLM how to invoke conductor — they enumerate the providers, the flags, the tags, and when to delegate. If the conductor CLI surface drifts (e.g., new `--offline` flag we just shipped, new `cost-aware` tag added later) and the prompts don't follow, the LLM caller routes suboptimally and we never notice.

The slice ships two layers:

**Layer 1 (snapshot tests, must-have).** For each subagent template in `_agent_templates.py`, assert it mentions the flags and concepts the agent is supposed to know about. Concrete checks: `ollama-offline` mentions `--offline`; `kimi-long-context` mentions `--effort` and `--tags long-context`; `conductor-auto` mentions every prefer mode and the four sandbox modes. The list of "must-mention" tokens lives in a single `expected_template_coverage` dict so adding a new flag automatically surfaces which prompts need updating.

**Layer 2 (instruction-following test, stretch).** Run a real LLM (Haiku via the Anthropic API, or Kimi via Cloudflare to keep cost low) against a fixture of 10–20 sample tasks and assert it produces a sensible `conductor` invocation. Gate on `RUN_LIVE_LLM=1` so it doesn't run in default CI. Out of scope for v1 of this slice if Layer 1 is bigger than expected; tracked as Phase 2.

Touches: `src/conductor/_agent_templates.py` (likely needs a tiny refactor to expose template strings as importable constants if not already), new `tests/test_agent_template_drift.py`, possibly new `tests/fixtures/agent_invocations.jsonl` for Layer 2.

Rough size: small-medium. ~200 lines for Layer 1 + tests; Layer 2 sized after Layer 1 lands.

### Slice C — Sandbox semantics + adversarial audit + guardrail tests

**Phase 0 (formal sandbox contract).** Codex review caught that an attack catalog without a written contract is just empirical-behavior testing. Add `.cortex/doctrine/0007-sandbox-semantics.md` (or `docs/sandbox.md` if doctrine isn't the right home) defining for each `--sandbox` mode (`none`, `read-only`, `workspace-write`, `strict`) the invariants that must hold — what file paths can be read/written, what subprocess commands can be run, what network access is allowed/denied. Each invariant is named so Phase 1 attacks validate against the contract, not against current code behavior.

**Phase 1 (adversarial audit).** Build `tests/security/run_attempts.py` — a small attempt-runner that takes a sandbox mode + a list of `(tool, args, expected_outcome)` tuples and records pass/fail/error per attempt against the contract from Phase 0. Catalogue ≥20 attempts across `Bash`, `Edit`, `Write`, `Read`, `Glob`, `Grep` sourced from CWE-22 (path traversal), CWE-78 (command injection), and AI-agent escape literature. Run against each sandbox mode in an isolated runner (Docker container or temp HOME, with network egress blocked except localhost) so a successful exfiltration attempt during the audit can't actually leak. Capture results in `.cortex/journal/YYYY-MM-DD-sandbox-audit.md`.

**Phase 2 (harden).** Convert each contract violation found in Phase 1 into a regression test in `tests/test_tools_security.py` and harden `src/conductor/tools/registry.py` (or new `sandbox.py` if existing inline checks don't compose) until all regression tests pass.

Touches: new `.cortex/doctrine/0007-sandbox-semantics.md`, new `tests/security/`, `src/conductor/tools/registry.py`, possibly new `src/conductor/tools/sandbox.py`.

Rough size: large and uncertain. Phase 0 is small (~1 day to write the contract). Phase 1 is exploratory (could surface 0 findings or 15). Phase 2 sized by Phase 1 output. Estimate: 0.5 day Phase 0, 1 day Phase 1, 1–3 days Phase 2. Most likely to expand beyond initial scope; the plan will be re-cut after Phase 1 if findings are extensive.

## Success Criteria

1. **Slice A — Phase 0:** `tests/test_adapters_subprocess.py` contains `@pytest.mark.skipif(not os.environ.get("RUN_LIVE_SMOKE"), reason=...)` cases for each of claude/codex/gemini that perform a 1-token round-trip and assert response shape. Cases skip cleanly under default `pytest -q` (no live calls) and pass under `RUN_LIVE_SMOKE=1 pytest -k live` when CLIs are installed.

2. **Slice A — Phase 1:** `.github/workflows/nightly-smoke.yml` exists and runs at 09:00 UTC daily. Workflow **hard-fails** the run when any of the three target CLIs is missing from the runner (no silent zero-coverage pass). A failed test opens a GitHub issue tagged `live-smoke-failure` automatically. Slice marked shipped after 7 consecutive nightly green runs.

3. **Slice B — Layer 1:** `tests/test_agent_template_drift.py` contains assertions covering every subagent in `_agent_templates.py`. Adding a new conductor CLI flag and forgetting to update its corresponding subagent prompt causes a test failure (verified by deliberately omitting `--offline` from `ollama-offline` and observing red).

4. **Slice C — Phase 0:** `.cortex/doctrine/0007-sandbox-semantics.md` (or equivalent) exists and defines named invariants per sandbox mode. Each invariant has a unique identifier referenced by Phase 1 attempts.

5. **Slice C — Phase 1:** ≥20 catalogued attempts run in an isolated environment (network-egress-blocked) with results recorded in `.cortex/journal/YYYY-MM-DD-sandbox-audit.md`. Each attempt cites the contract invariant from Phase 0 it targets.

6. **Slice C — Phase 2:** every confirmed contract violation from Phase 1 has a corresponding `tests/test_tools_security.py` regression test that fails on the un-hardened code and passes on the hardened code. Pre-existing `tests/test_tools.py` continues to pass with no regressions.

7. **Pipeline:** all three slices ship as independent PRs reviewed via Codex per `principles/git-workflow.md`. Each PR includes a journal entry per protocol Tier 1 triggers (T1.1 for Slice C if it touches `principles/`, T1.9 for all on merge).

## Work items

### Slice A — Subprocess-adapter live smoke

**Phase 0:**
- [ ] Add `RUN_LIVE_SMOKE=1`-gated test cases to `tests/test_adapters_subprocess.py` for each of claude, codex, gemini. Each performs a 1-token round-trip + asserts the parsed `CallResponse` shape.
- [ ] Confirm `pytest -q` skips them by default and `RUN_LIVE_SMOKE=1 pytest -k live` runs them.

**Phase 1:**
- [ ] Add `.github/workflows/nightly-smoke.yml` triggering at 09:00 UTC daily.
- [ ] Workflow installs the three CLIs (claude, codex, gemini); fails the run if any cannot be installed (no silent skip).
- [ ] Wire failure → `gh issue create --label live-smoke-failure` with the diff of stderr.
- [ ] Document the workflow + opt-out in CLAUDE.md "Testing" section.

### Slice B — Subagent prompt-drift testing

**Layer 1:**
- [ ] If needed, refactor `src/conductor/_agent_templates.py` to expose each subagent template as an importable string constant (likely already the case).
- [ ] New `tests/test_agent_template_drift.py` with one test per subagent. Each test reads the template and asserts a list of must-mention tokens (flags, tags, sandbox modes, provider IDs).
- [ ] Centralize the must-mention list in a single dict so adding a new flag flags every subagent that should mention it.
- [ ] Verify red-then-green: deliberately remove `--offline` from `ollama-offline`, confirm the test fails.

**Layer 2 (deferred unless Layer 1 underruns):**
- [ ] Build `tests/fixtures/agent_invocations.jsonl` with 10–20 sample tasks + expected `conductor` invocation shapes.
- [ ] Add `RUN_LIVE_LLM=1`-gated test that runs each through a real LLM (cheap provider, e.g. Haiku) and asserts the produced invocation is sensible.

### Slice C — Sandbox audit

**Phase 0:**
- [ ] Write `.cortex/doctrine/0007-sandbox-semantics.md` defining invariants per sandbox mode. Each invariant gets a unique ID (e.g., `S-NONE-1: no restriction; full filesystem access`, `S-RO-1: writes always denied regardless of path`, etc.).
- [ ] Get the contract reviewed (Codex pass + human pass) before Phase 1.

**Phase 1:**
- [ ] Build `tests/security/run_attempts.py` — attempt-runner with isolation (temp HOME, network-egress block via firewall rule or container).
- [ ] Catalogue ≥20 `(tool, args, expected_outcome, contract_id)` tuples sourced from CWE-22, CWE-78, AI-agent escape literature.
- [ ] Run catalogue against each sandbox mode; record results in `.cortex/journal/YYYY-MM-DD-sandbox-audit.md`.

**Phase 2 (depends on Phase 1):**
- [ ] For each confirmed contract violation, write a regression test in `tests/test_tools_security.py`.
- [ ] Harden `src/conductor/tools/registry.py` (or new `sandbox.py`) until all regression tests pass.
- [ ] Cross-link the doctrine entry with the regression tests as proof of contract compliance.

## Follow-ups (deferred)

The audit and Codex review surfaced these additional items. Each is acknowledged here and lands its own plan or journal entry if/when prioritized:

- **Cost observability (`conductor usage`)** — was the original Slice B; deferred because the persisted-state shape needs explicit provenance and a visible-failure path for unwritable cache (per `Derive, don't persist` and `No silent failures`) that the first-pass draft missed. Resolved-to: future plan `plans/conductor-cost-observability.md` (high-priority follow-up).
- **`offline_mode.py` silent-no-op fix** — the unwritable-cache silent-no-op pattern in the recently-shipped offline-mode code has the same `No silent failures` violation Codex flagged on Slice B. Resolved-to: future plan or a small drive-by PR.
- **Reachability blindness in `pick()`** — router picks providers without active health probing; `configured()` is env-var-deep, not network-deep. Resolved-to: future plan `plans/conductor-router-reachability.md`.
- **No durable session / conversation state** — every `call`/`exec` is single-shot; multi-turn requires caller-side context concatenation. Resolved-to: future plan `plans/conductor-sessions.md` (or accepted as a permanent boundary; see Known limitations).
- **Capability-tag empirical calibration** — `kimi.tags = ["long-context", "cheap", "vision", "code-review"]` are hand-assigned and unvalidated. Resolved-to: future plan `plans/conductor-tag-calibration.md` (low priority; speculative until a consumer reports bad routing).
- **Credential rotation/expiry handling** — no warning when a stored token nears expiry or has been unused for N months. Resolved-to: journal entry `journal/2026-04-24-credential-lifecycle-noted.md`.
- **No structured logs for CI ingestion** — everything is stderr text. Resolved-to: journal entry `journal/2026-04-24-observability-gap-noted.md`.
- **Dependency version pinning is soft (`>=`)** — every install can pick up different httpx / click. Resolved-to: journal entry `journal/2026-04-24-dependency-pinning-noted.md`.

## Known limitations at exit

After all three slices ship, conductor will still have these accepted limitations:

- **No spend visibility.** Until cost observability lands as its own follow-up, users cannot see what they spent without external accounting.
- **Single-shot semantics.** Each invocation is independent. Multi-turn conversational use cases require a higher-level tool (aider, future `conductor chat` if ever built) — see Follow-ups.
- **Subagent prompt coverage is structural, not semantic.** Slice B Layer 1 proves the right tokens are mentioned in each prompt; it does not prove an LLM reading the prompt produces good routing. Layer 2 closes that, but is deferred unless Layer 1 underruns.
- **Sandbox guarantees are best-effort, not formally verified.** Slice C raises confidence by writing the contract and adversarially testing against it, but does not prove the sandbox is sound. Formal verification is out of scope.
- **Live smoke depends on third-party CLI availability.** Slice A only catches drift on CLIs the runner has installed. The hard-fail-on-missing-CLI policy keeps coverage visible, but if the install step itself is broken (e.g., upstream Homebrew tap goes 404), the workflow fails for the wrong reason. Mitigation: install-step errors get the same `live-smoke-failure` issue label.

<!--
Authoring checklist:

- [x] Frontmatter populated (Goal-hash: pending — recompute via `cortex doctor` once available).
- [x] Why grounded in doctrine/0002 + 0004 + Codex-review journal.
- [x] Success criteria measurable (file paths, test counts, automated workflow signals).
- [x] Every deferred item resolves to a future plan or journal entry.
- [x] Known limitations at exit named.
-->
