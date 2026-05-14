#!/usr/bin/env bash
#
# lib/preflight.sh — deterministic review preflight checks.
#
# Public entrypoint:
#   touchstone_preflight_main [--diff <base-ref>|--all-files] [repo-root]
#
# Tooling policy: missing local linters are skipped with a visible line, not
# treated as failures. Touchstone projects can run on fresh machines where the
# deterministic gate should still enforce every installed check and the test
# suite without turning optional dev-tool installation into a merge blocker.
#
set -euo pipefail

PREFLIGHT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$PREFLIGHT_LIB_DIR/preflight-scope.sh" ]; then
  # shellcheck source=lib/preflight-scope.sh
  source "$PREFLIGHT_LIB_DIR/preflight-scope.sh"
fi

TOUCHSTONE_PREFLIGHT_SCOPE_MODE="${TOUCHSTONE_PREFLIGHT_SCOPE_MODE:-all}"
TOUCHSTONE_PREFLIGHT_DIFF_BASE="${TOUCHSTONE_PREFLIGHT_DIFF_BASE:-}"
TOUCHSTONE_PREFLIGHT_CACHE_KEY=""
TOUCHSTONE_PREFLIGHT_CACHE_FILE=""
TOUCHSTONE_PREFLIGHT_CACHE_INPUTS=""

touchstone_preflight_info() { printf '==> %s\n' "$*"; }
touchstone_preflight_ok() { printf '  OK %s\n' "$*"; }
touchstone_preflight_skip() { printf '  SKIP %s\n' "$*"; }
touchstone_preflight_fail() { printf '  FAIL %s\n' "$*" >&2; }

touchstone_preflight_truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

touchstone_preflight_unset_review_env() {
  unset TOUCHSTONE_CONDUCTOR_WITH
  unset TOUCHSTONE_CONDUCTOR_PREFER
  unset TOUCHSTONE_CONDUCTOR_EFFORT
  unset TOUCHSTONE_CONDUCTOR_TAGS
  unset TOUCHSTONE_CONDUCTOR_EXCLUDE
  unset TOUCHSTONE_REVIEWER
  unset CODEX_REVIEW_ASSIST
  unset CODEX_REVIEW_ASSIST_TIMEOUT
  unset CODEX_REVIEW_ASSIST_MAX_ROUNDS
  unset CODEX_REVIEW_BASE
  unset CODEX_REVIEW_BRANCH_NAME
  unset CODEX_REVIEW_CACHE_CLEAN
  unset CODEX_REVIEW_CONTEXT_MODE
  unset CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES
  unset CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES
  unset CODEX_REVIEW_DISABLE_CACHE
  unset CODEX_REVIEW_ENABLED
  unset CODEX_REVIEW_DIAGNOSTICS_FILE
  unset CODEX_REVIEW_FINDINGS_HISTORY_FILE
  unset CODEX_REVIEW_FORCE
  unset CODEX_REVIEW_MAX_DIFF_LINES
  unset CODEX_REVIEW_MAX_ITERATIONS
  unset CODEX_REVIEW_MODE
  unset CODEX_REVIEW_NO_AUTOFIX
  unset CODEX_REVIEW_ON_ERROR
  unset CODEX_REVIEW_SUMMARY_FILE
  unset CODEX_REVIEW_TIMEOUT
}

touchstone_preflight_main_sanitized() {
  (
    touchstone_preflight_unset_review_env
    touchstone_preflight_main "$@"
  )
}

