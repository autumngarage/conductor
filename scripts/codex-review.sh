#!/usr/bin/env bash
#
# hooks/codex-review.sh — legacy-compatible Conductor review + auto-fix loop.
#
# Preferred human-facing entry points are hooks/conductor-review.sh,
# scripts/conductor-review.sh, and `touchstone review`. This file keeps the
# historical codex-review protocol names working for existing projects.
#
# Touchstone 2.0+: the single reviewer is `conductor` (autumn-garage/conductor).
# Conductor owns per-provider model selection, auth, permission translation,
# route logging, and cost reporting. This hook uses Conductor's semantic review
# interface for read-only review, then asks for edit-capable execution only when
# fix mode has review findings to address.
# Wired into merge-pr.sh and default-branch pre-push checks.
#
# Loop:
#   1. Run Conductor read-only review against the local diff vs the default branch
#   2. If it says CODEX_REVIEW_CLEAN → push allowed.
#   3. If it says CODEX_REVIEW_BLOCKED and mode allows edits → run one
#      edit-capable fix pass for safe findings.
#   4. If it says CODEX_REVIEW_FIXED → it edited files. Stage + commit the
#      fixes (a new commit, NOT an amend) and loop back to step 1.
#   5. If it says CODEX_REVIEW_BLOCKED → push aborts, findings printed.
#   6. After max_iterations rounds without converging, push aborts.
#
# Reviewer selection:
#   2.0 uses a single adapter (`reviewer_conductor_*`) — see the
#   `reviewer_conductor_exec` block below. Legacy 1.x configs that set
#   `[review].reviewers = ["codex", "claude", ...]` are auto-detected at
#   startup and a one-time migration hint is printed; the values are
#   translated to Conductor provider pins.
#
# Configuration:
#   Place .touchstone-review.toml at the repo root. Legacy .codex-review.toml
#   is still read when the Touchstone-named config is absent. Key knobs:
#     [review].reviewer         = "conductor"  (only valid 2.0 value)
#     [review.conductor].prefer = best|cheapest|fastest|balanced
#     [review.conductor].effort = minimal|low|medium|high|max
#     [review.conductor].tags   = "code-review,..."
#     [review.conductor].with   = "<provider>"  (pins a specific provider)
#     [review.conductor].exclude = "<p1>,<p2>"  (skips in auto-routing)
#     [review.context].mode     = "auto"|"full"  (auto prunes simple diffs)
#   See hooks/conductor-review.config.example.toml for the full spec.
#
#   If no review config exists, ALL paths are treated as unsafe
#   (no auto-fix). This is the conservative default — opt in to auto-fix
#   explicitly by listing safe paths or setting safe_by_default = true.
#
# Modes:
#   review-only — Conductor semantic code review, no file edits or commits
#   fix         — full access: read, run commands, edit files, commit fixes
#   diff-only   — read-only: diff embedded in the prompt, no tool use
#   no-tests    — edit + commit, no command execution (skip test runs)
#
#   Modes are declared at the Conductor boundary: Touchstone translates
#   read-only review to `conductor review --base ... --brief-file -`;
#   edit-capable phases pass tool sets and Conductor maps them to each
#   provider's native contract. Set via CODEX_REVIEW_MODE env var or `mode`
#   in .touchstone-review.toml.
#
# Env overrides:
#   TOUCHSTONE_REVIEWER               — DEPRECATED in 2.0.0; auto-translates to TOUCHSTONE_CONDUCTOR_WITH=<provider>
#   TOUCHSTONE_CONDUCTOR_WITH         — pin a specific provider for auto-routing
#   TOUCHSTONE_CONDUCTOR_PREFER       — best|cheapest|fastest|balanced (default: size-aware)
#   TOUCHSTONE_CONDUCTOR_EFFORT       — minimal|low|medium|high|max (default: size-aware)
#   TOUCHSTONE_CONDUCTOR_TAGS         — comma-separated tag hints (default: code-review)
#   TOUCHSTONE_CONDUCTOR_EXCLUDE      — comma-separated providers to skip
#   TOUCHSTONE_CONDUCTOR_FALLBACK_RETRY — true/false; retry infra/sentinel failures through auto-routing once
#   CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS — silence one-time migration hints
#   CODEX_REVIEW_ENABLED              — true/false override for the [review].enabled setting
#   CODEX_REVIEW_MODE                 — review-only|fix|diff-only|no-tests (default: fix)
#   CODEX_REVIEW_BASE                 — base ref to diff against (default: origin/<default-branch>)
#   CODEX_REVIEW_MAX_ITERATIONS       — fix loop cap (default: from config, or 3)
#   CODEX_REVIEW_MAX_DIFF_LINES       — skip review if diff > this many lines (default: 5000)
#   CODEX_REVIEW_CACHE_CLEAN          — cache exact-input clean reviews (default: true)
#   CODEX_REVIEW_TIMEOUT              — optional wall-clock timeout per invocation in seconds (default: 0, no Touchstone wrapper timeout)
#   CODEX_REVIEW_ON_ERROR             — fail-open (default) or fail-closed
#   CODEX_REVIEW_CONTEXT_MODE         — auto|full|bounded prompt context selection (default: auto)
#   CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES — bounded-context diff line cap (default: 400)
#   CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES — bounded-context changed-file cap (default: 4)
#   CODEX_REVIEW_DISABLE_CACHE        — set to true/1 to force a fresh review
#   CODEX_REVIEW_DIAGNOSTICS_FILE     — optional JSONL path for review infra/fallback diagnostics
#   CODEX_REVIEW_FORCE                — set to true/1 to run even on non-default-branch pushes
#   CODEX_REVIEW_NO_AUTOFIX           — set to true/1 for review-only mode (backward compat)
#   CODEX_REVIEW_IN_PROGRESS          — internal guard to skip nested review runs
#   Legacy: TOUCHSTONE_LOCAL_REVIEWER_COMMAND, CODEX_REVIEW_ASSIST*  — parsed but inert in 2.0.
#
# To bypass entirely in an emergency: git push --no-verify
#
# Exit codes:
#   0 — clean review (or graceful skip), push allowed
#   1 — reviewer flagged blocking issues OR fix loop did not converge, push aborted
#
set -euo pipefail

