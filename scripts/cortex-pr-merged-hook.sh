#!/usr/bin/env bash
#
# hooks/cortex-pr-merged-hook.sh — auto-draft a Cortex `pr-merged` Journal
# entry after a PR squash-merges to the default branch.
#
# Implements Cortex Protocol § 2 Tier-1 trigger T1.9 ("Pull request merged
# to the default branch"). When a project has both Touchstone and Cortex
# installed, this hook fires from `merge-pr.sh` immediately after the
# remote merge succeeds and the local default branch is synced. It shells
# out to `cortex journal draft pr-merged --no-edit`, captures the new
# entry's path on stdout, refreshes `.cortex/state.md`, stages both files
# when present, commits with `--no-verify` on a follow-up branch, pushes
# that branch, and opens a normal PR for the journal commit. The direct
# default-branch push path is retained only as an explicit compatibility
# opt-in because it conflicts with Touchstone's no-commit-to-branch policy.
#
# Activation contract (ALL of these must hold; otherwise silent exit 0):
#   1. Push target is the default branch (main or master). Resolved by
#      asking `gh repo view`.
#   2. The repo has a `.cortex/` directory at the same level as `.git/`.
#   3. The `cortex` CLI is on $PATH.
#   4. `.touchstone-config` has `cortex_pr_merged_hook=auto`, `=on`, or
#      `=force`. Default for newly-bootstrapped projects: `auto`. Value
#      `off` disables. Missing key is treated as `auto` (so projects that
#      haven't migrated yet still benefit when the other gates pass).
#      Value `force` skips the substantive-merge gate and always journals.
#   5. The most recent commit is not this hook's own previous output
#      (recursion guard, cortex#193).
#   6. After the recursion guard, `cortex check-triggers --since HEAD~1`
#      reports at least one fired trigger, unless force mode is enabled
#      or the check-trigger gate is unavailable.
#
# Failure modes (no silent failures past activation):
#   - cortex missing mid-flow (between detection and exec): log to stderr
#     and exit 0 (degrade gracefully — don't fail the merge because the
#     CLI was uninstalled in a tiny race window).
#   - `cortex journal draft` exits non-zero: stderr surfaced, exit 1.
#   - Empty stdout (no path returned): stderr message, exit 1.
#   - Returned path doesn't exist after the call: stderr message, exit 1.
#   - `cortex refresh-state` exits non-zero: stderr surfaced, explicit
#     recovery command logged, and the hook continues when no partial
#     state.md change was left behind.
#   - `git commit` or `git push` failure: stderr message, exit 1.
#
# Inputs (env, all optional):
#   TOUCHSTONE_MERGED_PR        — PR number to thread through to the
#                                 commit message (e.g. supplied by
#                                 merge-pr.sh after `gh pr merge`).
#   TOUCHSTONE_CORTEX_HOOK_DISABLE
#                               — set to 1/true/on to short-circuit even
#                                 when config says auto/on. Useful for
#                                 tests that want to verify a path
#                                 without firing the writer.
#   TOUCHSTONE_CORTEX_HOOK_FORCE
#                               — set to 1/true/on to bypass the
#                                 substantive-merge gate and always
#                                 journal.
#   TOUCHSTONE_CORTEX_HOOK_DIRECT_PUSH
#                               — set to 1/true/on to use the legacy
#                                 direct default-branch push path. This
#                                 bypasses pre-push hooks for the
#                                 deterministic Cortex journal commit.
#   TOUCHSTONE_CORTEX_HOOK_BRANCH
#                               — override the follow-up branch name
#                                 (mainly for deterministic tests).
#   TOUCHSTONE_DEFAULT_BRANCH   — override the default-branch lookup
#                                 (the test fixture sets this so it
#                                 doesn't need a configured GitHub remote).
#
# Exit codes:
#   0 — fired and committed; OR silently skipped (inactive); OR cortex
#       went missing mid-flow (graceful degrade).
#   1 — activated and a real failure occurred (journal draft failed,
#       commit/push failed, missing path).
#
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# Read a flat key=value from .touchstone-config. Echoes the trimmed value
# on stdout. Empty output means "key absent or no value". Comment lines
# (starting with `#`) and blank lines are ignored. The final occurrence
# wins (matching the wider config-parser idiom in new-project.sh).
read_config_value() {
  local config_file="$1" key="$2"
  local line lhs rhs result=""
  [ -f "$config_file" ] || {
    printf ''
    return 0
  }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in '#'* | '') continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    lhs="${line%%=*}"
    lhs="${lhs#"${lhs%%[![:space:]]*}"}"
    lhs="${lhs%"${lhs##*[![:space:]]}"}"
    if [ "$lhs" = "$key" ]; then
      rhs="${line#*=}"
      rhs="${rhs#"${rhs%%[![:space:]]*}"}"
      rhs="${rhs%"${rhs##*[![:space:]]}"}"
      result="$rhs"
    fi
  done <"$config_file"
  printf '%s' "$result"
}

AUTO_DRAFT_SUBJECT_PREFIX='docs(journal): auto-draft pr-merged entry'
AUTO_DRAFT_BRANCH_PREFIX='docs/cortex-pr-merged'

sanitize_branch_component() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9._-]/-/g; s/--*/-/g; s/^-//; s/-$//'
}

