#!/usr/bin/env bash
#
# scripts/spawn-worktree.sh — create an isolated worktree for parallel work.
#
# Usage:
#   bash scripts/spawn-worktree.sh <type>/<slug>
#   bash scripts/spawn-worktree.sh <type>/<slug> ../explicit-path
#
# The default worktree path is ../<repo-name>-<slug-without-type>.
# If .worktreeinclude exists at the repo root, ignored untracked files matching
# its gitignore-style patterns are copied into the new worktree.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  source "$SCRIPT_DIR/../lib/events.sh"
else
  touchstone_emit_event() { :; }
fi

usage() {
  awk 'NR>2 && !/^#/ { exit } NR>2 { sub(/^# ?/, ""); print }' "$0"
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage >&2
  exit 1
fi

case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
esac

BRANCH="$1"
EXPLICIT_PATH="${2:-}"

if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "ERROR: spawn-worktree.sh must run inside a git repository." >&2
  exit 1
fi

cd "$REPO_ROOT"

case "$BRANCH" in
  */*)
    TYPE="${BRANCH%%/*}"
    SLUG="${BRANCH#*/}"
    ;;
  *)
    echo "ERROR: branch must follow <type>/<slug>, got '$BRANCH'." >&2
    exit 1
    ;;
esac

if [ -z "$TYPE" ] || [ -z "$SLUG" ] || [ "$SLUG" = "$BRANCH" ]; then
  echo "ERROR: branch must follow <type>/<slug>, got '$BRANCH'." >&2
  exit 1
fi

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "ERROR: branch already exists: $BRANCH" >&2
  exit 1
fi

REPO_NAME="$(basename "$REPO_ROOT")"
WORKTREE_PATH="${EXPLICIT_PATH:-../$REPO_NAME-$SLUG}"
WORKTREE_PARENT="$(dirname "$WORKTREE_PATH")"

if [ -e "$WORKTREE_PATH" ]; then
  echo "ERROR: worktree path already exists: $WORKTREE_PATH" >&2
  exit 1
fi

if [ ! -d "$WORKTREE_PARENT" ]; then
  echo "ERROR: parent directory does not exist: $WORKTREE_PARENT" >&2
  exit 1
fi

resolve_default_ref() {
  local origin_head ref branch
  origin_head="$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [ -n "$origin_head" ]; then
    ref="$origin_head"
    ref="${ref#refs/remotes/}"
    if git rev-parse --verify --quiet "$ref" >/dev/null; then
      printf '%s\n' "$ref"
      return 0
    fi
  fi

  for branch in main master; do
    if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
      printf '%s\n' "origin/$branch"
      return 0
    fi
    if git rev-parse --verify --quiet "$branch" >/dev/null; then
      printf '%s\n' "$branch"
      return 0
    fi
  done

  echo "ERROR: could not resolve a default branch ref (origin/HEAD, main, or master)." >&2
  return 1
}

guard_worktreeinclude_patterns() {
  local include_file="$1" raw pattern

  while IFS= read -r raw || [ -n "$raw" ]; do
    pattern="${raw#"${raw%%[![:space:]]*}"}"
    pattern="${pattern%"${pattern##*[![:space:]]}"}"
    [ -z "$pattern" ] && continue
    case "$pattern" in
      \#*) continue ;;
      !*)
        echo "ERROR: negated .worktreeinclude patterns are not supported: $pattern" >&2
        exit 1
        ;;
    esac
  done <"$include_file"
}

DEFAULT_REF="$(resolve_default_ref)"
BASE_BRANCH="${DEFAULT_REF#origin/}"

# Validate .worktreeinclude before any external side effect (branch creation,
# worktree directory) so a bad pattern doesn't leave behind half-spawned state
# that blocks retry.
INCLUDE_FILE="$REPO_ROOT/.worktreeinclude"
if [ -f "$INCLUDE_FILE" ]; then
  guard_worktreeinclude_patterns "$INCLUDE_FILE"
fi

echo "==> Creating worktree"
echo "    branch: $BRANCH"
echo "    path:   $WORKTREE_PATH"
echo "    base:   $DEFAULT_REF"
git worktree add "$WORKTREE_PATH" --no-track -b "$BRANCH" "$DEFAULT_REF"
touchstone_emit_event worktree_created \
  branch="$BRANCH" \
  worktree_path="$WORKTREE_PATH" \
  base_branch="$BASE_BRANCH" \
  repo_root="$REPO_ROOT"
# Keep first push ergonomic without wiring the new branch to origin/main.
git -C "$WORKTREE_PATH" config extensions.worktreeConfig true
git -C "$WORKTREE_PATH" config --worktree push.default current

COPIED=0
if [ -f "$INCLUDE_FILE" ]; then
  echo ""
  echo "==> Copying ignored files from .worktreeinclude"

  # Pattern matching is delegated to git so .worktreeinclude follows real
  # gitignore semantics (e.g. `local/*.json` does not match `local/x/y.json`).
  # We intersect two ls-files queries: files matched by .worktreeinclude as an
  # exclude-from, and files matched by the project's standard ignore set. The
  # intersection is the set of explicitly allowlisted, gitignored files.
  INCLUDE_LIST="$(mktemp)"
  STANDARD_LIST="$(mktemp)"
  trap 'rm -f "$INCLUDE_LIST" "$STANDARD_LIST"' EXIT
  git ls-files --others --ignored --exclude-from="$INCLUDE_FILE" \
    | LC_ALL=C sort >"$INCLUDE_LIST"
  git ls-files --others --ignored --exclude-standard \
    | LC_ALL=C sort >"$STANDARD_LIST"

  while IFS= read -r ignored_file; do
    [ -n "$ignored_file" ] || continue
    [ -f "$ignored_file" ] || continue
    mkdir -p "$WORKTREE_PATH/$(dirname "$ignored_file")"
    cp -p "$ignored_file" "$WORKTREE_PATH/$ignored_file"
    echo "    copied: $ignored_file"
    COPIED=$((COPIED + 1))
  done < <(comm -12 "$INCLUDE_LIST" "$STANDARD_LIST")

  if [ "$COPIED" -eq 0 ]; then
    echo "    (no matching ignored files)"
  fi
fi

SETUP_SCRIPT="$WORKTREE_PATH/scripts/setup-worktree-local.sh"
if [ -x "$SETUP_SCRIPT" ]; then
  echo ""
  echo "==> Running scripts/setup-worktree-local.sh"
  (cd "$WORKTREE_PATH" && bash scripts/setup-worktree-local.sh)
elif [ -f "$SETUP_SCRIPT" ]; then
  echo ""
  echo "==> Running scripts/setup-worktree-local.sh"
  (cd "$WORKTREE_PATH" && bash scripts/setup-worktree-local.sh)
fi

echo ""
echo "==> Worktree ready"
echo "    path:   $WORKTREE_PATH"
echo "    branch: $BRANCH"
echo "    next:   cd $WORKTREE_PATH"
