#!/usr/bin/env bash
#
# hooks/codex-review.sh — non-interactive AI code review + auto-fix loop.
# Supports multiple reviewers (Codex, Claude, Gemini) with a configurable
# fallback cascade. Wired into merge-pr.sh and default-branch pre-push checks.
#
# Loop:
#   1. Run the selected reviewer against the local diff vs the default branch
#   2. If reviewer says CODEX_REVIEW_CLEAN → push allowed.
#   3. If reviewer says CODEX_REVIEW_FIXED → it edited files. Stage + commit
#      the fixes (a new commit, NOT an amend) and loop back to step 1.
#   4. If reviewer says CODEX_REVIEW_BLOCKED → push aborts, findings printed.
#   5. After max_iterations rounds without converging, push aborts.
#
# Reviewer cascade:
#   The [review] section in .codex-review.toml lists reviewers to try in order.
#   The optional [review.routing] section can choose a different reviewer list
#   for small vs larger diffs. The first installed/authenticated reviewer wins.
#   If no [review] section exists, defaults to ["codex"] (backward compatible).
#
# Configuration:
#   Place a .codex-review.toml at the repo root to configure behavior.
#   See hooks/codex-review.config.example.toml for the full spec.
#
#   If no .codex-review.toml exists, ALL paths are treated as unsafe
#   (no auto-fix). This is the conservative default — opt in to auto-fix
#   explicitly by listing safe paths or setting safe_by_default = true.
#
# Modes:
#   review-only — reviewer can read + run commands, but cannot edit files or commit
#   fix         — full access: reviewer can edit, stage, and commit auto-fixes
#   diff-only   — read-only: reviewer can only read files, no commands or edits
#   no-tests    — reviewer can edit and commit, but cannot run commands (no test execution)
#
#   Modes are enforced at the wrapper level (tool restrictions, sandboxes), not just
#   in the prompt. Set via CODEX_REVIEW_MODE env var or `mode` in .codex-review.toml.
#
# Env overrides:
#   TOUCHSTONE_REVIEWER              — force a specific reviewer (skips cascade, hard-fails if unavailable)
#   TOUCHSTONE_LOCAL_REVIEWER_COMMAND — local reviewer command; reads prompt from stdin
#   TOUCHSTONE_LOCAL_REVIEWER_AUTH_COMMAND — optional command that must pass before local review runs
#   CODEX_REVIEW_ENABLED          — true/false override for the [review].enabled setting
#   CODEX_REVIEW_MODE             — review-only|fix|diff-only|no-tests (default: fix)
#   CODEX_REVIEW_BASE             — base ref to diff against (default: origin/<default-branch>)
#   CODEX_REVIEW_MAX_ITERATIONS   — fix loop cap (default: from config, or 3)
#   CODEX_REVIEW_MAX_DIFF_LINES   — skip review if diff > this many lines (default: 5000)
#   CODEX_REVIEW_CACHE_CLEAN      — cache exact-input clean reviews (default: true)
#   CODEX_REVIEW_TIMEOUT          — wall-clock timeout per invocation in seconds (default: 1800, 0=none)
#                                   Safety net for genuinely wedged reviewer processes, NOT a thinking-time
#                                   budget. Real reviews on big diffs can take 8–10 minutes; the cascade now
#                                   falls through to the next reviewer on timeout, so a hung reviewer no
#                                   longer silently passes the push.
#   CODEX_REVIEW_ASSIST           — enable peer reviewer help requests (default: false)
#   CODEX_REVIEW_ASSIST_TIMEOUT   — timeout for peer reviewer helper calls (default: 60)
#   CODEX_REVIEW_ASSIST_MAX_ROUNDS — max helper calls per review run (default: 1)
#   CODEX_REVIEW_ON_ERROR         — fail-open (default) or fail-closed
#   CODEX_REVIEW_DISABLE_CACHE    — set to true/1 to force a fresh review
#   CODEX_REVIEW_FORCE            — set to true/1 to run even on non-default-branch pushes
#   CODEX_REVIEW_NO_AUTOFIX       — set to true/1 for review-only mode (backward compat)
#   CODEX_REVIEW_IN_PROGRESS      — internal guard to skip nested review runs
#
# To bypass entirely in an emergency: git push --no-verify
#
# Exit codes:
#   0 — clean review (or graceful skip), push allowed
#   1 — Codex flagged blocking issues OR fix loop did not converge, push aborted
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
CONFIG_FILE="$REPO_ROOT/.codex-review.toml"
cd "$REPO_ROOT"

# --------------------------------------------------------------------------
# Configuration loading
# --------------------------------------------------------------------------

# Defaults (conservative: all paths unsafe, no auto-fix unless configured)
SAFE_BY_DEFAULT=false
MAX_ITERATIONS="${CODEX_REVIEW_MAX_ITERATIONS:-3}"
MAX_DIFF_LINES="${CODEX_REVIEW_MAX_DIFF_LINES:-5000}"
CACHE_CLEAN_REVIEWS="${CODEX_REVIEW_CACHE_CLEAN:-true}"
NO_AUTOFIX="${CODEX_REVIEW_NO_AUTOFIX:-false}"
CONFIG_MODE=""
REVIEW_ENABLED="${CODEX_REVIEW_ENABLED:-true}"
REVIEW_TIMEOUT="${CODEX_REVIEW_TIMEOUT:-1800}"
ON_ERROR="${CODEX_REVIEW_ON_ERROR:-fail-open}"
UNSAFE_PATHS=""
REVIEWER_CASCADE=()
LOCAL_REVIEWER_COMMAND="${TOUCHSTONE_LOCAL_REVIEWER_COMMAND:-}"
LOCAL_REVIEWER_AUTH_COMMAND="${TOUCHSTONE_LOCAL_REVIEWER_AUTH_COMMAND:-}"
ROUTING_ENABLED=false
ROUTING_SMALL_MAX_DIFF_LINES=400
ROUTING_SMALL_REVIEWERS=()
ROUTING_LARGE_REVIEWERS=()
ROUTING_DECISION="default"
ASSIST_ENABLED="${CODEX_REVIEW_ASSIST:-false}"
ASSIST_TIMEOUT="${CODEX_REVIEW_ASSIST_TIMEOUT:-60}"
ASSIST_MAX_ROUNDS="${CODEX_REVIEW_ASSIST_MAX_ROUNDS:-1}"
ASSIST_HELPERS=()

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

strip_toml_comment() {
  local line="$1"
  local out=""
  local char
  local in_single=false
  local in_double=false
  local len="${#line}"
  local i=0

  while [ "$i" -lt "$len" ]; do
    char="${line:$i:1}"

    if [ "$in_double" = true ] && [ "$char" = "\\" ]; then
      out="$out$char"
      i=$((i + 1))
      if [ "$i" -lt "$len" ]; then
        char="${line:$i:1}"
        out="$out$char"
      fi
      i=$((i + 1))
      continue
    fi

    if [ "$char" = '"' ] && [ "$in_single" = false ]; then
      if [ "$in_double" = true ]; then
        in_double=false
      else
        in_double=true
      fi
    elif [ "$char" = "'" ] && [ "$in_double" = false ]; then
      if [ "$in_single" = true ]; then
        in_single=false
      else
        in_single=true
      fi
    elif [ "$char" = "#" ] && [ "$in_single" = false ] && [ "$in_double" = false ]; then
      break
    fi

    out="$out$char"
    i=$((i + 1))
  done

  printf '%s' "$out"
}

append_unsafe_path() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"

  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac

  [ -z "$value" ] && return

  if [ -n "$UNSAFE_PATHS" ]; then
    UNSAFE_PATHS="${UNSAFE_PATHS}
$value"
  else
    UNSAFE_PATHS="$value"
  fi
}

append_unsafe_paths_csv() {
  local csv="$1"
  local item
  local -a items=()

  [ -n "$csv" ] || return 0

  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    append_unsafe_path "$item"
  done
}

append_reviewer() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  [ -z "$value" ] && return
  REVIEWER_CASCADE+=("$value")
}

append_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    append_reviewer "$item"
  done
}

append_routing_small_reviewer() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  [ -z "$value" ] && return
  ROUTING_SMALL_REVIEWERS+=("$value")
}

append_routing_small_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    append_routing_small_reviewer "$item"
  done
}

append_routing_large_reviewer() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  [ -z "$value" ] && return
  ROUTING_LARGE_REVIEWERS+=("$value")
}

append_routing_large_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    append_routing_large_reviewer "$item"
  done
}

append_assist_helper() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  [ -z "$value" ] && return
  ASSIST_HELPERS+=("$value")
}

append_assist_helpers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    append_assist_helper "$item"
  done
}

normalize_bool() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"

  case "$value" in
    true|1|yes|on) printf 'true' ;;
    false|0|no|off) printf 'false' ;;
    *) printf '%s' "$value" ;;
  esac
}

toml_string_value() {
  local value="$1"
  value="$(trim "$value")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

is_truthy() {
  case "$(normalize_bool "${1:-false}")" in
    true) return 0 ;;
    *) return 1 ;;
  esac
}

