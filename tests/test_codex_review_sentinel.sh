#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/codex-review.sh"

run_detector() {
  local input="$1"
  printf '%s' "$input" | CODEX_REVIEW_TEST_SENTINEL=1 bash "$SCRIPT"
}

assert_detector() {
  local name="$1"
  local input="$2"
  local expected="$3"
  local actual

  actual="$(run_detector "$input")"
  if [ "$actual" != "$expected" ]; then
    printf 'FAIL: %s\nexpected: [%s]\nactual:   [%s]\n' "$name" "$expected" "$actual" >&2
    exit 1
  fi
}

assert_detector \
  "exact sentinel" \
  $'Summary\nCODEX_REVIEW_CLEAN\n' \
  "CODEX_REVIEW_CLEAN"

assert_detector \
  "trailing whitespace" \
  $'Summary\nCODEX_REVIEW_CLEAN   \n\n' \
  "CODEX_REVIEW_CLEAN"

assert_detector \
  "footer after sentinel" \
  $'LGTM\nCODEX_REVIEW_CLEAN\n---\nreview complete\n' \
  "CODEX_REVIEW_CLEAN"

assert_detector \
  "indented sentinel" \
  $'Summary\n  CODEX_REVIEW_FIXED\t\nextra note\n' \
  "CODEX_REVIEW_FIXED"

assert_detector \
  "inline sentinel rejected" \
  $'Summary: CODEX_REVIEW_CLEAN\n' \
  ""

assert_detector \
  "multiple sentinel lines rejected" \
  $'CODEX_REVIEW_CLEAN\nCODEX_REVIEW_BLOCKED\n' \
  ""

# Regression: CONDUCTOR_BIN must be tokenized into CONDUCTOR_BIN_ARGV so that
# multi-word values (e.g. CONDUCTOR_BIN="uv run conductor") work in
# `command -v` checks and as command + args. The only legitimate single-token
# read of CONDUCTOR_BIN is the `read -ra` tokenization itself.
bad_uses="$(grep -nE '"\$CONDUCTOR_BIN"' "$SCRIPT" | grep -v 'read -ra' || true)"
if [ -n "$bad_uses" ]; then
  printf 'FAIL: CONDUCTOR_BIN must use ${CONDUCTOR_BIN_ARGV[@]} for invocation; found single-token use:\n%s\n' "$bad_uses" >&2
  exit 1
fi

route_preflight_args="$(grep -n 'args=(route --json' "$SCRIPT" | head -1)"
case "$route_preflight_args" in
  *'--kind "$subcommand"'*) ;;
  *)
    printf 'FAIL: route preflight must pass --kind "$subcommand" so review preflight and semantic review dispatch use the same routing semantics.\n%s\n' "$route_preflight_args" >&2
    exit 1
    ;;
esac

route_preflight_function="$(sed -n '/^conductor_route_preflight_for_phase()/,/^}/p' "$SCRIPT")"
case "$route_preflight_function" in
  *'args+=(--with "$CONDUCTOR_WITH")'*) ;;
  *)
    printf 'FAIL: pinned route preflight must pass --with "$CONDUCTOR_WITH" to conductor route.\n' >&2
    exit 1
    ;;
esac

exec_harness="$(mktemp)"
exec_args_file="$(mktemp)"
validate_harness="$(mktemp)"
trap 'rm -f "$exec_harness" "$exec_args_file" "$validate_harness"' EXIT
{
  sed -n '/^conductor_should_use_semantic_review()/,/^}/p' "$SCRIPT"
  sed -n '/^conductor_effective_with_for_phase()/,/^}/p' "$SCRIPT"
  sed -n '/^conductor_subcommand_for_mode()/,/^}/p' "$SCRIPT"
  sed -n '/^conductor_tools_for_mode()/,/^}/p' "$SCRIPT"
  sed -n '/^conductor_inner_timeout()/,/^}/p' "$SCRIPT"
  sed -n '/^reviewer_conductor_exec()/,/^}/p' "$SCRIPT"
  cat <<'HARNESS'
conductor() {
  printf '%s\n' "$*" >"$EXEC_ARGS_FILE"
  cat >/dev/null
  printf 'CODEX_REVIEW_FIXED\n'
}

REVIEW_MODE=fix
REVIEW_PHASE=fix
REVIEW_TIMEOUT=120
REVIEW_MAX_STALL_SEC=17
CONDUCTOR_WITH=codex
CONDUCTOR_EFFORT=high
REVIEW_CONDUCTOR_LOG_FILE=/tmp/touchstone-review-conductor.log
CODEX_REVIEW_PR_NUMBER=

reviewer_conductor_exec "fix prompt" >/dev/null
HARNESS
} >"$exec_harness"

EXEC_ARGS_FILE="$exec_args_file" bash "$exec_harness"
exec_args="$(cat "$exec_args_file")"
case "$exec_args" in
  exec\ *'--max-stall-seconds 17'*) ;;
  *)
    printf 'FAIL: fix-phase conductor exec must pass REVIEW_MAX_STALL_SEC as --max-stall-seconds.\nargs: %s\n' "$exec_args" >&2
    exit 1
    ;;
esac

# ── validate_fix_classification regression tests ──────────────────────────
{
  printf 'REVIEWER_LABEL=test-reviewer\n'
  sed -n '/^fix_classification_kind()/,/^}/p' "$SCRIPT"
  sed -n '/^validate_fix_classification()/,/^}/p' "$SCRIPT"
  cat <<'HARNESS'

assert_validate() {
  local name="$1" input="$2" should_pass="$3" actual
  if validate_fix_classification "$input" >/dev/null 2>&1; then
    actual="pass"
  else
    actual="fail"
  fi
  if [ "$actual" != "$should_pass" ]; then
    printf 'FAIL: validate_fix_classification: %s\nexpected: %s  got: %s\n' "$name" "$should_pass" "$actual" >&2
    exit 1
  fi
}

assert_validate "missing classification" \
  $'Some output\nCODEX_REVIEW_FIXED' \
  fail

assert_validate "malformed kind" \
  $'FIX_CLASSIFICATION: bogus-kind; findings-addressed: 1\nCODEX_REVIEW_FIXED' \
  fail

assert_validate "missing findings addressed" \
  $'FIX_CLASSIFICATION: substantive\nCODEX_REVIEW_FIXED' \
  fail

assert_validate "malformed findings addressed" \
  $'FIX_CLASSIFICATION: substantive; findings-addressed: many\nCODEX_REVIEW_FIXED' \
  fail

assert_validate "duplicate classification lines" \
  $'FIX_CLASSIFICATION: substantive; findings-addressed: 1\nFIX_CLASSIFICATION: style-only; findings-addressed: 0\nCODEX_REVIEW_FIXED' \
  fail

assert_validate "valid substantive" \
  $'FIX_CLASSIFICATION: substantive; findings-addressed: 2\nCODEX_REVIEW_FIXED' \
  pass

assert_validate "valid style-only" \
  $'FIX_CLASSIFICATION: style-only; findings-addressed: 0\nCODEX_REVIEW_FIXED' \
  pass

assert_validate "valid mixed" \
  $'FIX_CLASSIFICATION: mixed; findings-addressed: 1\nCODEX_REVIEW_FIXED' \
  pass
HARNESS
} >"$validate_harness"
bash "$validate_harness"

printf 'ok\n'
