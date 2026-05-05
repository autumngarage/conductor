---
Status: shipped
Written: 2026-04-23
Author: claude-code (Henry Modisett, @henry@perplexity.ai)
Goal-hash: cdr3ttu1
Updated-by:
  - 2026-04-23T21:50 claude-code (created as Stage 3 follow-on to the Touchstone × Conductor integration plan)
  - 2026-04-23T22:15 claude-code (Slice A/B/C all merged — conductor v0.3.0, v0.3.1, v0.3.2 cut; ASCII hero banner landed alongside)
Cites: plans/touchstone-conductor-integration, journal/2026-04-21-stage-1-2-delivered, doctrine/0003-llm-providers-compose-by-contract, doctrine/0004-conductor-as-fourth-peer
---

# Conductor v0.3 — HTTP tool-use loop

> Teach conductor's HTTP providers (kimi first, ollama next) to drive tool-using agent sessions. Today `kimi.exec(tools=...)` raises `UnsupportedCapability` — the router filters kimi/ollama out of any exec request with a non-empty tool set. This closes that gap.

## Status: shipped (2026-04-23)

All three slices merged same day. Tags cut for v0.3.0 / v0.3.1 / v0.3.2; homebrew-conductor bumped to v0.3.1 (Slice C bump pending Slice C PR merge). ASCII hero banner shipped alongside as a cross-slice improvement.

2026-04-29 update: the sandbox concept was removed from `conductor exec`.
Historical notes below that mention sandbox modes describe the old design;
the current exec path is unsandboxed and `--sandbox` is deprecated and ignored.