touchstone_preflight_repo_root() {
  local requested="${1:-}"
  if [ -n "$requested" ]; then
    (cd "$requested" && pwd)
    return
  fi
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

touchstone_preflight_hash_stream() {
  shasum -a 256 | awk '{ print $1 }'
}

touchstone_preflight_hash_file() {
  local path="$1"

  if [ -f "$path" ]; then
    shasum -a 256 "$path" | awk '{ print $1 }'
  else
    printf 'missing'
  fi
}

touchstone_preflight_hash_paths() {
  local repo_root="$1"
  shift
  local rel path

  for rel in "$@"; do
    path="$repo_root/$rel"
    printf '%s\t%s\n' "$rel" "$(touchstone_preflight_hash_file "$path")"
  done | touchstone_preflight_hash_stream
}

touchstone_preflight_hash_file_list() {
  local label path

  while [ "$#" -gt 0 ]; do
    label="$1"
    path="$2"
    shift 2
    printf '%s\t%s\n' "$label" "$(touchstone_preflight_hash_file "$path")"
  done | touchstone_preflight_hash_stream
}

touchstone_preflight_worktree_hash() {
  local repo_root="$1"

  (
    cd "$repo_root" || exit 1
    git status --porcelain --untracked-files=all
    printf '\n-- worktree diff --\n'
    git diff --binary
    printf '\n-- index diff --\n'
    git diff --cached --binary
    printf '\n-- untracked files --\n'
    while IFS= read -r -d '' rel; do
      printf 'path\t%s\n' "$rel"
      if [ -f "$rel" ]; then
        printf 'sha256\t%s\n' "$(touchstone_preflight_hash_file "$rel")"
      else
        printf 'sha256\tmissing\n'
      fi
    done < <(git ls-files --others --exclude-standard -z)
  ) 2>/dev/null | touchstone_preflight_hash_stream
}

touchstone_preflight_changed_paths_hash() {
  local repo_root="$1"
  local base_ref="$2"

  (cd "$repo_root" && git diff --name-only "$base_ref"...HEAD) 2>/dev/null \
    | sort -u \
    | touchstone_preflight_hash_stream
}

touchstone_preflight_tool_fingerprint() {
  local tool path version_hash

  for tool in shellcheck shfmt markdownlint-cli2 markdownlint actionlint; do
    path="$(command -v "$tool" 2>/dev/null || true)"
    if [ -n "$path" ]; then
      version_hash="$({ "$tool" --version 2>&1 || true; } | touchstone_preflight_hash_stream)"
      printf '%s\t%s\t%s\n' "$tool" "$path" "$version_hash"
    else
      printf '%s\tmissing\tmissing\n' "$tool"
    fi
  done | touchstone_preflight_hash_stream
}

touchstone_preflight_env_fingerprint() {
  {
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_SCRIPT=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_SCRIPT:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_COMMAND=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_COMMAND:-}"
  } | touchstone_preflight_hash_stream
}

touchstone_preflight_cache_inputs() {
  local base_ref="$1"
  local repo_root head_sha base_sha merge_base changed_paths_hash
  local checker_hash config_hash worktree_hash tool_hash env_hash

  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  repo_root="$(cd "$repo_root" && pwd)" || return 1
  head_sha="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)" || return 1
  base_sha="$(git -C "$repo_root" rev-parse --verify "$base_ref^{commit}" 2>/dev/null)" || return 1
  merge_base="$(git -C "$repo_root" merge-base "$base_ref" "$head_sha" 2>/dev/null)" || return 1
  changed_paths_hash="$(touchstone_preflight_changed_paths_hash "$repo_root" "$base_ref")" || return 1
  checker_hash="$(touchstone_preflight_hash_file_list \
    "lib/preflight.sh" "$PREFLIGHT_LIB_DIR/preflight.sh" \
    "lib/preflight-scope.sh" "$PREFLIGHT_LIB_DIR/preflight-scope.sh" \
    "scripts/touchstone-run.sh" "$PREFLIGHT_LIB_DIR/../scripts/touchstone-run.sh")"
  config_hash="$(touchstone_preflight_hash_paths "$repo_root" \
    ".touchstone-review.toml" \
    ".codex-review.toml" \
    ".touchstone-config" \
    ".touchstone-version" \
    ".pre-commit-config.yaml" \
    ".markdownlint.json")"
  worktree_hash="$(touchstone_preflight_worktree_hash "$repo_root")" || return 1
  tool_hash="$(touchstone_preflight_tool_fingerprint)"
  env_hash="$(touchstone_preflight_env_fingerprint)"

  printf 'version=2\n'
  printf 'repo_root=%s\n' "$repo_root"
  printf 'scope=diff\n'
  printf 'base_ref=%s\n' "$base_ref"
  printf 'base_sha=%s\n' "$base_sha"
  printf 'head_sha=%s\n' "$head_sha"
  printf 'merge_base=%s\n' "$merge_base"
  printf 'changed_paths_hash=%s\n' "$changed_paths_hash"
  printf 'checker_hash=%s\n' "$checker_hash"
  printf 'config_hash=%s\n' "$config_hash"
  printf 'worktree_hash=%s\n' "$worktree_hash"
  printf 'tool_hash=%s\n' "$tool_hash"
  printf 'env_hash=%s\n' "$env_hash"
}