# Parse .codex-review.toml if it exists.
# We do minimal TOML parsing in bash — just key = value pairs and string arrays.
if [ -f "$CONFIG_FILE" ]; then
  IN_UNSAFE_PATHS=false
  IN_REVIEWERS=false
  IN_ROUTING_SMALL_REVIEWERS=false
  IN_ROUTING_LARGE_REVIEWERS=false
  IN_ASSIST_HELPERS=false
  CURRENT_SECTION=""
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    # Strip comments and trim whitespace
    line="$(trim "$(strip_toml_comment "$raw_line")")"
    [ -z "$line" ] && continue

    # Track TOML section headers.
    if [[ "$line" == "["*"]" ]]; then
      IN_UNSAFE_PATHS=false
      IN_REVIEWERS=false
      IN_ROUTING_SMALL_REVIEWERS=false
      IN_ROUTING_LARGE_REVIEWERS=false
      IN_ASSIST_HELPERS=false
      CURRENT_SECTION="${line#\[}"
      CURRENT_SECTION="${CURRENT_SECTION%\]}"
      CURRENT_SECTION="$(trim "$CURRENT_SECTION")"
      continue
    fi

    # Continue multiline arrays regardless of section.
    if [ "$IN_UNSAFE_PATHS" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        append_unsafe_paths_csv "${line%%]*}"
        IN_UNSAFE_PATHS=false
      else
        append_unsafe_path "$line"
      fi
      continue
    fi
    if [ "$IN_REVIEWERS" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        append_reviewers_csv "${line%%]*}"
        IN_REVIEWERS=false
      else
        append_reviewer "$line"
      fi
      continue
    fi
    if [ "$IN_ROUTING_SMALL_REVIEWERS" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        append_routing_small_reviewers_csv "${line%%]*}"
        IN_ROUTING_SMALL_REVIEWERS=false
      else
        append_routing_small_reviewer "$line"
      fi
      continue
    fi
    if [ "$IN_ROUTING_LARGE_REVIEWERS" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        append_routing_large_reviewers_csv "${line%%]*}"
        IN_ROUTING_LARGE_REVIEWERS=false
      else
        append_routing_large_reviewer "$line"
      fi
      continue
    fi
    if [ "$IN_ASSIST_HELPERS" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        append_assist_helpers_csv "${line%%]*}"
        IN_ASSIST_HELPERS=false
      else
        append_assist_helper "$line"
      fi
      continue
    fi

    # Parse [review.routing] section keys.
    if [ "$CURRENT_SECTION" = "review.routing" ]; then
      case "$line" in
        enabled*=*)
          ROUTING_ENABLED="$(normalize_bool "${line#*=}")"
          ;;
        small_max_diff_lines*=*|small_diff_lines*=*)
          ROUTING_SMALL_MAX_DIFF_LINES="$(trim "${line#*=}")"
          ;;
        small_reviewers*=*)
          array_value="$(trim "${line#*=}")"
          array_value="${array_value#\[}"
          if [[ "$array_value" == *"]"* ]]; then
            append_routing_small_reviewers_csv "${array_value%%]*}"
          else
            append_routing_small_reviewers_csv "$array_value"
            IN_ROUTING_SMALL_REVIEWERS=true
          fi
          ;;
        large_reviewers*=*)
          array_value="$(trim "${line#*=}")"
          array_value="${array_value#\[}"
          if [[ "$array_value" == *"]"* ]]; then
            append_routing_large_reviewers_csv "${array_value%%]*}"
          else
            append_routing_large_reviewers_csv "$array_value"
            IN_ROUTING_LARGE_REVIEWERS=true
          fi
          ;;
      esac
      continue
    fi

    # Parse [review.assist] section keys.
    if [ "$CURRENT_SECTION" = "review.assist" ]; then
      case "$line" in
        enabled*=*)
          ASSIST_ENABLED="${CODEX_REVIEW_ASSIST:-$(normalize_bool "${line#*=}")}"
          ;;
        helpers*=*)
          array_value="$(trim "${line#*=}")"
          array_value="${array_value#\[}"
          if [[ "$array_value" == *"]"* ]]; then
            append_assist_helpers_csv "${array_value%%]*}"
          else
            append_assist_helpers_csv "$array_value"
            IN_ASSIST_HELPERS=true
          fi
          ;;
        helper*=*)
          append_assist_helper "${line#*=}"
          ;;
        timeout*=*)
          ASSIST_TIMEOUT="${CODEX_REVIEW_ASSIST_TIMEOUT:-$(trim "${line#*=}")}"
          ;;
        max_rounds*=*)
          ASSIST_MAX_ROUNDS="${CODEX_REVIEW_ASSIST_MAX_ROUNDS:-$(trim "${line#*=}")}"
          ;;
      esac
      continue
    fi

    # Parse [review] section keys.
    if [ "$CURRENT_SECTION" = "review" ]; then
      case "$line" in
        enabled*=*)
          REVIEW_ENABLED="${CODEX_REVIEW_ENABLED:-$(normalize_bool "${line#*=}")}"
          ;;
        reviewers*=*)
          array_value="$(trim "${line#*=}")"
          array_value="${array_value#\[}"
          if [[ "$array_value" == *"]"* ]]; then
            append_reviewers_csv "${array_value%%]*}"
          else
            append_reviewers_csv "$array_value"
            IN_REVIEWERS=true
          fi
          ;;
      esac
      continue
    fi

    # Parse [review.local] section keys.
    if [ "$CURRENT_SECTION" = "review.local" ]; then
      case "$line" in
        command*=*)
          if [ -z "${TOUCHSTONE_LOCAL_REVIEWER_COMMAND:-}" ]; then
            LOCAL_REVIEWER_COMMAND="$(toml_string_value "${line#*=}")"
          fi
          ;;
        auth_command*=*)
          if [ -z "${TOUCHSTONE_LOCAL_REVIEWER_AUTH_COMMAND:-}" ]; then
            LOCAL_REVIEWER_AUTH_COMMAND="$(toml_string_value "${line#*=}")"
          fi
          ;;
      esac
      continue
    fi

    # Parse [codex_review] section keys (also matches when no section header
    # has been seen yet, for backward compatibility with existing configs).
    case "$line" in
      max_iterations*=*)
        MAX_ITERATIONS="${CODEX_REVIEW_MAX_ITERATIONS:-$(trim "${line#*=}")}"
        ;;
      max_diff_lines*=*)
        MAX_DIFF_LINES="${CODEX_REVIEW_MAX_DIFF_LINES:-$(trim "${line#*=}")}"
        ;;
      cache_clean_reviews*=*)
        CACHE_CLEAN_REVIEWS="${CODEX_REVIEW_CACHE_CLEAN:-$(normalize_bool "${line#*=}")}"
        ;;
      safe_by_default*=*)
        val="$(trim "${line#*=}")"
        val="$(printf '%s' "$val" | tr '[:upper:]' '[:lower:]')"
        SAFE_BY_DEFAULT="$val"
        ;;
      mode*=*)
        val="$(trim "${line#*=}")"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        CONFIG_MODE="$val"
        ;;
      timeout*=*)
        REVIEW_TIMEOUT="${CODEX_REVIEW_TIMEOUT:-$(trim "${line#*=}")}"
        ;;
      on_error*=*)
        val="$(trim "${line#*=}")"
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        ON_ERROR="${CODEX_REVIEW_ON_ERROR:-$val}"
        ;;
      unsafe_paths*=*)
        array_value="$(trim "${line#*=}")"
        array_value="${array_value#\[}"
        if [[ "$array_value" == *"]"* ]]; then
          append_unsafe_paths_csv "${array_value%%]*}"
        else
          append_unsafe_paths_csv "$array_value"
          IN_UNSAFE_PATHS=true
        fi
        ;;
    esac
  done < "$CONFIG_FILE"
fi

resolve_default_branch() {
  local local_ref

  local_ref="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [ -n "$local_ref" ]; then
    printf '%s\n' "${local_ref#origin/}"
    return 0
  fi

  if command -v gh >/dev/null 2>&1; then
    gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || echo main
  else
    echo main
  fi
}

DEFAULT_BRANCH="$(resolve_default_branch)"
BASE="${CODEX_REVIEW_BASE:-origin/$DEFAULT_BRANCH}"
NO_AUTOFIX="$(normalize_bool "$NO_AUTOFIX")"

# Default reviewer cascade: codex-only (backward compat with existing configs).
if [ "${#REVIEWER_CASCADE[@]}" -eq 0 ]; then
  REVIEWER_CASCADE=("codex")
fi

# Default peer helpers, used only when review.assist is enabled. The active
# primary reviewer is skipped at runtime, so this order favors a different CLI.
if [ "${#ASSIST_HELPERS[@]}" -eq 0 ]; then
  ASSIST_HELPERS=("codex" "gemini" "claude")
fi
ASSIST_ENABLED="$(normalize_bool "$ASSIST_ENABLED")"
REVIEW_ENABLED="$(normalize_bool "$REVIEW_ENABLED")"
ROUTING_ENABLED="$(normalize_bool "$ROUTING_ENABLED")"

# TOUCHSTONE_REVIEWER env var overrides the cascade with a single forced reviewer.
if [ -n "${TOUCHSTONE_REVIEWER:-}" ]; then
  REVIEWER_CASCADE=("$TOUCHSTONE_REVIEWER")
fi

# --------------------------------------------------------------------------
# Mode resolution
# --------------------------------------------------------------------------
# Modes: review-only, fix, diff-only, no-tests
#   review-only — read + bash, no edits, no git ops (default for merge review)
#   fix         — full access, auto-fix + commit (default for pre-push)
#   diff-only   — read-only, no bash, no edits
#   no-tests    — edit + commit, no bash (skip test execution)

resolve_mode() {
  local mode="${CODEX_REVIEW_MODE:-}"

  # Backward compat: NO_AUTOFIX=true maps to review-only
  if [ -z "$mode" ] && is_truthy "$NO_AUTOFIX"; then
    mode="review-only"
  fi

  # Fall back to config, then default
  [ -n "$mode" ] || mode="${CONFIG_MODE:-fix}"

  case "$mode" in
    review-only|fix|diff-only|no-tests) ;;
    *)
      echo "WARNING: Invalid mode '$mode' — falling back to 'fix'. Valid: review-only, fix, diff-only, no-tests" >&2
      mode="fix"
      ;;
  esac
  printf '%s' "$mode"
}

REVIEW_MODE="$(resolve_mode)"

mode_allows_fix()  { [ "$REVIEW_MODE" = "fix" ] || [ "$REVIEW_MODE" = "no-tests" ]; }
mode_allows_bash() { [ "$REVIEW_MODE" = "fix" ] || [ "$REVIEW_MODE" = "review-only" ]; }

short_ref_name() {
  local ref="$1"
  ref="${ref#refs/heads/}"
  ref="${ref#refs/remotes/origin/}"
  printf '%s' "$ref"
}

is_pre_push_hook() {
  [ "${PRE_COMMIT:-}" = "1" ] && [ -n "${PRE_COMMIT_REMOTE_BRANCH:-}" ]
}

should_skip_pre_push_review() {
  local remote_branch default_branch

  is_pre_push_hook || return 1
  is_truthy "${CODEX_REVIEW_FORCE:-false}" && return 1

  remote_branch="$(short_ref_name "$PRE_COMMIT_REMOTE_BRANCH")"
  default_branch="$(short_ref_name "$DEFAULT_BRANCH")"

  if [ "$remote_branch" = "$default_branch" ]; then
    return 1
  fi

  echo "==> Review runs on pushes to $default_branch only — skipping push to $remote_branch."
  echo "    Force review with: CODEX_REVIEW_FORCE=1 git push"
  return 0
}

# --------------------------------------------------------------------------
# Repo-provided review context
# --------------------------------------------------------------------------

REVIEW_CONTEXT_FILE=""
for _candidate in "$REPO_ROOT/.codex-review-context.md" "$REPO_ROOT/.github/codex-review-context.md"; do
  if [ -f "$_candidate" ]; then
    REVIEW_CONTEXT_FILE="$_candidate"
    break
  fi
done

# --------------------------------------------------------------------------
# Build the auto-fix policy section of the prompt from config
# --------------------------------------------------------------------------

build_autofix_policy() {
  local policy=""

  if ! mode_allows_fix; then
    cat <<POLICY_EOF
Mode: $REVIEW_MODE — do not edit files. Do not stage, commit, or modify anything.

Review only:
- If there are no blocking issues, emit CLEAN.
- If any issue needs a code or documentation change, emit BLOCKED with findings.
- Do not emit FIXED.

When in doubt, STOP and emit BLOCKED.
POLICY_EOF
    return 0
  fi

  if [ "$SAFE_BY_DEFAULT" = "true" ]; then
    policy="By default, all paths are SAFE to auto-fix unless listed as unsafe."
  else
    policy="By default, all paths are NOT safe to auto-fix. Only fix issues in paths explicitly marked as safe."
  fi

  if [ -n "$UNSAFE_PATHS" ]; then
    policy="$policy

NOT safe to auto-fix — STOP and emit BLOCKED instead:
$(echo "$UNSAFE_PATHS" | while read -r p; do [ -n "$p" ] && echo "- Anything in $p"; done)"
  fi

  if [ "${WORKTREE_DIRTY_BEFORE_REVIEW:-false}" = true ]; then
    policy="$policy

The working tree already has uncommitted changes. Do not edit files in this run; emit BLOCKED for issues that need changes."
  fi

  if [ "$REVIEW_MODE" = "no-tests" ]; then
    policy="$policy

IMPORTANT: Mode is 'no-tests'. Do NOT run any shell commands, test suites, or build tools.
Review by reading files only. You may edit files to fix issues."
  fi

  policy="$policy

General auto-fix rules:
SAFE to auto-fix (apply the smallest possible change, then emit FIXED):
- Typos in comments / docstrings / log messages
- Missing null checks on optional fields
- Missing error logging on exception handlers (except: pass -> except Exception as e: logger.warning(...))
- Adding missing imports for symbols that are clearly used
- Replacing magic-number values with named constants in non-critical code

NOT safe to auto-fix regardless of path (STOP and emit BLOCKED):
- Anything that removes or weakens an existing test
- Anything that changes business logic or calculation semantics
- Anything where the fix requires a design decision (which of two approaches is right)
- Anything you're not at least 90% confident about

When in doubt, STOP and emit BLOCKED."

  echo "$policy"
}

build_assist_policy() {
  if ! is_truthy "$ASSIST_ENABLED" || [ "${ASSIST_MAX_ROUNDS:-0}" -le 0 ] 2>/dev/null; then
    return 0
  fi

  cat <<ASSIST_EOF

## Optional peer assistance

For larger or high-risk changes, you may ask one configured peer reviewer for a second opinion before making your final decision.
Use this only for a specific technical question where another reviewer could materially improve the result.

To request help, include exactly one block in your output and end with CODEX_REVIEW_BLOCKED:

TOUCHSTONE_HELP_REQUEST_BEGIN
question: <one concrete question for the peer reviewer>
context: <brief context; include files or risk areas if useful>
TOUCHSTONE_HELP_REQUEST_END
CODEX_REVIEW_BLOCKED

The hook will ask a peer reviewer in read-only mode, then call you once more with the peer answer.
On that second pass, do not request help again; emit the normal final sentinel.
ASSIST_EOF
}

# --------------------------------------------------------------------------
# Reviewer adapters
# --------------------------------------------------------------------------
# Each reviewer exposes three functions:
#   reviewer_<id>_available  — exit 0 if the CLI is installed
#   reviewer_<id>_auth_ok    — exit 0 if auth is configured
#   reviewer_<id>_exec PROMPT — run the review; stdout = output, exit code = success

reviewer_codex_available() { command -v codex >/dev/null 2>&1; }
reviewer_codex_auth_ok()   { codex login status >/dev/null 2>&1; }
reviewer_codex_exec() {
  # Codex sandbox: read-only (no file writes) or workspace-write (edits allowed).
  # Codex cannot selectively disable command execution, so diff-only and no-tests
  # degrade: diff-only → read-only sandbox, no-tests → workspace-write sandbox.
  # The prompt still instructs the reviewer, but enforcement is filesystem-only.
  local sandbox="read-only"
  if [ "$REVIEW_MODE" = "fix" ] || [ "$REVIEW_MODE" = "no-tests" ]; then
    sandbox="workspace-write"
  fi
  if [ "$REVIEW_MODE" = "diff-only" ] || [ "$REVIEW_MODE" = "no-tests" ]; then
    printf "  ${C_DIM}(codex: '%s' enforced via sandbox=%s + prompt; command restriction is prompt-level only)${C_RESET}\n" \
      "$REVIEW_MODE" "$sandbox" >&2
  fi
  CODEX_REVIEW_IN_PROGRESS=1 codex exec \
    --sandbox "$sandbox" --ephemeral "$1"
}

reviewer_claude_available() { command -v claude >/dev/null 2>&1; }
reviewer_claude_auth_ok()   { claude auth status >/dev/null 2>&1; }
reviewer_claude_exec() {
  # Claude has fine-grained --allowedTools: all four modes are fully enforced.
  local tools
  case "$REVIEW_MODE" in
    diff-only)    tools="Read,Grep,Glob" ;;
    review-only)  tools="Read,Grep,Glob,Bash" ;;
    no-tests)     tools="Read,Grep,Glob,Edit,Write" ;;
    fix)          tools="Read,Grep,Glob,Bash,Edit,Write" ;;
  esac
  CODEX_REVIEW_IN_PROGRESS=1 claude -p \
    --allowedTools "$tools" \
    --output-format text \
    "$1"
}

