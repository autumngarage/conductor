# LLMs are conductor's de-facto semantic router; tag selection happens above the CLI

**Date:** 2026-04-24
**Type:** decision
**Trigger:** T2.5 (inferred-invariant) — naming the architectural separation we are relying on
**Cites:** plans/conductor-blindspots, doctrine/0004-engineering-principles

> When an LLM (Claude Code, a subagent, any other agentic caller) shells out to `conductor`, the routing inputs (`--auto`, `--tags`, `--prefer`, `--effort`, `--exclude`, `--offline`) are constructed by that LLM. Conductor's router is then a pure deterministic function of those inputs. The de-facto pipeline is therefore: **LLM = semantic tag selector** above conductor; **conductor = rule scorer** inside.

## Context

A discussion on 2026-04-24 traced the decision chain end-to-end for the case of an LLM caller (Claude Code via a subagent) using conductor as a tool:

1. User types a task into the outer LLM.
2. Outer LLM decides whether to handle directly or delegate to a wired subagent (e.g. `conductor-auto`, `ollama-offline`, `kimi-long-context`).
3. Subagent reads the task, picks `conductor` CLI args.
4. `_apply_offline_flag` rewrites if `--offline`/`--no-offline` present.
5. `pick()` runs deterministic rule scoring over declared capability fields.
6. `_invoke_with_fallback` walks the ranking + applies sticky offline flag.
7. Provider executes.

The LLM's only influence on auto-mode lives at step 3. Once CLI args are set, conductor is pure. The router does not see intermediate fallback events; if the LLM made a poor tag choice, the router cannot course-correct.

## What we decided

Name this separation as a load-bearing invariant of the design rather than an emergent property:

- Conductor's router stays rule-based (per Doctrine 0005 — no semantic routing inside the router; no embeddings; no LLM-in-the-router).
- The semantic work of "this task looks like long-context, that one looks like vision" lives in the agent layer above conductor — specifically in the subagent prompts shipped by `conductor init --wire-agents` (source: `src/conductor/_agent_templates.py`).
- Improving auto-mode for LLM callers means improving two things: (a) the subagent prompts (text-only change in `_agent_templates.py`); (b) the zero-input default scoring (the `--prefer balanced` mode without tags, which today is degenerate "kimi on priority always").

## Consequences / action items

- [ ] **Subagent prompt-drift testing** lands as Slice B of the blindspot remediation plan (`plans/conductor-blindspots`). Without it, drift between conductor's flag surface and the prompts is silent.
- [ ] The "zero-input default" gap is acknowledged but not in scope for the current plan; future plan candidate.
- [ ] If the router ever gains an LLM-based scoring path, this entry must be revisited and likely superseded — it currently asserts the absence of LLMs from the router as a load-bearing invariant.