# extract_review_sentinel — find exactly one standalone CODEX_REVIEW_*
# sentinel line in the reviewer's output. The wrapper used to inspect
# only the final physical line via `tail -1 | tr -d '\r '` and case on
# that exact value; any footer after the sentinel — a stray markdown
# rule, a closing fence, an LLM's habitual "Hope this helps." — pushed
# the real sentinel off the last position and the wrapper reported
# malformed despite a clean review. Reading "the unique standalone
# sentinel line, anywhere in the output" is robust to that footer
# class while still rejecting ambiguous cases (zero or multiple
# sentinels) where the contract was actually broken.
extract_review_sentinel() {
  awk '
    /^[[:space:]]*CODEX_REVIEW_(CLEAN|FIXED|BLOCKED)[[:space:]]*$/ {
      line = $0
      gsub(/\r/, "", line)
      sub(/^[[:space:]]+/, "", line)
      sub(/[[:space:]]+$/, "", line)
      sentinel = line
      count++
    }
    /^[[:space:]]*"response"[[:space:]]*:/ {
      line = $0
      gsub(/\r/, "", line)
      sub(/^[[:space:]]*"response"[[:space:]]*:[[:space:]]*"/, "", line)
      while (match(line, /(^|\\n)CODEX_REVIEW_(CLEAN|FIXED|BLOCKED)(\\n|")/)) {
        candidate = substr(line, RSTART, RLENGTH)
        sub(/^\\n/, "", candidate)
        sub(/(\\n|")$/, "", candidate)
        sentinel = candidate
        count++
        line = substr(line, RSTART + RLENGTH)
      }
    }
    END {
      if (count == 1) {
        print sentinel
      }
    }
  '
}

# Test entry-point — when CODEX_REVIEW_TEST_SENTINEL=1, read stdin,
# pipe it through the helper, and exit. Lets shell tests verify the
# extraction logic in isolation without spinning up the full review
# pipeline.
if [ "${CODEX_REVIEW_TEST_SENTINEL:-0}" = "1" ]; then
  extract_review_sentinel
  exit 0
fi

# --------------------------------------------------------------------------
# Sentinel-cycle journal detection helpers
# --------------------------------------------------------------------------

# Returns 0 (truthy) when .sentinel/runs/ contains at least one .md artifact.
is_sentinel_authored_branch() {
  [ -d .sentinel/runs ] && find .sentinel/runs -name '*.md' -type f -print -quit | grep -q .
}

# Prints the path of the most-recently-modified sentinel run artifact.
find_latest_sentinel_run() {
  # ls -t is the simplest portable mtime sort; filenames here are controlled.
  # shellcheck disable=SC2012
  ls -t .sentinel/runs/*.md 2>/dev/null | head -1
}

# Extracts the cycle-id frontmatter field from a sentinel run artifact.
get_cycle_id_from_run() {
  local run_file="$1"
  awk '/^---$/{f=1-f; next} f && /^cycle-id:/{print $2; exit}' "$run_file"
}

# Returns the path of the journal entry whose filename contains cycle-id $1.
find_journal_entry_by_cycle_id() {
  local cycle_id="$1"
  find .cortex/journal -maxdepth 1 -name "*sentinel-cycle-${cycle_id}.md" -type f 2>/dev/null \
    | head -1
}

# Returns path to the most recent sentinel-cycle journal entry by mtime.
find_latest_sentinel_journal_entry() {
  # shellcheck disable=SC2012
  ls -t .cortex/journal/*sentinel-cycle*.md 2>/dev/null | head -1
}

# Returns path to the best-matching sentinel cycle journal entry:
#   1. If the latest run artifact has a cycle-id field, prefer the journal
#      entry whose filename contains that id (per sentinel#97 convention).
#   2. Fall back to the most recently modified sentinel-cycle journal entry.
find_sentinel_journal_entry() {
  local run_file cycle_id matched
  run_file="$(find_latest_sentinel_run)"
  if [ -n "$run_file" ]; then
    cycle_id="$(get_cycle_id_from_run "$run_file")"
    if [ -n "$cycle_id" ]; then
      matched="$(find_journal_entry_by_cycle_id "$cycle_id")"
      if [ -n "$matched" ]; then
        echo "$matched"
        return 0
      fi
    fi
  fi
  # Fallback: most recent journal entry by mtime.
  find_latest_sentinel_journal_entry || true
}

# Prints the <sentinel-cycle-context> block to stdout when this branch is
# sentinel-authored and a cycle journal entry can be resolved. Prints nothing
# (and warns to stderr) when detection fails, the journal is missing, or the
# journal cannot be read.
build_sentinel_journal_context() {
  if ! is_sentinel_authored_branch; then
    return 0
  fi
  local journal_entry
  journal_entry="$(find_sentinel_journal_entry)"
  if [ -z "$journal_entry" ] || [ ! -f "$journal_entry" ]; then
    echo "WARNING: sentinel branch detected but no cycle journal entry found in .cortex/journal/; reviewing without cycle context." >&2
    return 0
  fi
  if [ ! -r "$journal_entry" ]; then
    echo "WARNING: sentinel journal found at $journal_entry but could not be read; reviewing without cycle context." >&2
    return 0
  fi
  local journal_content
  if ! journal_content="$(cat "$journal_entry" 2>/dev/null)"; then
    echo "WARNING: sentinel journal found at $journal_entry but could not be read; reviewing without cycle context." >&2
    return 0
  fi
  printf '<sentinel-cycle-context>\n'
  printf 'This diff was authored by an autonomous Sentinel cycle. The cycle'"'"'s journal entry follows; read it for the planner'"'"'s reasoning, the coder'"'"'s attempts, and any rejections handled mid-cycle. Review the diff against this context.\n\n'
  printf '%s\n' "$journal_content"
  printf '</sentinel-cycle-context>\n'
}

# Test entry-point for sentinel journal context helpers.
# When CODEX_REVIEW_TEST_SENTINEL_CONTEXT=1, print the sentinel context block
# to stdout (empty if detection fails) and exit without running the full
# review pipeline.
if [ "${CODEX_REVIEW_TEST_SENTINEL_CONTEXT:-0}" = "1" ]; then
  build_sentinel_journal_context
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOUCHSTONE_ROOT="${TOUCHSTONE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

resolve_review_config_file() {
  local repo_root="$1"
  if [ -f "$repo_root/.touchstone-review.toml" ]; then
    printf '%s\n' "$repo_root/.touchstone-review.toml"
  elif [ -f "$repo_root/.codex-review.toml" ]; then
    printf '%s\n' "$repo_root/.codex-review.toml"
  else
    printf '%s\n' "$repo_root/.touchstone-review.toml"
  fi
}

CONFIG_FILE="$(resolve_review_config_file "$REPO_ROOT")"
CONFIG_DISPLAY_NAME="$(basename "$CONFIG_FILE")"
cd "$REPO_ROOT"

PREFLIGHT_SCRIPT="$TOUCHSTONE_ROOT/lib/preflight.sh"
if [ -f "$PREFLIGHT_SCRIPT" ]; then
  # shellcheck source=../lib/preflight.sh
  source "$PREFLIGHT_SCRIPT"
fi

# --------------------------------------------------------------------------
# Skip-event audit log
# --------------------------------------------------------------------------
#
# Every skip path and every successful review run writes a single TSV line
# to ~/.touchstone-review-log (overridable via TOUCHSTONE_REVIEW_LOG) so the
# user can audit how often the AI review safety net falls open silently.
# The "No silent failures" engineering principle applies: a Conductor
# outage during a critical week shouldn't be invisible.
#
# Format (6 tab-separated fields):
#   <ISO8601 timestamp>\t<project_path>\t<branch>\t<short_sha>\t<reason>\t<detail>
#
# Reason codes:
#   ran                        review actually executed (the denominator)
#   conductor-missing          Conductor CLI not installed / no provider authed (legacy)
#   conductor-error            reviewer crashed or returned non-zero (legacy catch-all)
#   network-error              (reserved — Conductor reports this via exit code)
#   config-parse-error         (reserved — TOML parser is permissive today)
#   config-disabled            [review].enabled=false in the review config
#   review-disabled-by-user    CODEX_REVIEW_ENABLED=false at the env layer
#   force-skip                 (reserved — no env var skips today; future use)
#   dry-run                    (reserved — bin/touchstone review --dry-run path)
#   other                      catch-all; detail field names the specific case
#
# Fail-open taxonomy (emitted when on_error=fail-open, the default):
#   FAIL_OPEN_TIMEOUT           reviewer exceeded CODEX_REVIEW_TIMEOUT; push allowed
#   FAIL_OPEN_PARSE_ERROR       no valid sentinel in reviewer output; push allowed
#   FAIL_OPEN_DEPENDENCY_MISSING  Conductor CLI not found on PATH; push allowed
#   FAIL_OPEN_PROVIDER_UNAVAILABLE  Conductor installed but no provider configured; push allowed
#   FAIL_OPEN_REVIEWER_ERROR    reviewer crashed or returned non-zero; push allowed
#
# Fail-open events are always visible: a stderr line formatted as
#   [fail-open:<CODE>] <reason> — AI review bypassed, push proceeds
# is emitted for each event so the absent safety boundary is never silent.
# Set on_error="fail-closed" in the review config to make these fatal.

# `${VAR-default}` (single dash) substitutes the default ONLY when VAR is
# unset, NOT when it is an empty string. This preserves the documented
# behavior that TOUCHSTONE_REVIEW_LOG="" disables logging entirely (the
# `[ -z "$log_file" ] && return 0` guard below relies on the empty
# string surviving expansion).
TOUCHSTONE_REVIEW_LOG="${TOUCHSTONE_REVIEW_LOG-$HOME/.touchstone-review-log}"
TOUCHSTONE_REVIEW_LOG_MAX_LINES=1000

log_skip_event() {
  # log_skip_event <reason_code> [<detail>]
  #
  # Fail-safe: never let a logging error break the hook. The hook is
  # already a fail-open safety net — the audit log must not become a new
  # blocking surface.
  local reason="$1"
  local detail="${2:-}"
  local log_file="$TOUCHSTONE_REVIEW_LOG"
  local timestamp branch sha tmp_dir tmp_file line_count keep_lines

  # Disable logging entirely by setting TOUCHSTONE_REVIEW_LOG=/dev/null or
  # to the empty string. Useful for callers (tests, scripted runs) that
  # don't want a log entry.
  [ -z "$log_file" ] && return 0
  [ "$log_file" = "/dev/null" ] && return 0

  # Portable ISO8601 with timezone offset (BSD `date` and GNU `date` both
  # accept `+%Y-%m-%dT%H:%M:%S%z`). The %z form ("-0700") is unambiguous.
  timestamp="$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo unknown)"

  # `git rev-parse --abbrev-ref HEAD` returns "HEAD" in detached state —
  # acceptable; the audit just needs a stable label.
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

  # Strip embedded tabs/newlines from any field — they would break the
  # TSV invariant and make the log un-greppable.
  reason="$(printf '%s' "$reason" | tr '\t\n' '  ')"
  detail="$(printf '%s' "$detail" | tr '\t\n' '  ')"
  branch="$(printf '%s' "$branch" | tr '\t\n' '  ')"

  tmp_dir="${TMPDIR:-/tmp}"
  tmp_dir="${tmp_dir%/}"
  tmp_file="$tmp_dir/touchstone-review-log.$$.tmp"

  # Ensure the parent directory exists. If creation fails, give up
  # silently — logging is best-effort.
  mkdir -p "$(dirname "$log_file")" 2>/dev/null || return 0

  # Cap the log at $TOUCHSTONE_REVIEW_LOG_MAX_LINES entries by tailing
  # the existing file before appending the new line, then atomically
  # replacing. `wc -l` and `tail -n N` are POSIX-portable (no GNU-only
  # flags) so this works on BSD/macOS without coreutils.
  if [ -f "$log_file" ]; then
    # Guard against `set -euo pipefail` propagating a failed pipeline up
    # to the hook's exit code — log_skip_event must never block a push,
    # even on an unreadable log file or a TOCTOU race against rotation.
    line_count="$(wc -l <"$log_file" 2>/dev/null | tr -d ' ')" || line_count=0
    line_count="${line_count:-0}"
    if [ "$line_count" -ge "$TOUCHSTONE_REVIEW_LOG_MAX_LINES" ]; then
      keep_lines=$((TOUCHSTONE_REVIEW_LOG_MAX_LINES - 1))
      tail -n "$keep_lines" "$log_file" >"$tmp_file" 2>/dev/null || : >"$tmp_file"
    else
      cat "$log_file" >"$tmp_file" 2>/dev/null || : >"$tmp_file"
    fi
  else
    : >"$tmp_file"
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$timestamp" "$REPO_ROOT" "$branch" "$sha" "$reason" "$detail" \
    >>"$tmp_file" 2>/dev/null || {
    rm -f "$tmp_file" 2>/dev/null
    return 0
  }

  mv "$tmp_file" "$log_file" 2>/dev/null || rm -f "$tmp_file" 2>/dev/null
  return 0
}

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
PREFLIGHT_REQUIRED=true
REVIEW_TIMEOUT="${CODEX_REVIEW_TIMEOUT:-0}"
ON_ERROR="${CODEX_REVIEW_ON_ERROR:-fail-open}"
UNSAFE_PATHS=""
HIGH_SCRUTINY_PATHS=""
HIGH_SCRUTINY_MODE="peer"
HIGH_SCRUTINY_TRIGGERED=false
HIGH_SCRUTINY_REASON=""
REVIEWER_CASCADE=()
# Legacy local-reviewer env vars — no longer drive behavior in 2.0+, but
# we still declare them so users with these set in their shell don't get
# an unexpected unbound-variable error and existing config-migration
# paths can detect a v1.x project. Register with the shellcheck-friendly
# : ${VAR:=} form (shellcheck flags bare assignment as SC2034 "unused").
# shellcheck disable=SC2269
TOUCHSTONE_LOCAL_REVIEWER_COMMAND="${TOUCHSTONE_LOCAL_REVIEWER_COMMAND:-}"
# shellcheck disable=SC2269
TOUCHSTONE_LOCAL_REVIEWER_AUTH_COMMAND="${TOUCHSTONE_LOCAL_REVIEWER_AUTH_COMMAND:-}"
# 2.0 conductor knobs — filled from [review.conductor] during TOML parse;
# env vars (TOUCHSTONE_CONDUCTOR_*) override just before invocation.
CONDUCTOR_WITH=""
CONDUCTOR_PREFER=""
CONDUCTOR_EFFORT=""
CONDUCTOR_TAGS=""
CONDUCTOR_EXCLUDE=""
CONDUCTOR_EXCLUDE_CONFIGURED=false
CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER=""
CONDUCTOR_PREFLIGHT_FIX_PROVIDER=""
ROUTING_ENABLED=true
ROUTING_SMALL_MAX_DIFF_LINES=400
ROUTING_SMALL_REVIEWERS=() # legacy 1.x shape; retained for back-compat parsing
ROUTING_LARGE_REVIEWERS=() # legacy 1.x shape; retained for back-compat parsing
# 2.0 routing knobs — override CONDUCTOR_* for small vs large diffs.
ROUTING_SMALL_WITH=""
ROUTING_SMALL_PREFER="cheapest"
ROUTING_SMALL_EFFORT="minimal"
ROUTING_SMALL_TAGS=""
ROUTING_LARGE_WITH=""
ROUTING_LARGE_PREFER="best"
ROUTING_LARGE_EFFORT="medium"
ROUTING_LARGE_TAGS=""
ROUTING_HIGH_RISK_WITH=""
ROUTING_HIGH_RISK_PREFER="best"
ROUTING_HIGH_RISK_EFFORT="high"
ROUTING_HIGH_RISK_TAGS=""
ROUTING_DECISION="default"
PROMPT_CONTEXT_MODE="${CODEX_REVIEW_CONTEXT_MODE:-auto}"
PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES="${CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES:-400}"
PROMPT_CONTEXT_SMALL_MAX_FILES="${CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES:-4}"
PROMPT_CONTEXT_FULL_PATHS=""
PROMPT_CONTEXT_ARCHITECTURAL_PATHS="AGENTS.md
CLAUDE.md
GEMINI.md
.touchstone-review.toml
.codex-review.toml
.codex-review-context.md
.github/codex-review-context.md
.github/workflows/
bootstrap/
hooks/conductor-review.sh
hooks/conductor-review.config.example.toml
hooks/codex-review.sh
hooks/codex-review.config.example.toml
scripts/conductor-review.sh
scripts/codex-review.sh
architecture/
docs/architecture/
principles/"
PROMPT_CONTEXT_DECISION="full"
PROMPT_CONTEXT_REASON="context mode not resolved"
PROMPT_CONTEXT_CHANGED_FILES=0
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

strip_toml_string() {
  # Trim whitespace and strip surrounding single/double quotes from a
  # scalar TOML value. No attempt at full TOML-string semantics — just
  # the quoted vs bare-word split that [review.conductor] keys use.
  local value="$1"
  value="$(trim "$value")"
  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
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
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
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

  IFS=',' read -r -a items <<<"$csv"
  for item in "${items[@]}"; do
    append_unsafe_path "$item"
  done
}

append_high_scrutiny_path() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"

  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac

  [ -z "$value" ] && return

  if [ -n "$HIGH_SCRUTINY_PATHS" ]; then
    HIGH_SCRUTINY_PATHS="${HIGH_SCRUTINY_PATHS}
$value"
  else
    HIGH_SCRUTINY_PATHS="$value"
  fi
}

append_high_scrutiny_paths_csv() {
  local csv="$1"
  local item
  local -a items=()

  [ -n "$csv" ] || return 0

  IFS=',' read -r -a items <<<"$csv"
  for item in "${items[@]}"; do
    append_high_scrutiny_path "$item"
  done
}

append_prompt_context_path() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"

  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac

  [ -z "$value" ] && return

  if [ -n "$PROMPT_CONTEXT_FULL_PATHS" ]; then
    PROMPT_CONTEXT_FULL_PATHS="${PROMPT_CONTEXT_FULL_PATHS}
$value"
  else
    PROMPT_CONTEXT_FULL_PATHS="$value"
  fi
}

append_prompt_context_paths_csv() {
  local csv="$1"
  local item
  local -a items=()

  [ -n "$csv" ] || return 0

  IFS=',' read -r -a items <<<"$csv"
  for item in "${items[@]}"; do
    append_prompt_context_path "$item"
  done
}

append_reviewer() {
  local value="$1"
  value="$(trim "$value")"
  value="${value%,}"
  value="$(trim "$value")"
  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  [ -z "$value" ] && return
  REVIEWER_CASCADE+=("$value")
}

append_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<<"$csv"
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
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  [ -z "$value" ] && return
  ROUTING_SMALL_REVIEWERS+=("$value")
}

append_routing_small_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<<"$csv"
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
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  [ -z "$value" ] && return
  ROUTING_LARGE_REVIEWERS+=("$value")
}

append_routing_large_reviewers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<<"$csv"
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
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  [ -z "$value" ] && return
  ASSIST_HELPERS+=("$value")
}

append_assist_helpers_csv() {
  local csv="$1" item
  local -a items=()
  [ -n "$csv" ] || return 0
  IFS=',' read -r -a items <<<"$csv"
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
    true | 1 | yes | on) printf 'true' ;;
    false | 0 | no | off) printf 'false' ;;
    *) printf '%s' "$value" ;;
  esac
}

normalize_conductor_review_tags() {
  printf '%s\n' "${1:-code-review}" \
    | awk -F',' '
      {
        for (i = 1; i <= NF; i++) {
          tag = $i
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", tag)
          if (tag == "" || tag == "tool-use") {
            continue
          }
          if (!(tag in seen)) {
            seen[tag] = 1
            order[++count] = tag
          }
        }
      }
      END {
        out = ""
        if (!("code-review" in seen)) {
          out = "code-review"
        }
        for (i = 1; i <= count; i++) {
          if (out != "") {
            out = out ","
          }
          out = out order[i]
        }
        print out
      }
    '
}

toml_string_value() {
  local value="$1"
  value="$(trim "$value")"
  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  printf '%s' "$value"
}

is_truthy() {
  case "$(normalize_bool "${1:-false}")" in
    true) return 0 ;;
    *) return 1 ;;
  esac
}

# Parse the review config if it exists.
# We do minimal TOML parsing in bash — just key = value pairs and string arrays.
if [ -f "$CONFIG_FILE" ]; then
  # Source the TOML library
  # shellcheck source=../lib/toml.sh
  source "$TOUCHSTONE_ROOT/lib/toml.sh"

  toml_config_callback() {
    local section="$1"
    local key="$2"
    local value="$3"

    case "$section" in
      "review.conductor")
        case "$key" in
          prefer) CONDUCTOR_PREFER="${CONDUCTOR_PREFER:-$(toml_unquote "$value")}" ;;
          effort) CONDUCTOR_EFFORT="${CONDUCTOR_EFFORT:-$(toml_unquote "$value")}" ;;
          tags) CONDUCTOR_TAGS="${CONDUCTOR_TAGS:-$(toml_normalize_array "$value")}" ;;
          with) CONDUCTOR_WITH="${CONDUCTOR_WITH:-$(toml_unquote "$value")}" ;;
          exclude)
            CONDUCTOR_EXCLUDE="${CONDUCTOR_EXCLUDE:-$(toml_normalize_array "$value")}"
            CONDUCTOR_EXCLUDE_CONFIGURED=true
            ;;
        esac
        ;;
      "review.routing")
        case "$key" in
          enabled) ROUTING_ENABLED="$(normalize_bool "$value")" ;;
          small_max_diff_lines | small_diff_lines) ROUTING_SMALL_MAX_DIFF_LINES="$value" ;;
          small_with) ROUTING_SMALL_WITH="$(toml_unquote "$value")" ;;
          small_prefer) ROUTING_SMALL_PREFER="$(toml_unquote "$value")" ;;
          small_effort) ROUTING_SMALL_EFFORT="$(toml_unquote "$value")" ;;
          small_tags) ROUTING_SMALL_TAGS="$(toml_unquote "$value")" ;;
          large_with) ROUTING_LARGE_WITH="$(toml_unquote "$value")" ;;
          large_prefer) ROUTING_LARGE_PREFER="$(toml_unquote "$value")" ;;
          large_effort) ROUTING_LARGE_EFFORT="$(toml_unquote "$value")" ;;
          large_tags) ROUTING_LARGE_TAGS="$(toml_unquote "$value")" ;;
          high_risk_with) ROUTING_HIGH_RISK_WITH="$(toml_unquote "$value")" ;;
          high_risk_prefer) ROUTING_HIGH_RISK_PREFER="$(toml_unquote "$value")" ;;
          high_risk_effort) ROUTING_HIGH_RISK_EFFORT="$(toml_unquote "$value")" ;;
          high_risk_tags) ROUTING_HIGH_RISK_TAGS="$(toml_unquote "$value")" ;;
          small_reviewers)
            if [[ "$value" == "["* ]]; then
              append_routing_small_reviewers_csv "$(toml_normalize_array "$value")"
            else
              append_routing_small_reviewers_csv "$value"
            fi
            ;;
          large_reviewers)
            if [[ "$value" == "["* ]]; then
              append_routing_large_reviewers_csv "$(toml_normalize_array "$value")"
            else
              append_routing_large_reviewers_csv "$value"
            fi
            ;;
        esac
        ;;
      "review.assist")
        case "$key" in
          enabled) ASSIST_ENABLED="${CODEX_REVIEW_ASSIST:-$(normalize_bool "$value")}" ;;
          helpers)
            if [[ "$value" == "["* ]]; then
              append_assist_helpers_csv "$(toml_normalize_array "$value")"
            else
              append_assist_helpers_csv "$value"
            fi
            ;;
          helper) append_assist_helper "$value" ;;
          timeout) ASSIST_TIMEOUT="${CODEX_REVIEW_ASSIST_TIMEOUT:-$value}" ;;
          max_rounds) ASSIST_MAX_ROUNDS="${CODEX_REVIEW_ASSIST_MAX_ROUNDS:-$value}" ;;
        esac
        ;;
      "review.context")
        case "$key" in
          mode) PROMPT_CONTEXT_MODE="${CODEX_REVIEW_CONTEXT_MODE:-$(toml_unquote "$value")}" ;;
          small_max_diff_lines | small_diff_lines) PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES="${CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES:-$value}" ;;
          small_max_files | max_files) PROMPT_CONTEXT_SMALL_MAX_FILES="${CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES:-$value}" ;;
          full_context_paths | full_context_patterns)
            if [[ "$value" == "["* ]]; then
              append_prompt_context_paths_csv "$(toml_normalize_array "$value")"
            else
              append_prompt_context_paths_csv "$value"
            fi
            ;;
        esac
        ;;
      "review")
        case "$key" in
          enabled) REVIEW_ENABLED="${CODEX_REVIEW_ENABLED:-$(normalize_bool "$value")}" ;;
          preflight_required) PREFLIGHT_REQUIRED="$(normalize_bool "$value")" ;;
          high_scrutiny_mode) HIGH_SCRUTINY_MODE="$(toml_unquote "$value")" ;;
          high_scrutiny_paths | high_scrutiny_patterns)
            if [[ "$value" == "["* ]]; then
              append_high_scrutiny_paths_csv "$(toml_normalize_array "$value")"
            else
              append_high_scrutiny_paths_csv "$value"
            fi
            ;;
          reviewers)
            if [[ "$value" == "["* ]]; then
              append_reviewers_csv "$(toml_normalize_array "$value")"
            else
              append_reviewers_csv "$value"
            fi
            ;;
        esac
        ;;
      "review.local")
        case "$key" in
          command | auth_command)
            if [ -z "${CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS:-}" ]; then
              echo "==> NOTE: [review.local] is ignored in Touchstone 2.0.0." >&2
              echo "    Register your command as a Conductor custom provider" >&2
              echo "    (roadmap: v0.3). Silence with CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS=1." >&2
              CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS=1
            fi
            ;;
        esac
        ;;
      "" | "codex_review")
        case "$key" in
          max_iterations) MAX_ITERATIONS="${CODEX_REVIEW_MAX_ITERATIONS:-$value}" ;;
          max_diff_lines) MAX_DIFF_LINES="${CODEX_REVIEW_MAX_DIFF_LINES:-$value}" ;;
          cache_clean_reviews) CACHE_CLEAN_REVIEWS="${CODEX_REVIEW_CACHE_CLEAN:-$(normalize_bool "$value")}" ;;
          safe_by_default) SAFE_BY_DEFAULT="$(normalize_bool "$value")" ;;
          mode) CONFIG_MODE="$(toml_unquote "$value")" ;;
          timeout) REVIEW_TIMEOUT="${CODEX_REVIEW_TIMEOUT:-$value}" ;;
          on_error) ON_ERROR="${CODEX_REVIEW_ON_ERROR:-$(toml_unquote "$value")}" ;;
          unsafe_paths)
            if [[ "$value" == "["* ]]; then
              append_unsafe_paths_csv "$(toml_normalize_array "$value")"
            else
              append_unsafe_paths_csv "$value"
            fi
            ;;
        esac
        ;;
    esac
  }

  toml_parse "$CONFIG_FILE" toml_config_callback
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

# Legacy-config migration: v1.x used `[review].reviewers = [...]` (an ordered
# cascade of codex/claude/gemini/local). Touchstone 2.0 routes through a
# single Conductor adapter; Conductor's auto-router handles cross-provider
# selection. If an older config is detected, translate + warn (one-time).
if [ "${#REVIEWER_CASCADE[@]}" -gt 0 ]; then
  LEGACY_CASCADE="${REVIEWER_CASCADE[*]}"
  # If the legacy cascade was just ("conductor") we leave it alone.
  if [ "${LEGACY_CASCADE}" != "conductor" ]; then
    echo "==> NOTE: [review].reviewers = [${LEGACY_CASCADE// /, }] is a v1.x config." >&2
    echo "    Touchstone 2.0 uses a single reviewer ('conductor') and delegates" >&2
    echo "    per-provider selection to the Conductor router. Migrating to:" >&2
    echo "        reviewer = \"conductor\"" >&2
    echo "        [review.conductor]" >&2
    echo "          prefer = \"best\"" >&2
    echo "          effort = \"high\"" >&2
    echo "    Update ${CONFIG_DISPLAY_NAME:-.touchstone-review.toml} at your convenience. See CHANGELOG for details." >&2
  fi
fi

# Default reviewer: conductor.
REVIEWER_CASCADE=("conductor")

# v1.x peer-review (ASSIST_HELPERS) is disabled in 2.0.0 and returns in v2.1
# via `conductor call --exclude <primary_provider>`. Users who had it enabled
# get a warning; the setting is ignored rather than throwing.
ASSIST_HELPERS=() # 1.x helpers field is ignored — Conductor picks the peer.

REVIEW_ENABLED="$(normalize_bool "$REVIEW_ENABLED")"
ROUTING_ENABLED="$(normalize_bool "$ROUTING_ENABLED")"

# TOUCHSTONE_REVIEWER env var is deprecated in 2.0.0. It was a v1.x-era
# single-reviewer override (codex | claude | gemini | local); today the only
# valid reviewer is 'conductor' itself. Users who want to pin a specific
# underlying provider should use TOUCHSTONE_CONDUCTOR_WITH=<provider>.
if [ -n "${TOUCHSTONE_REVIEWER:-}" ]; then
  case "$TOUCHSTONE_REVIEWER" in
    conductor)
      : # canonical — no translation needed
      ;;
    local)
      # Touchstone 2.0 retired the `local` reviewer; Conductor has no
      # provider by that name, so a raw translation (`--with local`) would
      # fail with "unknown provider". The explicit offline/local analog is
      # ollama. Warn and offer the migration; don't silently pin to something
      # that crashes at call-time.
      echo "==> NOTE: TOUCHSTONE_REVIEWER=local is deprecated in 2.0.0." >&2
      echo "    The 1.x 'local' reviewer is retired; Conductor has no provider by that name." >&2
      echo "    Migrating to explicit offline review: TOUCHSTONE_CONDUCTOR_WITH=ollama." >&2
      echo "    If you had a custom local command, register it as a Conductor custom" >&2
      echo "    provider when v0.3 ships: conductor providers add --name local --shell '<cmd>'" >&2
      # TOUCHSTONE_REVIEWER is env-scoped, so it trumps the TOML `with=` pin.
      CONDUCTOR_WITH="ollama"
      ;;
    openrouter | codex | claude | gemini)
      echo "==> NOTE: TOUCHSTONE_REVIEWER=$TOUCHSTONE_REVIEWER is deprecated in 2.0.0." >&2
      echo "    Pin an underlying provider with: TOUCHSTONE_CONDUCTOR_WITH=$TOUCHSTONE_REVIEWER" >&2
      CONDUCTOR_WITH="$TOUCHSTONE_REVIEWER"
      ;;
    *)
      echo "==> WARNING: TOUCHSTONE_REVIEWER=$TOUCHSTONE_REVIEWER is not a known legacy value." >&2
      echo "    Ignoring; Conductor will auto-route. To pin a provider, use" >&2
      echo "    TOUCHSTONE_CONDUCTOR_WITH=<provider> directly." >&2
      ;;
  esac
  REVIEWER_CASCADE=("conductor")
fi

# Env overrides for the conductor adapter (take precedence over the review config).
CONDUCTOR_WITH="${TOUCHSTONE_CONDUCTOR_WITH:-${CONDUCTOR_WITH:-}}"
CONDUCTOR_PREFER="${TOUCHSTONE_CONDUCTOR_PREFER:-${CONDUCTOR_PREFER:-best}}"
CONDUCTOR_EFFORT="${TOUCHSTONE_CONDUCTOR_EFFORT:-${CONDUCTOR_EFFORT:-high}}"
CONDUCTOR_TAGS="${TOUCHSTONE_CONDUCTOR_TAGS:-${CONDUCTOR_TAGS:-code-review}}"
CONDUCTOR_FALLBACK_RETRY="${TOUCHSTONE_CONDUCTOR_FALLBACK_RETRY:-true}"
if [ -n "${TOUCHSTONE_CONDUCTOR_EXCLUDE+x}" ]; then
  CONDUCTOR_EXCLUDE="$TOUCHSTONE_CONDUCTOR_EXCLUDE"
elif [ "$CONDUCTOR_EXCLUDE_CONFIGURED" = true ]; then
  CONDUCTOR_EXCLUDE="${CONDUCTOR_EXCLUDE:-}"
else
  CONDUCTOR_EXCLUDE="ollama"
fi

# --------------------------------------------------------------------------
# Mode resolution
# --------------------------------------------------------------------------
# Modes: review-only, fix, diff-only, no-tests
#   review-only — semantic read-only review, no edits, no git ops
#   fix         — full access, auto-fix + commit (default)
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
    review-only | fix | diff-only | no-tests) ;;
    *)
      echo "WARNING: Invalid mode '$mode' — falling back to 'fix'. Valid: review-only, fix, diff-only, no-tests" >&2
      mode="fix"
      ;;
  esac
  printf '%s' "$mode"
}

REVIEW_MODE="$(resolve_mode)"
SCOPED_LARGE_DIFF_REVIEW=false
SCOPED_LARGE_DIFF_SYNC_ONLY=false
SCOPED_LARGE_DIFF_ORIGINAL_LINES=""
SCOPED_LARGE_DIFF_LINES=""
SCOPED_LARGE_DIFF_INCLUDED_COUNT=0
SCOPED_LARGE_DIFF_EXCLUDED_COUNT=0
SCOPED_LARGE_DIFF_FILE=""
SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE=""
SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE=""

mode_allows_fix() {
  is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}" && return 1
  [ "$REVIEW_MODE" = "fix" ] || [ "$REVIEW_MODE" = "no-tests" ]
}
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

path_matches_context_pattern() {
  local path="$1"
  local pattern="$2"

  pattern="$(trim "$pattern")"
  [ -n "$path" ] || return 1
  [ -n "$pattern" ] || return 1

  case "$pattern" in
    */)
      [[ "$path" == "$pattern"* ]] && return 0
      ;;
    *\** | *\?* | *\[*)
      # shellcheck disable=SC2053 # Configured context patterns intentionally use globs.
      [[ "$path" == $pattern ]] && return 0
      ;;
    *)
      if [ "$path" = "$pattern" ] || [[ "$path" == "$pattern/"* ]]; then
        return 0
      fi
      ;;
  esac

  return 1
}

find_path_matching_context_patterns() {
  local paths="$1"
  local patterns="$2"
  local path pattern

  [ -n "$paths" ] || return 1
  [ -n "$patterns" ] || return 1

  while IFS= read -r path; do
    [ -n "$path" ] || continue
    while IFS= read -r pattern; do
      [ -n "$pattern" ] || continue
      if path_matches_context_pattern "$path" "$pattern"; then
        printf '%s (matched %s)' "$path" "$pattern"
        return 0
      fi
    done <<<"$patterns"
  done <<<"$paths"

  return 1
}

find_full_context_reason() {
  local changed_paths="$1"
  local match

  match="$(find_path_matching_context_patterns "$changed_paths" "$UNSAFE_PATHS" || true)"
  if [ -n "$match" ]; then
    printf 'high-risk path %s' "$match"
    return 0
  fi

  match="$(find_path_matching_context_patterns "$changed_paths" "$PROMPT_CONTEXT_ARCHITECTURAL_PATHS" || true)"
  if [ -n "$match" ]; then
    printf 'architectural path %s' "$match"
    return 0
  fi

  match="$(find_path_matching_context_patterns "$changed_paths" "$HIGH_SCRUTINY_PATHS" || true)"
  if [ -n "$match" ]; then
    printf 'configured high-scrutiny path %s' "$match"
    return 0
  fi

  match="$(find_path_matching_context_patterns "$changed_paths" "$PROMPT_CONTEXT_FULL_PATHS" || true)"
  if [ -n "$match" ]; then
    printf 'configured full-context path %s' "$match"
    return 0
  fi

  return 1
}

select_prompt_context_mode() {
  local diff_lines="$1"
  local changed_paths="$2"
  local requested full_reason size_reason

  PROMPT_CONTEXT_CHANGED_FILES="$(printf '%s\n' "$changed_paths" | sed '/^$/d' | wc -l | tr -d ' ')"
  requested="$(printf '%s' "${PROMPT_CONTEXT_MODE:-auto}" | tr '[:upper:]' '[:lower:]')"

  case "$requested" in
    auto | bounded | full) ;;
    *)
      echo "WARNING: Invalid review.context.mode='$PROMPT_CONTEXT_MODE' — using auto." >&2
      requested="auto"
      ;;
  esac

  if [ "$requested" = "full" ]; then
    PROMPT_CONTEXT_DECISION="full"
    PROMPT_CONTEXT_REASON="review.context.mode is full"
    return 0
  fi

  case "$PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES" in
    '' | *[!0-9]*)
      PROMPT_CONTEXT_DECISION="full"
      PROMPT_CONTEXT_REASON="invalid review.context.small_max_diff_lines='$PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES'"
      return 0
      ;;
  esac

  case "$PROMPT_CONTEXT_SMALL_MAX_FILES" in
    '' | *[!0-9]*)
      PROMPT_CONTEXT_DECISION="full"
      PROMPT_CONTEXT_REASON="invalid review.context.small_max_files='$PROMPT_CONTEXT_SMALL_MAX_FILES'"
      return 0
      ;;
  esac

  full_reason="$(find_full_context_reason "$changed_paths" || true)"
  if [ -n "$full_reason" ]; then
    PROMPT_CONTEXT_DECISION="full"
    PROMPT_CONTEXT_REASON="$full_reason"
    return 0
  fi

  if [ "$diff_lines" -gt "$PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES" ] 2>/dev/null \
    && [ "$PROMPT_CONTEXT_CHANGED_FILES" -gt "$PROMPT_CONTEXT_SMALL_MAX_FILES" ] 2>/dev/null; then
    size_reason="low-risk large/broad diff ($diff_lines > $PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES lines, $PROMPT_CONTEXT_CHANGED_FILES > $PROMPT_CONTEXT_SMALL_MAX_FILES files)"
  elif [ "$diff_lines" -gt "$PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES" ] 2>/dev/null; then
    size_reason="low-risk large diff ($diff_lines > $PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES lines)"
  elif [ "$PROMPT_CONTEXT_CHANGED_FILES" -gt "$PROMPT_CONTEXT_SMALL_MAX_FILES" ] 2>/dev/null; then
    size_reason="low-risk broad diff ($PROMPT_CONTEXT_CHANGED_FILES > $PROMPT_CONTEXT_SMALL_MAX_FILES files)"
  else
    size_reason="small/simple diff ($diff_lines <= $PROMPT_CONTEXT_SMALL_MAX_DIFF_LINES lines, $PROMPT_CONTEXT_CHANGED_FILES <= $PROMPT_CONTEXT_SMALL_MAX_FILES files)"
  fi

  PROMPT_CONTEXT_DECISION="bounded"
  PROMPT_CONTEXT_REASON="$size_reason with no high-risk, architectural, or configured full-context path matches"
}

