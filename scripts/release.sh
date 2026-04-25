#!/usr/bin/env bash
#
# scripts/release.sh — cut a conductor release.
#
# Usage:
#   scripts/release.sh --patch   # default
#   scripts/release.sh --minor
#   scripts/release.sh --major
#
# Conductor uses hatch-vcs, so the version is derived from the git tag —
# no source files to bump, no commit to make. The helper just tags, pushes
# the tag, and creates the GitHub release. The release-published event
# fires .github/workflows/release.yml, which auto-bumps the homebrew tap.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

bump="${1:---patch}"
case "$bump" in
  --major|--minor|--patch) ;;
  *) echo "ERROR: unknown bump arg: $bump (use --major, --minor, --patch)" >&2; exit 1 ;;
esac

command -v gh >/dev/null 2>&1 || { echo "ERROR: gh CLI not installed (need it for gh release create)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh not authenticated (run: gh auth login)" >&2; exit 1; }

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "ERROR: must be on main (currently $branch)" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "ERROR: working tree dirty" >&2; exit 1; }
git fetch --tags origin >/dev/null
[ "$(git rev-list --left-right --count origin/main...main)" = "0	0" ] || { echo "ERROR: local main out of sync with origin" >&2; exit 1; }

current_tag="$(git tag -l --sort=-v:refname 'v*' | head -1)"
current_version="${current_tag#v}"
IFS='.' read -r major minor patch <<< "$current_version"
case "$bump" in
  --major) major=$((major + 1)); minor=0; patch=0 ;;
  --minor) minor=$((minor + 1)); patch=0 ;;
  --patch) patch=$((patch + 1)) ;;
esac
new_tag="v${major}.${minor}.${patch}"

echo "==> Current: $current_tag"
echo "==> New:     $new_tag"

# Pin the release target to the verified-in-sync HEAD SHA, not the branch
# name — otherwise a commit that lands on origin/main between the sync
# check above and the gh call below would be released instead. The tag
# is created server-side as part of the release, so a failed release
# leaves no orphan tag for the next run to skip past.
target_sha="$(git rev-parse HEAD)"
gh release create "$new_tag" --target "$target_sha" --generate-notes
git fetch --tags origin >/dev/null || true

echo
echo "  ✓ Released $new_tag"
echo "  Tap bump is in flight via .github/workflows/release.yml"
echo "  Watch: gh run list --workflow=release.yml --repo autumngarage/conductor --limit 1"
echo "  Upgrade: brew update && brew upgrade conductor"
