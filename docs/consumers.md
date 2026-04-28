# Conductor — Subprocess CLI Contract for Consumers

This document is the durable contract for downstream tools that invoke Conductor via subprocess. It exists because Conductor is the LLM-routing layer for the autumn-garage quartet: tools compose by file contract, not Python import (Doctrine 0003 / 0004), and the CLI surface is the contract.

**Audience:** Authors of tools that shell out to `conductor call` — Touchstone (reviewer cascade), Sentinel (per-role LLM calls), or any future agent that wants capability-aware routing without bundling the providers itself.

## Stability contract

`conductor call`'s **flag surface** and **`--json` output schema** follow semver. Within a major version:

- Documented flags don't change meaning or get removed.
- Documented JSON fields don't get renamed or change types.
- Additive changes are always allowed: new flags, new fields. Consumers must ignore unknown JSON fields.
- The `raw` field of the JSON output is provider-specific and **explicitly not stable** — it passes through whatever the underlying provider CLI emitted. Consumers that read `raw` accept upstream churn.

Regression tests cover the high-risk pieces of this surface, including JSON auto-route output and the current error-code buckets. When this contract grows, add tests for the new stable field or flag in the same PR.

## Invocation forms

```bash
# Auto-routed by tag preference
conductor call --auto --tags <tag1,tag2> [options]

# Explicit provider
conductor call --with <provider> [options]
```

Exactly one of `--auto` or `--with` is required. `--auto` runs the router using `--tags`, `--prefer`, and `--exclude` to pick a configured provider; `--with` bypasses the router for direct provider use.

## Input

The task prompt, usually a delegation brief, comes from one of these sources:

1. `--brief "..."` / `--task "..."` — string flag. Avoid for long briefs (visible in `ps aux`).
2. `--brief-file <path>` / `--task-file <path>` — read from a UTF-8 file. Use `-` to read stdin explicitly.
3. **Stdin** — when no input flag is set, conductor reads stdin until EOF.

`--brief` and `--brief-file` are the preferred spellings for delegation. `--task` and `--task-file` remain compatibility aliases. Long prompts: prefer `--brief-file` or stdin to keep the brief out of process listings.

## Flags

The canonical reference is `conductor call --help`. The contract-level commitments:

| Flag | Type | Stability | Notes |
|---|---|---|---|
| `--with <provider>` | string | stable | One of: kimi, claude, codex, deepseek-chat, deepseek-reasoner, gemini, ollama, openrouter |
| `--auto` | bool | stable | Mutually exclusive with `--with` |
| `--tags <csv>` | string | stable | For `--auto` routing |
| `--prefer <mode>` | string | stable | One of: best, cheapest, fastest, balanced. Default: balanced |
| `--effort <level>` | string \| int | stable | One of: minimal, low, medium, high, max. Or integer token budget. Default: medium |
| `--exclude <csv>` | string | stable | Providers to skip in `--auto` |
| `--brief <text>` | string | stable | Inline delegation brief / prompt |
| `--brief-file <path>` | string | stable | File path, `-` for stdin |
| `--task <text>` | string | stable | Compatibility alias for `--brief` |
| `--task-file <path>` | string | stable | Compatibility alias for `--brief-file` |
| `--model <model>` | string | stable | Override provider default model |
| `--json` | bool | stable | Emit full `CallResponse` as JSON |
| `--verbose-route` | bool | stable | Print routing decision to stderr |
| `--silent-route` | bool | stable | Suppress route-log + caller-attribution |
| `--resume <session_id>` | string | stable | Resume claude/codex/gemini session |
| `--offline` / `--no-offline` | bool | stable | Force/clear local-only routing |
| `--profile <name>` | string | stable | Apply named profile defaults |

## Output (`--json`)

When `--json` is set, stdout receives a single JSON object on completion. Schema:

```json
{
  "text": "string — the model's response",
  "provider": "string — provider id (kimi, claude, codex, ...)",
  "model": "string — model id used",
  "duration_ms": "integer — total wall time",
  "usage": {
    "input_tokens": "integer",
    "output_tokens": "integer",
    "cached_tokens": "integer | null",
    "thinking_tokens": "integer | null",
    "effort": "string — minimal | low | medium | high | max",
    "thinking_budget": "integer — token budget for thinking"
  },
  "cost_usd": "number — best-effort cost estimate; null if unknown",
  "session_id": "string | null — for --resume on supporting providers",
  "raw": {
    "// passthrough from underlying provider — NOT stable across releases"
  }
}
```

### Auto-routing additions

When `--auto` is used, the JSON adds a `route` field with the same `RouteDecision` shape emitted by `conductor route --json`:

```json
{
  "...": "...",
  "route": {
    "provider": "claude",
    "prefer": "best",
    "effort": "medium",
    "thinking_budget": 8000,
    "tier": "frontier",
    "task_tags": ["code-review", "tool-use"],
    "matched_tags": ["code-review"],
    "tools_requested": [],
    "sandbox": "none",
    "ranked": [
      {
        "name": "claude",
        "tier": "frontier",
        "tier_rank": 4,
        "matched_tags": ["code-review"],
        "tag_score": 1,
        "cost_score": 0.045,
        "latency_ms": 7000,
        "health_penalty": 0.0,
        "combined_score": 4001.0,
        "unconfigured_reason": null
      }
    ],
    "candidates_skipped": [],
    "tag_default_applied": {},
    "tag_default_considered": [],
    "unconfigured_shadow": []
  }
}
```

For human diagnostics without `--json`, use `--verbose-route` to print the full ranking table on stderr. In `--json` mode, route logging is suppressed so stdout stays machine-parseable; read the `route` field instead.

### Error responses

On error, exit code is non-zero (see below) and stderr carries a one-line diagnostic. With `--json`, stdout is not guaranteed to contain a response object on failure; consumers should parse stdout only when present and keep stderr in their debug logs.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. JSON emitted (with `--json`); text emitted (without). |
| `2` | Usage, routing, configuration, or capability error. Examples: missing `--auto`/`--with`, invalid flag combination, no configured provider for the requested route, provider not configured, unsupported capability. |
| `1` | Provider/runtime failure or unclassified error. Examples: provider HTTP error, provider CLI failure, timeout surfaced by a provider, unexpected runtime exception. Read stderr. |

Consumers should handle `0` as success and treat every non-zero code as failure. `2` is usually actionable by changing flags, configuration, or routing inputs. `1` is the retry/log/surface bucket. More granular non-zero codes may be added in a future minor release; consumers must not assume the only possible failures are `1` and `2`.

## Examples

### Touchstone — code review

Pre-push gate calls Conductor for the codex review:

```bash
conductor call --auto --tags code-review --effort medium \
  --brief-file /tmp/diff.txt --json --silent-route
```

Touchstone parses `text` for the review verdict, `cost_usd` for accounting, `provider`/`model` for the route-log entry.

### Sentinel — per-role chat

Sentinel's roles (Monitor, Researcher, Planner, Reviewer) shell out for non-agentic chat:

```bash
conductor call --with claude --model sonnet --effort medium \
  --brief-file /tmp/system-prompt.txt --json
```

Sentinel maps the JSON response into its `ChatResponse` dataclass: `text` → `response`, `usage`/`cost_usd` → budget tracking, `session_id` → recorded for `--resume` if the role wants multi-turn continuity, `raw.stderr` if present → debug log.

### Sentinel — agentic code (Coder role)

The Coder role should use `conductor exec`, not `conductor call`. `exec` is the agentic subcommand for multi-turn work with tool access, preflight checks, sandbox settings, stall detection, and session logs. Its consumer contract is separate from this `conductor call` contract; use `conductor exec --help` for the current flag surface until that contract is documented.

## Versioning policy

- **Major** version bump (`0.x → 1.0`, `1.x → 2.0`): breaking flag/schema changes. Migration notes in CHANGELOG.
- **Minor** version bump: additive flags, additive JSON fields, new tags, new providers.
- **Patch** version bump: bug fixes, internal refactors, no consumer-visible change.

Consumers pin a major version in their own dependency declarations (brew formula `depends_on "autumngarage/tools/conductor"` resolves to current major; semver constraint expressed in formula version pin if needed).

## Provider-specific gotchas

- **`--resume <session_id>`** works only for claude / codex / gemini. Other providers ignore it or return error.
- **`ollama`** requires a local daemon (`ollama serve`); `conductor list` shows readiness.
- **`openrouter`** routes to whatever model `openrouter/auto` picks unless `--model` is set; cost estimates may be approximate.
- **`--effort max`** maps to provider-specific extended thinking where supported; for providers without thinking, it's treated as a hint.

## See also

- `conductor list` — runtime provider readiness (READY column, missing-config diagnostics).
- `conductor doctor` — environment checks, missing tools, configuration gaps.
- `conductor smoke` — minimal end-to-end test against each configured provider.
- Routing decisions logged to stderr (suppress with `--silent-route` for clean piping).

---

*This contract is the executable surface. Tests in this repo should pin every stable field or flag that downstream consumers depend on; CHANGELOG records each major-bump migration.*