build_prompt_context_instructions() {
  if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
    cat <<CONTEXT_EOF
## Project context mode

Large-diff scoped review boundary:
- Total branch diff: $SCOPED_LARGE_DIFF_ORIGINAL_LINES lines (> $MAX_DIFF_LINES cap).
- Scoped project-owned diff: $SCOPED_LARGE_DIFF_LINES lines across $SCOPED_LARGE_DIFF_INCLUDED_COUNT file(s).
- Excluded paths: $SCOPED_LARGE_DIFF_EXCLUDED_COUNT trusted Touchstone-managed file(s) that appeared in both base and HEAD .touchstone-manifest.
- .touchstone-manifest and mixed-ownership steering/config files are never excluded.

Review only the embedded scoped diff below. Do not inspect or reason about excluded managed paths unless they are shown in the scoped diff.
Do not edit files. Do not stage, commit, or modify anything. Do not emit CODEX_REVIEW_FIXED.
CONTEXT_EOF
    return 0
  fi

  if [ "$PROMPT_CONTEXT_DECISION" = "bounded" ]; then
    cat <<CONTEXT_EOF
## Project context mode

Full AGENTS.md and CLAUDE.md context was intentionally omitted because this is a $PROMPT_CONTEXT_REASON.
Use the bounded project review context below. Do not spend context loading AGENTS.md or CLAUDE.md unless the diff itself makes that necessary.

Bounded project review context:
- Review only concrete correctness, safety, compatibility, and test-coverage risks in this diff.
- Block silent failures, data loss, destructive operations without recovery, public boundary breaks, or missing regression tests for bug fixes.
- Treat security, auth, payments, migrations, release/deploy, and generated-artifact changes as blocking unless the diff proves they are safe.
- Do not flag formatting, naming, comments, speculative refactors, or broad design preferences without a specific failing behavior.
CONTEXT_EOF
    return 0
  fi

  cat <<CONTEXT_EOF
## Project context mode

Full project context is required because $PROMPT_CONTEXT_REASON.
Read AGENTS.md at the repo root for project context and the review rubric (if it exists).
If AGENTS.md has both authoring guidance and a review guide, use the review guide for findings.
Read CLAUDE.md at the repo root for additional Claude-specific project context (if it exists).
CONTEXT_EOF
}

# --------------------------------------------------------------------------
# Large-diff Touchstone sync slicing
# --------------------------------------------------------------------------

is_touchstone_source_repo() {
  local repo_root_physical touchstone_root_physical

  repo_root_physical="$(cd "$REPO_ROOT" 2>/dev/null && pwd -P)" || return 1
  touchstone_root_physical="$(cd "$TOUCHSTONE_ROOT" 2>/dev/null && pwd -P)" || return 1
  [ "$repo_root_physical" = "$touchstone_root_physical" ] || return 1

  # In downstream projects the installed hook runs from scripts/conductor-review.sh
  # or the legacy scripts/codex-review.sh compatibility path,
  # so TOUCHSTONE_ROOT intentionally resolves to the project root. Only disable
  # sync-slice elision in the actual Touchstone source checkout.
  [ -f "$repo_root_physical/bootstrap/update-project.sh" ] \
    && [ -f "$repo_root_physical/hooks/codex-review.sh" ] \
    && [ -f "$repo_root_physical/bin/touchstone" ]
}

manifest_paths_at_ref() {
  local ref="$1"

  git show "$ref:.touchstone-manifest" 2>/dev/null | awk '
    {
      sub(/\r$/, "")
      sub(/^[[:space:]]+/, "")
      sub(/[[:space:]]+$/, "")
    }
    $0 == "" { next }
    $0 ~ /^#/ { next }
    {
      sub(/^\.\//, "")
      print
    }
  '
}

path_is_mixed_ownership() {
  case "$1" in
    .touchstone-manifest | AGENTS.md | CLAUDE.md | GEMINI.md | .touchstone-review.toml | .codex-review.toml) return 0 ;;
    *) return 1 ;;
  esac
}

trusted_touchstone_managed_paths() {
  local base_ref="$1"
  local head_ref="$2"
  local base_paths head_paths

  base_paths="$(mktemp "${TMPDIR:-/tmp}/touchstone-base-manifest.XXXXXX")"
  head_paths="$(mktemp "${TMPDIR:-/tmp}/touchstone-head-manifest.XXXXXX")"

  manifest_paths_at_ref "$base_ref" | LC_ALL=C sort -u >"$base_paths"
  manifest_paths_at_ref "$head_ref" | LC_ALL=C sort -u >"$head_paths"

  if [ ! -s "$base_paths" ] || [ ! -s "$head_paths" ]; then
    rm -f "$base_paths" "$head_paths"
    return 1
  fi

  comm -12 "$base_paths" "$head_paths" | while IFS= read -r path; do
    [ -n "$path" ] || continue
    path_is_mixed_ownership "$path" && continue
    printf '%s\n' "$path"
  done
  rm -f "$base_paths" "$head_paths"
}

path_is_trusted_touchstone_managed() {
  local changed_path="$1"
  local trusted_paths_file="$2"
  local managed_path

  [ -f "$trusted_paths_file" ] || return 1
  while IFS= read -r managed_path; do
    [ -n "$managed_path" ] || continue
    if [ "$changed_path" = "$managed_path" ]; then
      return 0
    fi
    case "$managed_path" in
      */)
        case "$changed_path" in
          "$managed_path"*) return 0 ;;
        esac
        ;;
    esac
  done <"$trusted_paths_file"

  return 1
}

prepare_large_diff_scoped_review() {
  local total_lines="$1"
  local trusted_paths_file changed_path
  local -a scoped_paths=()

  case "$MAX_DIFF_LINES" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$total_lines" -gt "$MAX_DIFF_LINES" ] 2>/dev/null || return 0
  is_touchstone_source_repo && return 1

  trusted_paths_file="$(mktemp "${TMPDIR:-/tmp}/touchstone-trusted-manifest.XXXXXX")"
  if ! trusted_touchstone_managed_paths "$MERGE_BASE" HEAD >"$trusted_paths_file"; then
    rm -f "$trusted_paths_file"
    return 1
  fi
  if [ ! -s "$trusted_paths_file" ]; then
    rm -f "$trusted_paths_file"
    return 1
  fi

  SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-scoped-included.XXXXXX")"
  SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-scoped-excluded.XXXXXX")"
  : >"$SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE"
  : >"$SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE"

  while IFS= read -r -d '' changed_path; do
    [ -n "$changed_path" ] || continue
    if path_is_trusted_touchstone_managed "$changed_path" "$trusted_paths_file"; then
      printf '%s\n' "$changed_path" >>"$SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE"
    else
      printf '%s\n' "$changed_path" >>"$SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE"
    fi
  done < <(git diff --name-only -z "$MERGE_BASE"..HEAD)
  rm -f "$trusted_paths_file"

  SCOPED_LARGE_DIFF_EXCLUDED_COUNT="$(sed '/^$/d' "$SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE" | wc -l | tr -d ' ')"
  SCOPED_LARGE_DIFF_INCLUDED_COUNT="$(sed '/^$/d' "$SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE" | wc -l | tr -d ' ')"
  [ "$SCOPED_LARGE_DIFF_EXCLUDED_COUNT" -gt 0 ] 2>/dev/null || return 1

  if [ "$SCOPED_LARGE_DIFF_INCLUDED_COUNT" -eq 0 ] 2>/dev/null; then
    SCOPED_LARGE_DIFF_SYNC_ONLY=true
    SCOPED_LARGE_DIFF_ORIGINAL_LINES="$total_lines"
    return 0
  fi

  while IFS= read -r changed_path; do
    [ -n "$changed_path" ] || continue
    scoped_paths+=("$changed_path")
  done <"$SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE"

  SCOPED_LARGE_DIFF_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-scoped-diff.XXXXXX")"
  git diff "$MERGE_BASE"..HEAD -- "${scoped_paths[@]}" >"$SCOPED_LARGE_DIFF_FILE"
  SCOPED_LARGE_DIFF_LINES="$(wc -l <"$SCOPED_LARGE_DIFF_FILE" | tr -d ' ')"
  SCOPED_LARGE_DIFF_ORIGINAL_LINES="$total_lines"

  if [ "$SCOPED_LARGE_DIFF_LINES" -gt "$MAX_DIFF_LINES" ] 2>/dev/null; then
    SCOPED_LARGE_DIFF_REVIEW=false
    return 1
  fi

  SCOPED_LARGE_DIFF_REVIEW=true
  PROMPT_CONTEXT_DECISION="scoped"
  PROMPT_CONTEXT_REASON="large Touchstone-managed diff sliced to project-owned files"
  return 0
}

# --------------------------------------------------------------------------
# Build the auto-fix policy section of the prompt from config
# --------------------------------------------------------------------------

