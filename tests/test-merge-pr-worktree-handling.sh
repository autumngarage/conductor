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
extract_function "main_worktree_path" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
extract_function "removable_worktree_path_for_branch" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
extract_function "git_common_dir_for_worktree" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
extract_function "worktree_has_uncommitted_changes" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
extract_function "worktree_lock_reason" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
extract_function "remove_clean_merged_branch_worktree" "$MERGE_SCRIPT" "$FUNCTIONS_FILE.tmp"
cat "$FUNCTIONS_FILE.tmp" >> "$FUNCTIONS_FILE"
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

main_worktree="$(cd "$TEST_REPO" && main_worktree_path)"
if [ "$main_worktree" != "$expected" ]; then
  printf 'FAIL: main_worktree_path returned wrong path\n' >&2
  printf 'expected: %s\n' "$expected" >&2
  printf 'actual:   %s\n' "$main_worktree" >&2
  exit 1
fi

main_branch_removable="$(cd "$TEST_REPO" && removable_worktree_path_for_branch main)"
if [ -n "$main_branch_removable" ]; then
  printf 'FAIL: main worktree was considered removable\n' >&2
  printf 'actual: %s\n' "$main_branch_removable" >&2
  exit 1
fi

feature_branch_removable="$(cd "$TEST_REPO" && removable_worktree_path_for_branch feature)"
expected_feature_worktree="$(git -C "$FEATURE_WORKTREE" rev-parse --show-toplevel)"
if [ "$feature_branch_removable" != "$expected_feature_worktree" ]; then
  printf 'FAIL: linked feature worktree was not considered removable\n' >&2
  printf 'expected: %s\n' "$expected_feature_worktree" >&2
  printf 'actual:   %s\n' "$feature_branch_removable" >&2
  exit 1
fi

remove_output="$(cd "$TEST_REPO" && remove_clean_merged_branch_worktree "$FEATURE_WORKTREE")"
if [ -d "$FEATURE_WORKTREE" ]; then
  printf 'FAIL: clean feature worktree was not removed\n' >&2
  exit 1
fi
if [ "$remove_output" != "==> Removed worktree at $FEATURE_WORKTREE" ]; then
  printf 'FAIL: worktree removal printed wrong output\n' >&2
  printf 'actual: %s\n' "$remove_output" >&2
  exit 1
fi

CURRENT_WORKTREE="$TMP_ROOT/current-worktree"
git -C "$TEST_REPO" branch current
git -C "$TEST_REPO" worktree add -q "$CURRENT_WORKTREE" current
current_output="$(cd "$CURRENT_WORKTREE" && remove_clean_merged_branch_worktree "$CURRENT_WORKTREE")"
if [ -d "$CURRENT_WORKTREE" ]; then
  printf 'FAIL: current clean worktree was not removed\n' >&2
  exit 1
fi
if [ "$current_output" != "==> Removed worktree at $CURRENT_WORKTREE" ]; then
  printf 'FAIL: current worktree removal printed wrong output\n' >&2
  printf 'actual: %s\n' "$current_output" >&2
  exit 1
fi

DIRTY_WORKTREE="$TMP_ROOT/dirty-worktree"
git -C "$TEST_REPO" branch dirty
git -C "$TEST_REPO" worktree add -q "$DIRTY_WORKTREE" dirty
printf 'dirty\n' >> "$DIRTY_WORKTREE/README"
dirty_stderr="$TMP_ROOT/dirty.stderr"
dirty_output="$(cd "$TEST_REPO" && remove_clean_merged_branch_worktree "$DIRTY_WORKTREE" 2>"$dirty_stderr")"
if [ ! -d "$DIRTY_WORKTREE" ]; then
  printf 'FAIL: dirty feature worktree was removed\n' >&2
  exit 1
fi
if [ -n "$dirty_output" ]; then
  printf 'FAIL: dirty worktree removal printed stdout\n' >&2
  printf 'actual: %s\n' "$dirty_output" >&2
  exit 1
fi
if ! grep -Fq "WARN: worktree at $DIRTY_WORKTREE has uncommitted changes; not removing" "$dirty_stderr"; then
  printf 'FAIL: dirty worktree removal did not warn clearly\n' >&2
  cat "$dirty_stderr" >&2
  exit 1
fi

# Locked worktree: simulates the swarm-shipping case where a Claude
# Code agent harness locks the worktree it's running in. Removal must
# skip cleanly with a one-line note and return 0 (not fail under set -e).
LOCKED_WORKTREE="$TMP_ROOT/locked-worktree"
git -C "$TEST_REPO" branch locked
git -C "$TEST_REPO" worktree add -q "$LOCKED_WORKTREE" locked
git -C "$TEST_REPO" worktree lock --reason "claude agent agent-test" "$LOCKED_WORKTREE"

# Verify the helper sees the lock.
detected_lock="$(cd "$TEST_REPO" && worktree_lock_reason "$LOCKED_WORKTREE")"
if [ "$detected_lock" != "claude agent agent-test" ]; then
  printf 'FAIL: worktree_lock_reason did not surface the lock reason\n' >&2
  printf 'expected: claude agent agent-test\n' >&2
  printf 'actual:   %s\n' "$detected_lock" >&2
  exit 1
fi

locked_output="$(cd "$TEST_REPO" && remove_clean_merged_branch_worktree "$LOCKED_WORKTREE")"
if [ ! -d "$LOCKED_WORKTREE" ]; then
  printf 'FAIL: locked worktree was removed despite the lock\n' >&2
  exit 1
fi
if [[ "$locked_output" != "==> Worktree at $LOCKED_WORKTREE is locked"* ]]; then
  printf 'FAIL: locked worktree removal did not print expected note\n' >&2
  printf 'actual: %s\n' "$locked_output" >&2
  exit 1
fi
if [[ "$locked_output" != *"claude agent agent-test"* ]]; then
  printf 'FAIL: locked worktree note did not include the lock reason\n' >&2
  printf 'actual: %s\n' "$locked_output" >&2
  exit 1
fi

# Clean up the lock so the trap can rm -rf the tmp tree.
git -C "$TEST_REPO" worktree unlock "$LOCKED_WORKTREE" 2>/dev/null || true

printf 'ok\n'
