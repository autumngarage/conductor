# Codex `exec` agent-loop wedges at startup — production trace

**Date:** 2026-04-26
**Type:** incident
**Trigger:** T1.4 (consumer evidence) — production trace from a Claude Code session running on outrider, end-to-end, with conductor 0.5.1 and codex-cli 0.125.0. 5 dispatches, 3 silent-hang failures with the exact same signature.
**Cites:** #36 (stall watchdog), #38 (session_id stderr signal + forensic NDJSON), #34 (codex 0.125.0+ effort adapter)

> Conductor 0.5.x's stall watchdog and forensic-log work landed in time to bound the cost of these failures (10 min wasted per dispatch instead of 30–50). But codex `exec` mode itself wedges before producing any output on a meaningful fraction of dispatches today, with `smoke` and single-turn `call` both passing. This is a status report for the conductor team: what we observed, what worked vs. didn't, and what we'd want surfaced next.

## Context

Outrider had a production incident Apr 25–26 (research pipeline went silent for ~8 days; runner.py was missing entirely, agent loops never invoked). The fix path was open-ended at the start, mechanical by the end. Across the day we dispatched five tasks via the `codex-coding-agent` subagent (which calls `conductor exec --with codex --tools Read,Grep,Glob,Edit,Write,Bash --sandbox workspace-write …` under the hood):

| # | Task | Wall time | Outcome |
|---|------|-----------|---------|
| 1 | Rewrite `/rider-status` skill (11 fixes, ~4000-word prompt) | ~16 min | TIMED OUT × 2 (300s, then 590s on retry). Zero output. **Pre-0.5.x; before stall watchdog landed.** |
| 2 | Diagnose scheduler outage (open-ended investigation) | ~51 min | HUNG. Zero subprocess activity for the bulk of the run. Killed by SIGTERM. **Pre-0.5.x.** |
| 3 | Implement `outrider/runner.py` (tight spec, vanguard reference) | ~13 min | CLEAN MERGE. PR shipped. **Pre-0.5.x.** |
| 4 | Fix `/rider-status` API key list (3-line diff, exact text) | ~3 min | CLEAN MERGE. **Pre-0.5.x.** |
| 5 | Delete one stale test file (one-line `git rm`) | ~32 min | HUNG. Zero changes written. **Pre-0.5.x.** |

After dispatch #5, I delivered feedback to the conductor team about the silent-hang pattern. Henry then upgraded conductor (0.4.4 → 0.5.1) and re-ran `conductor init -y --wire-agents yes`. New flags `--max-stall-seconds` and unbounded `--timeout` (PR #36 / PR #35) were in place. Subagent template refreshed to `managed-by: conductor v0.5.1`.

I then dispatched three more tasks for a coordinated outrider sweep (port `bootstrap_trading_infra` + `KalshiClient`, migrate `get_agent_pnl_breakdown` away from cross-cluster SQL, migrate calibration-read to `outcomes_inbox`). Each prompt explicitly included an instruction to pass `--max-stall-seconds 600 --timeout 1800` to the inner `conductor exec` invocation.

## Failure pattern under 0.5.1 — three for three

| # | Task | Codex outcome | Killed by |
|---|------|---------------|-----------|
| 6 | bootstrap_trading_infra + KalshiClient | Wedged at startup. Smoke test passed (6.9s, 5 tokens). `exec` produced zero bytes both attempts. Worktree untouched. | `--max-stall-seconds 600` |
| 7 | trades-table query migration (small) | Wedged at startup. Two attempts. Zero bytes. | `--max-stall-seconds 600` |
| 8 | calibration-read migration (medium) | Wedged at startup. Zero bytes. | `--max-stall-seconds 600` |

All three were killed exactly at 600s with zero output. **The watchdog from #36 worked perfectly**: 30 minutes total wasted vs. ~99 minutes pre-fix. Net win regardless of the codex flakiness.

## What worked vs. what didn't

| Probe | Result |
|-------|--------|
| `conductor smoke codex` | ✓ |
| `conductor call --with codex --task "say hi"` | ✓ (6.9s, 5 tokens) |
| `conductor call --with claude --task "Respond with just: ok"` | ✓ (~1s) |
| `conductor exec --with codex --tools … --sandbox workspace-write --task "<real task>"` | ✗ — wedge before first byte, 3 for 3 |

The single-turn path is healthy. The agent-loop bootstrap inside codex is what's wedging — model warmup, sandbox session init, or first tool registration. **Whatever it is, it produces no output on stdout or stderr** (we'd have seen it in the subagent's relay, and the new `[conductor] codex session_id=…` line from #38 didn't appear either, which suggests codex isn't even emitting the `session.created` NDJSON event that #38 hooks).

## Why this matters for the new instrumentation