- Slice A → [conductor#7](https://github.com/autumngarage/conductor/pull/7) (v0.3.0)
- Slice B → [conductor#8](https://github.com/autumngarage/conductor/pull/8) (v0.3.1)
- Slice C → [conductor#10](https://github.com/autumngarage/conductor/pull/10) (v0.3.2, CI pending)
- Banner  → [conductor#9](https://github.com/autumngarage/conductor/pull/9) (CI pending)

Final test count: 282 (was 203 pre-Stage-3). Ruff clean throughout.

## Why (grounding)

Stage 1+2 of the integration plan shipped `conductor exec` for shell-out providers (claude/codex/gemini). HTTP providers (kimi, ollama) still can't participate in `exec` with tools — shell-out CLIs own their own tool-use loop internally, but HTTP providers need conductor to orchestrate the loop ourselves.

Three consumers block on this:

- **Sentinel migration (Stage 5)** — the coder role is multi-turn with tool use. Can't migrate to `conductor exec` until kimi/ollama can drive that loop, since sentinel supports them as coder backends.
- **Touchstone cheap-path reviews** — `[review.routing].small_with = "ollama"` + `mode = "review-only"` wants inspection tools. Today that filter collapses to no-tools + tag-only routing, so the router filters ollama out and small diffs fall through to hosted.
- **Direct conductor users** — `conductor exec --with kimi --tools Read,Grep ...` gets UnsupportedCapability with no escape hatch. Documented limitation, but a real one.

Grounds-in Doctrine 0003 (provider-contract composition): the `exec` interface is meant to be uniform across provider shapes. Today it isn't — shell-outs are exec-capable, HTTP ones aren't. Fixing that is load-bearing for the "every provider is a peer" property.

## End state — what HTTP tool-use looks like

### 1. kimi.exec drives a full tool-use loop

```python
# Internal flow inside KimiProvider.exec(task, tools={"Read","Grep"}):
# 1. Build messages = [{role: user, content: task}]
# 2. Build tools param from Conductor's portable Tool registry
# 3. POST chat/completions with tools; parse response
# 4. If assistant.tool_calls: execute each tool against the workspace,
#    append assistant + tool results to messages, GOTO 3
# 5. Else (text-only response): return CallResponse(text=..., ...)
# 6. Max iterations (default 10, configurable via --max-iterations flag or env)
```

Same surface as `conductor exec --auto --tools Read,Grep`. The loop runs inside kimi.exec. No changes to the CLI surface.

### 2. Portable Tool registry

A new `src/conductor/tools/` module owns the Tool protocol:

```python
class Tool(Protocol):
    name: str           # "Read" | "Grep" | "Glob" | "Edit" | "Write" | "Bash"
    description: str    # For the model's tools param
    parameters_schema: dict  # JSON Schema for tool input
    def execute(self, params: dict, *, cwd: Path) -> str: ...
```

Each built-in tool gets an implementation. Path validation on every filesystem tool: no absolute paths outside `cwd`, no `..` that escapes, no symlinks that escape.

### 3. Execution authority (updated)

`conductor exec` now runs unsandboxed. Built-in tools still validate
workspace paths, but process/environment authority is inherited from the
operator.

### 4. Router updates

- Kimi's `supported_tools` gains `{Read, Grep, Glob, Edit, Write, Bash}` (currently empty).
- Ollama follows in a later slice.
- Router filter logic is unchanged — the tool capability declarations just stop forcing kimi/ollama out of tool-using requests.

## Sequencing — three slices

Each slice is shippable in isolation, each is ~1 session of focused work.

### Slice A — kimi inspection tools (this session, v0.3.0-alpha)

- `src/conductor/tools/` module with Tool protocol, Read/Grep/Glob
- Path validation: refuse absolute paths outside cwd, refuse symlink-escape
- `KimiProvider.exec` runs the tool-use loop for inspection tool sets
- `supported_tools = {Read, Grep, Glob}`
- Max iterations hardcoded to 10 in v1
- Tests: mock httpx, assert tool loop; unit-test each Tool; path-validation tests; circuit-breaker test
- Ships as v0.3.0 — first slice is a real release, not -alpha, because the scope is complete for what it promises

### Slice B — kimi writes + ollama (~1 session)

- Add Edit/Write/Bash to Tool registry
- Path-constrained writes; Bash scoped to cwd
- Ollama gets the same tool-use loop (its API is similar enough — `/api/chat` with tools, `/api/generate` for text-only)
- v0.3.1

### Slice C — context-budget management (~1 session)

- Context-window tracking: abort loop if token budget exceeds model's context minus safety margin
- Cost accounting for each tool round trip
- v0.3.2

### Out of scope (explicitly)

- **Streaming tool-use** — each loop turn is non-streaming. Streaming is a separate feature (Stage 4) and adds complexity orthogonal to tool-use.
- **Parallel tool calls** — OpenAI's schema allows multiple tool_calls in one response. We serialize them; the model rarely emits >3 in one turn and serial execution is simpler. Parallel is a future optimization.
- **Cross-provider tool semantics parity** — provider CLIs expose different tool semantics. Conductor's unified tool set is the lowest common denominator. Documented in `conductor doctor --explain-tools` (future command).

## Risks

### R1 — Path-validation gaps

In-process tool execution is only as safe as its path validation. Things that have bitten other tools:

- `/tmp/../etc/passwd` — trivial; handle via `os.path.realpath` after joining
- Symlinks inside cwd pointing outside — check `realpath` of every opened file, not just the join
- Case-insensitive filesystems on macOS — path `A/B` might resolve to `a/b`; use `realpath` consistently
- Windows separators if run on Windows (not supported today; explicitly out of scope)

The path-validation helper is the one thing in this plan that *must* be correct. Unit tests the adversarial cases.

### R2 — Model-schema drift

OpenAI's function-calling schema has churned (functions → tools, different arg shapes). Moonshot/Kimi via Cloudflare mostly tracks it, but some quirks:

- Moonshot's spec doesn't support `tool_choice = "required"` (documented in kimi.py header)
- Multi-turn tool calls with thinking variants need `reasoning_content` echoed back on subsequent turns

Slice A's test fixtures should include at least one Moonshot-style response shape, not just OpenAI reference.

### R3 — Infinite / runaway loops

A misbehaving model can request tool call → look at output → request same tool call again forever. Max iterations (10) is the circuit breaker. If we hit the cap, return the last assistant message as CallResponse.text with a note appended.

### R4 — Cost surprise

A 10-iteration tool-use session at max effort on claude-level tokens can cost ~$1. That's not obvious to a user accustomed to single-turn call(). The route log should include per-iteration token counts, not just final totals. `max_cost_usd` gate lands in Stage 4 but is already-relevant.

## Success criteria

1. **`conductor exec --with kimi --tools Read,Grep --task "summarize this repo"` works** on a real kimi-authed setup. 5+ tool calls executed, final text response returned.
2. **Router auto-routes to kimi when only kimi has matching tags + tool support.** Previously forced hosted-only fallback.
3. **Path traversal blocked** — `conductor exec --with kimi --tools Read` asked to read `../../../etc/passwd` returns a tool error, not the file contents. Unit test on the validator.
4. **Max-iteration circuit breaker fires** on a mocked model that infinitely requests the same tool. Deterministic test.
5. **Sentinel migration plan unblocked** — a follow-up sentinel plan can reference this and say "ready." The actual migration is its own session.

## Effort estimate

- **Slice A (this session):** ~4-6 hours. Tools module + kimi loop + tests. Ships as v0.3.0.
- **Slice B:** ~4-6 hours. Writes + ollama. v0.3.1.
- **Slice C:** ~4-8 hours. Budget tracking. v0.3.2.

Total: ~2-3 sessions, which is why Stage 3 was scoped at 2-4 weeks in the original integration plan (that estimate assumed interruption-heavy cadence; focused sessions can be tighter).

## Relationship to the master integration plan

This plan implements Stage 3 of `plans/touchstone-conductor-integration`. Stage 5 (Sentinel migration) unblocks once Slice B ships — Sentinel's coder needs writes, so inspection-only kimi isn't enough. Stage 4 (streaming + precise cost + budgets) is orthogonal and can ship in parallel.
