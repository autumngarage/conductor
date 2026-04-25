# Blindspot audit follow-ups deferred from the top-3 remediation plan

**Date:** 2026-04-24
**Type:** decision
**Trigger:** T2.1 (decision phrased) — explicit deferral of audit-surfaced items, recorded so they don't get lost
**Cites:** plans/conductor-blindspots, journal/2026-04-24-codex-plan-review

> The blindspot audit on 2026-04-24 surfaced ~10 candidate gaps in conductor v0.4.2. The remediation plan picked three to ship; the rest are recorded here so future-us doesn't rediscover them.

## Context

Per Cortex SPEC § 4.2, every deferral in a Plan must resolve to another Plan or Journal entry in the same commit — no orphan deferrals. The remediation plan (`plans/conductor-blindspots`) defers seven items beyond its top-3 cut. Rather than spawn seven stub files, this single journal entry serves as the resolution target for all of them. When any individual item is prioritized, a focused Plan can be written and this entry can be referenced.

## What we decided

Defer the following audit findings, with disposition for each:

### Cost observability (`conductor usage`)
- **Was:** original Slice B of the remediation plan.
- **Why deferred:** the persisted derived-state shape needs explicit provenance and a visible-failure path for unwritable cache, per `Derive, don't persist` and `No silent failures`. The first-pass plan draft missed both. Cleanest to address them in a focused plan.
- **Priority:** high — shippable as soon as the principle fixes are designed in.
- **Future plan slug:** `plans/conductor-cost-observability` when filed.

### `offline_mode.py` silent-no-op fix
- **Was:** observed by Codex as the source pattern for the same gap on the deferred cost-observability slice.
- **Why deferred:** small drive-by PR, doesn't need a plan; just an issue or branch when convenient.
- **Priority:** medium — recently shipped, no incidents yet, but the principle violation is real.

### Reachability blindness in `pick()`
- **Was:** `configured()` checks env vars only, never network. Router confidently picks providers that are unreachable.
- **Why deferred:** the recently-shipped offline-mode UX (PR #16) is a reactive patch; a proactive `last_reached_at` health field on the router would be the real fix. Larger scope, separate plan.
- **Priority:** medium-high.
- **Future plan slug:** `plans/conductor-router-reachability` when filed.

### No durable session / conversation state
- **Was:** every `call`/`exec` is single-shot; ollama is stateless by design; subprocess providers manage their own session files but conductor doesn't unify.
- **Why deferred:** large feature; arguably a permanent boundary (conductor's charter is single-task, not interactive). Requires explicit product decision before planning.
- **Priority:** low unless a concrete consumer (Sentinel, Touchstone, Vesper) needs it.
- **Future plan slug:** `plans/conductor-sessions` if filed; otherwise documented as Known Limitation.

### Capability-tag empirical calibration
- **Was:** tags like `kimi.tags = ["long-context", "cheap", "vision", "code-review"]` are hand-assigned and unvalidated. No evidence kimi is actually good at code-review relative to claude.
- **Why deferred:** speculative — no consumer has reported bad routing yet. Build the eval harness only when there's a signal.
- **Priority:** low.
- **Future plan slug:** `plans/conductor-tag-calibration` when filed.

### Credential rotation/expiry handling
- **Was:** no warning when a stored token nears expiry or has been unused for N months.
- **Why deferred:** low frequency (tokens last months); not a blocker.
- **Priority:** low.

### No structured logs for CI ingestion
- **Was:** all logging is stderr text. CI parsers must regex.
- **Why deferred:** consumer-driven — Sentinel/Touchstone migrations to conductor will surface real demand. No demand yet.
- **Priority:** low.

### Dependency version pinning is soft
- **Was:** `pyproject.toml` uses `>=` ranges. Every install gets potentially different httpx/click versions.
- **Why deferred:** Homebrew-distributed binary insulates most users; Python-installed users could drift. Pin if a real incident occurs.
- **Priority:** low.

## Consequences / action items

- [ ] When any of the above is prioritized, write a focused Plan and update this entry with a forward-link.
- [ ] Re-review this list at the next blindspot audit (no fixed cadence; trigger on a major release or an incident).