reviewer_gemini_available() { command -v gemini >/dev/null 2>&1; }
reviewer_gemini_auth_ok() {
  [ -n "${GEMINI_API_KEY:-}" ] && return 0
  command -v gcloud >/dev/null 2>&1 && gcloud auth print-access-token >/dev/null 2>&1
}
reviewer_gemini_exec() {
  # Gemini: --yolo (full auto-approve) or not (no auto-approve).
  # Only fix mode uses --yolo. diff-only, review-only, and no-tests all run
  # without --yolo. no-tests cannot be fully enforced (edits without commands)
  # since Gemini lacks granular tool control.
  if [ "$REVIEW_MODE" = "fix" ]; then
    CODEX_REVIEW_IN_PROGRESS=1 gemini -p "$1" --yolo
  else
    if [ "$REVIEW_MODE" = "no-tests" ]; then
      printf "  ${C_DIM}(gemini: 'no-tests' mode degrades to review-only; gemini lacks granular tool control)${C_RESET}\n" >&2
    fi
    CODEX_REVIEW_IN_PROGRESS=1 gemini -p "$1"
  fi
}

reviewer_local_available() {
  [ -n "$LOCAL_REVIEWER_COMMAND" ]
}
reviewer_local_auth_ok() {
  [ -z "$LOCAL_REVIEWER_AUTH_COMMAND" ] && return 0
  bash -c "$LOCAL_REVIEWER_AUTH_COMMAND" >/dev/null 2>&1
}
append_local_context_file() {
  local title="$1"
  local file="$2"

  [ -f "$file" ] || return 0
  printf '\n## %s\n\n' "$title"
  printf '```markdown\n'
  cat "$file"
  printf '\n```\n'
}
build_local_reviewer_prompt() {
  local base_prompt="$1"

  cat <<LOCAL_PROMPT_EOF
$base_prompt

## Local reviewer input

You are running as a local command that receives only this stdin payload.
Use the embedded project context and diff below; do not assume you can call
repo-aware tools unless your wrapper command explicitly provides them.
LOCAL_PROMPT_EOF

  append_local_context_file "AGENTS.md" "$REPO_ROOT/AGENTS.md"
  append_local_context_file "CLAUDE.md" "$REPO_ROOT/CLAUDE.md"
  if [ -n "$REVIEW_CONTEXT_FILE" ]; then
    append_local_context_file "$(basename "$REVIEW_CONTEXT_FILE")" "$REVIEW_CONTEXT_FILE"
  fi

  printf '\n## Diff\n\n'
  printf '```diff\n'
  git diff "$MERGE_BASE"..HEAD 2>/dev/null || true
  printf '\n```\n'
}
reviewer_local_exec() {
  local local_prompt
  printf "  ${C_DIM}(local: running configured command; prompt is provided on stdin)${C_RESET}\n" >&2
  local_prompt="$(build_local_reviewer_prompt "$1")"
  CODEX_REVIEW_IN_PROGRESS=1 bash -c "$LOCAL_REVIEWER_COMMAND" <<< "$local_prompt"
}

