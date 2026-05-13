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

  sed -n "/^${name}()/,/^}/p" "$source_file" >"$output_file"
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
printf 'base\n' >"$TEST_REPO/README"
git -C "$TEST_REPO" add README
git -C "$TEST_REPO" commit -q -m base
git -C "$TEST_REPO" branch feature
git -C "$TEST_REPO" worktree add -q "$FEATURE_WORKTREE" feature

extract_function "worktree_path_for_branch" "$MERGE_SCRIPT" "$FUNCTIONS_FILE"
extract_function "cleanup_pr_worktree_after_merge" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >>"$FUNCTIONS_FILE"
cat >>"$FUNCTIONS_FILE" <<'EOF'
touchstone_emit_event() { :; }
EOF
# shellcheck source=/dev/null
source "$FUNCTIONS_FILE"

export DEFAULT_BRANCH=main
expected="$(git -C "$TEST_REPO" rev-parse --show-toplevel)"
actual="$(cd "$FEATURE_WORKTREE" && worktree_path_for_branch main)"

if [ "$actual" != "$expected" ]; then
  printf 'FAIL: worktree_path_for_branch main returned wrong path\n' >&2
  printf 'expected: %s\n' "$expected" >&2
  printf 'actual:   %s\n' "$actual" >&2
  exit 1
fi

export PR_WORKTREE_PATH="$FEATURE_WORKTREE"
remove_output="$(cd "$TEST_REPO" && cleanup_pr_worktree_after_merge)"
if [ -d "$FEATURE_WORKTREE" ]; then
  printf 'FAIL: clean feature worktree was not removed\n' >&2
  exit 1
fi
if [[ "$remove_output" != *"==> Merged PR worktree removed."* ]]; then
  printf 'FAIL: worktree removal printed wrong output\n' >&2
  printf 'actual: %s\n' "$remove_output" >&2
  exit 1
fi

CURRENT_WORKTREE="$TMP_ROOT/current-worktree"
git -C "$TEST_REPO" branch current
git -C "$TEST_REPO" worktree add -q "$CURRENT_WORKTREE" current
export PR_WORKTREE_PATH="$CURRENT_WORKTREE"
current_output="$(cd "$CURRENT_WORKTREE" && cleanup_pr_worktree_after_merge)"
if [ -d "$CURRENT_WORKTREE" ]; then
  printf 'FAIL: current clean worktree was not removed from sibling default worktree\n' >&2
  exit 1
fi
if [[ "$current_output" != *"==> Merged PR worktree removed."* ]]; then
  printf 'FAIL: current worktree removal printed wrong output\n' >&2
  printf 'actual: %s\n' "$current_output" >&2
  exit 1
fi

DIRTY_WORKTREE="$TMP_ROOT/dirty-worktree"
git -C "$TEST_REPO" branch dirty
git -C "$TEST_REPO" worktree add -q "$DIRTY_WORKTREE" dirty
printf 'dirty\n' >>"$DIRTY_WORKTREE/README"
dirty_stderr="$TMP_ROOT/dirty.stderr"
export PR_WORKTREE_PATH="$DIRTY_WORKTREE"
dirty_output="$(cd "$TEST_REPO" && cleanup_pr_worktree_after_merge 2>"$dirty_stderr")"
if [ ! -d "$DIRTY_WORKTREE" ]; then
  printf 'FAIL: dirty feature worktree was removed\n' >&2
  exit 1
fi
if [ -n "$dirty_output" ]; then
  printf 'FAIL: dirty worktree removal printed stdout\n' >&2
  printf 'actual: %s\n' "$dirty_output" >&2
  exit 1
fi
if ! grep -Fq "WARNING: Merged PR worktree '$DIRTY_WORKTREE' has uncommitted changes; leaving it in place." "$dirty_stderr"; then
  printf 'FAIL: dirty worktree removal did not warn clearly\n' >&2
  cat "$dirty_stderr" >&2
  exit 1
fi

LOCKED_WORKTREE="$TMP_ROOT/locked-worktree"
git -C "$TEST_REPO" branch locked
git -C "$TEST_REPO" worktree add -q "$LOCKED_WORKTREE" locked
git -C "$TEST_REPO" worktree lock --reason "claude agent agent-test" "$LOCKED_WORKTREE"

locked_stderr="$TMP_ROOT/locked.stderr"
export PR_WORKTREE_PATH="$LOCKED_WORKTREE"
locked_output="$(cd "$TEST_REPO" && cleanup_pr_worktree_after_merge 2>"$locked_stderr")"
if [ ! -d "$LOCKED_WORKTREE" ]; then
  printf 'FAIL: locked worktree was removed despite the lock\n' >&2
  exit 1
fi
if [[ "$locked_output" != *"==> Removing merged PR worktree '$LOCKED_WORKTREE' ..."* ]]; then
  printf 'FAIL: locked worktree cleanup did not attempt removal\n' >&2
  printf 'actual: %s\n' "$locked_output" >&2
  exit 1
fi
if ! grep -Fq "WARNING: Could not remove merged PR worktree '$LOCKED_WORKTREE'." "$locked_stderr"; then
  printf 'FAIL: locked worktree cleanup did not warn clearly\n' >&2
  cat "$locked_stderr" >&2
  exit 1
fi

git -C "$TEST_REPO" worktree unlock "$LOCKED_WORKTREE" 2>/dev/null || true

printf 'ok\n'
