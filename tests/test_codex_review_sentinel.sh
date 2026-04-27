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

printf 'ok\n'