build_autofix_policy() {
  local policy=""

  if ! mode_allows_fix || [ "${REVIEW_PHASE:-review}" != "fix" ]; then
    if [ "$SAFE_BY_DEFAULT" = "true" ]; then
      policy="By default, all paths are SAFE to auto-fix unless listed as unsafe."
    else
      policy="By default, all paths are NOT safe to auto-fix. Only paths explicitly marked as safe are fixable."
    fi
    if [ -n "$UNSAFE_PATHS" ]; then
      policy="$policy

NOT safe to auto-fix:
$(echo "$UNSAFE_PATHS" | while read -r p; do [ -n "$p" ] && echo "- Anything in $p"; done)"
    fi
    cat <<POLICY_EOF
Mode: $REVIEW_MODE, phase: read-only review — do not edit files. Do not stage, commit, or modify anything.

Auto-fix classification context:
$policy

Review only:
- If there are no blocking issues, emit CLEAN.
- If any issue needs a code or documentation change, emit BLOCKED with findings.
- If a finding appears safely auto-fixable under the project policy, mark that line with [fixable].
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

build_fix_prompt() {
  local review_output="$1"
  local fix_policy
  fix_policy="$(REVIEW_PHASE=fix build_autofix_policy)"

  cat <<FIX_PROMPT_EOF
You are applying safe fixes after a read-only Touchstone review.

The read-only reviewer found these blockers:

$review_output

$(build_prompt_context_instructions)

Examine the diff vs $BASE using your tools, then apply fixes only for the
review findings below.

## Fix policy

$fix_policy

Apply only the smallest safe changes needed for findings you can confidently fix.
Do not broaden the diff, do not weaken tests, and do not fix findings outside the safe path policy.

Output contract — strict:
- Emit CODEX_REVIEW_FIXED if you changed files.
- Emit CODEX_REVIEW_BLOCKED if any blocker remains unsafe or unclear to fix.
- Do not emit CODEX_REVIEW_CLEAN from the fix phase.

The LAST line of your output must be exactly CODEX_REVIEW_FIXED or CODEX_REVIEW_BLOCKED.
FIX_PROMPT_EOF
}

build_preflight_review_policy() {
  case "${DETERMINISTIC_PREFLIGHT_RESULT:-not-run}" in
    passed*)
      cat <<POLICY_EOF
## Deterministic preflight

Deterministic preflight already passed for this diff before the live review.
Do not rerun the full preflight, the full \`tests/test-*.sh\` suite, or broad lint/build sweeps.
Run focused commands only when needed to verify a specific suspected blocker that the preflight did not already cover.
POLICY_EOF
      ;;
    *) ;;
  esac
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
# Every reviewer exposes three functions:
#   reviewer_<id>_available  — exit 0 if the reviewer can be invoked
#   reviewer_<id>_auth_ok    — exit 0 if at least one underlying model is authed
#   reviewer_<id>_exec PROMPT — run the review; stdout = output, exit code = success
#
# Touchstone 2.0 ships a single reviewer, `conductor`, which wraps the
# autumn-garage/conductor CLI. Conductor owns the per-provider translation
# (`--sandbox`, `--allowedTools`, `--yolo`, etc. are entirely its concern);
# Touchstone just declares capability-level intent through portable tool names
# and lets the router pick.

reviewer_conductor_available() {
  command -v conductor >/dev/null 2>&1
}

reviewer_conductor_auth_ok() {
  # Delegate to `conductor doctor --json` — cheap check, makes no upstream
  # calls, confirms at least one provider is configured.
  local doctor_json
  doctor_json=$(conductor doctor --json 2>/dev/null) || return 1
  echo "$doctor_json" | grep -q '"configured"[[:space:]]*:[[:space:]]*true'
}

reviewer_conductor_exec() {
  local prompt="$1"
  local phase="${REVIEW_PHASE:-review}"
  local -a args=()
  local subcommand
  local tools
  local effective_with

  # REVIEW_MODE + REVIEW_PHASE → Conductor job shape. The default phase uses
  # Conductor's semantic review command and lets Conductor own routing policy.
  # Edit-capable work is a separate phase after a BLOCKED read-only review.
  subcommand="$(conductor_subcommand_for_mode "$phase")"
  tools="$(conductor_tools_for_mode "$phase")"
  effective_with="$(conductor_effective_with_for_phase "$phase")"

  if [ "$subcommand" = "review" ]; then
    local review_tags
    review_tags="$(normalize_conductor_review_tags "${CONDUCTOR_TAGS:-}")"
    [ -n "$effective_with" ] && args+=(--with "$effective_with")
    if [ -z "$effective_with" ]; then
      args+=(--prefer "${CONDUCTOR_PREFER:-best}")
      [ -n "$review_tags" ] && args+=(--tags "$review_tags")
    fi
    args+=(--effort "${CONDUCTOR_EFFORT:-high}")
    if [ -n "${CONDUCTOR_EXCLUDE:-}" ]; then
      args+=(--exclude "$CONDUCTOR_EXCLUDE")
    fi
    args+=(--base "$BASE" --brief-file -)
    if [ "${REVIEW_TIMEOUT:-0}" -gt 0 ] 2>/dev/null; then
      args+=(--timeout "$REVIEW_TIMEOUT")
    fi
    printf '%s' "$prompt" \
      | CODEX_REVIEW_IN_PROGRESS=1 conductor review "${args[@]}"
    return
  fi

  # Provider selection for exec/call paths: --with <id> pins a provider;
  # otherwise --auto lets the router pick based on prefer + effort + tags.
  if [ -n "$effective_with" ]; then
    args+=(--with "$effective_with")
  else
    args+=(--auto)
    args+=(--prefer "${CONDUCTOR_PREFER:-best}")
    [ -n "${CONDUCTOR_TAGS:-}" ] && args+=(--tags "$CONDUCTOR_TAGS")
    [ -n "${CONDUCTOR_EXCLUDE:-}" ] && args+=(--exclude "$CONDUCTOR_EXCLUDE")
  fi
  args+=(--effort "${CONDUCTOR_EFFORT:-high}")

  if [ "$subcommand" = "exec" ]; then
    args+=(--tools "$tools")
    if [ "${REVIEW_TIMEOUT:-0}" -gt 0 ] 2>/dev/null; then
      args+=(--timeout "$REVIEW_TIMEOUT")
    fi
    if [ -n "${REVIEW_CONDUCTOR_LOG_FILE:-}" ]; then
      args+=(--log-file "$REVIEW_CONDUCTOR_LOG_FILE")
    fi
  fi

  # Pass the prompt via stdin. Avoids argv length limits on large diffs and
  # matches Conductor's established stdin-fallback path.
  printf '%s' "$prompt" \
    | CODEX_REVIEW_IN_PROGRESS=1 conductor "$subcommand" "${args[@]}"
}

conductor_csv_empty_or_only_ollama() {
  local raw="${1:-}"
  local item
  local -a items

  [ -n "$raw" ] || return 0
  IFS=',' read -r -a items <<<"$raw"
  for item in "${items[@]}"; do
    item="$(trim "$item")"
    [ -z "$item" ] && continue
    [ "$item" = "ollama" ] || return 1
  done
  return 0
}

conductor_should_use_semantic_review() {
  [ "${CONDUCTOR_WITH:-}" != "ollama" ] || return 1
  return 0
}

conductor_effective_with_for_phase() {
  local phase="${1:-review}"

  if [ -n "${CONDUCTOR_WITH:-}" ]; then
    printf '%s' "$CONDUCTOR_WITH"
    return 0
  fi

  case "$phase" in
    review) printf '%s' "${CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER:-}" ;;
    fix) printf '%s' "${CONDUCTOR_PREFLIGHT_FIX_PROVIDER:-}" ;;
  esac
}

conductor_native_review_provider() {
  case "$1" in
    codex | claude | gemini) return 0 ;;
    *) return 1 ;;
  esac
}

conductor_review_pin_exclude() {
  local pinned="$1"
  local existing="$2"
  local provider
  local exclude="$existing"

  for provider in claude codex deepseek-chat deepseek-reasoner gemini kimi ollama openrouter; do
    [ "$provider" = "$pinned" ] && continue
    exclude="$(exclude_provider_once "$exclude" "$provider")"
  done
  printf '%s' "$exclude"
}

conductor_subcommand_for_mode() {
  local phase="${1:-review}"

  case "$phase:$REVIEW_MODE" in
    review:diff-only) printf 'call' ;;
    review:*)
      if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
        printf 'call'
      elif conductor_should_use_semantic_review; then
        printf 'review'
      else
        printf 'exec'
      fi
      ;;
    fix:*) printf 'exec' ;;
    *) printf 'exec' ;;
  esac
}

conductor_tools_for_mode() {
  local phase="${1:-review}"

  case "$phase:$REVIEW_MODE" in
    review:diff-only) printf '' ;;
    review:no-tests) printf 'Read,Grep,Glob' ;;
    review:*) printf 'Read,Grep,Glob,Bash' ;;
    fix:no-tests) printf 'Read,Grep,Glob,Edit,Write' ;;
    fix:*) printf 'Read,Grep,Glob,Bash,Edit,Write' ;;
    *) printf 'Read,Grep,Glob,Bash' ;;
  esac
}

conductor_route_json_string_field() {
  local json="$1"
  local field="$2"
  printf '%s\n' "$json" \
    | sed -nE 's/.*"'"$field"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' \
    | head -1
}

conductor_csv_contains() {
  local csv="$1"
  local wanted="$2"
  local item
  local -a items

  [ -n "$csv" ] || return 1
  IFS=',' read -r -a items <<<"$csv"
  for item in "${items[@]}"; do
    item="$(trim "$item")"
    if [ "$item" = "$wanted" ]; then
      return 0
    fi
  done
  return 1
}

conductor_semantic_review_provider() {
  case "$1" in
    claude | codex | gemini | openrouter) return 0 ;;
    *) return 1 ;;
  esac
}

conductor_semantic_review_exclude() {
  local existing="$1"
  local provider
  local exclude="$existing"

  for provider in deepseek-chat deepseek-reasoner kimi ollama; do
    exclude="$(exclude_provider_once "$exclude" "$provider")"
  done
  printf '%s' "$exclude"
}

conductor_route_preflight_for_phase() {
  local phase="$1"
  local subcommand="$2"
  local tools="$3"
  local route_tags route_exclude route_stderr route_json route_rc provider error
  local estimated_input_tokens
  local -a args

  route_tags="$(normalize_conductor_review_tags "${CONDUCTOR_TAGS:-}")"
  route_exclude="${CONDUCTOR_EXCLUDE:-}"

  if [ "$subcommand" = "review" ]; then
    if [ -n "${CONDUCTOR_WITH:-}" ] && ! conductor_semantic_review_provider "$CONDUCTOR_WITH"; then
      echo "ERROR: Review route preflight failed before invoking reviewer." >&2
      echo "       phase: $phase (subcommand=$subcommand, tools=${tools:-none})" >&2
      echo "       requested provider: $CONDUCTOR_WITH" >&2
      echo "       missing capability: pinned provider cannot satisfy semantic conductor review" >&2
      echo "       next action: use claude, codex, gemini, openrouter, or unset TOUCHSTONE_CONDUCTOR_WITH for auto-routing." >&2
      return 1
    fi
    route_exclude="$(conductor_semantic_review_exclude "$route_exclude")"
  fi

  if [ -n "${CONDUCTOR_WITH:-}" ]; then
    if conductor_csv_contains "$route_exclude" "$CONDUCTOR_WITH"; then
      echo "ERROR: Review route preflight failed before invoking reviewer." >&2
      echo "       phase: $phase (subcommand=$subcommand, tools=${tools:-none})" >&2
      echo "       requested provider: $CONDUCTOR_WITH" >&2
      echo "       missing capability: pinned provider is excluded by TOUCHSTONE_CONDUCTOR_EXCLUDE/[review.conductor].exclude" >&2
      echo "       next action: remove $CONDUCTOR_WITH from the exclusion list, unset TOUCHSTONE_CONDUCTOR_WITH, or pin a viable provider such as openrouter." >&2
      return 1
    fi
    route_exclude="$(conductor_review_pin_exclude "$CONDUCTOR_WITH" "$route_exclude")"
  fi

  estimated_input_tokens=$((ROUTING_DIFF_LINE_COUNT * 20 + 1000))
  args=(route --json --prefer "${CONDUCTOR_PREFER:-best}" --effort "${CONDUCTOR_EFFORT:-high}"
    --estimated-input-tokens "$estimated_input_tokens" --estimated-output-tokens 500)
  [ -n "$route_tags" ] && args+=(--tags "$route_tags")
  [ -n "$tools" ] && args+=(--tools "$tools")
  [ -n "$route_exclude" ] && args+=(--exclude "$route_exclude")

  route_stderr="$(mktemp "${TMPDIR:-/tmp}/touchstone-route-preflight.XXXXXX")"
  set +e
  route_json="$(conductor "${args[@]}" 2>"$route_stderr")"
  route_rc=$?
  set -e

  provider="$(conductor_route_json_string_field "$route_json" provider)"
  error="$(conductor_route_json_string_field "$route_json" error)"
  if [ -z "$error" ] && [ -s "$route_stderr" ]; then
    error="$(tr '\n' ' ' <"$route_stderr" | sed 's/[[:space:]][[:space:]]*/ /g')"
  fi
  rm -f "$route_stderr"

  if [ "$route_rc" -ne 0 ] || [ -z "$provider" ]; then
    [ -n "$error" ] || error="no provider satisfies this route"
    echo "ERROR: Review route preflight failed before invoking reviewer." >&2
    echo "       phase: $phase (subcommand=$subcommand, tools=${tools:-none})" >&2
    echo "       requested provider: ${CONDUCTOR_WITH:-auto}" >&2
    echo "       provider exclusions: ${CONDUCTOR_EXCLUDE:-none}" >&2
    case "$error" in
      *"does not support tools"*) echo "       missing capability: $error" >&2 ;;
      *) echo "       reason: $error" >&2 ;;
    esac
    echo "       next action: set TOUCHSTONE_CONDUCTOR_WITH=openrouter, remove over-broad exclusions, or run conductor doctor." >&2
    return 1
  fi

  if [ -n "${CONDUCTOR_WITH:-}" ] && [ "$provider" != "$CONDUCTOR_WITH" ]; then
    echo "ERROR: Review route preflight selected '$provider' while '$CONDUCTOR_WITH' was pinned." >&2
    echo "       next action: unset TOUCHSTONE_CONDUCTOR_WITH or pin the selected viable provider." >&2
    return 1
  fi

  case "$phase" in
    review) CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER="$provider" ;;
    fix) CONDUCTOR_PREFLIGHT_FIX_PROVIDER="$provider" ;;
  esac

  echo "==> Review route preflight: $phase route viable via $provider (subcommand=$subcommand, tools=${tools:-none})"
  return 0
}

run_conductor_route_preflight() {
  local review_subcommand review_tools fix_tools

  [ "${ACTIVE_REVIEWER:-}" = "conductor" ] || return 0

  # Keep the extra subprocess out of ordinary feature-branch pre-push hooks;
  # the merge gate sets CODEX_REVIEW_PR_NUMBER and is where route viability
  # must fail closed before provider wall-clock time is spent.
  if [ -z "${CODEX_REVIEW_PR_NUMBER:-}" ] && ! is_truthy "${TOUCHSTONE_REVIEW_ROUTE_PREFLIGHT:-false}"; then
    return 0
  fi

  if ! conductor route --help >/dev/null 2>&1; then
    echo "WARNING: conductor route preflight is unavailable; continuing without route viability validation." >&2
    return 0
  fi

  CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER=""
  CONDUCTOR_PREFLIGHT_FIX_PROVIDER=""

  echo "==> Review route preflight: mode=$REVIEW_MODE with=${CONDUCTOR_WITH:-auto} prefer=${CONDUCTOR_PREFER:-best} effort=${CONDUCTOR_EFFORT:-high} exclude=${CONDUCTOR_EXCLUDE:-none}"

  review_subcommand="$(conductor_subcommand_for_mode review)"
  review_tools=""
  if [ "$review_subcommand" = "exec" ]; then
    review_tools="$(conductor_tools_for_mode review)"
  fi
  conductor_route_preflight_for_phase review "$review_subcommand" "$review_tools" || return 1

  if mode_allows_fix; then
    fix_tools="$(conductor_tools_for_mode fix)"
    conductor_route_preflight_for_phase fix exec "$fix_tools" || return 1
  fi
}

# --------------------------------------------------------------------------
# Reviewer cascade resolver
# --------------------------------------------------------------------------

ACTIVE_REVIEWER=""
REVIEWER_STATUS=""

resolve_reviewer() {
  local reviewer
  ACTIVE_REVIEWER=""
  REVIEWER_STATUS=""

  for reviewer in "${REVIEWER_CASCADE[@]}"; do
    if ! declare -F "reviewer_${reviewer}_available" >/dev/null; then
      REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: unknown reviewer\n"
      continue
    fi
    if ! "reviewer_${reviewer}_available"; then
      case "$reviewer" in
        conductor)
          REVIEWER_STATUS="${REVIEWER_STATUS}    conductor: CLI not found on PATH\n"
          REVIEWER_STATUS="${REVIEWER_STATUS}      → brew install autumngarage/conductor/conductor\n"
          REVIEWER_STATUS="${REVIEWER_STATUS}      → conductor init   (configure providers interactively)\n"
          ;;
        local)
          REVIEWER_STATUS="${REVIEWER_STATUS}    local: command not configured\n"
          ;;
        *)
          REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: CLI not installed\n"
          ;;
      esac
      continue
    fi
    if ! "reviewer_${reviewer}_auth_ok"; then
      case "$reviewer" in
        conductor)
          REVIEWER_STATUS="${REVIEWER_STATUS}    conductor: no provider is configured\n"
          REVIEWER_STATUS="${REVIEWER_STATUS}      → conductor doctor    (diagnose what's missing)\n"
          REVIEWER_STATUS="${REVIEWER_STATUS}      → conductor init      (guided provider setup)\n"
          ;;
        *)
          REVIEWER_STATUS="${REVIEWER_STATUS}    ${reviewer}: auth check failed\n"
          ;;
      esac
      continue
    fi
    ACTIVE_REVIEWER="$reviewer"
    return 0
  done

  return 1
}

ASSIST_REVIEWER=""
ASSIST_REVIEWER_STATUS=""

resolve_assist_reviewer() {
  local helper
  ASSIST_REVIEWER=""
  ASSIST_REVIEWER_STATUS=""

  for helper in ${ASSIST_HELPERS[@]+"${ASSIST_HELPERS[@]}"}; do
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
  local changed_paths="${2:-}"
  local risk_reason

  is_truthy "$ROUTING_ENABLED" || return 0
  [ -z "${TOUCHSTONE_REVIEWER:-}" ] || return 0

  case "$ROUTING_SMALL_MAX_DIFF_LINES" in
    '' | *[!0-9]*)
      echo "WARNING: Invalid review.routing.small_max_diff_lines='$ROUTING_SMALL_MAX_DIFF_LINES' — ignoring routing." >&2
      return 0
      ;;
  esac

  # Legacy 1.x cascade arrays survive for back-compat; 2.0 routing lives in
  # the per-bucket CONDUCTOR_* overrides. In 2.0 the cascade is always
  # ("conductor") after migration, so the array swap is a no-op — the real
  # routing choice is the CONDUCTOR_WITH / PREFER / EFFORT / TAGS swap.
  if [ "${#ROUTING_SMALL_REVIEWERS[@]}" -eq 0 ]; then
    ROUTING_SMALL_REVIEWERS=("${REVIEWER_CASCADE[@]}")
  fi
  if [ "${#ROUTING_LARGE_REVIEWERS[@]}" -eq 0 ]; then
    ROUTING_LARGE_REVIEWERS=("${REVIEWER_CASCADE[@]}")
  fi

  risk_reason="$(find_full_context_reason "$changed_paths" || true)"

  if [ -n "$risk_reason" ]; then
    REVIEWER_CASCADE=("${ROUTING_LARGE_REVIEWERS[@]}")
    ROUTING_DECISION="high-risk"
    [ -n "$ROUTING_HIGH_RISK_WITH" ] && CONDUCTOR_WITH="${TOUCHSTONE_CONDUCTOR_WITH:-$ROUTING_HIGH_RISK_WITH}"
    [ -n "$ROUTING_HIGH_RISK_PREFER" ] && CONDUCTOR_PREFER="${TOUCHSTONE_CONDUCTOR_PREFER:-$ROUTING_HIGH_RISK_PREFER}"
    [ -n "$ROUTING_HIGH_RISK_EFFORT" ] && CONDUCTOR_EFFORT="${TOUCHSTONE_CONDUCTOR_EFFORT:-$ROUTING_HIGH_RISK_EFFORT}"
    [ -n "$ROUTING_HIGH_RISK_TAGS" ] && CONDUCTOR_TAGS="${TOUCHSTONE_CONDUCTOR_TAGS:-$ROUTING_HIGH_RISK_TAGS}"
    echo "==> Review routing: high-risk diff ($risk_reason; $diff_lines lines) — with=${CONDUCTOR_WITH:-auto} prefer=$CONDUCTOR_PREFER effort=$CONDUCTOR_EFFORT"
  elif [ "$diff_lines" -le "$ROUTING_SMALL_MAX_DIFF_LINES" ] 2>/dev/null; then
    REVIEWER_CASCADE=("${ROUTING_SMALL_REVIEWERS[@]}")
    ROUTING_DECISION="small"
    # Apply 2.0 small-bucket overrides. Non-empty fields win; env still
    # trumps via the earlier cascade (TOUCHSTONE_CONDUCTOR_* set on the
    # command line or in the shell override the config-driven bucket).
    [ -n "$ROUTING_SMALL_WITH" ] && CONDUCTOR_WITH="${TOUCHSTONE_CONDUCTOR_WITH:-$ROUTING_SMALL_WITH}"
    [ -n "$ROUTING_SMALL_PREFER" ] && CONDUCTOR_PREFER="${TOUCHSTONE_CONDUCTOR_PREFER:-$ROUTING_SMALL_PREFER}"
    [ -n "$ROUTING_SMALL_EFFORT" ] && CONDUCTOR_EFFORT="${TOUCHSTONE_CONDUCTOR_EFFORT:-$ROUTING_SMALL_EFFORT}"
    [ -n "$ROUTING_SMALL_TAGS" ] && CONDUCTOR_TAGS="${TOUCHSTONE_CONDUCTOR_TAGS:-$ROUTING_SMALL_TAGS}"
    echo "==> Review routing: small diff ($diff_lines <= $ROUTING_SMALL_MAX_DIFF_LINES) — with=${CONDUCTOR_WITH:-auto} prefer=$CONDUCTOR_PREFER effort=$CONDUCTOR_EFFORT"
  else
    REVIEWER_CASCADE=("${ROUTING_LARGE_REVIEWERS[@]}")
    ROUTING_DECISION="large-low-risk"
    [ -n "$ROUTING_LARGE_WITH" ] && CONDUCTOR_WITH="${TOUCHSTONE_CONDUCTOR_WITH:-$ROUTING_LARGE_WITH}"
    [ -n "$ROUTING_LARGE_PREFER" ] && CONDUCTOR_PREFER="${TOUCHSTONE_CONDUCTOR_PREFER:-$ROUTING_LARGE_PREFER}"
    [ -n "$ROUTING_LARGE_EFFORT" ] && CONDUCTOR_EFFORT="${TOUCHSTONE_CONDUCTOR_EFFORT:-$ROUTING_LARGE_EFFORT}"
    [ -n "$ROUTING_LARGE_TAGS" ] && CONDUCTOR_TAGS="${TOUCHSTONE_CONDUCTOR_TAGS:-$ROUTING_LARGE_TAGS}"
    echo "==> Review routing: larger low-risk diff ($diff_lines > $ROUTING_SMALL_MAX_DIFF_LINES) — with=${CONDUCTOR_WITH:-auto} prefer=$CONDUCTOR_PREFER effort=$CONDUCTOR_EFFORT"
  fi
}

apply_high_scrutiny_policy() {
  local changed_paths="${1:-}"
  local mode match

  HIGH_SCRUTINY_TRIGGERED=false
  HIGH_SCRUTINY_REASON=""

  [ -n "$changed_paths" ] || return 0
  match="$(find_path_matching_context_patterns "$changed_paths" "$HIGH_SCRUTINY_PATHS" || true)"
  [ -n "$match" ] || return 0

  mode="$(printf '%s' "${HIGH_SCRUTINY_MODE:-peer}" | tr '[:upper:]' '[:lower:]')"
  case "$mode" in
    peer | council) ;;
    off | none | false | disabled)
      return 0
      ;;
    *)
      echo "WARNING: Invalid review.high_scrutiny_mode='$HIGH_SCRUTINY_MODE' — using peer." >&2
      mode="peer"
      ;;
  esac

  HIGH_SCRUTINY_TRIGGERED=true
  HIGH_SCRUTINY_MODE="$mode"
  HIGH_SCRUTINY_REASON="configured high-scrutiny path $match"

  if [ -z "${CODEX_REVIEW_ASSIST+x}" ]; then
    ASSIST_ENABLED=true
  fi

  echo "==> High-scrutiny review: $HIGH_SCRUTINY_REASON — ${HIGH_SCRUTINY_MODE} second opinion enabled"
}

run_reviewer() {
  "reviewer_${ACTIVE_REVIEWER}_exec" "$1"
}

reviewer_label_for() {
  case "$1" in
    conductor) printf 'Conductor' ;;
    *) printf '%s' "$1" ;;
  esac
}

reviewer_label() {
  reviewer_label_for "$ACTIVE_REVIEWER"
}

# --------------------------------------------------------------------------
# Timeout and error handling
# --------------------------------------------------------------------------

REVIEW_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-output.XXXXXX")"
ASSIST_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-assist-output.XXXXXX")"
REVIEW_STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-stderr.XXXXXX")"
REVIEW_CONDUCTOR_LOG_FILE="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-conductor.XXXXXX")"
PEER_CONDUCTOR_LOG_FILE=""
REVIEW_LOCK_DIR=""
REVIEW_LOCK_ACQUIRED=false
REVIEW_LOCK_TOKEN=""

cleanup_review_process() {
  rm -f "$REVIEW_OUTPUT_FILE" "$ASSIST_OUTPUT_FILE" "$REVIEW_STDERR_FILE" "$REVIEW_CONDUCTOR_LOG_FILE"
  rm -f \
    "${SCOPED_LARGE_DIFF_FILE:-}" \
    "${SCOPED_LARGE_DIFF_INCLUDED_PATHS_FILE:-}" \
    "${SCOPED_LARGE_DIFF_EXCLUDED_PATHS_FILE:-}" \
    2>/dev/null || true
  if [ "$REVIEW_LOCK_ACQUIRED" = true ] && [ -n "$REVIEW_LOCK_DIR" ]; then
    if [ "$(review_lock_metadata_value token 2>/dev/null || true)" = "$REVIEW_LOCK_TOKEN" ]; then
      rm -rf "$REVIEW_LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

trap cleanup_review_process EXIT

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

# run_reviewer_with_timeout TIMEOUT_SECS
#   Runs the reviewer, captures output to REVIEW_OUTPUT_FILE, returns exit code.
#   Exit 124 = timeout. Works correctly with subshells (no $() capture needed).
run_reviewer_with_timeout() {
  local timeout_secs="$1"
  local prompt="${2:-$REVIEW_PROMPT}"
  local output_file="${3:-$REVIEW_OUTPUT_FILE}"

  # Capture stderr separately — conductor emits its route-log there, which
  # we want to surface in the transcript. Pre-2.0 reviewers wrote noise to
  # stderr, hence the historical /dev/null redirect; capturing instead is
  # safe because non-[conductor] lines are filtered before display.
  : >"$REVIEW_STDERR_FILE"
  if [ "${ACTIVE_REVIEWER:-}" = "conductor" ] && [ -n "${REVIEW_CONDUCTOR_LOG_FILE:-}" ]; then
    : >"$REVIEW_CONDUCTOR_LOG_FILE"
  fi

  # No timeout: run directly
  if [ "$timeout_secs" -le 0 ] 2>/dev/null; then
    run_reviewer "$prompt" >"$output_file" 2>>"$REVIEW_STDERR_FILE"
    return $?
  fi

  # Run reviewer in background, kill if it exceeds timeout.
  (
    run_reviewer "$prompt" >"$output_file" 2>>"$REVIEW_STDERR_FILE" &
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

  # Map the raw reason to a specific fail-open taxonomy code so the audit
  # log and console output are unambiguous about *why* the safety net opened.
  local fail_open_code
  case "$reason" in
    timeout*) fail_open_code="FAIL_OPEN_TIMEOUT" ;;
    "malformed sentinel") fail_open_code="FAIL_OPEN_PARSE_ERROR" ;;
    "provider unavailable:"*) fail_open_code="FAIL_OPEN_PROVIDER_UNAVAILABLE" ;;
    *) fail_open_code="FAIL_OPEN_REVIEWER_ERROR" ;;
  esac

  if [ "$ON_ERROR" = "fail-closed" ]; then
    echo "==> ERROR ($reason) — blocking push (on_error=fail-closed)." >&2
    # Logged as a skip even though the push is blocked, because the
    # review still failed to produce a verdict — the audit cares about
    # "did the safety net actually run?" not "did the push proceed?".
    log_skip_event "$fail_open_code" "fail-closed:${reason}"
    exit 1
  else
    # Explicit stderr notice — the safety boundary being absent must never
    # be invisible ("No silent failures" principle). The [fail-open:<code>]
    # prefix is machine-parseable for log aggregators.
    echo "[fail-open:${fail_open_code}] ${reason} — AI review bypassed, push proceeds" >&2
    if [ "$reason" = "malformed sentinel" ]; then
      echo "[fail-open:${fail_open_code}] missing sentinel — review verdict is untrustworthy; push allowed by policy" >&2
    fi
    echo "==> ERROR ($reason) — not blocking push (on_error=fail-open)."
    echo "    Set on_error = \"fail-closed\" in ${CONFIG_DISPLAY_NAME:-.touchstone-review.toml} to block on errors."
    log_skip_event "$fail_open_code" "fail-open:${reason}"
    exit 0
  fi
}

review_lock_metadata_value() {
  local key="$1"
  local file="${2:-$REVIEW_LOCK_DIR/metadata}"

  [ -f "$file" ] || return 0
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$file" 2>/dev/null || true
}

review_lock_pid_is_alive() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  case "$pid" in
    *[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null
}

review_lock_branch_name() {
  local branch="${CODEX_REVIEW_BRANCH_NAME:-}"
  if [ -z "$branch" ]; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  fi
  printf '%s' "$branch"
}

write_review_lock_metadata() {
  local metadata_file="$REVIEW_LOCK_DIR/metadata"

  {
    printf 'token=%s\n' "$REVIEW_LOCK_TOKEN"
    printf 'pid=%s\n' "$$"
    printf 'started_at_epoch=%s\n' "$(date +%s)"
    printf 'branch=%s\n' "$(review_lock_branch_name)"
    printf 'base=%s\n' "$BASE"
    printf 'merge_base=%s\n' "$MERGE_BASE"
    printf 'head=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'cwd=%s\n' "$(pwd -P)"
    printf 'command=%s\n' "$0"
  } >"$metadata_file" 2>/dev/null || true
}

acquire_review_lock() {
  local wait_seconds="${CODEX_REVIEW_LOCK_WAIT_SECONDS:-600}"
  local stale_seconds="${CODEX_REVIEW_LOCK_STALE_SECONDS:-7200}"
  local start_epoch now_epoch elapsed lock_pid lock_started lock_age announced=false

  if is_truthy "${CODEX_REVIEW_DISABLE_LOCK:-false}"; then
    return 0
  fi

  REVIEW_LOCK_DIR="$(git rev-parse --git-path touchstone/codex-review.lock)"
  mkdir -p "$(dirname "$REVIEW_LOCK_DIR")" 2>/dev/null || true
  start_epoch="$(date +%s)"
  REVIEW_LOCK_TOKEN="$$:$start_epoch"

  while ! mkdir "$REVIEW_LOCK_DIR" 2>/dev/null; do
    now_epoch="$(date +%s)"
    lock_pid="$(review_lock_metadata_value pid)"
    lock_started="$(review_lock_metadata_value started_at_epoch)"

    if ! review_lock_pid_is_alive "$lock_pid"; then
      echo "==> Removing stale review lock at $REVIEW_LOCK_DIR (pid ${lock_pid:-unknown} is not running)."
      rm -rf "$REVIEW_LOCK_DIR" 2>/dev/null || true
      continue
    fi

    if [ -n "$lock_started" ] && [ "$lock_started" -le "$now_epoch" ] 2>/dev/null; then
      lock_age=$((now_epoch - lock_started))
      if [ "$stale_seconds" -ge 0 ] 2>/dev/null && [ "$lock_age" -gt "$stale_seconds" ]; then
        echo "==> Removing stale review lock at $REVIEW_LOCK_DIR (age ${lock_age}s > ${stale_seconds}s)."
        rm -rf "$REVIEW_LOCK_DIR" 2>/dev/null || true
        continue
      fi
    fi

    if [ "$announced" = false ]; then
      echo "==> Another Touchstone review gate is already running for this checkout; waiting for the lock."
      echo "    lock: $REVIEW_LOCK_DIR"
      echo "    pid:  ${lock_pid:-unknown}"
      echo "    head: $(review_lock_metadata_value head)"
      announced=true
    fi

    elapsed=$((now_epoch - start_epoch))
    if [ "$wait_seconds" -ge 0 ] 2>/dev/null && [ "$elapsed" -ge "$wait_seconds" ]; then
      handle_error "review lock busy"
    fi
    sleep 2
  done

  REVIEW_LOCK_ACQUIRED=true
  write_review_lock_metadata
}

# --------------------------------------------------------------------------
# Pre-flight checks
# --------------------------------------------------------------------------

# Feature-branch pushes should stay fast. Manual invocations and direct pushes
# to the default branch still run the review.
if is_truthy "${CODEX_REVIEW_IN_PROGRESS:-false}"; then
  echo "==> Review already in progress — skipping nested review."
  log_skip_event other nested-review-in-progress
  exit 0
fi

if should_skip_pre_push_review; then
  log_skip_event other "feature-branch-push:${PRE_COMMIT_REMOTE_BRANCH:-unknown}"
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
      echo "==> Conductor review skipped — first push on a fresh scaffold (HEAD is the initial commit)."
      log_skip_event other fresh-scaffold-first-push
      exit 0
    fi
  fi
fi

if ! is_truthy "$REVIEW_ENABLED"; then
  echo "==> AI review disabled by ${CONFIG_DISPLAY_NAME:-.touchstone-review.toml} — skipping review."
  # Distinguish "user set CODEX_REVIEW_ENABLED=false in their env" from
  # "the project's review config has enabled=false" — the env var
  # always wins, so its presence is the signal.
  if [ -n "${CODEX_REVIEW_ENABLED:-}" ] && ! is_truthy "${CODEX_REVIEW_ENABLED}"; then
    log_skip_event review-disabled-by-user "CODEX_REVIEW_ENABLED=${CODEX_REVIEW_ENABLED}"
  else
    log_skip_event config-disabled "review.enabled=false"
  fi
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
  log_skip_event other "merge-base-missing:${BASE}"
  exit 0
fi

# Skip if no changes vs base.
if git diff --quiet "$MERGE_BASE"..HEAD; then
  echo "==> No changes vs $BASE — skipping review."
  log_skip_event other "no-changes-vs:${BASE}"
  exit 0
fi

acquire_review_lock

DETERMINISTIC_PREFLIGHT_RESULT="not-run"

run_deterministic_preflight() {
  if ! is_truthy "$PREFLIGHT_REQUIRED"; then
    echo "==> Preflight disabled by [review].preflight_required=false."
    DETERMINISTIC_PREFLIGHT_RESULT="skipped: disabled by config"
    return 0
  fi
  if is_truthy "${TOUCHSTONE_NO_PREFLIGHT:-false}"; then
    echo "==> Skipping preflight because TOUCHSTONE_NO_PREFLIGHT=1."
    DETERMINISTIC_PREFLIGHT_RESULT="skipped: TOUCHSTONE_NO_PREFLIGHT=1"
    return 0
  fi
  if is_truthy "${TOUCHSTONE_PREFLIGHT_ALREADY_RAN:-false}"; then
    echo "==> Skipping preflight because this review was invoked after a clean preflight."
    DETERMINISTIC_PREFLIGHT_RESULT="passed before this review"
    return 0
  fi
  if ! declare -F touchstone_preflight_main >/dev/null 2>&1; then
    echo "==> Preflight helper not found at $PREFLIGHT_SCRIPT — skipping preflight."
    DETERMINISTIC_PREFLIGHT_RESULT="skipped: helper missing"
    return 0
  fi

  echo "==> Running deterministic preflight before AI review (diff vs $BASE) ..."
  if touchstone_preflight_main_sanitized --diff "$BASE" "$REPO_ROOT"; then
    DETERMINISTIC_PREFLIGHT_RESULT="passed before this review"
    return 0
  fi

  echo "ERROR: Deterministic preflight failed; refusing to spend provider tokens on review." >&2
  echo "       Fix the preflight failure or set TOUCHSTONE_NO_PREFLIGHT=1 for an emergency bypass." >&2
  return 1
}

run_deterministic_preflight

WORKTREE_DIRTY_BEFORE_REVIEW=false
if [ -n "$(git status --porcelain)" ]; then
  WORKTREE_DIRTY_BEFORE_REVIEW=true
fi

ROUTING_DIFF_LINE_COUNT="$(git diff "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
PROMPT_CONTEXT_CHANGED_PATHS="$(git diff --name-only "$MERGE_BASE"..HEAD 2>/dev/null || true)"
apply_high_scrutiny_policy "$PROMPT_CONTEXT_CHANGED_PATHS"
apply_review_routing "$ROUTING_DIFF_LINE_COUNT" "$PROMPT_CONTEXT_CHANGED_PATHS"

# Resolve which reviewer to use from the cascade.
if ! resolve_reviewer; then
  unavailable_code="FAIL_OPEN_DEPENDENCY_MISSING"
  unavailable_reason="dependency-missing"
  unavailable_message="conductor CLI not found on PATH"
  if reviewer_conductor_available; then
    unavailable_code="FAIL_OPEN_PROVIDER_UNAVAILABLE"
    unavailable_reason="provider-unavailable"
    unavailable_message="conductor installed but no provider configured"
  fi

  if [ -n "${TOUCHSTONE_REVIEWER:-}" ]; then
    echo "ERROR: TOUCHSTONE_REVIEWER=$TOUCHSTONE_REVIEWER but that reviewer is not available:" >&2
    printf '%b' "$REVIEWER_STATUS" >&2
    echo "  Set TOUCHSTONE_CONDUCTOR_WITH=<provider> to pin an underlying provider," >&2
    echo "  or unset TOUCHSTONE_REVIEWER to let Conductor auto-route." >&2
    exit 1
  fi
  if [ "$ON_ERROR" = "fail-closed" ]; then
    echo "==> No reviewer available — AI review cannot run."
  else
    echo "==> No reviewer available — push will proceed without AI review."
  fi
  printf '%b' "$REVIEWER_STATUS"
  echo "    Touchstone 2.0 routes every review through the \`conductor\` CLI."
  echo "    Fix above, then re-run \`git push\` to trigger review again."
  echo "==> Review status: provider/infrastructure unavailable; findings=0; exit_reason=$unavailable_reason"
  if [ -n "${CODEX_REVIEW_SUMMARY_FILE:-}" ]; then
    printf '{"reviewer":"Conductor","provider":"unknown","model":"unknown","peer_provider":"none","route":"%s","mode":"%s","context":"%s","prefer":"%s","effort":"%s","files":%d,"diff_lines":%d,"iterations":0,"fix_commits":0,"peer_assists":0,"high_scrutiny_triggered":%s,"high_scrutiny_mode":"%s","high_scrutiny_reason":"","findings":0,"fallback_attempted":false,"fallback_primary_provider":"","fallback_retry_provider":"","fallback_excluded_providers":"","fallback_reason":"","diagnostics_file":"","diagnostics_events":0,"exit_reason":"%s","elapsed_seconds":0}\n' \
      "$ROUTING_DECISION" "$REVIEW_MODE" "$PROMPT_CONTEXT_DECISION" "${CONDUCTOR_PREFER:-auto}" "${CONDUCTOR_EFFORT:-default}" \
      "$(git diff --name-only "$MERGE_BASE"..HEAD 2>/dev/null | wc -l | tr -d ' ')" "$ROUTING_DIFF_LINE_COUNT" \
      "${HIGH_SCRUTINY_TRIGGERED:-false}" "${HIGH_SCRUTINY_MODE:-peer}" "$unavailable_reason" \
      >"$CODEX_REVIEW_SUMMARY_FILE" 2>/dev/null || true
  fi
  # Distinguish: CLI not on PATH vs CLI present but no provider configured.
  # Emit a visible [fail-open:<code>] line so the absent safety boundary
  # is not silent ("No silent failures" principle).
  if [ "$ON_ERROR" = "fail-closed" ]; then
    echo "[fail-closed:${unavailable_code}] ${unavailable_message} — AI review unavailable, push blocked" >&2
    echo "==> ERROR ($unavailable_reason) — blocking push (on_error=fail-closed)." >&2
    log_skip_event "$unavailable_code" "fail-closed:${unavailable_reason}"
    exit 1
  fi
  echo "[fail-open:${unavailable_code}] ${unavailable_message} — AI review bypassed, push proceeds" >&2
  log_skip_event "$unavailable_code" "fail-open:${unavailable_reason}"
  exit 0
fi
REVIEWER_LABEL="$(reviewer_label)"
select_prompt_context_mode "$ROUTING_DIFF_LINE_COUNT" "$PROMPT_CONTEXT_CHANGED_PATHS"
prepare_large_diff_scoped_review "$ROUTING_DIFF_LINE_COUNT" || true
echo "==> Using reviewer: $REVIEWER_LABEL"
if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
  echo "==> Prompt context: scoped large-diff review (${SCOPED_LARGE_DIFF_LINES}/${SCOPED_LARGE_DIFF_ORIGINAL_LINES} lines reviewed)"
else
  echo "==> Prompt context: $PROMPT_CONTEXT_DECISION ($PROMPT_CONTEXT_REASON)"
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

$(build_prompt_context_instructions)

Do NOT flag: formatting, style, naming, missing docstrings, speculative refactors, "you could consider" observations without a concrete bug.

## Goal and context

The following commit messages describe the intent and strategy behind these changes.
Use them to understand *why* the code was changed, not just *what* changed.
Do not flag intentional design decisions that are explained in the commit messages.

$(git log --reverse --format='### %s%n%n%b' "$MERGE_BASE"..HEAD 2>/dev/null | sed '/^$/N;/^\n$/d')

$(if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
  printf 'Examine only the embedded scoped project-owned diff below. Do not review excluded Touchstone-managed sync files.\n'
else
  printf 'Examine the diff vs %s using your tools.\n' "$BASE"
fi)
$(if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
  printf '\n## Diff (scoped project-owned slice)\n\n```diff\n'
  cat "$SCOPED_LARGE_DIFF_FILE"
  printf '```\n'
elif [ "$REVIEW_MODE" = "diff-only" ]; then
  printf '\n## Diff (included because mode=diff-only restricts tool access)\n\n```diff\n'
  git diff "$MERGE_BASE"..HEAD 2>/dev/null
  printf '```\n'
fi)

