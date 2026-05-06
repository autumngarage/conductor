#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MERGE_SCRIPT="$REPO_ROOT/scripts/merge-pr.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/merge-pr-worktree.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

extract_function() {
  local name="$1"
  local source_file="$2"
  local output_file="$3"

  sed -n "/^${name}()/,/^}/p" "$source_file" > "$output_file"
  if ! grep -q "^${name}()" "$output_file"; then
    printf 'FAIL: could not extract %s from %s\n' "$name" "$source_file" >&2
    exit 1
  fi
}

TEST_REPO="$TMP_ROOT/repo"
FEATURE_WORKTREE="$TMP_ROOT/feature-worktree"
FUNCTIONS_FILE="$TMP_ROOT/functions.sh"

git init -q -b main "$TEST_REPO"
git -C "$TEST_REPO" config commit.gpgsign false
git -C "$TEST_REPO" config user.name test
git -C "$TEST_REPO" config user.email test@example.invalid
printf 'base\n' > "$TEST_REPO/README"
git -C "$TEST_REPO" add README
git -C "$TEST_REPO" commit -q -m base
git -C "$TEST_REPO" branch feature
git -C "$TEST_REPO" worktree add -q "$FEATURE_WORKTREE" feature

extract_function "worktree_path_for_branch" "$MERGE_SCRIPT" "$FUNCTIONS_FILE"
# shellcheck source=/dev/null
source "$FUNCTIONS_FILE"

expected="$(git -C "$TEST_REPO" rev-parse --show-toplevel)"
actual="$(cd "$FEATURE_WORKTREE" && worktree_path_for_branch main)"

if [ "$actual" != "$expected" ]; then
  printf 'FAIL: worktree_path_for_branch main returned wrong path\n' >&2
  printf 'expected: %s\n' "$expected" >&2
  printf 'actual:   %s\n' "$actual" >&2
  exit 1
fi

printf 'ok\n'