# --------------------------------------------------------------------------
# Reviewer cascade resolver
# --------------------------------------------------------------------------

ACTIVE_REVIEWER=""
REVIEWER_STATUS=""
AVAILABLE_CASCADE=()

resolve_reviewer() {
  local reviewer
  ACTIVE_REVIEWER=""
  REVIEWER_STATUS=""
  AVAILABLE_CASCADE=()

  for reviewer in "${REVIEWER_CASCADE[@]}"; do
    if ! declare -F "reviewer_${reviewer}_available" >/dev/null; then
      REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: unknown reviewer\n"
      continue
    fi
    if ! "reviewer_${reviewer}_available"; then
      if [ "$reviewer" = "local" ]; then
        REVIEWER_STATUS="${REVIEWER_STATUS}    local: command not configured\n"
      else
        REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: CLI not installed\n"
      fi
      continue
    fi
    if ! "reviewer_${reviewer}_auth_ok"; then
      REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: auth check failed\n"
      continue
    fi
    AVAILABLE_CASCADE+=("$reviewer")
    REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: ready\n"
  done

  if [ "${#AVAILABLE_CASCADE[@]}" -eq 0 ]; then
    return 1
  fi
  ACTIVE_REVIEWER="${AVAILABLE_CASCADE[0]}"
  return 0
}

ASSIST_REVIEWER=""
ASSIST_REVIEWER_STATUS=""

resolve_assist_reviewer() {
  local helper
  ASSIST_REVIEWER=""
  ASSIST_REVIEWER_STATUS=""

  for helper in "${ASSIST_HELPERS[@]}"; do
    if [ "$helper" = "$ACTIVE_REVIEWER" ]; then
      ASSIST_REVIEWER_STATUS="${ASSIST_REVIEWER_STATUS}    ${helper}: skipped primary reviewer\n"
      continue
    fi
    if ! declare -F "reviewer_${helper}_available" >/dev/null; then
      ASSIST_REVIEWER_STATUS="${ASSIST_REVIEWER_STATUS}    ${helper}: unknown reviewer\n"
      continue
    fi
    if ! "reviewer_${helper}_available"; then
      ASSIST_REVIEWER_STATUS="${ASSIST_REVIEWER_STATUS}    ${helper}: CLI not installed\n"
      continue
    fi
    if ! "reviewer_${helper}_auth_ok"; then
      ASSIST_REVIEWER_STATUS="${ASSIST_REVIEWER_STATUS}    ${helper}: auth check failed\n"
      continue
    fi
    ASSIST_REVIEWER="$helper"
    return 0
  done

  return 1
}

apply_review_routing() {
  local diff_lines="$1"

  is_truthy "$ROUTING_ENABLED" || return 0
  [ -z "${TOUCHSTONE_REVIEWER:-}" ] || return 0

  case "$ROUTING_SMALL_MAX_DIFF_LINES" in
    ''|*[!0-9]*)
      echo "WARNING: Invalid review.routing.small_max_diff_lines='$ROUTING_SMALL_MAX_DIFF_LINES' — ignoring routing." >&2
      return 0
      ;;
  esac

  if [ "${#ROUTING_SMALL_REVIEWERS[@]}" -eq 0 ]; then
    ROUTING_SMALL_REVIEWERS=("local" "${REVIEWER_CASCADE[@]}")
  fi
  if [ "${#ROUTING_LARGE_REVIEWERS[@]}" -eq 0 ]; then
    ROUTING_LARGE_REVIEWERS=("${REVIEWER_CASCADE[@]}")
  fi

  if [ "$diff_lines" -le "$ROUTING_SMALL_MAX_DIFF_LINES" ] 2>/dev/null; then
    REVIEWER_CASCADE=("${ROUTING_SMALL_REVIEWERS[@]}")
    ROUTING_DECISION="small"
    echo "==> Review routing: small diff ($diff_lines <= $ROUTING_SMALL_MAX_DIFF_LINES) — trying: ${REVIEWER_CASCADE[*]}"
  else
    REVIEWER_CASCADE=("${ROUTING_LARGE_REVIEWERS[@]}")
    ROUTING_DECISION="large"
    echo "==> Review routing: larger diff ($diff_lines > $ROUTING_SMALL_MAX_DIFF_LINES) — trying: ${REVIEWER_CASCADE[*]}"
  fi
}

run_reviewer() {
  "reviewer_${ACTIVE_REVIEWER}_exec" "$1"
}

reviewer_label_for() {
  case "$1" in
    codex)  printf 'Codex' ;;
    claude) printf 'Claude' ;;
    gemini) printf 'Gemini' ;;
    local)  printf 'Local command' ;;
    *)      printf '%s' "$1" ;;
  esac
}

reviewer_label() {
  reviewer_label_for "$ACTIVE_REVIEWER"
}

# --------------------------------------------------------------------------
# Timeout and error handling
# --------------------------------------------------------------------------

REVIEW_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-output.XXXXXX")"
REVIEW_STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-stderr.XXXXXX")"
ASSIST_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-assist-output.XXXXXX")"
ASSIST_STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-assist-stderr.XXXXXX")"
trap 'rm -f "$REVIEW_OUTPUT_FILE" "$REVIEW_STDERR_FILE" "$ASSIST_OUTPUT_FILE" "$ASSIST_STDERR_FILE"' EXIT

# Print the last N lines of a captured stderr file, one indented line at a time.
# If empty, prints "<no stderr captured>" so a silent failure still emits a hint.
print_stderr_tail() {
  local file="$1"
  local label="${2:-stderr}"
  local lines="${3:-5}"

  if [ ! -s "$file" ]; then
    printf "    %s: <no stderr captured>\n" "$label"
    return 0
  fi
  printf "    %s (last %d lines, full: %s):\n" "$label" "$lines" "$file"
  tail -n "$lines" "$file" | sed 's/^/      /'
}

# Classify reviewer stderr into a short tag for the cascade chain summary.
# Returns one of: usage-limit | auth | network | error
classify_reviewer_failure() {
  local file="$1"

  if [ ! -s "$file" ]; then
    printf 'error'
    return 0
  fi
  # grep -qiE returns 0 on match; -E for alternation.
  if grep -qiE 'rate.?limit|usage.limit|quota.exhaust|too many requests|429|"status"[[:space:]]*:[[:space:]]*429|insufficient.quota|exceeded' "$file"; then
    printf 'usage-limit'
    return 0
  fi
  if grep -qiE 'unauthor|forbidden|invalid.api.?key|authentication|not.logged.in|please.log.in|401|403' "$file"; then
    printf 'auth'
    return 0
  fi
  if grep -qiE 'network|timed.out|connection.refused|dns|unreachable|tls' "$file"; then
    printf 'network'
    return 0
  fi
  printf 'error'
}

kill_process_tree() {
  local pid="$1"
  local signal="$2"
  local children child

  children="$(ps -axo pid=,ppid= 2>/dev/null | awk -v ppid="$pid" '$2 == ppid { print $1 }' || true)"
  for child in $children; do
    kill_process_tree "$child" "$signal"
  done

  kill "-$signal" "$pid" 2>/dev/null || true
}

