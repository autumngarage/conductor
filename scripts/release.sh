#!/usr/bin/env bash
#
# scripts/release.sh — cut a conductor release.
#
# Usage:
#   scripts/release.sh --patch   # default
#   scripts/release.sh --minor
#   scripts/release.sh --major
#   scripts/release.sh --patch --wait
#   scripts/release.sh --patch --wait --update-consumers --consumer-config consumers.toml
#
# Conductor uses hatch-vcs, so the version is derived from the git tag —
# no source files to bump, no commit to make. The helper just tags, pushes
# the tag, and creates the GitHub release. The release-published event
# fires .github/workflows/release.yml, which auto-bumps the homebrew tap.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF_USAGE'
Usage: scripts/release.sh [--patch|--minor|--major] [options]

Options:
  --wait                       Watch the release workflow and fail if it fails.
  --update-consumers           Run `conductor update-all` after the release step.
  --consumer-paths PATHS       Comma-separated repos for `conductor update-all`.
  --consumer-config FILE       TOML config file for `conductor update-all`.
  --consumer-branch BRANCH     Branch name for consumer refreshes.
  --no-auto-stash              Pass through to `conductor update-all`.
  -h, --help                   Show this help.
EOF_USAGE
}

bump="--patch"
wait_for_release=false
update_consumers=false
consumer_paths=""
consumer_config=""
consumer_branch=""
consumer_no_auto_stash=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --major | --minor | --patch)
      bump="$1"
      ;;
    --wait)
      wait_for_release=true
      ;;
    --update-consumers)
      update_consumers=true
      ;;
    --consumer-paths)
      shift
      [ "$#" -gt 0 ] || {
        echo "ERROR: --consumer-paths requires a value" >&2
        exit 1
      }
      consumer_paths="$1"
      ;;
    --consumer-config)
      shift
      [ "$#" -gt 0 ] || {
        echo "ERROR: --consumer-config requires a value" >&2
        exit 1
      }
      consumer_config="$1"
      ;;
    --consumer-branch)
      shift
      [ "$#" -gt 0 ] || {
        echo "ERROR: --consumer-branch requires a value" >&2
        exit 1
      }
      consumer_branch="$1"
      ;;
    --no-auto-stash)
      consumer_no_auto_stash=true
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown arg: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

command -v gh >/dev/null 2>&1 || {
  echo "ERROR: gh CLI not installed (need it for gh release create)" >&2
  exit 1
}
gh auth status >/dev/null 2>&1 || {
  echo "ERROR: gh not authenticated (run: gh auth login)" >&2
  exit 1
}

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || {
  echo "ERROR: must be on main (currently $branch)" >&2
  exit 1
}
[ -z "$(git status --porcelain)" ] || {
  echo "ERROR: working tree dirty" >&2
  exit 1
}
git fetch --tags origin >/dev/null
[ "$(git rev-list --left-right --count origin/main...main)" = "0	0" ] || {
  echo "ERROR: local main out of sync with origin" >&2
  exit 1
}

current_tag="$(git tag -l --sort=-v:refname 'v*' | head -1)"
current_version="${current_tag#v}"
IFS='.' read -r major minor patch <<<"$current_version"
case "$bump" in
  --major)
    major=$((major + 1))
    minor=0
    patch=0
    ;;
  --minor)
    minor=$((minor + 1))
    patch=0
    ;;
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

release_run_id() {
  local tag="$1"
  gh run list \
    --workflow=release.yml \
    --repo autumngarage/conductor \
    --limit 20 \
    --json databaseId,displayTitle,headBranch \
    --jq '.[] | select(.headBranch == "'"$tag"'" or .displayTitle == "'"$tag"'") | .databaseId' \
    | head -1
}

if [ "$wait_for_release" = true ]; then
  echo "==> Waiting for release workflow for $new_tag ..."
  run_id=""
  for _attempt in $(seq 1 20); do
    run_id="$(release_run_id "$new_tag")"
    [ -n "$run_id" ] && break
    sleep 3
  done
  if [ -z "$run_id" ]; then
    echo "ERROR: release workflow run for $new_tag did not appear" >&2
    exit 1
  fi
  gh run watch "$run_id" --repo autumngarage/conductor --exit-status
fi

if [ "$update_consumers" = true ]; then
  echo "==> Refreshing configured consumer repos to $new_tag ..."
  update_args=(update-all)
  [ -n "$consumer_paths" ] && update_args+=(--paths "$consumer_paths")
  [ -n "$consumer_config" ] && update_args+=(--config-file "$consumer_config")
  update_args+=(--branch "${consumer_branch:-chore/conductor-refresh-${new_tag}}")
  [ "$consumer_no_auto_stash" = true ] && update_args+=(--no-auto-stash)
  conductor "${update_args[@]}"
fi

echo
echo "  ✓ Released $new_tag"
echo "  Tap bump is in flight via .github/workflows/release.yml"
echo "  Watch: gh run list --workflow=release.yml --repo autumngarage/conductor --limit 1"
echo "  Upgrade: brew update && brew upgrade autumngarage/conductor/conductor"