resolve_journal_branch() {
  if [ -n "${TOUCHSTONE_CORTEX_HOOK_BRANCH:-}" ]; then
    printf '%s' "$TOUCHSTONE_CORTEX_HOOK_BRANCH"
    return 0
  fi

  local suffix timestamp
  if [ -n "${TOUCHSTONE_MERGED_PR:-}" ]; then
    suffix="pr-${TOUCHSTONE_MERGED_PR}"
  else
    suffix="manual"
  fi
  timestamp="$(date '+%Y%m%d%H%M%S')"
  printf '%s-%s-%s' "$AUTO_DRAFT_BRANCH_PREFIX" "$(sanitize_branch_component "$suffix")" "$timestamp"
}

git_push_clean_env() {
  env -u SKIP_REVIEW -u SKIP_CODEX_REVIEW git -C "$PROJECT_DIR" push "$@"
}

resolve_default_branch() {
  if [ -n "${TOUCHSTONE_DEFAULT_BRANCH:-}" ]; then
    printf '%s' "$TOUCHSTONE_DEFAULT_BRANCH"
    return 0
  fi
  local resolved=""
  if command -v gh >/dev/null 2>&1; then
    resolved="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || true)"
  fi
  if [ -z "$resolved" ]; then
    # Fall back to the local symbolic-ref of origin/HEAD; finally to "main".
    resolved="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
  fi
  printf '%s' "${resolved:-main}"
}

# 1. Detection — silent skip if any precondition fails.
PROJECT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$PROJECT_DIR" ]; then
  exit 0
fi

current_branch="$(git -C "$PROJECT_DIR" branch --show-current 2>/dev/null || true)"
default_branch="$(resolve_default_branch)"
if [ -z "$current_branch" ] || [ "$current_branch" != "$default_branch" ]; then
  exit 0
fi

if [ ! -d "$PROJECT_DIR/.cortex" ]; then
  exit 0
fi

# Project-level opt-out via env (test fixtures + emergency disable).
if truthy "${TOUCHSTONE_CORTEX_HOOK_DISABLE:-0}"; then
  exit 0
fi

config_value="$(read_config_value "$PROJECT_DIR/.touchstone-config" cortex_pr_merged_hook)"
force_journal=0
case "$config_value" in
  off | OFF | Off) exit 0 ;;
  on | ON | On | auto | AUTO | Auto | "") ;; # default to auto when absent
  force | FORCE | Force) force_journal=1 ;;
  *)
    # Unknown value — treat as off but warn so the project can fix the
    # config without surprise behavior.
    log "cortex-pr-merged-hook: unknown cortex_pr_merged_hook='$config_value' (expected: auto|on|off|force); skipping."
    exit 0
    ;;
esac
if truthy "${TOUCHSTONE_CORTEX_HOOK_FORCE:-0}"; then
  force_journal=1
fi