PR #38 (early session_id stderr signal + forensic NDJSON log on failure) shipped earlier today. It assumes the codex CLI emits at least the `session.created` event before wedging. Our trace suggests **codex is wedging before that event fires** — at least sometimes — which would mean:

- The session_id stderr signal would never trigger.
- The forensic NDJSON log would either be empty or not exist (depending on when conductor opens the file handle).
- Wrapping agents have nothing to attribute the failure to: no session_id, no ndjson, no `--resume` target.

This is worth verifying. If our hypothesis is right, the coverage gap is "codex hangs *before* `session.created`" and the response would be a fallback signal — e.g. write the conductor-side request envelope (the prompt, the tool list, the sandbox config) to the forensic NDJSON when the watchdog kills the run with zero output, even if codex itself contributed nothing.

## Reproduction details

**Versions:**
- conductor 0.5.1 (formula `autumngarage/conductor/conductor` 0.5.1)
- codex-cli 0.125.0 (`/opt/homebrew/bin/codex`)
- macOS Darwin 25.4.0
- Python 3.12 / 3.14

**Subagent invocation pattern (from `~/.claude/agents/codex-coding-agent.md` v0.5.1):**
```
conductor exec --with codex --tools Read,Grep,Glob,Edit,Write,Bash \
    --sandbox workspace-write --task "<prompt>" --json
```
Note: the v0.5.1 subagent template doesn't include `--max-stall-seconds` by default. We added it via prompt-time instruction. **A v0.5.1 template update to include `--max-stall-seconds 600` for unattended runs would close that gap.** Workaround memory entry saved on the consumer side, but template-side is the right fix.

**Prompt sizes:** the three failing prompts ranged from ~800 to ~3500 words. The successful #3 (runner.py) was ~2500 words and worked. So size is not the obvious differentiator within the same session.

**Timing:** all five pre-fix attempts and all three post-fix attempts happened within a single 8-hour window (~16:00–01:00 UTC-4). Same machine, same network, same auth.

## What we'd want surfaced

In rough priority:

1. **Pre-`session.created` failure is invisible.** If our hypothesis is correct, the new forensic NDJSON log doesn't capture this class of wedge because it's keyed on parsing codex's own NDJSON stream. A conductor-side "what we sent" envelope written on watchdog kill would let us confirm whether codex received a request that should have started a session.
2. **Subagent template default for `--max-stall-seconds`.** v0.5.1 still has the old example invocation. New users won't know the flag exists. Recommended default of `--max-stall-seconds 600` for unattended runs would let them inherit hang detection without reading the changelog. (This is a doc/template fix, not a code fix.)
3. **Health probe asymmetry.** `conductor smoke codex` and `conductor call --with codex` both pass while `exec` is broken. If `exec` failures are upstream of `session.created`, a *real* health probe might do a minimal `exec` round-trip (e.g. `--task "echo hi"` with a 30s budget) rather than a single-turn `call`. The current smoke is honest about its scope; just noting that consumers of `smoke` may infer more.
4. **`--effort` translation regression watch.** PR #34 fixed the `--effort → model_reasoning_effort` mapping for codex 0.125.0+. The subagent passes `--effort max` by default; if the translation is brittle on certain codex builds, that could be a wedge source. Worth checking codex CLI logs for what arrives at the model layer.
5. **Reproducibility help.** Our worktrees were auto-cleaned (no changes made = clean). If conductor preserved a per-failure tarball of `(subagent prompt + flags + env + last N stderr lines)`, post-mortem would be tractable. Today we're inferring from logs that no longer exist.

## Open questions

- Is this wedge limited to specific prompt content, or specific session-state shapes? The successful #3 was a similar-sized prompt. Hard to tell from N=8 dispatches.
- Does `codex exec` directly (without conductor) wedge the same way? The codex-coding-agent suggested that probe but neither of us ran it; doing so would isolate conductor vs codex.
- Is `codex-cli 0.125.0` particularly affected, or would a downgrade to a prior version recover? The PR #34 commit message implies meaningful behavior changes between codex versions.

## Disposition

- **For conductor team:** consider items 1–5 above; the per-failure envelope (#1) and template default (#2) feel highest leverage.
- **For consumer side (outrider):** rerouted the three failing tasks to `conductor exec --with claude` (or fallback to in-process Claude tool calls). Memory entry saved noting the codex `exec` instability — will retry codex on future tasks once a fix lands or on a different codex CLI version.
- **No code change in this PR.** This is an incident report; the conductor team owns the response.

If a session-id correlation would help, dispatch #7 (the small one) ran at roughly 23:13–01:14 PT 2026-04-26. The `--with-codex` adapter ran twice within that window. We didn't capture session_ids because codex didn't emit `session.created` (per the hypothesis above); if conductor logs the watchdog kill with timestamps in any persistent location, those should pin the runs.