touchstone_preflight_cache_prepare() {
  local base_ref="$1"
  local cache_dir

  TOUCHSTONE_PREFLIGHT_CACHE_KEY=""
  TOUCHSTONE_PREFLIGHT_CACHE_FILE=""
  TOUCHSTONE_PREFLIGHT_CACHE_INPUTS=""

  if [ -z "$base_ref" ]; then
    return 1
  fi
  if touchstone_preflight_truthy "${TOUCHSTONE_PREFLIGHT_DISABLE_CACHE:-false}"; then
    return 1
  fi

  TOUCHSTONE_PREFLIGHT_CACHE_INPUTS="$(touchstone_preflight_cache_inputs "$base_ref")" || return 1
  TOUCHSTONE_PREFLIGHT_CACHE_KEY="$(printf '%s\n' "$TOUCHSTONE_PREFLIGHT_CACHE_INPUTS" | touchstone_preflight_hash_stream)"
  cache_dir="$(git rev-parse --git-path touchstone/preflight-clean 2>/dev/null)" || return 1
  TOUCHSTONE_PREFLIGHT_CACHE_FILE="$cache_dir/$TOUCHSTONE_PREFLIGHT_CACHE_KEY.clean"
}

touchstone_preflight_cache_short_key() {
  printf '%s' "${TOUCHSTONE_PREFLIGHT_CACHE_KEY:0:12}"
}

touchstone_preflight_cache_hit() {
  local marker_inputs

  [ -n "$TOUCHSTONE_PREFLIGHT_CACHE_FILE" ] || return 1
  [ -f "$TOUCHSTONE_PREFLIGHT_CACHE_FILE" ] || return 1
  grep -q '^result=preflight_clean$' "$TOUCHSTONE_PREFLIGHT_CACHE_FILE" || return 1
  marker_inputs="$(sed '1,2d' "$TOUCHSTONE_PREFLIGHT_CACHE_FILE")"
  [ "$marker_inputs" = "$TOUCHSTONE_PREFLIGHT_CACHE_INPUTS" ]
}

touchstone_preflight_write_clean_cache() {
  local cache_dir tmp

  [ -n "$TOUCHSTONE_PREFLIGHT_CACHE_FILE" ] || return 0
  [ -n "$TOUCHSTONE_PREFLIGHT_CACHE_INPUTS" ] || return 0
  cache_dir="$(dirname "$TOUCHSTONE_PREFLIGHT_CACHE_FILE")"
  if ! mkdir -p "$cache_dir" 2>/dev/null; then
    echo "WARNING: could not create preflight cache directory $cache_dir; continuing without cache." >&2
    return 0
  fi

  tmp="$TOUCHSTONE_PREFLIGHT_CACHE_FILE.$$"
  if {
    printf 'result=preflight_clean\n'
    printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
    printf '%s\n' "$TOUCHSTONE_PREFLIGHT_CACHE_INPUTS"
  } >"$tmp" 2>/dev/null && mv "$tmp" "$TOUCHSTONE_PREFLIGHT_CACHE_FILE" 2>/dev/null; then
    return 0
  fi

  rm -f "$tmp" 2>/dev/null || true
  echo "WARNING: could not write preflight cache marker $TOUCHSTONE_PREFLIGHT_CACHE_FILE; continuing without cache." >&2
  return 0
}

touchstone_preflight_all_files() {
  git ls-files 2>/dev/null
}

touchstone_preflight_changed_files() {
  if [ "$TOUCHSTONE_PREFLIGHT_SCOPE_MODE" = "diff" ]; then
    if ! declare -F compute_changed_paths_against >/dev/null 2>&1; then
      echo "ERROR: preflight diff mode requires lib/preflight-scope.sh." >&2
      return 2
    fi
    compute_changed_paths_against "$TOUCHSTONE_PREFLIGHT_DIFF_BASE" | sort -u
    return
  fi

  touchstone_preflight_all_files
}

touchstone_preflight_shell_files() {
  touchstone_preflight_changed_files \
    | awk '
        /^completions\// { next }
        /^prototypes\// { next }
        { print }
      ' \
    | while IFS= read -r path; do
      [ -n "$path" ] || continue
      [ -f "$path" ] || continue
      case "$path" in
        *.sh | bin/touchstone)
          printf '%s\n' "$path"
          ;;
        *)
          if IFS= read -r first_line <"$path" \
            && printf '%s\n' "$first_line" | grep -Eq '^#!.*(sh|bash|zsh|ksh)'; then
            printf '%s\n' "$path"
          fi
          ;;
      esac
    done
}

touchstone_preflight_shfmt_files() {
  touchstone_preflight_shell_files \
    | awk '
        $0 == "bin/touchstone" { next }
        { print }
      '
}

touchstone_preflight_markdown_files() {
  touchstone_preflight_changed_files \
    | awk '
        /^\.cortex\// { next }
        /\.md$/ { print }
      '
}

