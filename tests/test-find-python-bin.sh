#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOUCHSTONE_SCRIPT="$REPO_ROOT/scripts/touchstone-run.sh"
PYTEST_SCRIPT="$REPO_ROOT/scripts/run-pytest-in-venv.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_eq() {
  local name="$1" expected="$2" actual="$3"

  if [ "$actual" != "$expected" ]; then
    printf 'FAIL: %s\nexpected: [%s]\nactual:   [%s]\n' "$name" "$expected" "$actual" >&2
    exit 1
  fi
}

assert_contains() {
  local name="$1" haystack="$2" needle="$3"

  case "$haystack" in
    *"$needle"*) ;;
    *) fail "$name missing [$needle] in [$haystack]" ;;
  esac
}

setup_worktree_fixture() {
  local root="$1" parent worktree

  parent="$root/parent"
  worktree="$root/foo"

  mkdir -p "$parent/.git/worktrees/foo" "$parent/.venv/bin" "$worktree"
  printf '#!/usr/bin/env bash\n' > "$parent/.venv/bin/python"
  chmod +x "$parent/.venv/bin/python"
  printf 'gitdir: %s\n' "$parent/.git/worktrees/foo" > "$worktree/.git"
}

run_touchstone_lookup() {
  local worktree="$1"
  (
    cd "$worktree"
    # shellcheck source=/dev/null
    TOUCHSTONE_RUN_SOURCE_ONLY=1 source "$TOUCHSTONE_SCRIPT"
    find_python_bin
  )
}

run_pytest_lookup() {
  local worktree="$1"
  (
    cd "$worktree"
    # shellcheck source=/dev/null
    RUN_PYTEST_IN_VENV_SOURCE_ONLY=1 source "$PYTEST_SCRIPT"
    find_python
  )
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

setup_worktree_fixture "$tmp/with-venv"
expected="$tmp/with-venv/parent/.venv/bin/python"

actual="$(run_touchstone_lookup "$tmp/with-venv/foo")"
assert_eq "touchstone worktree parent venv" "$expected" "$actual"

actual="$(run_pytest_lookup "$tmp/with-venv/foo")"
assert_eq "pytest helper worktree parent venv" "$expected" "$actual"

mkdir -p "$tmp/no-venv/parent/.git/worktrees/foo" "$tmp/no-venv/foo"
printf 'gitdir: %s\n' "$tmp/no-venv/parent/.git/worktrees/foo" > "$tmp/no-venv/foo/.git"

err_file="$tmp/touchstone.err"
if run_touchstone_lookup "$tmp/no-venv/foo" > "$tmp/touchstone.out" 2> "$err_file"; then
  fail "touchstone lookup should fail without any project venv"
fi
err="$(cat "$err_file")"
assert_contains "touchstone error" "$err" "ERROR: no project virtualenv found."
assert_contains "touchstone checkout tried" "$err" "Tried: $tmp/no-venv/foo/.venv/bin/python (this checkout)"
assert_contains "touchstone parent tried" "$err" "Tried: $tmp/no-venv/parent/.venv/bin/python (worktree parent)"

err_file="$tmp/pytest.err"
if run_pytest_lookup "$tmp/no-venv/foo" > "$tmp/pytest.out" 2> "$err_file"; then
  fail "pytest helper lookup should fail without any project venv"
fi
err="$(cat "$err_file")"
assert_contains "pytest error" "$err" "ERROR: no project virtualenv found."
assert_contains "pytest checkout tried" "$err" "Tried: $tmp/no-venv/foo/.venv/bin/python (this checkout)"
assert_contains "pytest parent tried" "$err" "Tried: $tmp/no-venv/parent/.venv/bin/python (worktree parent)"

printf 'ok\n'