# run_reviewer_with_timeout TIMEOUT_SECS [PROMPT] [STDOUT_FILE] [STDERR_FILE]
#   Runs the reviewer, captures stdout to STDOUT_FILE and stderr to STDERR_FILE,
#   returns exit code. Exit 124 = timeout. Works correctly with subshells (no
#   $() capture needed).
run_reviewer_with_timeout() {
  local timeout_secs="$1"
  local prompt="${2:-$REVIEW_PROMPT}"
  local output_file="${3:-$REVIEW_OUTPUT_FILE}"
  local stderr_file="${4:-$REVIEW_STDERR_FILE}"

  # Truncate stderr file so each attempt starts fresh (prevents stale stderr
  # from a prior reviewer in the cascade leaking into the new one's classification).
  : > "$stderr_file"

  # No timeout: run directly
  if [ "$timeout_secs" -le 0 ] 2>/dev/null; then
    run_reviewer "$prompt" > "$output_file" 2> "$stderr_file"
    return $?
  fi

  # Run reviewer in background, kill if it exceeds timeout.
  (
    run_reviewer "$prompt" > "$output_file" 2> "$stderr_file" &
    local reviewer_pid=$!

    terminate_reviewer() {
      kill_process_tree "$reviewer_pid" TERM
      sleep 1
      kill_process_tree "$reviewer_pid" KILL
      wait "$reviewer_pid" >/dev/null 2>&1 || true
      exit 143
    }

    trap terminate_reviewer TERM INT
    wait "$reviewer_pid"
  ) &
  local pid=$!
  (
    sleep "$timeout_secs"
    kill_process_tree "$pid" TERM
    sleep 10
    kill_process_tree "$pid" KILL
  ) &
  local watchdog=$!

  wait "$pid" 2>/dev/null
  local rc=$?
  kill_process_tree "$watchdog" TERM
  wait "$watchdog" >/dev/null 2>&1 || true

  # SIGTERM/SIGKILL from the watchdog means timeout; normalize to 124.
  if [ "$rc" -eq 143 ] || [ "$rc" -eq 137 ]; then
    return 124
  fi
  return "$rc"
}

handle_error() {
  local reason="$1"

  if [ "$ON_ERROR" = "fail-closed" ]; then
    echo "==> ERROR ($reason) — blocking push (on_error=fail-closed)." >&2
    exit 1
  else
    echo "==> ERROR ($reason) — not blocking push (on_error=fail-open)."
    echo "    Set on_error = \"fail-closed\" in .codex-review.toml to block on errors."
    exit 0
  fi
}

# --------------------------------------------------------------------------
# Pre-flight checks
# --------------------------------------------------------------------------

# Feature-branch pushes should stay fast. Manual invocations and direct pushes
# to the default branch still run the review.
if is_truthy "${CODEX_REVIEW_IN_PROGRESS:-false}"; then
  echo "==> Review already in progress — skipping nested review."
  exit 0
fi

if should_skip_pre_push_review; then
  exit 0
fi

# First-push exemption: a pre-push to the default branch with a single commit
# on HEAD is the initial scaffold push. Reviewing AI-generated template files
# has near-zero signal and spends quota that belongs to real PRs. Skip with a
# visible line so the absent safety boundary is not silent. Defensive: if
# `git rev-list` fails for any reason (no commits, detached state, etc.), fall
# through to the normal review path instead of silently skipping.
if is_pre_push_hook && ! is_truthy "${CODEX_REVIEW_FORCE:-false}"; then
  _firstpush_remote_branch="$(short_ref_name "${PRE_COMMIT_REMOTE_BRANCH:-}")"
  _firstpush_default_branch="$(short_ref_name "$DEFAULT_BRANCH")"
  if [ "$_firstpush_remote_branch" = "$_firstpush_default_branch" ]; then
    if _firstpush_commit_count="$(git rev-list --count HEAD 2>/dev/null)" \
      && [ "$_firstpush_commit_count" = "1" ]; then
      echo "==> Codex review skipped — first push on a fresh scaffold (HEAD is the initial commit)."
      exit 0
    fi
  fi
fi

if ! is_truthy "$REVIEW_ENABLED"; then
  echo "==> AI review disabled by .codex-review.toml — skipping review."
  exit 0
fi

# Fetch latest base ref for the default review target (silent on failure —
# offline, rebasing, etc.). If CODEX_REVIEW_BASE is set, trust the caller.
if [ -z "${CODEX_REVIEW_BASE:-}" ]; then
  git fetch origin "$DEFAULT_BRANCH" --quiet 2>/dev/null || true
fi

# Find merge base so we review only this branch's commits.
if ! MERGE_BASE="$(git merge-base "$BASE" HEAD 2>/dev/null)"; then
  echo "==> Couldn't find merge base with $BASE — skipping review."
  exit 0
fi

# Skip if no changes vs base.
if git diff --quiet "$MERGE_BASE"..HEAD; then
  echo "==> No changes vs $BASE — skipping review."
  exit 0
fi

WORKTREE_DIRTY_BEFORE_REVIEW=false
if [ -n "$(git status --porcelain)" ]; then
  WORKTREE_DIRTY_BEFORE_REVIEW=true
fi

ROUTING_DIFF_LINE_COUNT="$(git diff "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
apply_review_routing "$ROUTING_DIFF_LINE_COUNT"

# Resolve which reviewer to use from the cascade.
if ! resolve_reviewer; then
  if [ -n "${TOUCHSTONE_REVIEWER:-}" ]; then
    echo "ERROR: TOUCHSTONE_REVIEWER=$TOUCHSTONE_REVIEWER but that reviewer is not available:" >&2
    printf '%b' "$REVIEWER_STATUS" >&2
    exit 1
  fi
  echo "==> No reviewer available — skipping review."
  printf '%b' "$REVIEWER_STATUS"
  echo "    Install at least one: codex, claude, or gemini CLI; or configure [review.local].command."
  exit 0
fi
REVIEWER_LABEL="$(reviewer_label)"
if [ "${#AVAILABLE_CASCADE[@]}" -gt 1 ]; then
  cascade_labels=""
  for _r in "${AVAILABLE_CASCADE[@]}"; do
    if [ -z "$cascade_labels" ]; then
      cascade_labels="$(reviewer_label_for "$_r")"
    else
      cascade_labels="${cascade_labels} → $(reviewer_label_for "$_r")"
    fi
  done
  echo "==> Reviewer cascade: $cascade_labels"
else
  echo "==> Using reviewer: $REVIEWER_LABEL"
fi
if [ -n "$REVIEW_CONTEXT_FILE" ]; then
  echo "==> Review context: $(basename "$REVIEW_CONTEXT_FILE")"
fi

# --------------------------------------------------------------------------
# Build the review prompt
# --------------------------------------------------------------------------

AUTOFIX_POLICY="$(build_autofix_policy)"

read -r -d '' REVIEW_PROMPT <<PROMPT_EOF || true
You are reviewing AND optionally auto-fixing a pull request before it reaches the default branch.

Read AGENTS.md at the repo root for the full review rubric (if it exists).
Read CLAUDE.md at the repo root for project context (if it exists).

Do NOT flag: formatting, style, naming, missing docstrings, speculative refactors, "you could consider" observations without a concrete bug.

## Goal and context

The following commit messages describe the intent and strategy behind these changes.
Use them to understand *why* the code was changed, not just *what* changed.
Do not flag intentional design decisions that are explained in the commit messages.

$(git log --reverse --format='### %s%n%n%b' "$MERGE_BASE"..HEAD 2>/dev/null | sed '/^$/N;/^\n$/d')

Examine the diff vs $BASE using your tools.
$(if [ "$REVIEW_MODE" = "diff-only" ]; then
printf '\n## Diff (included because mode=diff-only restricts tool access)\n\n```\n'
git diff "$MERGE_BASE"..HEAD 2>/dev/null
printf '```\n'
fi)

## Auto-fix policy

$AUTOFIX_POLICY
$(build_assist_policy)
$(if [ -n "$REVIEW_CONTEXT_FILE" ]; then
printf '\n## Project review context\n\n'
cat "$REVIEW_CONTEXT_FILE"
fi)

## Output contract — strict

The LAST line of your output must be exactly one of these three sentinels (no extra characters, no trailing whitespace):

- CODEX_REVIEW_CLEAN — no blocking issues found, operation should proceed
- CODEX_REVIEW_FIXED — you applied auto-fixes, script will commit and re-review
- CODEX_REVIEW_BLOCKED — you found blocking issues you cannot/should not auto-fix

If you emit CODEX_REVIEW_BLOCKED, list each blocking issue on its own line in the format:
- path/to/file.py:LINE — short description of what's wrong

If you emit CODEX_REVIEW_FIXED, briefly describe what you fixed (one line per fix).

Do not invent new sentinels. Do not output anything after the sentinel line.
PROMPT_EOF

# --------------------------------------------------------------------------
# Clean-review cache
# --------------------------------------------------------------------------

cache_enabled() {
  case "$(normalize_bool "${CODEX_REVIEW_DISABLE_CACHE:-false}")" in
    true) return 1 ;;
  esac

  case "$(normalize_bool "$CACHE_CLEAN_REVIEWS")" in
    true) return 0 ;;
    *) return 1 ;;
  esac
}

hash_stdin() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    cksum | awk '{print $1 "-" $2}'
  fi
}

append_cache_file() {
  local label="$1"
  local path="$2"

  printf '\n-- %s --\n' "$label"
  if [ -f "$path" ]; then
    cat "$path"
  else
    printf '<missing>\n'
  fi
}