# Recursion guard (cortex#193). A merge of this hook's own auto-draft
# output carries the same subject this script would generate; short-
# circuit before any cortex invocation so we don't journal our own
# journal commit.
last_subject="$(git -C "$PROJECT_DIR" log -1 --format=%s HEAD 2>/dev/null || true)"
case "$last_subject" in
  "$AUTO_DRAFT_SUBJECT_PREFIX"*) exit 0 ;;
esac

if ! command -v cortex >/dev/null 2>&1; then
  # Detection passed (`.cortex/` exists, config is auto/on) but the CLI
  # is missing. The brief calls this a graceful-degrade case — don't
  # fail the merge over a missing optional tool.
  exit 0
fi

# 2. Substantive-merge gate (cortex#206). Only draft a pr-merged
# journal entry when Cortex reports at least one trigger fired. If the
# gate is unavailable, log the degradation and fall back to the prior
# journal-every-merge behavior; a spurious journal is recoverable, while
# a silently skipped substantive merge is not.
fired_triggers_ndjson=""
if [ "$force_journal" -eq 0 ]; then
  if ! git -C "$PROJECT_DIR" rev-parse --verify --quiet HEAD~1 >/dev/null 2>&1; then
    log "cortex-pr-merged-hook: HEAD has no parent commit; substantive-merge gate skipped, falling back to journal-every-merge."
  else
    check_triggers_stdout=""
    check_triggers_stderr=""
    check_triggers_status=0
    ct_stderr_file="$(mktemp -t cortex-pr-merged-hook.XXXXXX 2>/dev/null || mktemp)"
    trap 'rm -f "$ct_stderr_file"' EXIT
    check_triggers_stdout="$(cd "$PROJECT_DIR" && cortex check-triggers --since HEAD~1 2>"$ct_stderr_file")" \
      || check_triggers_status=$?
    check_triggers_stderr="$(cat "$ct_stderr_file" 2>/dev/null || true)"
    rm -f "$ct_stderr_file"
    trap - EXIT

    if [ "$check_triggers_status" -ne 0 ]; then
      if [ -n "$check_triggers_stderr" ]; then
        printf '%s\n' "$check_triggers_stderr" >&2
      fi
      log "cortex-pr-merged-hook: cortex check-triggers unavailable; falling back to journal-every-merge."
    elif [ -z "$check_triggers_stdout" ]; then
      exit 0
    else
      fired_triggers_ndjson="$check_triggers_stdout"
    fi
  fi
fi

# 3. Activated path — from here on, errors are visible failures.

# Refuse to run on a dirty tree: we'd silently fold uncommitted user work
# into the auto-commit. The caller (merge-pr.sh) leaves a clean tree by
# the time we reach this point; if anything else changed, that's a real
# problem the operator should see.
if [ -n "$(git -C "$PROJECT_DIR" status --porcelain)" ]; then
  log "cortex-pr-merged-hook: working tree has uncommitted changes after merge; refusing to auto-draft (would fold user changes into the auto-commit)."
  exit 1
fi

# `cortex journal draft pr-merged --no-edit` writes the entry and prints
# the absolute path on stdout. We capture stdout to grab that path; we
# leave stderr untouched so any cortex-side warnings (gh not auth'd, etc)
# surface to the operator running the merge.
draft_stdout=""
draft_status=0
draft_stdout="$(cd "$PROJECT_DIR" \
  && CORTEX_PR_MERGED_FIRED_TRIGGERS="$fired_triggers_ndjson" \
    cortex journal draft pr-merged --no-edit)" \
  || draft_status=$?
if [ "$draft_status" -ne 0 ]; then
  log "cortex-pr-merged-hook: cortex journal draft pr-merged exited $draft_status."
  exit 1
fi

# Take the last non-empty stdout line as the path. cortex's draft command
# emits exactly one line (the absolute path) but a trailing newline or a
# warning printed by an upstream Python wrapper could appear; the most-
# recent line is the path.
candidate="$(printf '%s\n' "$draft_stdout" | awk 'NF{p=$0} END{print p}')"
if [ -z "$candidate" ]; then
  log "cortex-pr-merged-hook: cortex journal draft returned no path on stdout."
  exit 1
fi

if [ ! -f "$candidate" ]; then
  log "cortex-pr-merged-hook: returned path '$candidate' is not a regular file."
  exit 1
fi

