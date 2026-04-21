# .cortex/ — project memory

This directory is the project's Cortex memory. Six layers follow the Cortex spec (see `SPEC.md` at the project root, or the canonical copy at <https://github.com/autumngarage/cortex>). Agents read `protocol.md` and `state.md` at session start; humans edit `journal/`, `plans/`, and `doctrine/` directly; `map.md` and `state.md` regenerate via `cortex refresh-map` / `cortex refresh-state` (Phase C — not yet shipped as of the current CLI release).

## Layers

- `doctrine/` — immutable load-bearing claims. Numbered ADR-style entries; never deleted; superseded entries stay in place with a link forward.
- `journal/` — append-only event log. One event per dated file (`YYYY-MM-DD-<slug>.md`); never edited in place; consolidated via monthly/quarterly digests.
- `plans/` — active efforts with measurable `Success Criteria`. One file per effort; status transitions (`active` → `shipped|cancelled|deferred|blocked`) tracked in frontmatter.
- `map.md` — derived structural view of the project (packages, boundaries, data flow). Regenerated from code + manifests.
- `state.md` — derived current operational state (priorities, in-flight plans, recent wins). Regenerated from journal + plans.
- `procedures/` — versioned how-tos and stable interface contracts. Mutable in place; breaking changes bump the doc's own version.
- `templates/` — shapes to copy from when authoring new entries. One template per trigger in `protocol.md`.

## Safe to hand-edit

Everything except `.index.json` is plain Markdown you can edit in any editor. A few notes:

- `state.md` and `map.md` ship as hand-authored placeholders — edit them freely until `cortex refresh-state` / `cortex refresh-map` ship in Phase C. Once they ship, prefer the refresh commands and treat hand-edits as temporary.
- Never edit an existing `journal/` entry in place. If new information changes an old conclusion, write a new entry that cites and revises the old one.
- Never edit an accepted `doctrine/` entry's body. Supersede by writing a new entry with `Supersedes: <nnnn>` and flipping the old entry's `Status:` to `Superseded-by <n>` in the same commit.
- `.index.json` is machine-maintained; hand-editing it is a spec violation.

## Next steps

- The agent write contract lives at `@.cortex/protocol.md` — import it into `AGENTS.md` or `CLAUDE.md` so every session follows the same rules.
- Run `cortex doctor` to validate this directory against the spec. It checks scaffold structure, frontmatter, required sections on plans, filename patterns on journal entries, and the seven-field metadata contract on derived layers.
- Run `cortex manifest --budget <N>` to see the session-start slice an agent would load.