touchstone_preflight_workflow_files() {
  touchstone_preflight_changed_files \
    | awk '
        /^\.github\/workflows\/.*\.ya?ml$/ { print }
      '
}

touchstone_preflight_test_files() {
  touchstone_preflight_changed_files \
    | awk '
        /^tests\/test-.*\.sh$/ { print }
      ' \
    | while IFS= read -r path; do
      [ -n "$path" ] || continue
      [ -f "$path" ] || continue
      printf '%s\n' "$path"
    done
}

touchstone_preflight_is_touchstone_repo() {
  [ -f VERSION ] \
    && [ -f bootstrap/new-project.sh ] \
    && [ -f scripts/touchstone-run.sh ] \
    && [ -d tests ]
}

touchstone_preflight_run_list() {
  local label="$1"
  local command_name="$2"
  shift 2
  local -a args=("$@")
  local -a files=()
  local file
  local rc=0

  while IFS= read -r file; do
    [ -n "$file" ] || continue
    files+=("$file")
  done
  if [ "${#files[@]}" -eq 0 ]; then
    touchstone_preflight_skip "$label (no matching files)"
    return 0
  fi

  if ! command -v "$command_name" >/dev/null 2>&1; then
    touchstone_preflight_skip "$label ($command_name not installed)"
    return 0
  fi

  touchstone_preflight_info "$label"
  if [ "${#args[@]}" -gt 0 ]; then
    "$command_name" "${args[@]}" "${files[@]}" || rc=$?
  else
    "$command_name" "${files[@]}" || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    touchstone_preflight_ok "$label"
    return 0
  fi

  touchstone_preflight_fail "$label"
  return 1
}

touchstone_preflight_markdownlint() {
  local -a files=()
  local file

  while IFS= read -r file; do
    [ -n "$file" ] || continue
    files+=("$file")
  done < <(touchstone_preflight_markdown_files)
  if [ "${#files[@]}" -eq 0 ]; then
    touchstone_preflight_skip "markdownlint (no matching files)"
    return 0
  fi

  if command -v markdownlint-cli2 >/dev/null 2>&1; then
    touchstone_preflight_info "markdownlint-cli2"
    if markdownlint-cli2 "${files[@]}"; then
      touchstone_preflight_ok "markdownlint-cli2"
      return 0
    fi
    touchstone_preflight_fail "markdownlint-cli2"
    return 1
  fi

  if command -v markdownlint >/dev/null 2>&1; then
    touchstone_preflight_info "markdownlint"
    if markdownlint --config .markdownlint.json "${files[@]}"; then
      touchstone_preflight_ok "markdownlint"
      return 0
    fi
    touchstone_preflight_fail "markdownlint"
    return 1
  fi

  touchstone_preflight_skip "markdownlint (markdownlint-cli2/markdownlint not installed)"
  return 0
}

touchstone_preflight_validate() {
  local validate_script="${TOUCHSTONE_PREFLIGHT_VALIDATE_SCRIPT:-scripts/touchstone-run.sh}"
  local validate_command="${TOUCHSTONE_PREFLIGHT_VALIDATE_COMMAND:-}"

  if [ -n "$validate_command" ]; then
    touchstone_preflight_info "tests ($validate_command)"
    if TOUCHSTONE_PREFLIGHT_IN_PROGRESS=1 bash -c "$validate_command"; then
      touchstone_preflight_ok "tests"
      return 0
    fi
    touchstone_preflight_fail "tests"
    return 1
  fi

  if [ "$TOUCHSTONE_PREFLIGHT_SCOPE_MODE" = "diff" ]; then
    local -a test_files=()
    local test_file failures=0

    if touchstone_preflight_is_touchstone_repo; then
      touchstone_preflight_info "tests (touchstone self-tests)"
      for test_file in tests/test-*.sh; do
        [ -f "$test_file" ] || continue
        if TOUCHSTONE_PREFLIGHT_IN_PROGRESS=1 bash "$test_file"; then
          :
        else
          failures=$((failures + 1))
        fi
      done
      if [ "$failures" -eq 0 ]; then
        touchstone_preflight_ok "tests"
        return 0
      fi
      touchstone_preflight_fail "tests"
      return 1
    fi

    if [ -f "$validate_script" ]; then
      touchstone_preflight_info "tests (touchstone-run validate)"
      if TOUCHSTONE_PREFLIGHT_IN_PROGRESS=1 bash "$validate_script" validate; then
        touchstone_preflight_ok "tests"
        return 0
      fi
      touchstone_preflight_fail "tests"
      return 1
    fi

    while IFS= read -r test_file; do
      [ -n "$test_file" ] || continue
      test_files+=("$test_file")
    done < <(touchstone_preflight_test_files)
    if [ "${#test_files[@]}" -eq 0 ]; then
      touchstone_preflight_skip "tests (diff mode: no changed tests; project validate is full-project)"
      return 0
    fi

    touchstone_preflight_info "tests (changed test files)"
    for test_file in "${test_files[@]}"; do
      if TOUCHSTONE_PREFLIGHT_IN_PROGRESS=1 bash "$test_file"; then
        :
      else
        failures=$((failures + 1))
      fi
    done
    if [ "$failures" -eq 0 ]; then
      touchstone_preflight_ok "tests"
      return 0
    fi
    touchstone_preflight_fail "tests"
    return 1
  fi

  if [ ! -f "$validate_script" ]; then
    touchstone_preflight_skip "tests ($validate_script not found)"
    return 0
  fi

  touchstone_preflight_info "tests (touchstone-run validate)"
  if TOUCHSTONE_PREFLIGHT_IN_PROGRESS=1 bash "$validate_script" validate; then
    touchstone_preflight_ok "tests"
    return 0
  fi

  touchstone_preflight_fail "tests"
  return 1
}