review_cache_key() {
  {
    printf 'touchstone-codex-review-cache-v2\n'
    printf 'reviewer=%s\n' "$ACTIVE_REVIEWER"
    printf 'review_route=%s\n' "$ROUTING_DECISION"
    printf 'review_enabled=%s\n' "$REVIEW_ENABLED"
    printf 'local_reviewer_command=%s\n' "$LOCAL_REVIEWER_COMMAND"
    printf 'base=%s\n' "$BASE"
    printf 'merge_base=%s\n' "$MERGE_BASE"
    printf 'worktree_dirty_before_review=%s\n' "$WORKTREE_DIRTY_BEFORE_REVIEW"
    printf 'assist_enabled=%s\n' "$ASSIST_ENABLED"
    printf 'assist_timeout=%s\n' "$ASSIST_TIMEOUT"
    printf 'assist_max_rounds=%s\n' "$ASSIST_MAX_ROUNDS"
    printf 'assist_helpers=%s\n' "${ASSIST_HELPERS[*]}"
    printf '\n-- prompt --\n%s\n' "$REVIEW_PROMPT"
    append_cache_file "AGENTS.md" "$REPO_ROOT/AGENTS.md"
    append_cache_file "CLAUDE.md" "$REPO_ROOT/CLAUDE.md"
    append_cache_file ".codex-review.toml" "$CONFIG_FILE"
    append_cache_file "codex-review.sh" "$0"
    if [ -n "$REVIEW_CONTEXT_FILE" ]; then
      append_cache_file "codex-review-context" "$REVIEW_CONTEXT_FILE"
    fi
    printf '\n-- branch diff --\n'
    git diff --binary "$MERGE_BASE"..HEAD
  } | hash_stdin
}

clean_review_cache_dir() {
  git rev-parse --git-path touchstone/codex-review-clean
}

clean_review_cache_file() {
  local key="$1"
  printf '%s/%s.clean' "$(clean_review_cache_dir)" "$key"
}

write_clean_review_cache() {
  local key="$1"
  local line_count="$2"
  local cache_dir cache_file

  [ -n "$key" ] || return 0
  cache_dir="$(clean_review_cache_dir)"
  cache_file="$(clean_review_cache_file "$key")"

  mkdir -p "$cache_dir" 2>/dev/null || return 0
  {
    printf 'result=CODEX_REVIEW_CLEAN\n'
    printf 'base=%s\n' "$BASE"
    printf 'merge_base=%s\n' "$MERGE_BASE"
    printf 'head=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'diff_lines=%s\n' "$line_count"
    printf 'reviewed_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  } > "$cache_file" 2>/dev/null || true
}

changed_paths() {
  {
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
  } | sed '/^$/d' | sort -u
}

path_is_unsafe() {
  local path="$1"
  local unsafe_path

  [ -n "$UNSAFE_PATHS" ] || return 1

  while IFS= read -r unsafe_path; do
    [ -z "$unsafe_path" ] && continue
    case "$unsafe_path" in
      */)
        [[ "$path" == "$unsafe_path"* ]] && return 0
        ;;
      *)
        if [ "$path" = "$unsafe_path" ] || [[ "$path" == "$unsafe_path/"* ]]; then
          return 0
        fi
        ;;
    esac
  done <<< "$UNSAFE_PATHS"

  return 1
}

path_allows_autofix() {
  local path="$1"

  if [ "$SAFE_BY_DEFAULT" != "true" ]; then
    return 1
  fi

  if path_is_unsafe "$path"; then
    return 1
  fi

  return 0
}

disallowed_autofix_paths() {
  local changed="$1"
  local path
  local disallowed=""

  while IFS= read -r path; do
    [ -z "$path" ] && continue
    if ! path_allows_autofix "$path"; then
      if [ -n "$disallowed" ]; then
        disallowed="${disallowed}
$path"
      else
        disallowed="$path"
      fi
    fi
  done <<< "$changed"

  printf '%s' "$disallowed"
}

extract_help_request() {
  awk '
    /^TOUCHSTONE_HELP_REQUEST_BEGIN$/ { in_request = 1; next }
    /^TOUCHSTONE_HELP_REQUEST_END$/ { exit }
    in_request { print }
  ' <<EOF
$1
EOF
}

