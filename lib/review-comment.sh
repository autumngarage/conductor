#!/usr/bin/env bash
#
# lib/review-comment.sh — shared PR comment helpers for review events.
#
# Public interface:
#   format_clean_review_comment <review-summary-json>
#   format_review_failure_comment <review-summary-json> <review-output> [retry-command] [failed-provider-csv]
#   format_advisory_findings_comment <review-summary-json> <review-output>
#   format_findings_history_comment <history-jsonl-path>
#   post_pr_review_comment <pr-number> <comment-string>
#   read_latest_review_event <jsonl-path>
#
set -euo pipefail

review_comment_json_field() {
  local json="$1"
  local key="$2"
  printf '%s\n' "$json" | sed -nE 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -1
}

review_comment_json_number() {
  local json="$1"
  local key="$2"
  printf '%s\n' "$json" | sed -nE 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -1
}

review_comment_clean_value() {
  local value="$1"
  value="$(printf '%s' "${value:-unknown}" | tr '\r\n\t' '   ')"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [ -n "$value" ] || value="unknown"
  printf '%s' "$value"
}

review_comment_findings_from_output() {
  printf '%s\n' "$1" | awk '/^- / { print } /^$/ { if (found) exit } /^- / { found = 1 }'
}

format_clean_review_comment() {
  local json="$1"
  local reviewer provider model peer iterations mode findings
  local fallback_primary fallback_retry fallback_reason fallback_excluded diagnostics_file diagnostics_events

  reviewer="$(review_comment_clean_value "$(review_comment_json_field "$json" reviewer)")"
  provider="$(review_comment_clean_value "$(review_comment_json_field "$json" provider)")"
  model="$(review_comment_clean_value "$(review_comment_json_field "$json" model)")"
  peer="$(review_comment_clean_value "$(review_comment_json_field "$json" peer_provider)")"
  iterations="$(review_comment_clean_value "$(review_comment_json_number "$json" iterations)")"
  mode="$(review_comment_clean_value "$(review_comment_json_field "$json" mode)")"
  findings="$(review_comment_clean_value "$(review_comment_json_number "$json" findings)")"
  fallback_primary="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_primary_provider)")"
  fallback_retry="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_retry_provider)")"
  fallback_reason="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_reason)")"
  fallback_excluded="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_excluded_providers)")"
  diagnostics_file="$(review_comment_clean_value "$(review_comment_json_field "$json" diagnostics_file)")"
  diagnostics_events="$(review_comment_clean_value "$(review_comment_json_number "$json" diagnostics_events)")"

  {
    printf '%s review clean - provider: %s, model: %s, peer: %s, iterations: %s, mode: %s, findings: %s' \
      "$reviewer" "$provider" "$model" "$peer" "$iterations" "$mode" "$findings"
    if [ "$fallback_primary" != "unknown" ] || [ "$fallback_retry" != "unknown" ] || [ "$fallback_reason" != "unknown" ]; then
      printf '\n- Fallback: `%s` -> `%s` (%s)' "$fallback_primary" "$fallback_retry" "$fallback_reason"
    fi
    if [ "$fallback_excluded" != "unknown" ]; then
      printf '\n- Fallback excluded: `%s`' "$fallback_excluded"
    fi
    if [ "$diagnostics_file" != "unknown" ]; then
      printf '\n- Diagnostics: `%s` (%s event(s))' "$diagnostics_file" "$diagnostics_events"
    fi
  }
}

format_review_failure_comment() {
  local json="$1"
  local output="${2:-}"
  local retry_command="${3:-}"
  local failed_providers="${4:-}"
  local reviewer provider model peer iterations mode findings exit_reason fallback_primary fallback_retry fallback_reason fallback_excluded
  local diagnostics_file diagnostics_events findings_block output_excerpt

  reviewer="$(review_comment_clean_value "$(review_comment_json_field "$json" reviewer)")"
  provider="$(review_comment_clean_value "$(review_comment_json_field "$json" provider)")"
  model="$(review_comment_clean_value "$(review_comment_json_field "$json" model)")"
  peer="$(review_comment_clean_value "$(review_comment_json_field "$json" peer_provider)")"
  iterations="$(review_comment_clean_value "$(review_comment_json_number "$json" iterations)")"
  mode="$(review_comment_clean_value "$(review_comment_json_field "$json" mode)")"
  findings="$(review_comment_clean_value "$(review_comment_json_number "$json" findings)")"
  exit_reason="$(review_comment_clean_value "$(review_comment_json_field "$json" exit_reason)")"
  fallback_primary="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_primary_provider)")"
  fallback_retry="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_retry_provider)")"
  fallback_reason="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_reason)")"
  fallback_excluded="$(review_comment_clean_value "$(review_comment_json_field "$json" fallback_excluded_providers)")"
  diagnostics_file="$(review_comment_clean_value "$(review_comment_json_field "$json" diagnostics_file)")"
  diagnostics_events="$(review_comment_clean_value "$(review_comment_json_number "$json" diagnostics_events)")"
  findings_block="$(review_comment_findings_from_output "$output")"
  output_excerpt="$(printf '%s\n' "$output" | sed -n '1,80p')"

  {
    if [ "${findings:-0}" != "0" ] || [ -n "$findings_block" ]; then
      printf '%s merge review blocked with concrete finding(s) - exit: %s, provider: %s, model: %s, peer: %s, iterations: %s, mode: %s, findings: %s\n\n' \
        "$reviewer" "$exit_reason" "$provider" "$model" "$peer" "$iterations" "$mode" "$findings"
    else
      printf '%s merge review failed before a trusted clean verdict - exit: %s, provider: %s, model: %s, peer: %s, iterations: %s, mode: %s, findings: %s\n\n' \
        "$reviewer" "$exit_reason" "$provider" "$model" "$peer" "$iterations" "$mode" "$findings"
    fi

    if [ -n "$failed_providers" ]; then
      printf -- '- Failed/stalled provider(s): `%s`\n' "$failed_providers"
    fi
    if [ "$fallback_primary" != "unknown" ] || [ "$fallback_retry" != "unknown" ] || [ "$fallback_reason" != "unknown" ]; then
      printf -- '- Fallback: `%s` -> `%s` (%s)\n' "$fallback_primary" "$fallback_retry" "$fallback_reason"
    fi
    if [ "$fallback_excluded" != "unknown" ]; then
      printf -- '- Fallback excluded: `%s`\n' "$fallback_excluded"
    fi
    if [ "$diagnostics_file" != "unknown" ]; then
      printf -- '- Diagnostics: `%s` (%s event(s))\n' "$diagnostics_file" "$diagnostics_events"
    fi
    if [ -n "$retry_command" ]; then
      printf -- '- Retry: `%s`\n' "$retry_command"
    fi

    if [ -n "$findings_block" ]; then
      printf '\n%s\n' "$findings_block"
    elif [ -n "$output_excerpt" ]; then
      printf '\n<details>\n<summary>Review transcript excerpt</summary>\n\n```text\n%s\n```\n</details>\n' "$output_excerpt"
    fi
  }
}

