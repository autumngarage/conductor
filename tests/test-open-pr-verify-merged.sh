#!/usr/bin/env bash
# Regression tests for verify_pr_merged in scripts/open-pr.sh (issue #489).
#
# The previous implementation read only `mergedAt` from a single `gh pr view`
# call, which produced false-alarm ORPHAN RISK banners on PRs that were
# actually merged — the API briefly returned empty `mergedAt` right after
# `gh pr merge` returned. The fix prefers `state == MERGED` as authoritative
# and retries once on transient empties.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPEN_PR_SCRIPT="$REPO_ROOT/scripts/open-pr.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/open-pr-verify.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

FUNCTIONS_FILE="$TMP_ROOT/functions.sh"

# Extract the verify_pr_merged function only (the rest of open-pr.sh has
# side effects on source). Strip the comment block above it so awk's brace
# matching doesn't confuse helper code.
awk '/^verify_pr_merged\(\)/,/^}$/' "$OPEN_PR_SCRIPT" >"$FUNCTIONS_FILE"
if ! grep -q "^verify_pr_merged()" "$FUNCTIONS_FILE"; then
  printf 'FAIL: could not extract verify_pr_merged from %s\n' "$OPEN_PR_SCRIPT" >&2
  exit 1
fi

# Inject a fake `gh` whose behavior is driven by FAKE_GH_PAYLOAD env vars.
# Each call consumes the next payload and increments the call counter so we
# can assert the retry-after-empty path. Use a counter file because env
# mutations don't propagate out of the gh function back to the test runner.
COUNTER_FILE="$TMP_ROOT/calls"
PAYLOAD_DIR="$TMP_ROOT/payloads"
mkdir -p "$PAYLOAD_DIR"

cat >>"$FUNCTIONS_FILE" <<'STUB'
gh() {
  if [ "$1" != "pr" ] || [ "$2" != "view" ]; then
    printf 'unexpected gh call: %s\n' "$*" >&2
    return 1
  fi
  local n
  n="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"
  n=$((n + 1))
  printf '%s\n' "$n" >"$COUNTER_FILE"
  if [ -f "$PAYLOAD_DIR/$n" ]; then
    cat "$PAYLOAD_DIR/$n"
    return 0
  fi
  return 1
}
STUB

# shellcheck source=/dev/null
source "$FUNCTIONS_FILE"

# Make the retry backoff effectively zero so the test runs fast.
export VERIFY_PR_MERGED_BACKOFF_SEC=0
export COUNTER_FILE PAYLOAD_DIR

assert_call_count() {
  local expected="$1" actual
  actual="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"
  if [ "$actual" != "$expected" ]; then
    printf 'FAIL: expected %s gh calls, got %s\n' "$expected" "$actual" >&2
    exit 1
  fi
}

reset_fake() {
  printf '0\n' >"$COUNTER_FILE"
  rm -f "$PAYLOAD_DIR"/*
}

# ---------------------------------------------------------------------------
# Test 1: clean happy path — first call returns MERGED + mergedAt.
# ---------------------------------------------------------------------------
reset_fake
printf '{"state":"MERGED","mergedAt":"2026-05-20T12:00:00Z"}\n' >"$PAYLOAD_DIR/1"
if ! out="$(verify_pr_merged 100 2>&1)"; then
  printf 'FAIL test 1: verify_pr_merged returned non-zero on clear MERGED state\n' >&2
  exit 1
fi
echo "$out" | grep -q "Verified: PR #100 merged at 2026-05-20T12:00:00Z" \
  || {
    printf 'FAIL test 1: expected Verified line, got: %s\n' "$out" >&2
    exit 1
  }
assert_call_count 1
printf 'PASS test 1: clean MERGED + mergedAt\n'

# ---------------------------------------------------------------------------
# Test 2: the #489 bug case — MERGED but mergedAt briefly empty. Should
# still return 0 because state is authoritative.
# ---------------------------------------------------------------------------
reset_fake
printf '{"state":"MERGED","mergedAt":""}\n' >"$PAYLOAD_DIR/1"
if ! out="$(verify_pr_merged 200 2>&1)"; then
  printf 'FAIL test 2: returned non-zero when state=MERGED but mergedAt empty (the regression)\n' >&2
  exit 1
fi
echo "$out" | grep -q "state=MERGED" \
  || {
    printf 'FAIL test 2: expected state=MERGED diagnostic, got: %s\n' "$out" >&2
    exit 1
  }
assert_call_count 1
printf 'PASS test 2: MERGED state with empty mergedAt (the #489 fix)\n'

# ---------------------------------------------------------------------------
# Test 3: transient empty on first call, MERGED on retry. Should succeed.
# ---------------------------------------------------------------------------
reset_fake
printf '{"state":"OPEN","mergedAt":""}\n' >"$PAYLOAD_DIR/1"
printf '{"state":"MERGED","mergedAt":"2026-05-20T12:00:01Z"}\n' >"$PAYLOAD_DIR/2"
if ! verify_pr_merged 300 >/dev/null 2>&1; then
  printf 'FAIL test 3: retry path did not succeed\n' >&2
  exit 1
fi
assert_call_count 2
printf 'PASS test 3: retry succeeds when first attempt sees stale OPEN\n'

# ---------------------------------------------------------------------------
# Test 4: genuinely open PR — both attempts return OPEN. Must return 1.
# ---------------------------------------------------------------------------
reset_fake
printf '{"state":"OPEN","mergedAt":""}\n' >"$PAYLOAD_DIR/1"
printf '{"state":"OPEN","mergedAt":""}\n' >"$PAYLOAD_DIR/2"
if verify_pr_merged 400 >/dev/null 2>&1; then
  printf 'FAIL test 4: verify_pr_merged returned 0 on truly open PR\n' >&2
  exit 1
fi
assert_call_count 2
printf 'PASS test 4: open PR correctly reports not merged\n'

# ---------------------------------------------------------------------------
# Test 5: gh fails entirely (network/auth). Must return 1, must not crash.
# ---------------------------------------------------------------------------
reset_fake
# No payloads written -> fake gh returns non-zero with empty output.
if verify_pr_merged 500 >/dev/null 2>&1; then
  printf 'FAIL test 5: verify_pr_merged returned 0 when gh failed entirely\n' >&2
  exit 1
fi
assert_call_count 2
printf 'PASS test 5: gh failure reports not merged (does not crash)\n'

printf '\nALL verify_pr_merged regression tests passed.\n'