## Auto-fix policy

$AUTOFIX_POLICY
$(build_preflight_review_policy)
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
This is a strict gate contract: the very last physical line of the entire response must be exactly one sentinel token and nothing else.
PROMPT_EOF

# --------------------------------------------------------------------------
# Inject sentinel-cycle journal context into the reviewer prompt (if any)
# --------------------------------------------------------------------------
SENTINEL_JOURNAL_CONTEXT="$(build_sentinel_journal_context)"
if [ -n "$SENTINEL_JOURNAL_CONTEXT" ]; then
  REVIEW_PROMPT="${SENTINEL_JOURNAL_CONTEXT}

${REVIEW_PROMPT}"
  echo "==> Sentinel cycle journal injected into reviewer context."
fi

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
  # Use the `${VAR:-}` default-to-empty form for every variable: with
  # `set -u` active, an unset reference would abort the subshell partway
  # through and silently truncate the cache-key input. (Pre-2.0 the
  # function half-broke this way — only the first few fields contributed
  # to the hash, so adding new fields had no effect on cache invalidation.
  # Keep this defensive on every line.)
  {
    printf 'touchstone-codex-review-cache-v3\n'
    printf 'reviewer=%s\n' "${ACTIVE_REVIEWER:-}"
    printf 'review_mode=%s\n' "${REVIEW_MODE:-}"
    printf 'review_route=%s\n' "${ROUTING_DECISION:-}"
    printf 'prompt_context_mode=%s\n' "${PROMPT_CONTEXT_DECISION:-}"
    printf 'prompt_context_reason=%s\n' "${PROMPT_CONTEXT_REASON:-}"
    printf 'review_enabled=%s\n' "${REVIEW_ENABLED:-}"
    printf 'local_reviewer_command=%s\n' "${LOCAL_REVIEWER_COMMAND:-}"
    printf 'base=%s\n' "${BASE:-}"
    printf 'merge_base=%s\n' "${MERGE_BASE:-}"
    printf 'worktree_dirty_before_review=%s\n' "${WORKTREE_DIRTY_BEFORE_REVIEW:-}"
    printf 'assist_enabled=%s\n' "${ASSIST_ENABLED:-}"
    printf 'assist_timeout=%s\n' "${ASSIST_TIMEOUT:-}"
    printf 'assist_max_rounds=%s\n' "${ASSIST_MAX_ROUNDS:-}"
    printf 'assist_helpers=%s\n' "${ASSIST_HELPERS[*]:-}"
    printf 'high_scrutiny_triggered=%s\n' "${HIGH_SCRUTINY_TRIGGERED:-false}"
    printf 'high_scrutiny_mode=%s\n' "${HIGH_SCRUTINY_MODE:-peer}"
    printf 'high_scrutiny_reason=%s\n' "${HIGH_SCRUTINY_REASON:-}"
    # Conductor knobs (CLI-effective values, post env+config resolution).
    # Without these, a review at prefer=cheapest/effort=minimal would
    # silently satisfy a later push expecting prefer=best/effort=high
    # because the diff hash matches.
    printf 'conductor_with=%s\n' "${CONDUCTOR_WITH:-}"
    printf 'conductor_preflight_review_provider=%s\n' "${CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER:-}"
    printf 'conductor_preflight_fix_provider=%s\n' "${CONDUCTOR_PREFLIGHT_FIX_PROVIDER:-}"
    printf 'conductor_prefer=%s\n' "${CONDUCTOR_PREFER:-}"
    printf 'conductor_effort=%s\n' "${CONDUCTOR_EFFORT:-}"
    printf 'conductor_tags=%s\n' "${CONDUCTOR_TAGS:-}"
    printf 'conductor_exclude=%s\n' "${CONDUCTOR_EXCLUDE:-}"
    printf '\n-- prompt --\n%s\n' "${REVIEW_PROMPT:-}"
    if [ "${PROMPT_CONTEXT_DECISION:-full}" = "full" ]; then
      append_cache_file "AGENTS.md" "${REPO_ROOT:-}/AGENTS.md"
      append_cache_file "CLAUDE.md" "${REPO_ROOT:-}/CLAUDE.md"
    else
      printf '\n-- AGENTS.md --\n<omitted: prompt_context_mode=%s>\n' "${PROMPT_CONTEXT_DECISION:-}"
      printf '\n-- CLAUDE.md --\n<omitted: prompt_context_mode=%s>\n' "${PROMPT_CONTEXT_DECISION:-}"
    fi
    append_cache_file "review-config" "${CONFIG_FILE:-}"
    append_cache_file "conductor-review.sh" "$0"
    if [ -n "${REVIEW_CONTEXT_FILE:-}" ]; then
      append_cache_file "codex-review-context" "$REVIEW_CONTEXT_FILE"
    fi
    printf '\n-- branch diff --\n'
    git diff --binary "${MERGE_BASE:-HEAD}"..HEAD
  } | hash_stdin
}