format_advisory_findings_comment() {
  local json="$1"
  local output="$2"
  local reviewer provider model iterations mode findings findings_block

  reviewer="$(review_comment_clean_value "$(review_comment_json_field "$json" reviewer)")"
  provider="$(review_comment_clean_value "$(review_comment_json_field "$json" provider)")"
  model="$(review_comment_clean_value "$(review_comment_json_field "$json" model)")"
  iterations="$(review_comment_clean_value "$(review_comment_json_number "$json" iterations)")"
  mode="$(review_comment_clean_value "$(review_comment_json_field "$json" mode)")"
  findings="$(review_comment_clean_value "$(review_comment_json_number "$json" findings)")"
  findings_block="$(review_comment_findings_from_output "$output")"

  {
    printf '%s advisory review found %s finding(s) - provider: %s, model: %s, iterations: %s, mode: %s\n\n' \
      "$reviewer" "$findings" "$provider" "$model" "$iterations" "$mode"
    if [ -n "$findings_block" ]; then
      printf '%s\n' "$findings_block"
    else
      printf 'Review exited with findings, but no bullet-form findings were parsed from reviewer output.\n'
    fi
  }
}

format_findings_history_comment() {
  local history_path="$1"
  local actionable_count total_count

  [ -f "$history_path" ] || return 1
  actionable_count="$(grep -E -c '"result":"CODEX_REVIEW_(BLOCKED|FIXED)"' "$history_path" 2>/dev/null || true)"
  total_count="$(grep -c '"schema":"touchstone.review.findings_history.v1"' "$history_path" 2>/dev/null || true)"
  [ "${actionable_count:-0}" -gt 0 ] || return 1

  {
    printf '<details>\n'
    printf '<summary>Conductor review findings history (%s actionable iteration(s), %s total)</summary>\n\n' "$actionable_count" "$total_count"
    awk '
      function field(line, key, pattern, value) {
        pattern = "\"" key "\":\"(([^\"\\\\]|\\\\.)*)\""
        if (match(line, pattern)) {
          value = substr(line, RSTART + length(key) + 4, RLENGTH - length(key) - 5)
          gsub(/\\n/, "\n", value)
          gsub(/\\"/, "\"", value)
          gsub(/\\\\/, "\\", value)
          return value
        }
        return ""
      }
      function number_field(line, key, pattern, value) {
        pattern = "\"" key "\":[0-9]+"
        if (match(line, pattern)) {
          value = substr(line, RSTART + length(key) + 3, RLENGTH - length(key) - 3)
          return value
        }
        return "0"
      }
      {
        result = field($0, "result")
        if (result == "") {
          next
        }
        iteration = number_field($0, "iteration")
        findings_count = number_field($0, "findings_count")
        auto_fixed_count = number_field($0, "auto_fixed_count")
        head = field($0, "head")
        commits = field($0, "commits_since_prior")
        findings = field($0, "findings")

        printf "### Iteration %s: %s\n\n", iteration, result
        printf "- Findings raised: %s\n", findings_count
        printf "- Auto-fixed by reviewer: %s\n", auto_fixed_count
        if (commits != "") {
          printf "- Commits since prior finding: %s\n", commits
        }
        if (head != "") {
          printf "- Reviewed HEAD: `%s`\n", head
        }
        if (findings != "") {
          printf "\n%s\n", findings
        }
        printf "\n"
      }
    ' "$history_path"
    printf '</details>\n'
  }
}

post_pr_review_comment() {
  local pr_number="$1"
  local comment="$2"

  gh pr comment "$pr_number" --body "$comment"
}

read_latest_review_event() {
  local log_path="$1"
  [ -f "$log_path" ] || return 1
  tail -n 1 "$log_path"
}