if [ -n "$fired_triggers_ndjson" ]; then
  {
    printf '\n## Triggers fired\n\n'
    if command -v jq >/dev/null 2>&1; then
      printf '%s\n' "$fired_triggers_ndjson" | while IFS= read -r line; do
        [ -n "$line" ] || continue
        files="$(printf '%s' "$line" | jq -r '(.files // []) | join(", ")' 2>/dev/null || true)"
        trigger="$(printf '%s' "$line" | jq -r '.trigger // ""' 2>/dev/null || true)"
        reason="$(printf '%s' "$line" | jq -r '.reason // ""' 2>/dev/null || true)"
        if [ -n "$files" ]; then
          printf -- '- %s — %s (files: %s)\n' "$trigger" "$reason" "$files"
        else
          printf -- '- %s — %s\n' "$trigger" "$reason"
        fi
      done
    else
      log "cortex-pr-merged-hook: jq not on PATH; appending raw NDJSON to triggers section."
      printf '```ndjson\n%s\n```\n' "$fired_triggers_ndjson"
    fi
  } >>"$candidate"
fi

# Refresh derived state so the journal entry and state snapshot land in
# the same hook commit. This is best-effort because Cortex is an optional
# integration; if an older CLI lacks the command, the merge follow-up
# still records the journal entry and tells the operator how to repair it.
refresh_status=0
(cd "$PROJECT_DIR" && cortex refresh-state --path "$PROJECT_DIR") \
  || refresh_status=$?
if [ "$refresh_status" -ne 0 ]; then
  log "cortex-pr-merged-hook: cortex refresh-state --path '$PROJECT_DIR' exited $refresh_status; .cortex/state.md was not refreshed for this hook commit."
  log "  Run: cd '$PROJECT_DIR' && cortex refresh-state --path '$PROJECT_DIR' && git add .cortex/state.md && git commit -m 'docs(cortex): refresh state'"
  if [ -n "$(git -C "$PROJECT_DIR" status --porcelain -- .cortex/state.md)" ]; then
    log "cortex-pr-merged-hook: refresh-state left .cortex/state.md changed despite failing; refusing to auto-commit partial Cortex state."
    exit 1
  fi
elif [ ! -f "$PROJECT_DIR/.cortex/state.md" ]; then
  log "cortex-pr-merged-hook: cortex refresh-state succeeded but .cortex/state.md is absent; skipping state staging."
  log "  Run: cd '$PROJECT_DIR' && cortex doctor"
  if [ -n "$(git -C "$PROJECT_DIR" status --porcelain -- .cortex/state.md)" ]; then
    log "cortex-pr-merged-hook: refresh-state removed tracked .cortex/state.md; refusing to auto-commit without a Cortex state snapshot."
    exit 1
  fi
fi

# 3. Stage + commit + publish as a follow-up journal change.
rel_path="${candidate#"$PROJECT_DIR"/}"
pr_suffix=""
if [ -n "${TOUCHSTONE_MERGED_PR:-}" ]; then
  pr_suffix=" for #${TOUCHSTONE_MERGED_PR}"
fi
commit_message="${AUTO_DRAFT_SUBJECT_PREFIX}${pr_suffix}"
publish_direct=false
journal_branch=""

if truthy "${TOUCHSTONE_CORTEX_HOOK_DIRECT_PUSH:-0}" \
  || truthy "${TOUCHSTONE_CORTEX_HOOK_SKIP_PUSH:-0}"; then
  publish_direct=true
else
  journal_branch="$(resolve_journal_branch)"
  if git -C "$PROJECT_DIR" show-ref --verify --quiet "refs/heads/$journal_branch"; then
    log "cortex-pr-merged-hook: local journal branch '$journal_branch' already exists; refusing to overwrite."
    exit 1
  fi
  if git -C "$PROJECT_DIR" ls-remote --exit-code --heads origin "$journal_branch" >/dev/null 2>&1; then
    log "cortex-pr-merged-hook: remote journal branch '$journal_branch' already exists; refusing to overwrite."
    exit 1
  fi
  if ! git -C "$PROJECT_DIR" checkout -b "$journal_branch" >/dev/null; then
    log "cortex-pr-merged-hook: failed to create journal branch '$journal_branch'."
    exit 1
  fi