touchstone_preflight_run() {
  local repo_root="$1"
  local failures=0

  cd "$repo_root"
  touchstone_preflight_info "preflight in $repo_root"
  if [ "$TOUCHSTONE_PREFLIGHT_SCOPE_MODE" = "diff" ]; then
    touchstone_preflight_info "scope: changed files vs $TOUCHSTONE_PREFLIGHT_DIFF_BASE"
  else
    touchstone_preflight_info "scope: all tracked files"
  fi

  touchstone_preflight_shell_files \
    | touchstone_preflight_run_list "shellcheck" shellcheck --severity=warning \
    || failures=$((failures + 1))
  touchstone_preflight_shfmt_files \
    | touchstone_preflight_run_list "shfmt -d" shfmt -d -i 2 -ci -bn \
    || failures=$((failures + 1))
  touchstone_preflight_markdownlint || failures=$((failures + 1))
  touchstone_preflight_workflow_files \
    | touchstone_preflight_run_list "actionlint" actionlint \
    || failures=$((failures + 1))
  touchstone_preflight_validate || failures=$((failures + 1))

  if [ "$failures" -eq 0 ]; then
    touchstone_preflight_info "preflight clean"
    return 0
  fi

  touchstone_preflight_fail "preflight failed ($failures check group(s))"
  return 1
}

touchstone_preflight_main() {
  local repo_root repo_root_arg="" saw_diff=false saw_all=false

  TOUCHSTONE_PREFLIGHT_SCOPE_MODE="all"
  TOUCHSTONE_PREFLIGHT_DIFF_BASE=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --diff)
        if [ "$#" -lt 2 ]; then
          echo "ERROR: --diff requires a base ref." >&2
          return 2
        fi
        TOUCHSTONE_PREFLIGHT_SCOPE_MODE="diff"
        TOUCHSTONE_PREFLIGHT_DIFF_BASE="$2"
        saw_diff=true
        shift 2
        ;;
      --all-files | --full)
        TOUCHSTONE_PREFLIGHT_SCOPE_MODE="all"
        TOUCHSTONE_PREFLIGHT_DIFF_BASE=""
        saw_all=true
        shift
        ;;
      -h | --help)
        cat <<'EOF'
Usage: bash lib/preflight.sh [--diff <base-ref>|--all-files] [repo-root]

Runs deterministic preflight checks. Without --diff, preflight preserves the
legacy full-project behavior. With --diff, the invariant is: preflight runs on
the changed file set versus the base ref, not the whole project, unless
--all-files is explicitly passed.

Scoped checks: shellcheck, shfmt, markdownlint, actionlint.
Full-project check: the project validate command remains full-project.
EOF
        return 0
        ;;
      --*)
        echo "ERROR: unknown preflight option: $1" >&2
        return 2
        ;;
      *)
        if [ -n "$repo_root_arg" ]; then
          echo "ERROR: unexpected extra preflight argument: $1" >&2
          return 2
        fi
        repo_root_arg="$1"
        shift
        ;;
    esac
  done

  if [ "$saw_diff" = true ] && [ "$saw_all" = true ]; then
    echo "ERROR: use only one of --diff or --all-files." >&2
    return 2
  fi

  repo_root="$(touchstone_preflight_repo_root "$repo_root_arg")"
  touchstone_preflight_run "$repo_root"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  touchstone_preflight_main "$@"
fi