clean_review_cache_dir() {
  git rev-parse --git-path touchstone/codex-review-clean
}

clean_review_cache_file() {
  local key="$1"
  printf '%s/%s.clean' "$(clean_review_cache_dir)" "$key"
}

review_clean_marker_branch() {
  local branch="${CODEX_REVIEW_BRANCH_NAME:-}"
  if [ -z "$branch" ]; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  fi
  [ "$branch" != "HEAD" ] || branch=""
  printf '%s' "$branch"
}

review_clean_marker_key() {
  local branch="$1"
  printf '%s' "$branch" | sed 's/[^A-Za-z0-9._-]/_/g'
}

review_clean_marker_dir() {
  git rev-parse --git-path touchstone/reviewer-clean
}

write_clean_review_marker() {
  local line_count="$1"
  local branch marker_dir marker_file

  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 0

  marker_dir="$(review_clean_marker_dir)"
  marker_file="$marker_dir/$(review_clean_marker_key "$branch").clean"

  mkdir -p "$marker_dir" 2>/dev/null || return 0
  {
    printf 'result=CODEX_REVIEW_CLEAN\n'
    printf 'branch=%s\n' "$branch"
    printf 'base=%s\n' "$BASE"
    printf 'merge_base=%s\n' "$MERGE_BASE"
    printf 'head=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'diff_lines=%s\n' "$line_count"
    printf 'reviewed_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  } >"$marker_file" 2>/dev/null || true
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
  } >"$cache_file" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Issue #163: persisted prior findings.
#
# When a review iteration ends in CODEX_REVIEW_BLOCKED, persist the findings
# list under .git/touchstone/reviewer-findings/<branch>.findings so the next
# push attempt on the same branch can prepend a "verification checkpoint" to
# the reviewer's prompt: "verify each prior finding is closed; report NEW
# blockers only." This converts the second review from a blind full audit
# into a targeted delta scan.
#
# The state is operational (lives in .git/, never committed). Validation
# rules: prior_head must be an ancestor of current HEAD (no force-push, no
# branch reset). Any unreadable / unparseable / mismatched state is treated
# as blank slate so the fail-open invariant on the reviewer is preserved.
# --------------------------------------------------------------------------

review_findings_dir() {
  git rev-parse --git-path touchstone/reviewer-findings
}

review_findings_file() {
  local branch="$1"
  printf '%s/%s.findings' "$(review_findings_dir)" "$(review_clean_marker_key "$branch")"
}

review_findings_history_dir() {
  git rev-parse --git-path touchstone/reviewer-findings-history
}

review_findings_history_file() {
  local branch="$1"
  printf '%s/%s.jsonl' "$(review_findings_history_dir)" "$(review_clean_marker_key "$branch")"
}

# Strip trailing whitespace from each line and drop empty trailing lines.
# Findings text comes from the reviewer's stdout, which can include shell
# color codes or stray indentation; the gate only persists lines that start
# with `- ` (the BLOCKED contract).
extract_findings_block() {
  printf '%s\n' "$1" | awk '/^- / { print } /^$/ { if (have_finding) exit }'
}

write_review_findings() {
  local findings_text="$1"
  local branch findings_dir findings_file findings_block

  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 0

  findings_block="$(extract_findings_block "$findings_text")"
  if [ -z "$findings_block" ]; then
    # BLOCKED with no parseable bullet lines: do not persist a stale or
    # empty-looking checkpoint that would confuse the next iteration.
    clear_review_findings
    return 0
  fi

  findings_dir="$(review_findings_dir)"
  findings_file="$(review_findings_file "$branch")"

  mkdir -p "$findings_dir" 2>/dev/null || return 0
  {
    printf 'result=CODEX_REVIEW_BLOCKED\n'
    printf 'branch=%s\n' "$branch"
    printf 'base=%s\n' "$BASE"
    printf 'merge_base=%s\n' "$MERGE_BASE"
    printf 'head=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'reviewed_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'findings_count=%s\n' "$(printf '%s\n' "$findings_block" | grep -c '^- ' || true)"
    printf -- '--- findings start ---\n'
    printf '%s\n' "$findings_block"
    printf -- '--- findings end ---\n'
  } >"$findings_file" 2>/dev/null || true
}

clear_review_findings() {
  local branch findings_file
  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 0
  findings_file="$(review_findings_file "$branch")"
  [ -f "$findings_file" ] || return 0
  rm -f "$findings_file" 2>/dev/null || true
}

json_escape() {
  awk '
    BEGIN {
      tab = sprintf("%c", 9)
      cr = sprintf("%c", 13)
    }
    {
      gsub(/\\/, "\\\\")
      gsub(/"/, "\\\"")
      gsub(tab, "\\t")
      gsub(cr, "\\r")
      gsub(/[[:cntrl:]]/, "?")
      if (NR > 1) {
        printf "\\n"
      }
      printf "%s", $0
    }
  '
}

review_diagnostics_dir() {
  git rev-parse --git-path touchstone/reviewer-diagnostics
}

review_diagnostics_path() {
  local key stamp diagnostics_dir branch
  if [ -n "${REVIEW_DIAGNOSTICS_FILE:-}" ]; then
    printf '%s' "$REVIEW_DIAGNOSTICS_FILE"
    return 0
  fi
  if [ -n "${CODEX_REVIEW_DIAGNOSTICS_FILE:-}" ]; then
    REVIEW_DIAGNOSTICS_FILE="$CODEX_REVIEW_DIAGNOSTICS_FILE"
    printf '%s' "$REVIEW_DIAGNOSTICS_FILE"
    return 0
  fi

  if [ -n "${CODEX_REVIEW_PR_NUMBER:-}" ]; then
    key="pr-${CODEX_REVIEW_PR_NUMBER}"
  else
    branch="$(review_clean_marker_branch)"
    if [ -n "$branch" ]; then
      key="branch-$(review_clean_marker_key "$branch")"
    else
      key="manual"
    fi
  fi
  key="$(review_clean_marker_key "$key")"
  stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
  diagnostics_dir="$(review_diagnostics_dir)"
  REVIEW_DIAGNOSTICS_FILE="$diagnostics_dir/${key}-${stamp}-$$.jsonl"
  printf '%s' "$REVIEW_DIAGNOSTICS_FILE"
}

review_diagnostics_tail_json() {
  local file="$1"
  local lines="${2:-80}"
  [ -f "$file" ] || return 0
  tail -n "$lines" "$file" 2>/dev/null | json_escape
}

review_diagnostics_event_count() {
  local path="$1"
  [ -f "$path" ] || {
    printf '0'
    return 0
  }
  wc -l <"$path" 2>/dev/null | tr -d ' '
}

append_review_diagnostic_event() {
  local event="$1"
  local reason="$2"
  local output_file="${3:-$REVIEW_OUTPUT_FILE}"
  local path path_dir timestamp branch head provider model iteration
  local stdout_tail stderr_tail conductor_log_tail

  path="$(review_diagnostics_path 2>/dev/null || true)"
  [ -n "$path" ] || return 0
  [ "$path" != "/dev/null" ] || return 0
  REVIEW_DIAGNOSTICS_FILE="$path"
  path_dir="$(dirname "$path")"
  mkdir -p "$path_dir" 2>/dev/null || return 0

  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || branch="unknown"
  head="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  provider="$(parse_primary_provider)"
  [ -n "$provider" ] || provider="$(conductor_effective_with_for_phase review)"
  [ -n "$provider" ] || provider="unknown"
  model="$(parse_primary_model)"
  [ -n "$model" ] || model="unknown"
  iteration="${iter:-0}"
  case "$iteration" in
    '' | *[!0-9]*) iteration=0 ;;
  esac

  stdout_tail="$(review_diagnostics_tail_json "$output_file" 120)"
  stderr_tail="$(review_diagnostics_tail_json "$REVIEW_STDERR_FILE" 120)"
  conductor_log_tail="$(review_diagnostics_tail_json "$REVIEW_CONDUCTOR_LOG_FILE" 120)"

  if printf '{"schema":"touchstone.review.diagnostics.v1","timestamp":"%s","event":"%s","reason":"%s","reviewer":"%s","provider":"%s","model":"%s","branch":"%s","base":"%s","merge_base":"%s","head":"%s","pr_number":"%s","iteration":%d,"mode":"%s","route":"%s","prefer":"%s","effort":"%s","stdout_tail":"%s","stderr_tail":"%s","conductor_log_tail":"%s"}\n' \
    "$timestamp" \
    "$(printf '%s' "$event" | json_escape)" \
    "$(printf '%s' "$reason" | json_escape)" \
    "$(printf '%s' "${REVIEWER_LABEL:-unknown}" | json_escape)" \
    "$(printf '%s' "$provider" | json_escape)" \
    "$(printf '%s' "$model" | json_escape)" \
    "$(printf '%s' "$branch" | json_escape)" \
    "$(printf '%s' "${BASE:-}" | json_escape)" \
    "$(printf '%s' "${MERGE_BASE:-}" | json_escape)" \
    "$(printf '%s' "$head" | json_escape)" \
    "$(printf '%s' "${CODEX_REVIEW_PR_NUMBER:-}" | json_escape)" \
    "$iteration" \
    "$(printf '%s' "${REVIEW_MODE:-}" | json_escape)" \
    "$(printf '%s' "${ROUTING_DECISION:-}" | json_escape)" \
    "$(printf '%s' "${CONDUCTOR_PREFER:-auto}" | json_escape)" \
    "$(printf '%s' "${CONDUCTOR_EFFORT:-default}" | json_escape)" \
    "$stdout_tail" "$stderr_tail" "$conductor_log_tail" \
    >>"$path" 2>/dev/null; then
    if [ "${REVIEW_DIAGNOSTICS_NOTICE_PRINTED:-false}" = false ]; then
      echo "==> Review diagnostics: $path"
      REVIEW_DIAGNOSTICS_NOTICE_PRINTED=true
    fi
  else
    echo "WARNING: unable to persist review diagnostics at $path" >&2
  fi
}

review_history_path() {
  local branch
  if [ -n "${CODEX_REVIEW_FINDINGS_HISTORY_FILE:-}" ]; then
    printf '%s' "$CODEX_REVIEW_FINDINGS_HISTORY_FILE"
    return 0
  fi
  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 1
  review_findings_history_file "$branch"
}

review_history_last_head() {
  local history_file="$1"
  [ -f "$history_file" ] || return 0
  awk -F'"head":"' '
    /"schema":"touchstone.review.findings_history.v1"/ && NF > 1 {
      split($2, parts, "\"")
      head = parts[1]
    }
    END { if (head != "") print head }
  ' "$history_file" 2>/dev/null
}

review_history_commits_since_prior() {
  local prior_head="$1"
  local current_head="$2"
  [ -n "$prior_head" ] || return 0
  [ -n "$current_head" ] || return 0
  [ "$prior_head" != "$current_head" ] || return 0
  git rev-parse --verify --quiet "$prior_head^{commit}" >/dev/null 2>&1 || return 0
  git merge-base --is-ancestor "$prior_head" "$current_head" 2>/dev/null || return 0
  git log --format='%h %s' "$prior_head..$current_head" 2>/dev/null \
    | awk 'NF { if (out != "") out = out "; "; out = out $0 } END { print out }'
}

extract_review_body_without_sentinel() {
  printf '%s\n' "$1" | awk '
    /^[[:space:]]*CODEX_REVIEW_(CLEAN|FIXED|BLOCKED)[[:space:]]*$/ { next }
    { print }
  ' | sed '/^[[:space:]]*$/d'
}

append_findings_history_event() {
  local result="$1"
  local iteration="$2"
  local output="${3:-}"
  local auto_fixed_count="${4:-0}"
  local branch history_file history_dir timestamp head prior_head commits findings_block findings_count
  local esc_branch esc_base esc_merge_base esc_head esc_commits esc_findings esc_mode

  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 0
  if ! history_file="$(review_history_path)"; then
    return 0
  fi
  history_dir="$(dirname "$history_file")"
  mkdir -p "$history_dir" 2>/dev/null || return 0

  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "")"
  head="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  prior_head="$(review_history_last_head "$history_file")"
  commits="$(review_history_commits_since_prior "$prior_head" "$head")"

  findings_block="$(extract_findings_block "$output")"
  if [ -z "$findings_block" ] && [ "$result" = "CODEX_REVIEW_FIXED" ]; then
    findings_block="$(extract_review_body_without_sentinel "$output")"
  fi
  findings_count="$(printf '%s\n' "$findings_block" | grep -c '^- ' || true)"
  if [ "$result" = "CODEX_REVIEW_FIXED" ] && [ "$auto_fixed_count" -eq 0 ] && [ -n "$findings_block" ]; then
    auto_fixed_count="$findings_count"
    [ "$auto_fixed_count" -gt 0 ] || auto_fixed_count=1
  fi

  esc_branch="$(printf '%s' "$branch" | json_escape)"
  esc_base="$(printf '%s' "$BASE" | json_escape)"
  esc_merge_base="$(printf '%s' "$MERGE_BASE" | json_escape)"
  esc_head="$(printf '%s' "$head" | json_escape)"
  esc_commits="$(printf '%s' "$commits" | json_escape)"
  esc_findings="$(printf '%s' "$findings_block" | json_escape)"
  esc_mode="$(printf '%s' "$REVIEW_MODE" | json_escape)"

  printf '{"schema":"touchstone.review.findings_history.v1","timestamp":"%s","branch":"%s","base":"%s","merge_base":"%s","head":"%s","iteration":%s,"result":"%s","mode":"%s","findings_count":%s,"auto_fixed_count":%s,"fix_commits":%s,"commits_since_prior":"%s","findings":"%s"}\n' \
    "$timestamp" "$esc_branch" "$esc_base" "$esc_merge_base" "$esc_head" "$iteration" "$result" "$esc_mode" \
    "${findings_count:-0}" "${auto_fixed_count:-0}" "$FIX_COMMITS" "$esc_commits" "$esc_findings" \
    >>"$history_file" 2>/dev/null || true
}

# Build a "verification checkpoint" prompt prefix when a prior BLOCKED review
# is recorded for this branch and the prior HEAD is a strict ancestor of the
# current HEAD. Returns empty on any error or staleness so callers can treat
# it as opt-in: a missing or invalid file simply produces no prefix and the
# reviewer runs as a normal fresh audit.
build_review_verification_checkpoint() {
  local branch findings_file prior_head prior_reviewed_at findings_block current_head

  branch="$(review_clean_marker_branch)"
  [ -n "$branch" ] || return 0
  findings_file="$(review_findings_file "$branch")"
  [ -f "$findings_file" ] || return 0

  prior_head="$(awk -F= '$1 == "head" { print $2; exit }' "$findings_file" 2>/dev/null)"
  prior_reviewed_at="$(awk -F= '$1 == "reviewed_at" { print $2; exit }' "$findings_file" 2>/dev/null)"
  [ -n "$prior_head" ] || return 0

  if ! git rev-parse --verify --quiet "$prior_head^{commit}" >/dev/null 2>&1; then
    # The recorded commit is not in this clone (force-push, gc, fresh
    # clone). Drop the stale file and treat as blank slate.
    clear_review_findings
    return 0
  fi

  current_head="$(git rev-parse HEAD 2>/dev/null)"
  if [ -z "$current_head" ]; then
    return 0
  fi

  # Allow same-HEAD (re-running on the same commit) and strict-descendant
  # (the operator amended or added commits). Reject everything else (the
  # branch was reset or rebased onto unrelated history → prior findings no
  # longer describe this code).
  if [ "$current_head" != "$prior_head" ] \
    && ! git merge-base --is-ancestor "$prior_head" "$current_head" 2>/dev/null; then
    clear_review_findings
    return 0
  fi

  findings_block="$(awk '
    /^--- findings start ---$/ { in_block = 1; next }
    /^--- findings end ---$/   { exit }
    in_block { print }
  ' "$findings_file" 2>/dev/null)"
  [ -n "$findings_block" ] || return 0

  cat <<CHECKPOINT_EOF
## Prior review findings — verify, do not restart

This branch was reviewed at HEAD ${prior_head} on ${prior_reviewed_at:-unknown}, and the reviewer flagged the following blockers:

${findings_block}

Directive for this iteration:

1. For each finding above, check whether the current diff (HEAD vs ${BASE}) closes it. If a finding is fully addressed, do not re-flag it.
2. After verifying prior findings, scan the commits since ${prior_head}..HEAD for any NEW blockers in the same weak-point classes (boundary checks, concurrency, fail-open semantics, version-contract bypasses, etc.). Group related new blockers together.
3. Do not start fresh: prior findings that are already addressed must not appear in your output. Findings that are still open should be re-listed verbatim so the operator can confirm they are still open.
4. If every prior finding is closed and you find no new blockers, emit CODEX_REVIEW_CLEAN.

CHECKPOINT_EOF
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
  done <<<"$UNSAFE_PATHS"

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
  done <<<"$changed"

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

$(build_prompt_context_instructions)

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
REVIEW_PHASE="review"
ASSIST_ROUNDS=0
BANNER_PRINTED=false
REVIEW_START_TIME="$(date +%s)"
REVIEW_FILES_INSPECTED="$(git diff --name-only "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
REVIEW_EXIT_REASON=""
REVIEW_FALLBACK_ATTEMPTED=false
REVIEW_FALLBACK_PRIMARY_PROVIDER=""
REVIEW_FALLBACK_RETRY_PROVIDER=""
REVIEW_FALLBACK_EXCLUDED_PROVIDERS=""
REVIEW_FALLBACK_REASON=""
FALLBACK_REVIEW_EXIT=0
REVIEW_DIAGNOSTICS_FILE=""
REVIEW_DIAGNOSTICS_NOTICE_PRINTED=false

# --------------------------------------------------------------------------
# Phase labels
# --------------------------------------------------------------------------

phase() {
  printf "  ${C_DIM}[%s] %s${C_RESET}\n" "$(date +%H:%M:%S)" "$1"
}

# Extract Conductor's route-log lines from REVIEW_STDERR_FILE and print
# them into the transcript. The log tells the user which provider was
# picked, how hard it thought, how long it took, and what it cost —
# the observability promise of the Conductor integration.
print_route_log() {
  [ -f "$REVIEW_STDERR_FILE" ] || return 0
  # Conductor's route-log lines all start with `[conductor]`; subsequent
  # wrapped lines (the cost/token summary) start with whitespace. Continue
  # printing while indented continuation lines follow; reset on any other
  # line. Tolerates conductor's varied wrap-line punctuation (· vs · vs .)
  # and any traceback/warning text on stderr (those reset the state).
  local log
  log="$(awk '/^\[conductor\]/ { emit=1; print; next } emit && /^[[:space:]]/ { print; next } { emit=0 }' "$REVIEW_STDERR_FILE")"
  [ -n "$log" ] || return 0
  # Indent to align with the other phase/banner lines.
  printf '%s\n' "$log" | while IFS= read -r line; do
    printf "  ${C_DIM}%s${C_RESET}\n" "$line"
  done
}

latest_conductor_session_log() {
  local session_dir="${CONDUCTOR_SESSION_DIR:-${HOME:-}/.cache/conductor/sessions}"
  [ -n "$session_dir" ] || return 0
  [ -d "$session_dir" ] || return 0
  ls -t "$session_dir"/*.ndjson 2>/dev/null | head -1
}

conductor_log_event_line() {
  local log_file="$1"
  local event="$2"
  [ -f "$log_file" ] || return 0
  awk -v event="$event" '
    index($0, "\"event\": \"" event "\"") || index($0, "\"event\":\"" event "\"") {
      line = $0
    }
    END {
      if (line != "") {
        print line
      }
    }
  ' "$log_file" 2>/dev/null || true
}

conductor_log_string_field() {
  local log_file="$1"
  local event="$2"
  local key="$3"
  local line
  line="$(conductor_log_event_line "$log_file" "$event")"
  [ -n "$line" ] || return 0
  printf '%s\n' "$line" \
    | sed -nE 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' \
    | head -1
}

conductor_stderr_success_provider() {
  local stderr_file="${1:-$REVIEW_STDERR_FILE}"
  [ -f "$stderr_file" ] || return 0
  awk '
    /review tried providers:/ { line = $0 }
    END {
      if (line == "") {
        exit
      }
      sub(/^.*review tried providers:[[:space:]]*/, "", line)
      n = split(line, parts, ",")
      for (i = 1; i <= n; i++) {
        part = parts[i]
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", part)
        if (part ~ /\(success\)/) {
          split(part, fields, /[[:space:]]+/)
          print fields[1]
          exit
        }
      }
    }
  ' "$stderr_file" 2>/dev/null || true
}