fi

if ! git -C "$PROJECT_DIR" add -- "$rel_path"; then
  log "cortex-pr-merged-hook: git add '$rel_path' failed."
  exit 1
fi
if [ -f "$PROJECT_DIR/.cortex/state.md" ]; then
  if ! git -C "$PROJECT_DIR" add -- .cortex/state.md; then
    log "cortex-pr-merged-hook: git add '.cortex/state.md' failed."
    exit 1
  fi
fi

# --no-verify is intentional. This is a deterministic auto-commit of a
# template-shaped journal file generated by the cortex CLI a moment ago.
# Running the normal commit hooks here adds latency and can recurse into
# review/validate paths while the merge command is still cleaning up.
if ! git -C "$PROJECT_DIR" commit --no-verify -m "$commit_message" >/dev/null; then
  log "cortex-pr-merged-hook: git commit failed."
  exit 1
fi

# Skip the push only when explicitly requested (tests use this to verify
# the local commit shape without hitting a remote).
if truthy "${TOUCHSTONE_CORTEX_HOOK_SKIP_PUSH:-0}"; then
  exit 0
fi

if [ "$publish_direct" = true ]; then
  # --no-verify is intentional here for the same reason as the commit
  # bypass above. The direct-push mode is an explicit compatibility path
  # for this deterministic auto-journal commit; without the bypass,
  # default-branch guards reject the generated post-merge push.
  if ! git_push_clean_env --no-verify origin "HEAD:${default_branch}"; then
    log "cortex-pr-merged-hook: git push to origin/${default_branch} failed."
    log "  The auto-draft entry committed locally as ${rel_path}."
    log "  Recovery: cd '$PROJECT_DIR' && TOUCHSTONE_EMERGENCY=1 git push --no-verify origin HEAD:${default_branch}"
    exit 1
  fi
  exit 0
fi

if ! git_push_clean_env -u origin "$journal_branch"; then
  git -C "$PROJECT_DIR" checkout "$default_branch" >/dev/null 2>&1 || true
  log "cortex-pr-merged-hook: git push of journal branch '$journal_branch' failed."
  log "  The auto-draft entry remains on local branch ${journal_branch}; push or delete it when ready."
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  git -C "$PROJECT_DIR" checkout "$default_branch" >/dev/null 2>&1 || true
  log "cortex-pr-merged-hook: gh CLI not found after pushing '$journal_branch'; open a PR for that branch manually."
  exit 1
fi

pr_body="$(mktemp -t cortex-pr-merged-pr.XXXXXX 2>/dev/null || mktemp)"
{
  printf 'Auto-drafted Cortex journal entry for merged PR'
  if [ -n "${TOUCHSTONE_MERGED_PR:-}" ]; then
    printf ' #%s' "$TOUCHSTONE_MERGED_PR"
  fi
  printf '.\n\n'
  printf 'This PR keeps the T1.9 journal artifact off the local default branch and lets normal repository policy merge it.\n'
} >"$pr_body"

pr_url=""
if ! pr_url="$(cd "$PROJECT_DIR" \
  && gh pr create \
    --base "$default_branch" \
    --head "$journal_branch" \
    --title "$commit_message" \
    --body-file "$pr_body" 2>/dev/null)"; then
  rm -f "$pr_body"
  git -C "$PROJECT_DIR" checkout "$default_branch" >/dev/null 2>&1 || true
  log "cortex-pr-merged-hook: failed to open PR for journal branch '$journal_branch'."
  log "  Branch was pushed; open a PR manually when ready."
  exit 1
fi
rm -f "$pr_body"

if ! git -C "$PROJECT_DIR" checkout "$default_branch" >/dev/null; then
  log "cortex-pr-merged-hook: opened journal PR but failed to restore local '$default_branch'."
  exit 1
fi
git -C "$PROJECT_DIR" branch -D "$journal_branch" >/dev/null 2>&1 || true

if [ -n "$pr_url" ]; then
  log "cortex-pr-merged-hook: opened Cortex journal PR: $pr_url"
else
  log "cortex-pr-merged-hook: opened Cortex journal PR for branch '$journal_branch'."
fi

exit 0
