# Cortex Protocol

> The set of rules an agent follows to read and write `.cortex/`. Projects import this file into `AGENTS.md` (or `CLAUDE.md`) so every agent working on the project follows the same contract.

**Protocol version:** 0.2.0 (draft, ships with SPEC.md v0.3.1-dev)
**Status:** Proposed
**Imports:** this file is imported into `AGENTS.md` via `@.cortex/protocol.md`

---

## 1. Read on session start

The agent's first action in any session on a Cortex-enabled project is to load the session manifest:

```
cortex manifest --budget <N>
```

The manifest is a token-budgeted slice of `.cortex/`, not the whole store. Default load:

| Component | Budget share | Selection |
|---|---|---|
| `state.md` (full) | ~1.5k | Always loaded |
| Doctrine | ~3k | All entries marked `Load-priority: always`, then most recent by `Date:` until budget exhausted |
| Active Plans (status = `active`) | ~2k | All |
| Journal entries from last 72h + latest digest | ~1.5k | By date |
| Promotion-queue depth summary | ~100t | Count only |

**No semantic retrieval at session start.** Cortex storage is markdown + git + grep — not a vector store (Doctrine 0005 #1, supersedes 0004). The default manifest loads Doctrine by `Load-priority: always` pins plus recency, never by embedding similarity. Projects that want semantic retrieval wire up their own index over `.cortex/` as a read-side layer; that index is out of scope for the Protocol.

**Mid-session retrieval is grep.** When the agent needs Doctrine or Journal content not in the manifest, it greps `.cortex/` directly or uses `cortex grep` (a frontmatter-aware wrapper shipping in Phase B). Protocol-aware tooling may provide typed-link traversal; the primitive is ripgrep.

**Graceful degradation.** At 32k context, the manifest falls back to State only. At 100k+, it may include Journal from last 7d. The CLI computes the slice; the agent receives the output.

**Fallback when the CLI is unavailable.** A Cortex project without the `cortex` CLI installed (or in an environment where shelling out is blocked) MUST still be loadable. The minimum viable manifest is:

```
# In AGENTS.md or CLAUDE.md:
@.cortex/protocol.md
@.cortex/state.md
```

The agent imports those two files at session start via the host's `@path` mechanism. This yields Protocol + State without the budgeted Doctrine/Journal/Plans slice — degraded but correct. The rest of `.cortex/` remains available via grep. `cortex doctor` warns when a project ships this fallback-only configuration against a corpus large enough that recency-by-grep is insufficient (default threshold: >20 Doctrine entries or >100 Journal entries).

**The agent does not read `.cortex/` directory contents directly at session start unless the user asks or the fallback configuration is in use.** Post-session-start, grep and targeted reads are expected. This keeps Time To First Token bounded and prevents accidental full-directory loads on large corpora.

---

## 2. Write on triggers (Tier 1 — machine-observable)

These triggers are **deterministic, auditable, and enforceable**. When any of them fires, the agent writes a Journal entry from the matching template. A post-session audit (`cortex doctor --audit`) verifies compliance.

| # | Trigger | Template |
|---|---|---|
| T1.1 | Diff touches `.cortex/doctrine/`, `.cortex/plans/`, `principles/`, or `SPEC.md` | `journal/decision.md` |
| T1.2 | Test command failed after succeeding earlier in the session | `journal/incident.md` |
| T1.3 | A Plan's `Status:` field changed (`active` → `shipped|cancelled|deferred|blocked`) | `journal/plan-transition.md` |
| T1.4 | File deletion exceeding N lines (default N=100; configurable per project) | `journal/decision.md` |
| T1.5 | Dependency manifest changed (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`) | `journal/decision.md` |
| T1.6 | Sentinel cycle ended (`.sentinel/runs/<timestamp>.md` written) | `journal/sentinel-cycle.md` |
| T1.7 | Touchstone pre-merge ran on architecturally significant diff (touches `principles/`, `.cortex/doctrine/`, `SPEC.md`, or matches configured patterns) | `doctrine/candidate.md` (draft, awaits promotion) |
| T1.8 | Commit message matches patterns: `fix: ... regression`, `refactor: ... (removes|introduces)`, `feat: ... (breaking|replaces)` | `journal/decision.md` |
| T1.9 | Pull request merged to the default branch (main/master) | `journal/pr-merged.md` |

**Why T1.9 matters.** The merge is the canonical "this shipped" event for team-shared memory. T1.3 (plan transition) and T1.8 (commit-message pattern) are near-misses: a PR can merge without a plan-status change, and commit-pattern matching is fuzzy. A post-merge summary closes the loop — it is the durable record that ties Plans, Journal entries written during the branch, and the final diff together at the moment ratification happened. Authored by whichever agent/human runs the merge command (or by a post-merge hook when present).

**Enforcement.** Tier 1 triggers are machine-detectable; `cortex doctor --audit` walks the git log for the session period and verifies that every qualifying event has a corresponding Journal entry. Missing entries are warnings in solo mode, errors in triad mode (where Touchstone's pre-push hook blocks the push).

**Trigger thresholds are project-configurable.** `.cortex/protocol.md` in a project can override: `N` for T1.4 file-deletion threshold; regex patterns for T1.7 architecturally-significant detection; commit-message patterns for T1.8; whether T1.9 fires on every merge or only on merges matching architecturally-significant patterns (default: every merge).

---

## 3. Write on triggers (Tier 2 — advisory)

These are **judgment-based**. The agent is asked to journal when it notices these, but non-compliance is not auditable and not enforced. Treat as good-citizen behavior, not contract.

| # | Signal | Template |
|---|---|---|
| T2.1 | User or agent phrases a decision (`"we decided"`, `"let's"`, `"we chose X over Y"`) | `journal/decision.md` |
| T2.2 | A failed attempt or dead-end that taught something non-obvious before the retry | `journal/decision.md` (with `failed-approach: true`) |
| T2.3 | Surprise about existing code (`"wait, why is this done this way"`) that leads to a hypothesis | `journal/decision.md` (with `investigation: true`) |
| T2.4 | User phrasings like `"remember this"`, `"don't forget"`, `"worth noting"` | `journal/decision.md` |
| T2.5 | A constraint or invariant the agent inferred and is relying on (`"this must stay synchronous because…"`) | `journal/decision.md` (with `inferred-invariant: true`) |

**Why separate tiers?** Round-2 critique flagged that mixing machine-observable events with judgment heuristics in one list made the Protocol a "checklist, not a protocol." The split preserves the good-citizen behaviors (T2) without pretending they're as enforceable as T1. Only T1 compliance is verified; T2 is advisory.

---

## 4. Invariants

Three rules apply to every write, regardless of trigger:

### 4.1 Journal is append-only

Never edit an existing Journal entry in place. New entry per event. If new information changes an old conclusion, write a new entry that cites and revises the old one; the old one stays unchanged.

### 4.2 Doctrine is immutable

Changes to Doctrine happen by writing a new entry with `supersedes: 0003` frontmatter. The old entry stays with `Status: Superseded-by 0012` and a link forward. The promotion queue surfaces Doctrine entries that look stale against recent Journal evidence, as candidates for supersede.

### 4.3 Generated layers declare provenance

Every generated file (`map.md`, `state.md`, digests) declares seven metadata fields:

```yaml
---
Generated: 2026-04-17T14:22:00-04:00
Generator: cortex refresh-state v0.3.1
Sources:
  - HEAD sha: abc1234
  - .cortex/journal/2026-04-01..2026-04-17 (23 entries)
  - .cortex/plans/*.md (5 active)
  - .sentinel/runs/2026-04-17-1430.md
Corpus: 23 Journal entries, 5 Plans, 1 Sentinel cycle
Omitted: journal/2026-04-13-wip-debugging (marked noisy)
Incomplete: []   # non-empty = best-effort
Conflicts-preserved:
  - "retry backoff" — journal/2026-04-10 argues exponential; journal/2026-04-15 argues fixed
---
```

`Incomplete: []` means "I looked at every input I could." A non-empty `Incomplete:` means consumer should treat the layer as best-effort. Missing metadata fields fail `cortex doctor` validation.

**Digest-specific rules** (for monthly and quarterly digests):

- **Depth cap.** A digest may cite other digests at most one level deep. Quarterly digests must also cite ≥5 raw Journal entries directly. Digest-of-digest-of-digest is forbidden to bound drift under repeated consolidation.
- **Audit sampling.** `cortex doctor --audit-digests` picks N random claims from a digest and verifies each traces back to at least one source entry. Failures surface as warnings.

---

## 5. Templates

Each template lives in `.cortex/templates/` and specifies required frontmatter and prose sections. The agent fills the template from conversation context; the CLI validates that required fields are present.

Templates shipped with the Protocol (filenames):

- `journal/decision.md` — generic decision entry
- `journal/incident.md` — SRE-postmortem shape (context, impact, timeline, action items, what-went-well / what-went-poorly)
- `journal/plan-transition.md` — Plan status change
- `journal/sentinel-cycle.md` — end-of-cycle summary
- `journal/pr-merged.md` — post-merge summary (T1.9)
- `doctrine/candidate.md` — Doctrine draft pending promotion
- `digest/monthly.md` — monthly Journal digest
- `digest/quarterly.md` — quarterly digest

Projects can add custom templates under `.cortex/templates/` and reference them from custom triggers. Templates are plain Markdown with YAML frontmatter; no DSL.

---

## 6. Project customization

Every line of this file is overridable per-project. A project's own `.cortex/protocol.md` can:

- **Disable a trigger** — `disabled: [T1.8]` at the top of the file.
- **Override thresholds** — `T1.4.line-threshold: 200` (instead of default 100).
- **Add patterns** — `T1.7.patterns: ["src/core/**", "migrations/**"]` for which paths count as architecturally significant in *this* project.
- **Add custom triggers** — new `T1.N` or `T2.N` entries with project-specific events and templates.
- **Add custom templates** — new files under `.cortex/templates/`.

`cortex doctor` checks the project's Protocol against the shipped version; a project that omits all of Tier 1 is flagged as non-conformant but not blocked.

---

## 7. Relationship to SPEC.md

This Protocol file specifies *when and how* agents write. [SPEC.md](../SPEC.md) specifies *what the file format looks like* — the directory layout, layer contracts, field conventions, cross-layer rules. The Protocol depends on the spec; the spec exists independent of the Protocol (a project could hand-author `.cortex/` following SPEC.md without using the Protocol, though it would lose the enforcement story).

When SPEC.md bumps to a new major version, the Protocol bumps alongside. Minor versions can advance independently as long as compatibility is preserved.

---

## 8. What this Protocol is not

- **Not a prompt.** The Protocol is declarative specification, not instructions to an LLM. Agents implementing the Protocol may use prompts internally; those prompts are the agent's business.
- **Not agent-agnostic.** Different agent runtimes (Claude Code, Cursor, Aider, custom) may implement the Protocol differently. The Protocol defines the contract; each agent decides how to meet it.
- **Not an enforcement mechanism.** Enforcement of Tier 1 compliance lives in the CLI (`cortex doctor --audit`) and in Touchstone's pre-push hook (when present). The Protocol specifies the *rules*; enforcement ships with the tools.
- **Not the only way to write `.cortex/`.** Humans can write directly. `cortex journal draft <type>` lets humans scaffold entries. The Protocol is for agents working continuously; human writes are always allowed and always respect the same invariants (§ 4).
