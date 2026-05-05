# {{ Release vX.Y.Z — short title }}

**Date:** {{ YYYY-MM-DD }}
**Type:** release
**Trigger:** T1.10
**Tag:** {{ git tag, e.g. v0.3.0 }}
**Cites:** plans/{{ <slug> }}, journal/{{ <date>-<slug> }}

> {{ One sentence: what artifact shipped, where it lives now, which downstream docs reference it. }}

## Artifact

- **Kind:** {{ Homebrew tap | PyPI release | Docker image | GitHub Release | git tag | other }}
- **Location:** {{ e.g. `autumngarage/cortex` tap formula, `pip install cortex==0.3.0`, `ghcr.io/autumngarage/cortex:0.3.0`, https://github.com/autumngarage/cortex/releases/tag/v0.3.0 }}
- **Version:** {{ vX.Y.Z }}
- **Tag:** {{ git tag, e.g. v0.3.0 }}
- **Release notes:** {{ link to GitHub Release page or release-notes section }}

## What shipped

{{ Bulleted list of user-visible changes in this release. Reference the plan(s) this release closes out. }}

## Downstream docs this changes

Files in this repo that reference the artifact location or version, and need a follow-up update if the release changed any of them. List each file even if it didn't change in this release — the list is the set of "places that could go stale if a future release moves the artifact." This is the `--audit-instructions` (v0.5.0) seed.

- {{ CLAUDE.md — install command, version reference }}
- {{ README.md — install / quickstart }}
- {{ Homebrew tap repo (`autumngarage/homebrew-cortex`) — formula `url` + `sha256` }}
- {{ docs/PITCH.md — version mention if applicable }}
- {{ ... }}

## Follow-ups (deferred to future work)

- [ ] {{ item — resolved to: plans/<new-slug> | journal/<date>-<slug> | doctrine/<nnnn>-<slug> }}

(Per SPEC § 4.2, deferred items must resolve to another Plan, Journal entry, or Doctrine entry in the same commit as the release entry.)