build_assist_prompt() {
  local primary_label="$1"
  local help_request="$2"

  cat <<ASSIST_PROMPT_EOF
You are a peer reviewer giving a focused second opinion before a push.

Do not edit files. Do not stage, commit, or modify anything. You are advisory only.
Answer the primary reviewer concisely and directly. Do not emit CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, or CODEX_REVIEW_BLOCKED.

Read AGENTS.md at the repo root for the review rubric if it exists.
Read CLAUDE.md at the repo root for project context if it exists.

## Primary reviewer

$primary_label asked:

$help_request

## Branch context

Base: $BASE
Merge base: $MERGE_BASE

Commit messages:

$(git log --reverse --format='### %s%n%n%b' "$MERGE_BASE"..HEAD 2>/dev/null | sed '/^$/N;/^\n$/d')
$(if [ -n "$REVIEW_CONTEXT_FILE" ]; then
printf '\n## Project review context\n\n'
cat "$REVIEW_CONTEXT_FILE"
fi)

## Diff

\`\`\`diff
$(git diff "$MERGE_BASE"..HEAD 2>/dev/null)
\`\`\`
ASSIST_PROMPT_EOF
}

build_assisted_final_prompt() {
  local help_request="$1"
  local helper_label="$2"
  local helper_output="$3"

  cat <<ASSISTED_PROMPT_EOF
$REVIEW_PROMPT

## Peer reviewer answer

You previously asked for a second opinion:

$help_request

$helper_label answered:

$helper_output

Now make the final review decision. Do not request peer assistance again.
The LAST line of your output must be exactly one of:
- CODEX_REVIEW_CLEAN
- CODEX_REVIEW_FIXED
- CODEX_REVIEW_BLOCKED
ASSISTED_PROMPT_EOF
}

run_assist_review() {
  local help_request="$1"
  local primary_reviewer="$ACTIVE_REVIEWER"
  local primary_label="$REVIEWER_LABEL"
  local primary_mode="$REVIEW_MODE"
  local helper_label assist_prompt rc helper_output assisted_prompt

  if ! resolve_assist_reviewer; then
    echo "==> Peer assistance requested, but no helper reviewer is available:"
    printf '%b' "$ASSIST_REVIEWER_STATUS"
    return 1
  fi

  helper_label="$(reviewer_label_for "$ASSIST_REVIEWER")"
  phase "asking $helper_label for peer assistance"
  assist_prompt="$(build_assist_prompt "$primary_label" "$help_request")"

  ACTIVE_REVIEWER="$ASSIST_REVIEWER"
  REVIEW_MODE="diff-only"
  set +e
  run_reviewer_with_timeout "$ASSIST_TIMEOUT" "$assist_prompt" "$ASSIST_OUTPUT_FILE"
  rc=$?
  set -e
  ACTIVE_REVIEWER="$primary_reviewer"
  REVIEW_MODE="$primary_mode"
  REVIEWER_LABEL="$primary_label"

  helper_output="$(cat "$ASSIST_OUTPUT_FILE" 2>/dev/null || true)"
  if [ "$rc" -eq 124 ]; then
    echo "==> $helper_label peer assistance timed out after ${ASSIST_TIMEOUT}s."
    return 1
  fi
  if [ "$rc" -ne 0 ]; then
    echo "==> $helper_label peer assistance failed with exit $rc."
    return 1
  fi

  phase "re-reviewing with $primary_label after peer assistance"
  assisted_prompt="$(build_assisted_final_prompt "$help_request" "$helper_label" "$helper_output")"
  ASSIST_ROUNDS=$((ASSIST_ROUNDS + 1))
  ASSIST_FINAL_REVIEW_RAN=true
  set +e
  run_reviewer_with_timeout "$REVIEW_TIMEOUT" "$assisted_prompt" "$REVIEW_OUTPUT_FILE"
  rc=$?
  set -e
  return "$rc"
}

# --------------------------------------------------------------------------
# Review loop
# --------------------------------------------------------------------------

FIX_COMMITS=0
ASSIST_ROUNDS=0
BANNER_PRINTED=false
REVIEW_START_TIME="$(date +%s)"
REVIEW_FILES_INSPECTED="$(git diff --name-only "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
REVIEW_EXIT_REASON=""

# REVIEWER_CHAIN records each reviewer's outcome as "name:status" entries
# (e.g. "codex:usage-limit", "claude:clean"). Surfaced in the summary block
# and the JSON output so a fallback path is legible after the fact.
REVIEWER_CHAIN=()
FALLTHROUGH_REASON=""

format_reviewer_chain() {
  local IFS=,
  printf '%s' "${REVIEWER_CHAIN[*]}"
}

# --------------------------------------------------------------------------
# Phase labels
# --------------------------------------------------------------------------

phase() {
  printf "  ${C_DIM}[%s] %s${C_RESET}\n" "$(date +%H:%M:%S)" "$1"
}

# --------------------------------------------------------------------------
# Worktree invariant checking
# --------------------------------------------------------------------------

WORKTREE_HEAD_BEFORE="$(git rev-parse HEAD)"
WORKTREE_BRANCH_BEFORE="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
WORKTREE_STATUS_BEFORE="$(git status --porcelain)"

check_worktree_invariants() {
  local current_head current_branch current_status violations=""

  current_head="$(git rev-parse HEAD)"
  current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
  current_status="$(git status --porcelain)"

  if [ "$current_head" != "$WORKTREE_HEAD_BEFORE" ]; then
    violations="${violations}    HEAD changed: $WORKTREE_HEAD_BEFORE -> $current_head\n"
  fi
  if [ "$current_branch" != "$WORKTREE_BRANCH_BEFORE" ]; then
    violations="${violations}    Branch changed: $WORKTREE_BRANCH_BEFORE -> $current_branch\n"
  fi
  if [ "$current_status" != "$WORKTREE_STATUS_BEFORE" ]; then
    violations="${violations}    Working tree status changed\n"
  fi

  if [ -n "$violations" ]; then
    printf "\n  ${C_RED}WARNING: Worktree mutated during '%s' review:${C_RESET}\n" "$REVIEW_MODE"
    printf '%b' "$violations"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------
# Structured summary
# --------------------------------------------------------------------------

print_summary() {
  local elapsed mins secs findings
  elapsed=$(( $(date +%s) - REVIEW_START_TIME ))
  mins=$((elapsed / 60))
  secs=$((elapsed % 60))
  findings="${REVIEW_FINDINGS_COUNT:-0}"

  local chain_str
  chain_str="$(format_reviewer_chain)"

  printf "\n  ${C_DIM}─── review summary ────────────────────────${C_RESET}\n"
  printf "  ${C_DIM}reviewer:       %s${C_RESET}\n" "$REVIEWER_LABEL"
  if [ -n "$chain_str" ]; then
    printf "  ${C_DIM}cascade chain:  %s${C_RESET}\n" "$chain_str"
  fi
  if [ "$ROUTING_DECISION" != "default" ]; then
    printf "  ${C_DIM}route:          %s${C_RESET}\n" "$ROUTING_DECISION"
  fi
  printf "  ${C_DIM}mode:           %s${C_RESET}\n" "$REVIEW_MODE"
  printf "  ${C_DIM}files:          %s${C_RESET}\n" "$REVIEW_FILES_INSPECTED"
  printf "  ${C_DIM}diff lines:     %s${C_RESET}\n" "$DIFF_LINE_COUNT"
  printf "  ${C_DIM}iterations:     %s/%s${C_RESET}\n" "${iter:-0}" "$MAX_ITERATIONS"
  printf "  ${C_DIM}fix commits:    %s${C_RESET}\n" "$FIX_COMMITS"
  printf "  ${C_DIM}peer assists:   %s${C_RESET}\n" "$ASSIST_ROUNDS"
  printf "  ${C_DIM}findings:       %s${C_RESET}\n" "$findings"
  printf "  ${C_DIM}exit reason:    %s${C_RESET}\n" "$REVIEW_EXIT_REASON"
  printf "  ${C_DIM}elapsed:        %dm%ds${C_RESET}\n" "$mins" "$secs"
  printf "  ${C_DIM}──────────────────────────────────────────${C_RESET}\n"

  if [ -n "${CODEX_REVIEW_SUMMARY_FILE:-}" ]; then
    printf '{"reviewer":"%s","reviewer_chain":"%s","route":"%s","mode":"%s","files":%d,"diff_lines":%d,"iterations":%d,"fix_commits":%d,"peer_assists":%d,"findings":%d,"exit_reason":"%s","elapsed_seconds":%d}\n' \
      "$REVIEWER_LABEL" "$chain_str" "$ROUTING_DECISION" "$REVIEW_MODE" "$REVIEW_FILES_INSPECTED" "$DIFF_LINE_COUNT" \
      "${iter:-0}" "$FIX_COMMITS" "$ASSIST_ROUNDS" "$findings" "$REVIEW_EXIT_REASON" "$elapsed" \
      > "$CODEX_REVIEW_SUMMARY_FILE" 2>/dev/null || true
  fi
}

# Colors (respect NO_COLOR).
# shellcheck disable=SC2034  # C_GREEN / C_CYAN kept for palette parity;
# other color vars are referenced in printf statements above and below.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_DIM='\033[2m' C_GREEN='\033[0;32m'
  C_YELLOW='\033[0;33m' C_RED='\033[0;31m' C_CYAN='\033[0;36m' C_RESET='\033[0m'
  C_TTY=1
else
  C_DIM='' C_GREEN='' C_YELLOW='' C_RED='' C_CYAN='' C_RESET=''
  C_TTY=0
fi

# --------------------------------------------------------------------------
# Branded UI — double-rail verdicts signed "touchstone".
# Mirrors lib/ui.sh; kept inline so this hook stays self-contained when
# synced into downstream projects as scripts/codex-review.sh.
# --------------------------------------------------------------------------

TK_BRAND_ORANGE="#FF6B35"
TK_BRAND_LIME="#A3E635"
TK_BRAND_RED="#EF4444"
TK_BRAND_DIM="#6B7280"

tk_have_gum() { command -v gum >/dev/null 2>&1; }

tk_paint() {
  # tk_paint <hex> <bold|plain> <text...>
  # Falls back to plain text if gum is missing, disabled, or fails —
  # the hook is running under `set -euo pipefail`, so silent gum failure
  # would otherwise produce empty strings in the verdict lines.
  local color="$1"; shift
  local flag="$1"; shift
  local rendered=""
  if [ "$C_TTY" = "1" ] && tk_have_gum; then
    if [ "$flag" = "bold" ]; then
      rendered="$(gum style --foreground "$color" --bold "$*" 2>/dev/null || true)"
    else
      rendered="$(gum style --foreground "$color" "$*" 2>/dev/null || true)"
    fi
  fi
  if [ -n "$rendered" ]; then
    printf '%s' "$rendered"
  else
    printf '%s' "$*"
  fi
}

tk_rail() {
  local rendered=""
  if [ "$C_TTY" = "1" ] && tk_have_gum; then
    rendered="$(gum style --foreground "$TK_BRAND_ORANGE" "▌▌" 2>/dev/null || true)"
  fi
  if [ -n "$rendered" ]; then
    printf '%s' "$rendered"
  else
    printf '▌▌'
  fi
}

tk_signature_line() {
  # Dim "touchstone vX.Y.Z" line; version resolved via TOUCHSTONE_ROOT when set.
  local version=""
  if [ -n "${TOUCHSTONE_ROOT:-}" ] && [ -f "$TOUCHSTONE_ROOT/VERSION" ]; then
    version="$(tr -d '[:space:]' < "$TOUCHSTONE_ROOT/VERSION" 2>/dev/null || true)"
  fi
  if [ -n "$version" ]; then
    tk_paint "$TK_BRAND_DIM" plain "touchstone v${version}"
  else
    tk_paint "$TK_BRAND_DIM" plain "touchstone"
  fi
}

tk_verdict() {
  # tk_verdict <ok|fail|info> <headline> [subtitle]
  local state="$1" headline="$2" subtitle="${3:-}"
  local rail mark painted_headline
  rail="$(tk_rail)"

  case "$state" in
    ok)   mark="$(tk_paint "$TK_BRAND_LIME" plain "✓")"
          painted_headline="$(tk_paint "$TK_BRAND_LIME" bold "$headline")" ;;
    fail) mark="$(tk_paint "$TK_BRAND_RED"  plain "✗")"
          painted_headline="$(tk_paint "$TK_BRAND_RED"  bold "$headline")" ;;
    *)    mark="$(tk_paint "$TK_BRAND_DIM"  plain "•")"
          painted_headline="$(tk_paint "$TK_BRAND_DIM"  bold "$headline")" ;;
  esac

  printf '\n  %s  %s  %s\n' "$rail" "$painted_headline" "$mark"
  if [ -n "$subtitle" ]; then
    printf '  %s  %s\n' "$rail" "$(tk_paint "$TK_BRAND_DIM" plain "$subtitle")"
  fi
  printf '  %s  %s\n\n' "$rail" "$(tk_signature_line)"
}

print_banner() {
  [ "$BANNER_PRINTED" = false ] || return 0
  local label
  label="$(reviewer_label)"
  tk_verdict info "REVIEW STARTING" "${label} · merge code review"
  BANNER_PRINTED=true
}

for _outer_idx in "${!AVAILABLE_CASCADE[@]}"; do
  ACTIVE_REVIEWER="${AVAILABLE_CASCADE[$_outer_idx]}"
  REVIEWER_LABEL="$(reviewer_label)"
  BANNER_PRINTED=false
  FALLTHROUGH_REASON=""

for iter in $(seq 1 "$MAX_ITERATIONS"); do
  phase "loading diff"
  DIFF_LINE_COUNT="$(git diff "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
  if [ "$DIFF_LINE_COUNT" -gt "$MAX_DIFF_LINES" ]; then
    echo "==> Diff is $DIFF_LINE_COUNT lines (> $MAX_DIFF_LINES cap) — skipping review."
    echo "    Override with: CODEX_REVIEW_MAX_DIFF_LINES=100000 git push"
    exit 0
  fi

  phase "checking cache"
  REVIEW_CACHE_KEY=""
  if cache_enabled; then
    REVIEW_CACHE_KEY="$(review_cache_key 2>/dev/null || true)"
    if [ -n "$REVIEW_CACHE_KEY" ] && [ -f "$(clean_review_cache_file "$REVIEW_CACHE_KEY")" ]; then
      echo "==> Review previously passed for this exact diff — skipping repeat review."
      echo "    Force a fresh review with: CODEX_REVIEW_DISABLE_CACHE=1 git push"
      exit 0
    fi
  fi

  print_banner
  printf "  ${C_DIM}iteration ${iter}/${MAX_ITERATIONS} · ${DIFF_LINE_COUNT} lines vs ${BASE}${C_RESET}\n"
  phase "reviewing with $REVIEWER_LABEL"

  set +e
  run_reviewer_with_timeout "$REVIEW_TIMEOUT"
  EXIT=$?
  set -e
  OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"

  # Check worktree invariants in non-fix modes.
  # This is a hard failure regardless of on_error policy — a reviewer that
  # mutates the worktree in review-only mode is a safety violation.
  if ! mode_allows_fix; then
    if ! check_worktree_invariants; then
      REVIEW_EXIT_REASON="worktree-mutated"
      print_summary
      echo "==> ERROR: Worktree was mutated in '$REVIEW_MODE' mode — blocking push." >&2
      exit 1
    fi
  fi

  if [ "$EXIT" -eq 124 ]; then
    phase "timed out"
    echo "==> $REVIEWER_LABEL timed out after ${REVIEW_TIMEOUT}s."
    print_stderr_tail "$REVIEW_STDERR_FILE" "$REVIEWER_LABEL stderr"
    REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:timeout")
    FALLTHROUGH_REASON="timeout after ${REVIEW_TIMEOUT}s"
    break
  fi

  if [ $EXIT -ne 0 ]; then
    failure_tag="$(classify_reviewer_failure "$REVIEW_STDERR_FILE")"
    echo "==> $REVIEWER_LABEL review failed with exit $EXIT (${failure_tag})."
    print_stderr_tail "$REVIEW_STDERR_FILE" "$REVIEWER_LABEL stderr"
    REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:${failure_tag}")
    FALLTHROUGH_REASON="${failure_tag} (exit $EXIT)"
    break
  fi

  HELP_REQUEST=""
  if is_truthy "$ASSIST_ENABLED" && [ "$ASSIST_ROUNDS" -lt "$ASSIST_MAX_ROUNDS" ] 2>/dev/null; then
    HELP_REQUEST="$(extract_help_request "$OUTPUT" | sed '/^[[:space:]]*$/d' || true)"
    if [ -n "$HELP_REQUEST" ]; then
      ASSIST_FINAL_REVIEW_RAN=false
      set +e
      run_assist_review "$HELP_REQUEST"
      ASSIST_EXIT=$?
      set -e

      if [ "$ASSIST_FINAL_REVIEW_RAN" = true ]; then
        EXIT="$ASSIST_EXIT"
        OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"

        if ! mode_allows_fix; then
          if ! check_worktree_invariants; then
            REVIEW_EXIT_REASON="worktree-mutated"
            print_summary
            echo "==> ERROR: Worktree was mutated in '$REVIEW_MODE' mode — blocking push." >&2
            exit 1
          fi
        fi

        if [ "$EXIT" -eq 124 ]; then
          phase "timed out"
          echo "==> $REVIEWER_LABEL timed out after ${REVIEW_TIMEOUT}s after peer assistance."
          print_stderr_tail "$REVIEW_STDERR_FILE" "$REVIEWER_LABEL stderr"
          REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:timeout-after-assist")
          FALLTHROUGH_REASON="timeout after ${REVIEW_TIMEOUT}s after peer assistance"
          break
        fi

        if [ "$EXIT" -ne 0 ]; then
          failure_tag="$(classify_reviewer_failure "$REVIEW_STDERR_FILE")"
          echo "==> $REVIEWER_LABEL review failed with exit $EXIT (${failure_tag}) after peer assistance."
          print_stderr_tail "$REVIEW_STDERR_FILE" "$REVIEWER_LABEL stderr"
          REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:${failure_tag}-after-assist")
          FALLTHROUGH_REASON="${failure_tag} (exit $EXIT) after peer assistance"
          break
        fi
      else
        echo "==> Continuing with the primary reviewer output."
      fi
    fi
  fi

  LAST_LINE="$(printf '%s\n' "$OUTPUT" | tail -1 | tr -d '\r ')"
  case "$LAST_LINE" in
    CODEX_REVIEW_CLEAN)
      phase "done — clean"
      clean_subtitle="${REVIEWER_LABEL} · ${DIFF_LINE_COUNT} lines · push approved"
      if [ "$FIX_COMMITS" -gt 0 ]; then
        clean_subtitle="${clean_subtitle} · ${FIX_COMMITS} auto-fix commit(s)"
      fi
      tk_verdict ok "ALL CLEAR" "$clean_subtitle"
      REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:clean")
      REVIEW_EXIT_REASON="clean"
      print_summary
      write_clean_review_cache "$REVIEW_CACHE_KEY" "$DIFF_LINE_COUNT"
      exit 0
      ;;

    CODEX_REVIEW_FIXED)
      if ! mode_allows_fix; then
        echo "==> $REVIEWER_LABEL emitted FIXED in '$REVIEW_MODE' mode."
        echo "    The reviewer was restricted from editing — this should not happen."
        echo "    Inspect the working tree before continuing."
        exit 1
      fi

      AUTOFIX_CHANGED_PATHS="$(changed_paths)"
      if [ -z "$AUTOFIX_CHANGED_PATHS" ]; then
        echo "==> $REVIEWER_LABEL emitted FIXED but no working-tree changes detected."
        echo "    Treating as ambiguous — not blocking push."
        exit 0
      fi

      if [ "$WORKTREE_DIRTY_BEFORE_REVIEW" = true ]; then
        echo "==> $REVIEWER_LABEL emitted FIXED, but the working tree was already dirty before review."
        echo "    Refusing to auto-commit because that could include unrelated local changes."
        echo "    Commit or stash local changes, then push again."
        exit 1
      fi

      DISALLOWED_AUTOFIX_PATHS="$(disallowed_autofix_paths "$AUTOFIX_CHANGED_PATHS")"
      if [ -n "$DISALLOWED_AUTOFIX_PATHS" ]; then
        echo "==> $REVIEWER_LABEL edited paths that are not allowed by .codex-review.toml."
        echo "    Refusing to auto-commit. Review these changes manually:"
        printf '%s\n' "$DISALLOWED_AUTOFIX_PATHS" | sed 's/^/    - /'
        echo "    Inspect the working-tree diff before deciding whether to keep or discard them."
        exit 1
      fi

      phase "applying fixes"
      printf "\n  ${C_YELLOW}🔧 Auto-fixing...${C_RESET}\n\n"
      git diff --stat
      echo ""

      git add -A
      git commit -m "fix: address $REVIEWER_LABEL review findings (auto, $REVIEW_MODE, iter $iter)"
      WORKTREE_DIRTY_BEFORE_REVIEW=false
      FIX_COMMITS=$((FIX_COMMITS + 1))
      echo "==> Created fix commit $(git rev-parse --short HEAD). Re-running review on new HEAD..."
      echo ""
      continue
      ;;

    CODEX_REVIEW_BLOCKED)
      phase "done — blocked"
      REVIEW_FINDINGS_COUNT="$(printf '%s\n' "$OUTPUT" | grep -c '^- ' || true)"
      blocked_subtitle="${REVIEWER_LABEL} flagged issues to address · push refused"
      tk_verdict fail "PUSH BLOCKED" "$blocked_subtitle"
      printf '%s\n' "$OUTPUT" | sed 's/^/    /'
      echo ""
      if [ "$FIX_COMMITS" -gt 0 ]; then
        echo "    Note: $REVIEWER_LABEL made $FIX_COMMITS fix commit(s) earlier this run that are still in your local history."
        echo "    To undo them: git reset --hard HEAD~$FIX_COMMITS"
      fi
      echo "    Address findings and try again. Emergency override: git push --no-verify"
      REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:blocked")
      REVIEW_EXIT_REASON="blocked"
      print_summary
      exit 1
      ;;

    *)
      echo "==> $REVIEWER_LABEL output did not match the expected sentinel contract."
      echo "    Last line was: '$LAST_LINE'"
      echo "    Raw output (first 20 lines):"
      printf '%s\n' "$OUTPUT" | head -20 | sed 's/^/    /'
      REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:malformed")
      REVIEW_EXIT_REASON="malformed-sentinel"
      print_summary
      handle_error "malformed sentinel"
      ;;
  esac
done

  # End of inner iter loop — decide whether to fall through to next reviewer.
  if [ -n "$FALLTHROUGH_REASON" ]; then
    next_idx=$((_outer_idx + 1))
    if [ "$next_idx" -lt "${#AVAILABLE_CASCADE[@]}" ]; then
      next_reviewer="${AVAILABLE_CASCADE[$next_idx]}"
      next_label="$(reviewer_label_for "$next_reviewer")"
      echo ""
      echo "==> ${REVIEWER_LABEL}: ${FALLTHROUGH_REASON} — falling back to ${next_label}"
      continue
    fi
    # Cascade exhausted — every reviewer that was available failed at runtime.
    echo ""
    echo "==> All reviewers in the cascade failed."
    echo "    Chain: $(format_reviewer_chain)"
    REVIEW_EXIT_REASON="cascade-exhausted"
    print_summary
    handle_error "cascade exhausted: $(format_reviewer_chain)"
  fi
  # Otherwise the inner loop fell off the end without converging — that's a
  # genuine "diff is contentious" signal, not "reviewer broke." Don't try the
  # next reviewer; preserve the original non-convergence behavior below.
  break
done

echo ""
echo "==> Review loop did not converge after $MAX_ITERATIONS iterations."
echo "    $REVIEWER_LABEL made $FIX_COMMITS fix commit(s) but kept finding new issues."
echo "    Push aborted. Investigate manually:"
echo "      git log --oneline -$((MAX_ITERATIONS + 1))"
echo "      git diff HEAD~$FIX_COMMITS..HEAD"
echo ""
echo "    To undo all auto-fix commits: git reset --hard HEAD~$FIX_COMMITS"
echo "    Emergency override: git push --no-verify"
REVIEWER_CHAIN+=("${ACTIVE_REVIEWER}:max-iterations")
REVIEW_EXIT_REASON="max-iterations"
print_summary
exit 1
