# Pre-Implementation Checklist

Before writing code, walk through these questions. If any answer exposes duplicated infrastructure, a symptom patch, a second code path, or unclear ownership, stop and discuss scope before continuing.

This checklist is a pre-flight prompt; the canonical rules live in [engineering-principles.md](engineering-principles.md).

## 1. Am I adding to or patching local infrastructure that shared infrastructure should own?

Search the project's existing shared layers (utilities, base classes, common modules) before writing or extending anything local. If a subsystem hand-rolls something the shared layer already provides, the fix is migration — not more hand-rolling. A patch on hand-rolled code deepens the debt; migration eliminates it.

## 2. Am I fixing the root cause or the symptom?

See **No band-aids** in the engineering principles. If this is a symptom patch, the PR must say so explicitly and name the root cause. Patching a symptom is sometimes the right call (time pressure, scope, risk) — but it must be a conscious, documented choice, not an accident.

## 3. Will this create a second code path?

See **One code path** in the engineering principles. If you must add a divergence, can you delete the old path in the same PR? If not, document the owner, the removal condition, and a follow-up issue before merging. Two code paths that do "almost the same thing" are a maintenance trap — they drift apart silently and bugs in one don't surface until production.

## 4. Am I changing a public boundary?

If this touches a public API, config file, schema, CLI flag, hook, template, or generated artifact, see **Preserve compatibility at boundaries** in the engineering principles. Downstream consumers may lag — the PR must include a compatibility or migration plan before merging.

## 5. Is this action reversible?

If this deletes, migrates, rewrites history, or has external side effects, see **Make irreversible actions recoverable**. The PR must describe how failure leaves the system in a known recoverable state — dry run, backup, idempotency key, rollback, or forward-fix plan.