conductor_stderr_first_tried_provider() {
  local stderr_file="${1:-$REVIEW_STDERR_FILE}"
  [ -f "$stderr_file" ] || return 0
  awk '
    /review tried providers:/ { line = $0 }
    END {
      if (line == "") {
        exit
      }
      sub(/^.*review tried providers:[[:space:]]*/, "", line)
      split(line, parts, ",")
      part = parts[1]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", part)
      split(part, fields, /[[:space:]]+/)
      print fields[1]
    }
  ' "$stderr_file" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Peer review ([review.assist], v2.1) — second-opinion pass via Conductor.
# --------------------------------------------------------------------------

# Parse the provider Conductor picked for the most recent primary call.
# Reads the route-log line from REVIEW_STDERR_FILE. Returns the provider
# name on stdout, or empty if not found. Tolerates both real conductor's
# unicode arrow and ASCII-fallback shapes.
#
# shellcheck disable=SC2120  # $1 is intentionally optional with a default.
parse_primary_provider() {
  local provider
  provider="$(conductor_log_string_field "${REVIEW_CONDUCTOR_LOG_FILE:-}" provider_finished provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi
  provider="$(conductor_stderr_success_provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi
  provider="$(conductor_log_string_field "${REVIEW_CONDUCTOR_LOG_FILE:-}" provider_failed provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi
  provider="$(conductor_log_string_field "${REVIEW_CONDUCTOR_LOG_FILE:-}" provider_started provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi
  provider="$(conductor_stderr_first_tried_provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi

  local stderr_file="$REVIEW_STDERR_FILE"
  [ -f "$stderr_file" ] || {
    printf ''
    return
  }
  local line
  line="$(grep -m1 -E '(^\[conductor\]|Conductor).*(→|->)' "$stderr_file" 2>/dev/null || true)"
  [ -n "$line" ] || {
    printf ''
    return
  }
  # Extract the provider name following the arrow. Handles:
  #   [conductor] auto (...) → claude (tier: ...)
  #   [conductor] auto (...) -> claude (tier: ...)
  # `sed -nE` treats `(a|b)` as ERE alternation.
  printf '%s' "$line" | sed -nE 's/.*(→|-> ?)([a-zA-Z0-9_.-]+).*/\2/p' | head -1
}

parse_primary_model() {
  local model
  model="$(conductor_log_string_field "${REVIEW_CONDUCTOR_LOG_FILE:-}" provider_finished model)"
  if [ -n "$model" ]; then
    printf '%s' "$model"
    return
  fi

  local stderr_file="$REVIEW_STDERR_FILE"
  [ -f "$stderr_file" ] || {
    printf ''
    return
  }
  local line
  line="$(grep -m1 '^\[conductor\]' "$stderr_file" 2>/dev/null || true)"
  [ -n "$line" ] || {
    printf ''
    return
  }
  printf '%s' "$line" \
    | sed -nE 's/.*(model|model_id|model-id)[=:][[:space:]]*([a-zA-Z0-9_.:/@-]+).*/\2/p' \
    | head -1
}

parse_peer_provider() {
  local provider
  provider="$(conductor_log_string_field "${PEER_CONDUCTOR_LOG_FILE:-}" provider_finished provider)"
  if [ -n "$provider" ]; then
    printf '%s' "$provider"
    return
  fi
  if [ "${ASSIST_ROUNDS_DONE:-0}" -gt 0 ]; then
    printf 'unknown'
  else
    printf 'none'
  fi
}

conductor_invocation_label() {
  local conductor_path subcommand
  conductor_path="$(command -v conductor 2>/dev/null || printf 'conductor')"
  subcommand="$(conductor_subcommand_for_mode)"
  printf '%s %s' "$conductor_path" "$subcommand"
}

print_malformed_sentinel_diagnostics() {
  if [ "$ACTIVE_REVIEWER" != "conductor" ]; then
    return 0
  fi

  local selected_provider
  selected_provider="$(parse_primary_provider)"
  [ -n "$selected_provider" ] || selected_provider="$(conductor_effective_with_for_phase review)"
  [ -n "$selected_provider" ] || selected_provider="unknown"

  echo "    Conductor selected provider: $selected_provider"
  echo "    Conductor command invoked: $(conductor_invocation_label)"
}

exclude_provider_once() {
  local existing="$1"
  local provider="$2"
  local item
  local -a existing_items

  [ -n "$provider" ] || {
    printf '%s' "$existing"
    return 0
  }

  if [ -n "$existing" ]; then
    IFS=',' read -r -a existing_items <<<"$existing"
    for item in "${existing_items[@]}"; do
      item="$(trim "$item")"
      if [ "$item" = "$provider" ]; then
        printf '%s' "$existing"
        return 0
      fi
    done
  fi

  if [ -n "$existing" ]; then
    printf '%s,%s' "$existing" "$provider"
  else
    printf '%s' "$provider"
  fi
}

exclude_provider_csv() {
  local existing="$1"
  local providers="$2"
  local item result
  local -a provider_items

  result="$existing"
  if [ -n "$providers" ]; then
    IFS=',' read -r -a provider_items <<<"$providers"
    for item in "${provider_items[@]}"; do
      item="$(trim "$item")"
      [ -n "$item" ] || continue
      result="$(exclude_provider_once "$result" "$item")"
    done
  fi
  printf '%s' "$result"
}

conductor_failed_provider_csv() {
  local csv="" provider

  while IFS= read -r provider; do
    provider="$(trim "$provider")"
    [ -n "$provider" ] || continue
    csv="$(exclude_provider_once "$csv" "$provider")"
  done < <(
    if [ -f "${REVIEW_CONDUCTOR_LOG_FILE:-}" ]; then
      awk '
        /"event"[[:space:]]*:[[:space:]]*"provider_failed"/ {
          line = $0
          sub(/^.*"provider"[[:space:]]*:[[:space:]]*"/, "", line)
          sub(/".*$/, "", line)
          if (line != "") {
            print line
          }
        }
      ' "$REVIEW_CONDUCTOR_LOG_FILE" 2>/dev/null || true
    fi
    if [ -f "${REVIEW_STDERR_FILE:-}" ]; then
      awk '
        /review tried providers:/ { line = $0 }
        END {
          if (line == "") {
            exit
          }
          sub(/^.*review tried providers:[[:space:]]*/, "", line)
          n = split(line, parts, ",")
          for (i = 1; i <= n; i++) {
            part = parts[i]
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", part)
            if (part == "" || part ~ /\(success\)/) {
              continue
            }
            split(part, fields, /[[:space:]]+/)
            if (fields[1] != "") {
              print fields[1]
            }
          }
        }
      ' "$REVIEW_STDERR_FILE" 2>/dev/null || true
    fi
  )

  printf '%s' "$csv"
}

review_attempt_worktree_unchanged() {
  local head_before="$1"
  local status_before="$2"
  local head_after status_after

  head_after="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  status_after="$(git status --porcelain)"

  [ "$head_after" = "$head_before" ] && [ "$status_after" = "$status_before" ]
}

try_review_fallback_retry() {
  local reason="$1"
  local head_before="$2"
  local status_before="$3"
  local prompt="${4:-$REVIEW_PROMPT}"
  local output_file="${5:-$REVIEW_OUTPUT_FILE}"
  local failed_provider failed_providers previous_with previous_exclude
  local previous_preflight_review_provider previous_preflight_fix_provider fallback_exclude

  [ "${ACTIVE_REVIEWER:-}" = "conductor" ] || return 1
  is_truthy "$CONDUCTOR_FALLBACK_RETRY" || return 1
  [ "$REVIEW_FALLBACK_ATTEMPTED" = false ] || return 1

  if ! review_attempt_worktree_unchanged "$head_before" "$status_before"; then
    echo "==> Review fallback skipped: failed attempt changed the worktree."
    return 1
  fi

  failed_provider="$(parse_primary_provider)"
  [ -n "$failed_provider" ] || failed_provider="$(conductor_effective_with_for_phase review)"
  failed_providers="$(conductor_failed_provider_csv)"
  if [ -n "$failed_provider" ] && [ "$failed_provider" != "unknown" ]; then
    failed_providers="$(exclude_provider_once "$failed_providers" "$failed_provider")"
  fi
  if [ -z "$failed_provider" ] || [ "$failed_provider" = "unknown" ]; then
    echo "==> Review fallback skipped: could not identify the failed Conductor provider."
    return 1
  fi
  [ -n "$failed_providers" ] || failed_providers="$failed_provider"

  REVIEW_FALLBACK_ATTEMPTED=true
  REVIEW_FALLBACK_PRIMARY_PROVIDER="$failed_provider"
  REVIEW_FALLBACK_EXCLUDED_PROVIDERS="$failed_providers"
  REVIEW_FALLBACK_REASON="$reason"
  append_review_diagnostic_event "fallback-trigger" "$reason" "$output_file"

  previous_with="$CONDUCTOR_WITH"
  previous_exclude="$CONDUCTOR_EXCLUDE"
  previous_preflight_review_provider="$CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER"
  previous_preflight_fix_provider="$CONDUCTOR_PREFLIGHT_FIX_PROVIDER"
  fallback_exclude="$(exclude_provider_csv "$CONDUCTOR_EXCLUDE" "$failed_providers")"

  phase "retrying with Conductor fallback (excluding $failed_providers)"
  echo "==> Review infrastructure/noncompliance failure: $reason"
  echo "==> Retrying once with auto-routing; excluded provider(s): ${fallback_exclude:-<none>}"

  CONDUCTOR_WITH=""
  CONDUCTOR_EXCLUDE="$fallback_exclude"
  CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER=""
  CONDUCTOR_PREFLIGHT_FIX_PROVIDER=""
  set +e
  run_reviewer_with_timeout "$REVIEW_TIMEOUT" "$prompt" "$output_file"
  FALLBACK_REVIEW_EXIT=$?
  set -e
  REVIEW_FALLBACK_RETRY_PROVIDER="$(parse_primary_provider)"
  [ -n "$REVIEW_FALLBACK_RETRY_PROVIDER" ] || REVIEW_FALLBACK_RETRY_PROVIDER="unknown"

  CONDUCTOR_WITH="$previous_with"
  CONDUCTOR_EXCLUDE="$previous_exclude"
  CONDUCTOR_PREFLIGHT_REVIEW_PROVIDER="$previous_preflight_review_provider"
  CONDUCTOR_PREFLIGHT_FIX_PROVIDER="$previous_preflight_fix_provider"

  # The clean-review cache key was computed for the primary route. A fallback
  # clean result is valid for this run, but should not satisfy a later exact
  # cache lookup for the primary provider.
  REVIEW_CACHE_KEY=""
  return 0
}

# Run a peer/council review via Conductor. Peer mode excludes the primary's
# provider. Council mode asks Conductor for a multi-model synthesis.
# Advisory — second-opinion output appears in the transcript but does not gate
# the merge. When peer mode can't identify the primary provider, skip rather
# than invoke `conductor` without --exclude (which could reuse the primary).
run_peer_review() {
  local primary_output="$1"
  local primary_provider mode
  primary_provider="$(parse_primary_provider)"
  mode="peer"
  if [ "${HIGH_SCRUTINY_TRIGGERED:-false}" = true ]; then
    mode="${HIGH_SCRUTINY_MODE:-peer}"
  fi

  if [ "$mode" = "peer" ] && [ -z "$primary_provider" ]; then
    phase "peer review skipped — couldn't identify primary provider"
    return 0
  fi

  if [ "$mode" = "council" ]; then
    phase "council review — asking Conductor for high-scrutiny synthesis"
  else
    phase "peer review — asking Conductor for a second opinion (excluding $primary_provider)"
  fi

  local peer_prompt
  peer_prompt="$(build_peer_review_prompt "$primary_output")"

  # Peer is single-turn (no tools). `conductor call` sees the primary's
  # findings + a framing prompt; the router picks a non-primary provider.
  local peer_output peer_log_before peer_log_after
  # Peer call runs synchronously and relies on Conductor's own provider/stall
  # handling; ASSIST_TIMEOUT still applies to explicit assistant loops.
  PEER_CONDUCTOR_LOG_FILE=""
  peer_log_before="$(latest_conductor_session_log)"
  if [ "$mode" = "council" ]; then
    local brief_file
    brief_file="$(mktemp "${TMPDIR:-/tmp}/touchstone-review-council.XXXXXX.md")"
    printf '%s\n' "$peer_prompt" >"$brief_file"
    peer_output="$(conductor ask --kind council --effort medium --brief-file "$brief_file" 2>/dev/null || true)"
    rm -f "$brief_file"
  else
    peer_output="$(printf '%s' "$peer_prompt" \
      | conductor call --auto \
        --exclude "$primary_provider" \
        --tags code-review \
        --effort medium \
        --silent-route \
        2>/dev/null || true)"
  fi
  peer_log_after="$(latest_conductor_session_log)"
  if [ -n "$peer_log_after" ] && [ "$peer_log_after" != "$peer_log_before" ]; then
    PEER_CONDUCTOR_LOG_FILE="$peer_log_after"
  fi

  if [ -z "$peer_output" ]; then
    phase "peer review produced no output (skipped)"
    return 0
  fi

  if [ "$mode" = "council" ]; then
    printf "\n  ${C_DIM}── council review (%s) ──${C_RESET}\n" "${HIGH_SCRUTINY_REASON:-high-scrutiny}"
  else
    printf "\n  ${C_DIM}── peer review (excluded %s) ──${C_RESET}\n" "$primary_provider"
  fi
  printf '%s\n' "$peer_output" | sed 's/^/  /'
  printf "\n"
  ASSIST_ROUNDS=$((ASSIST_ROUNDS + 1))
  ASSIST_ROUNDS_DONE=$((${ASSIST_ROUNDS_DONE:-0} + 1))
}

build_peer_review_prompt() {
  local primary_output="$1"
  cat <<EOF
You are a peer code reviewer giving a second opinion on another AI reviewer's output.
You are asked to be a QUICK second opinion, NOT to redo the review from scratch.

The primary reviewer examined a code change and produced the output below. Your job:
  1. Do you AGREE or DISAGREE with the primary's overall verdict (CLEAN / FIXED / BLOCKED)?
  2. Anything the primary MISSED that you'd flag?
  3. Anything the primary FLAGGED that you think is a false positive?

Keep your response under 300 words. Lead with AGREE or DISAGREE on a line by itself.

--- Primary reviewer output: ---
$primary_output
--- End primary output ---
EOF
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

block_if_worktree_mutated_in_review_mode() {
  if mode_allows_fix && [ "${REVIEW_PHASE:-review}" != "review" ]; then
    return 0
  fi

  if check_worktree_invariants; then
    return 0
  fi

  REVIEW_EXIT_REASON="worktree-mutated"
  print_summary
  if mode_allows_fix; then
    echo "==> ERROR: Worktree was mutated during the read-only review phase in '$REVIEW_MODE' mode — blocking push." >&2
  else
    echo "==> ERROR: Worktree was mutated in '$REVIEW_MODE' mode — blocking push." >&2
  fi
  exit 1
}

run_fix_phase_for_blocked_review() {
  local review_output="$1"
  local fix_prompt fix_exit fix_output fix_sentinel

  mode_allows_fix || return 1
  [ "${REVIEW_FIX_ATTEMPTED:-false}" = false ] || return 1

  if [ "$WORKTREE_DIRTY_BEFORE_REVIEW" = true ]; then
    echo "==> Skipping auto-fix: working tree was already dirty before review."
    return 1
  fi

  REVIEW_FIX_ATTEMPTED=true
  fix_prompt="$(build_fix_prompt "$review_output")"

  phase "fixing review findings with $REVIEWER_LABEL"
  set +e
  REVIEW_PHASE=fix run_reviewer_with_timeout "$REVIEW_TIMEOUT" "$fix_prompt" "$REVIEW_OUTPUT_FILE"
  fix_exit=$?
  set -e
  fix_output="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"
  print_route_log

  if [ "$fix_exit" -eq 124 ]; then
    echo "==> $REVIEWER_LABEL fix phase timed out after ${REVIEW_TIMEOUT}s; blocking on the read-only findings."
    OUTPUT="$review_output"
    LAST_SENTINEL="CODEX_REVIEW_BLOCKED"
    return 0
  fi

  if [ "$fix_exit" -ne 0 ]; then
    echo "==> $REVIEWER_LABEL fix phase failed with exit $fix_exit; blocking on the read-only findings."
    OUTPUT="$review_output"
    LAST_SENTINEL="CODEX_REVIEW_BLOCKED"
    return 0
  fi

  fix_sentinel="$(printf '%s\n' "$fix_output" | extract_review_sentinel)"
  case "$fix_sentinel" in
    CODEX_REVIEW_FIXED)
      OUTPUT="$fix_output"
      LAST_SENTINEL="CODEX_REVIEW_FIXED"
      return 0
      ;;
    CODEX_REVIEW_BLOCKED)
      OUTPUT="${review_output}

Fix phase output:
${fix_output}"
      LAST_SENTINEL="CODEX_REVIEW_BLOCKED"
      return 0
      ;;
    *)
      echo "==> $REVIEWER_LABEL fix phase output did not match the sentinel contract; blocking on the read-only findings."
      OUTPUT="${review_output}

Malformed fix phase output:
${fix_output}"
      LAST_SENTINEL="CODEX_REVIEW_BLOCKED"
      return 0
      ;;
  esac
}

# --------------------------------------------------------------------------
# Structured summary
# --------------------------------------------------------------------------

print_summary() {
  local elapsed mins secs findings provider model peer_provider diagnostics_file diagnostics_events
  elapsed=$(($(date +%s) - REVIEW_START_TIME))
  mins=$((elapsed / 60))
  secs=$((elapsed % 60))
  findings="${REVIEW_FINDINGS_COUNT:-0}"
  provider="$(parse_primary_provider)"
  [ -n "$provider" ] || provider="$(conductor_effective_with_for_phase review)"
  [ -n "$provider" ] || provider="unknown"
  model="$(parse_primary_model)"
  [ -n "$model" ] || model="unknown"
  peer_provider="$(parse_peer_provider)"
  [ -n "$peer_provider" ] || peer_provider="none"
  diagnostics_file="${REVIEW_DIAGNOSTICS_FILE:-}"
  diagnostics_events=0
  if [ -n "$diagnostics_file" ] && [ -f "$diagnostics_file" ]; then
    diagnostics_events="$(review_diagnostics_event_count "$diagnostics_file")"
    [ -n "$diagnostics_events" ] || diagnostics_events=0
  else
    diagnostics_file=""
  fi

  printf "\n  ${C_DIM}─── review summary ────────────────────────${C_RESET}\n"
  printf "  ${C_DIM}reviewer:       %s${C_RESET}\n" "$REVIEWER_LABEL"
  if [ "$ROUTING_DECISION" != "default" ]; then
    printf "  ${C_DIM}route:          %s${C_RESET}\n" "$ROUTING_DECISION"
  fi
  printf "  ${C_DIM}mode:           %s${C_RESET}\n" "$REVIEW_MODE"
  printf "  ${C_DIM}context:        %s${C_RESET}\n" "$PROMPT_CONTEXT_DECISION"
  printf "  ${C_DIM}budget:         prefer=%s effort=%s${C_RESET}\n" "${CONDUCTOR_PREFER:-auto}" "${CONDUCTOR_EFFORT:-default}"
  printf "  ${C_DIM}files:          %s${C_RESET}\n" "$REVIEW_FILES_INSPECTED"
  printf "  ${C_DIM}diff lines:     %s${C_RESET}\n" "$DIFF_LINE_COUNT"
  printf "  ${C_DIM}iterations:     %s/%s${C_RESET}\n" "${iter:-0}" "$MAX_ITERATIONS"
  printf "  ${C_DIM}fix commits:    %s${C_RESET}\n" "$FIX_COMMITS"
  printf "  ${C_DIM}peer assists:   %s${C_RESET}\n" "$ASSIST_ROUNDS"
  if [ "${HIGH_SCRUTINY_TRIGGERED:-false}" = true ]; then
    printf "  ${C_DIM}high scrutiny:  %s (%s)${C_RESET}\n" "$HIGH_SCRUTINY_MODE" "$HIGH_SCRUTINY_REASON"
  fi
  printf "  ${C_DIM}findings:       %s${C_RESET}\n" "$findings"
  if [ "$REVIEW_FALLBACK_ATTEMPTED" = true ]; then
    printf "  ${C_DIM}fallback:       %s -> %s (%s)${C_RESET}\n" \
      "${REVIEW_FALLBACK_PRIMARY_PROVIDER:-unknown}" \
      "${REVIEW_FALLBACK_RETRY_PROVIDER:-unknown}" \
      "${REVIEW_FALLBACK_REASON:-unknown}"
    if [ -n "${REVIEW_FALLBACK_EXCLUDED_PROVIDERS:-}" ]; then
      printf "  ${C_DIM}fallback skip:  %s${C_RESET}\n" "${REVIEW_FALLBACK_EXCLUDED_PROVIDERS:-unknown}"
    fi
  fi
  if [ -n "$diagnostics_file" ]; then
    printf "  ${C_DIM}diagnostics:    %s (%s event(s))${C_RESET}\n" "$diagnostics_file" "$diagnostics_events"
  fi
  printf "  ${C_DIM}exit reason:    %s${C_RESET}\n" "$REVIEW_EXIT_REASON"
  printf "  ${C_DIM}elapsed:        %dm%ds${C_RESET}\n" "$mins" "$secs"
  printf "  ${C_DIM}──────────────────────────────────────────${C_RESET}\n"

  if [ -n "${CODEX_REVIEW_SUMMARY_FILE:-}" ]; then
    printf '{"reviewer":"%s","provider":"%s","model":"%s","peer_provider":"%s","route":"%s","mode":"%s","context":"%s","prefer":"%s","effort":"%s","files":%d,"diff_lines":%d,"iterations":%d,"fix_commits":%d,"peer_assists":%d,"high_scrutiny_triggered":%s,"high_scrutiny_mode":"%s","high_scrutiny_reason":"%s","findings":%d,"fallback_attempted":%s,"fallback_primary_provider":"%s","fallback_retry_provider":"%s","fallback_excluded_providers":"%s","fallback_reason":"%s","diagnostics_file":"%s","diagnostics_events":%d,"exit_reason":"%s","elapsed_seconds":%d}\n' \
      "$REVIEWER_LABEL" "$provider" "$model" "$peer_provider" "$ROUTING_DECISION" "$REVIEW_MODE" "$PROMPT_CONTEXT_DECISION" "${CONDUCTOR_PREFER:-auto}" "${CONDUCTOR_EFFORT:-default}" "$REVIEW_FILES_INSPECTED" "$DIFF_LINE_COUNT" \
      "${iter:-0}" "$FIX_COMMITS" "$ASSIST_ROUNDS" "${HIGH_SCRUTINY_TRIGGERED:-false}" \
      "$(printf '%s' "${HIGH_SCRUTINY_MODE:-peer}" | json_escape)" \
      "$(printf '%s' "${HIGH_SCRUTINY_REASON:-}" | json_escape)" \
      "$findings" "$REVIEW_FALLBACK_ATTEMPTED" \
      "$(printf '%s' "${REVIEW_FALLBACK_PRIMARY_PROVIDER:-}" | json_escape)" \
      "$(printf '%s' "${REVIEW_FALLBACK_RETRY_PROVIDER:-}" | json_escape)" \
      "$(printf '%s' "${REVIEW_FALLBACK_EXCLUDED_PROVIDERS:-}" | json_escape)" \
      "$(printf '%s' "${REVIEW_FALLBACK_REASON:-}" | json_escape)" \
      "$(printf '%s' "$diagnostics_file" | json_escape)" "$diagnostics_events" \
      "$REVIEW_EXIT_REASON" "$elapsed" \
      >"$CODEX_REVIEW_SUMMARY_FILE" 2>/dev/null || true
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
# synced into downstream projects as scripts/conductor-review.sh, with
# scripts/codex-review.sh retained as the legacy compatibility path.
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
  local color="$1"
  shift
  local flag="$1"
  shift
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
    version="$(tr -d '[:space:]' <"$TOUCHSTONE_ROOT/VERSION" 2>/dev/null || true)"
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
    ok)
      mark="$(tk_paint "$TK_BRAND_LIME" plain "✓")"
      painted_headline="$(tk_paint "$TK_BRAND_LIME" bold "$headline")"
      ;;
    fail)
      mark="$(tk_paint "$TK_BRAND_RED" plain "✗")"
      painted_headline="$(tk_paint "$TK_BRAND_RED" bold "$headline")"
      ;;
    *)
      mark="$(tk_paint "$TK_BRAND_DIM" plain "•")"
      painted_headline="$(tk_paint "$TK_BRAND_DIM" bold "$headline")"
      ;;
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

