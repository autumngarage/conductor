#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPEN_PR_SCRIPT="$REPO_ROOT/scripts/open-pr.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/open-pr-verify-merged.XXXXXX")"
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

FUNCTIONS_FILE="$TMP_ROOT/functions.sh"
extract_function "verify_pr_merged" "$OPEN_PR_SCRIPT" "$FUNCTIONS_FILE"
# shellcheck source=/dev/null
source "$FUNCTIONS_FILE"

FAKE_BIN="$TMP_ROOT/bin"
mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

args="$*"
mode="${GH_MODE:-open}"

if [[ "$args" == *"--json mergedAt"* ]]; then
  case "$mode" in
    mergedat) printf '2026-05-18T16:00:00Z\n' ;;
    *) printf '\n' ;;
  esac
  exit 0
fi

if [[ "$args" == *"--json state"* ]]; then
  case "$mode" in
    state-merged) printf 'MERGED\n' ;;
    *) printf 'OPEN\n' ;;
  esac
  exit 0
fi

exit 1
EOF
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"

if ! GH_MODE=mergedat verify_pr_merged 488 >"$TMP_ROOT/mergedat.out"; then
  printf 'FAIL: verify_pr_merged should succeed when mergedAt is present\n' >&2
  exit 1
fi
if ! grep -Fq "Verified: PR #488 merged at" "$TMP_ROOT/mergedat.out"; then
  printf 'FAIL: mergedAt success output missing verification line\n' >&2
  cat "$TMP_ROOT/mergedat.out" >&2
  exit 1
fi

if ! GH_MODE=state-merged verify_pr_merged 489 >"$TMP_ROOT/state-merged.out"; then
  printf 'FAIL: verify_pr_merged should succeed when state is MERGED and mergedAt lags\n' >&2
  exit 1
fi
if ! grep -Fq "state=MERGED; mergedAt pending" "$TMP_ROOT/state-merged.out"; then
  printf 'FAIL: state=MERGED success output missing verification line\n' >&2
  cat "$TMP_ROOT/state-merged.out" >&2
  exit 1
fi

if GH_MODE=open verify_pr_merged 490 >"$TMP_ROOT/open.out"; then
  printf 'FAIL: verify_pr_merged should fail when PR is not merged\n' >&2
  exit 1
fi

printf 'ok\n'