if ! run_conductor_route_preflight; then
  REVIEW_EXIT_REASON="provider-unavailable"
  REVIEW_FINDINGS_COUNT=0
  DIFF_LINE_COUNT="$ROUTING_DIFF_LINE_COUNT"
  print_summary
  handle_error "provider unavailable: route preflight"
fi

# --------------------------------------------------------------------------
# Issue #163: prepend a verification checkpoint when this branch already
# has a recorded BLOCKED review whose HEAD is an ancestor of current HEAD.
# Empty string (no prior file, force-push, parse error) means a normal
# fresh review, so this is fail-open by construction. Runs after all helper
# definitions and right before the review loop.
# --------------------------------------------------------------------------
REVIEW_VERIFICATION_CHECKPOINT="$(build_review_verification_checkpoint)"
if [ -n "$REVIEW_VERIFICATION_CHECKPOINT" ]; then
  REVIEW_PROMPT="${REVIEW_VERIFICATION_CHECKPOINT}

${REVIEW_PROMPT}"
  echo "==> Prior review findings injected; reviewer will verify + delta-scan instead of restarting."
fi

for iter in $(seq 1 "$MAX_ITERATIONS"); do
  REVIEW_FIX_ATTEMPTED=false
  phase "loading diff"
  DIFF_LINE_COUNT="$(git diff "$MERGE_BASE"..HEAD | wc -l | tr -d ' ')"
  if [ "$DIFF_LINE_COUNT" -gt "$MAX_DIFF_LINES" ]; then
    if is_truthy "${SCOPED_LARGE_DIFF_REVIEW:-false}"; then
      echo "==> Diff is $DIFF_LINE_COUNT lines (> $MAX_DIFF_LINES cap)."
      echo "==> Running scoped project-owned review: $SCOPED_LARGE_DIFF_LINES lines across $SCOPED_LARGE_DIFF_INCLUDED_COUNT file(s); excluded $SCOPED_LARGE_DIFF_EXCLUDED_COUNT trusted Touchstone-managed file(s)."
      DIFF_LINE_COUNT="$SCOPED_LARGE_DIFF_LINES"
    elif is_truthy "${SCOPED_LARGE_DIFF_SYNC_ONLY:-false}"; then
      echo "==> Diff is $DIFF_LINE_COUNT lines (> $MAX_DIFF_LINES cap), but all changed paths are trusted Touchstone-managed sync files."
      echo "==> Skipping AI review for managed sync-only diff."
      log_skip_event other "diff-too-large-managed-sync-only:${DIFF_LINE_COUNT}>${MAX_DIFF_LINES}"
      exit 0
    else
      echo "==> Diff is $DIFF_LINE_COUNT lines (> $MAX_DIFF_LINES cap) — skipping review."
      echo "    Override with: CODEX_REVIEW_MAX_DIFF_LINES=100000 git push"
      if [ "$ON_ERROR" = "fail-closed" ]; then
        REVIEW_EXIT_REASON="error"
        handle_error "diff too large:${DIFF_LINE_COUNT}>${MAX_DIFF_LINES}"
      fi
      log_skip_event other "diff-too-large:${DIFF_LINE_COUNT}>${MAX_DIFF_LINES}"
      exit 0
    fi
  fi

  phase "checking cache"
  REVIEW_CACHE_KEY=""
  if cache_enabled; then
    REVIEW_CACHE_KEY="$(review_cache_key 2>/dev/null || true)"
    if [ -n "$REVIEW_CACHE_KEY" ] && [ -f "$(clean_review_cache_file "$REVIEW_CACHE_KEY")" ]; then
      echo "==> Review previously passed for this exact diff — skipping repeat review."
      echo "    Force a fresh review with: CODEX_REVIEW_DISABLE_CACHE=1 git push"
      REVIEW_EXIT_REASON="cache-hit"
      REVIEW_FINDINGS_COUNT=0
      print_summary
      log_skip_event other "cache-hit:${REVIEW_CACHE_KEY}"
      exit 0
    fi
  fi

  print_banner
  printf "  ${C_DIM}iteration ${iter}/${MAX_ITERATIONS} · ${DIFF_LINE_COUNT} lines vs ${BASE}${C_RESET}\n"
  phase "reviewing with $REVIEWER_LABEL"

  REVIEW_ATTEMPT_HEAD_BEFORE="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  REVIEW_ATTEMPT_STATUS_BEFORE="$(git status --porcelain)"
  set +e
  run_reviewer_with_timeout "$REVIEW_TIMEOUT"
  EXIT=$?
  set -e
  OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"

  # Surface Conductor's route-log (provider, cost, tokens, duration). If
  # the reviewer isn't Conductor the filter matches nothing and this is a
  # no-op, so it's safe to call unconditionally.
  print_route_log

  # Peer review (v2.1): when [review.assist].enabled=true, ask Conductor
  # for a second opinion excluding the primary provider. Advisory — the
  # peer's verdict does NOT gate the merge; the primary's sentinel wins.
  # Fires once per iteration, respects ASSIST_MAX_ROUNDS.
  if is_truthy "${ASSIST_ENABLED:-false}" \
    && [ "${ASSIST_ROUNDS_DONE:-0}" -lt "${ASSIST_MAX_ROUNDS:-1}" ] \
    && [ -n "$OUTPUT" ]; then
    run_peer_review "$OUTPUT" || true
  fi

  # Check worktree invariants in non-fix modes.
  # This is a hard failure regardless of on_error policy — a reviewer that
  # mutates the worktree in review-only mode is a safety violation.
  block_if_worktree_mutated_in_review_mode

  if [ "$EXIT" -eq 124 ]; then
    if try_review_fallback_retry "timeout after ${REVIEW_TIMEOUT}s" "$REVIEW_ATTEMPT_HEAD_BEFORE" "$REVIEW_ATTEMPT_STATUS_BEFORE"; then
      EXIT="$FALLBACK_REVIEW_EXIT"
      OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"
      print_route_log
      block_if_worktree_mutated_in_review_mode
    fi
  fi

  if [ "$EXIT" -eq 124 ]; then
    phase "timed out"
    echo "==> $REVIEWER_LABEL timed out after ${REVIEW_TIMEOUT}s."
    REVIEW_EXIT_REASON="timeout"
    append_review_diagnostic_event "review-timeout" "timeout after ${REVIEW_TIMEOUT}s"
    print_summary
    handle_error "timeout after ${REVIEW_TIMEOUT}s"
  fi

  if [ $EXIT -ne 0 ]; then
    if try_review_fallback_retry "reviewer exit $EXIT" "$REVIEW_ATTEMPT_HEAD_BEFORE" "$REVIEW_ATTEMPT_STATUS_BEFORE"; then
      EXIT="$FALLBACK_REVIEW_EXIT"
      OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"
      print_route_log
      block_if_worktree_mutated_in_review_mode
    fi
  fi

  if [ $EXIT -ne 0 ]; then
    echo "==> $REVIEWER_LABEL review failed with exit $EXIT."
    REVIEW_EXIT_REASON="error"
    append_review_diagnostic_event "review-error" "reviewer exit $EXIT"
    print_summary
    handle_error "reviewer exit $EXIT"
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

        block_if_worktree_mutated_in_review_mode

        if [ "$EXIT" -eq 124 ]; then
          phase "timed out"
          echo "==> $REVIEWER_LABEL timed out after ${REVIEW_TIMEOUT}s after peer assistance."
          REVIEW_EXIT_REASON="timeout"
          append_review_diagnostic_event "assist-timeout" "timeout after ${REVIEW_TIMEOUT}s after peer assistance"
          print_summary
          handle_error "timeout after ${REVIEW_TIMEOUT}s after peer assistance"
        fi

        if [ "$EXIT" -ne 0 ]; then
          echo "==> $REVIEWER_LABEL review failed with exit $EXIT after peer assistance."
          REVIEW_EXIT_REASON="error"
          append_review_diagnostic_event "assist-error" "reviewer exit $EXIT after peer assistance"
          print_summary
          handle_error "reviewer exit $EXIT after peer assistance"
        fi
      else
        echo "==> Continuing with the primary reviewer output."
      fi
    fi
  fi

  while :; do
    LAST_SENTINEL="$(printf '%s\n' "$OUTPUT" | extract_review_sentinel)"
    case "$LAST_SENTINEL" in
      CODEX_REVIEW_CLEAN | CODEX_REVIEW_FIXED | CODEX_REVIEW_BLOCKED) break ;;
    esac

    LAST_LINE="$(printf '%s\n' "$OUTPUT" | awk 'NF { line = $0 } END { print line }' | tr -d '\r')"
    if try_review_fallback_retry "malformed sentinel" "$REVIEW_ATTEMPT_HEAD_BEFORE" "$REVIEW_ATTEMPT_STATUS_BEFORE"; then
      EXIT="$FALLBACK_REVIEW_EXIT"
      OUTPUT="$(cat "$REVIEW_OUTPUT_FILE" 2>/dev/null || true)"
      print_route_log
      block_if_worktree_mutated_in_review_mode

      if [ "$EXIT" -eq 124 ]; then
        phase "timed out"
        echo "==> $REVIEWER_LABEL timed out after ${REVIEW_TIMEOUT}s during fallback retry."
        REVIEW_EXIT_REASON="timeout"
        append_review_diagnostic_event "fallback-timeout" "timeout after ${REVIEW_TIMEOUT}s during fallback retry"
        print_summary
        handle_error "timeout after ${REVIEW_TIMEOUT}s during fallback retry"
      fi

      if [ "$EXIT" -ne 0 ]; then
        echo "==> $REVIEWER_LABEL fallback review failed with exit $EXIT."
        REVIEW_EXIT_REASON="error"
        append_review_diagnostic_event "fallback-error" "reviewer exit $EXIT during fallback retry"
        print_summary
        handle_error "reviewer exit $EXIT during fallback retry"
      fi
      continue
    fi
    break
  done

  if [ "$LAST_SENTINEL" = "CODEX_REVIEW_BLOCKED" ] && mode_allows_fix; then
    run_fix_phase_for_blocked_review "$OUTPUT" || true
  fi

  case "$LAST_SENTINEL" in
    CODEX_REVIEW_CLEAN)
      phase "done — clean"
      clean_subtitle="${REVIEWER_LABEL} · ${DIFF_LINE_COUNT} lines · push approved"
      if [ "$FIX_COMMITS" -gt 0 ]; then
        clean_subtitle="${clean_subtitle} · ${FIX_COMMITS} auto-fix commit(s)"
      fi
      tk_verdict ok "ALL CLEAR" "$clean_subtitle"
      REVIEW_EXIT_REASON="clean"
      print_summary
      write_clean_review_cache "$REVIEW_CACHE_KEY" "$DIFF_LINE_COUNT"
      write_clean_review_marker "$DIFF_LINE_COUNT"
      clear_review_findings
      append_findings_history_event "CODEX_REVIEW_CLEAN" "$iter" "$OUTPUT" 0
      # The "ran" denominator: a successful review actually executed.
      # skip-rate = log_skip_count / (log_skip_count + log_ran_count).
      log_skip_event ran "clean:iter=${iter}:lines=${DIFF_LINE_COUNT}:fix-commits=${FIX_COMMITS}"
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
        log_skip_event other "ambiguous-fixed-no-changes:iter=${iter}"
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
        echo "==> $REVIEWER_LABEL edited paths that are not allowed by ${CONFIG_DISPLAY_NAME:-.touchstone-review.toml}."
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
      append_findings_history_event "CODEX_REVIEW_FIXED" "$iter" "$OUTPUT" 0
      WORKTREE_DIRTY_BEFORE_REVIEW=false
      WORKTREE_HEAD_BEFORE="$(git rev-parse HEAD)"
      WORKTREE_BRANCH_BEFORE="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
      WORKTREE_STATUS_BEFORE="$(git status --porcelain)"
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
      REVIEW_EXIT_REASON="blocked"
      print_summary
      # Issue #163: persist the blocking findings so the next push attempt
      # on a descendant HEAD prepends a verification checkpoint instead of
      # auditing from scratch.
      write_review_findings "$OUTPUT"
      append_findings_history_event "CODEX_REVIEW_BLOCKED" "$iter" "$OUTPUT" 0
      # The reviewer actually ran and produced a verdict — counts as "ran"
      # for the skip-rate denominator even though the push is blocked.
      log_skip_event ran "blocked:iter=${iter}:findings=${REVIEW_FINDINGS_COUNT}"
      exit 1
      ;;

    *)
      LAST_LINE="$(printf '%s\n' "$OUTPUT" | awk 'NF { line = $0 } END { print line }' | tr -d '\r')"
      echo "==> $REVIEWER_LABEL output did not match the expected sentinel contract."
      if [ -n "$LAST_SENTINEL" ]; then
        echo "    Last matching sentinel line was: '$LAST_SENTINEL'"
      else
        echo "    No unique standalone sentinel line was found."
      fi
      print_malformed_sentinel_diagnostics
      echo "    Last non-blank line was: '$LAST_LINE'"
      echo "    Raw output (first 20 lines):"
      printf '%s\n' "$OUTPUT" | head -20 | sed 's/^/    /'
      REVIEW_EXIT_REASON="malformed-sentinel"
      append_review_diagnostic_event "malformed-sentinel" "malformed sentinel"
      print_summary
      handle_error "malformed sentinel"
      ;;
  esac
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
REVIEW_EXIT_REASON="max-iterations"
print_summary
# The reviewer ran (multiple times) but didn't converge — counts as "ran"
# for the denominator since the safety net wasn't bypassed.
log_skip_event ran "max-iterations:fix-commits=${FIX_COMMITS}"
exit 1
