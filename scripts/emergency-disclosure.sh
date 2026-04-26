#!/usr/bin/env bash
#
# hooks/emergency-disclosure.sh — Claude Code PreToolUse hook that blocks
# `git push --no-verify` invocations unless TOUCHSTONE_EMERGENCY=1 is set
# in the environment, in which case it logs the bypass for the next PR
# to disclose. Wired via .claude/settings.json from templates/.
#
# --no-verify bypasses pre-push hooks (Codex review, default-branch
# checks). Routine pushes should not bypass these — the emergency path
# is documented in principles/git-workflow.md and convention requires
# the next PR to include an "Emergency-bypass disclosure" section.
#
# Override path: set TOUCHSTONE_EMERGENCY=1 for the session. The bypass
# is appended to .touchstone/emergency-bypass.log so a follow-up PR
# template (or a future bin/touchstone status check) can surface it.
#
# Hook protocol:
#   stdin   — JSON describing the tool call
#   exit 0  — allow the tool call
#   exit 2  — block; stderr is shown to the user and surfaced to Claude
#
set -euo pipefail

input="$(cat)"

# Fast path — bail unless raw JSON contains both "git push" and "--no-verify".
if ! printf '%s' "$input" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*--no-verify\b'; then
  exit 0
fi
if ! printf '%s' "$input" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*\bgit[[:space:]]+push\b'; then
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "emergency-disclosure: jq not installed — hook bypassed (install jq to enable)" >&2
  exit 0
fi

command="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // ""')"

# Re-verify with the parsed command.
if ! printf '%s' "$command" | grep -qE '\bgit[[:space:]]+push\b'; then
  exit 0
fi
if ! printf '%s' "$command" | grep -qE '\-\-no-verify\b'; then
  exit 0
fi

if [ "${TOUCHSTONE_EMERGENCY:-0}" != "1" ]; then
  cat >&2 <<EOF
==> Blocked by Touchstone emergency-disclosure: 'git push --no-verify'

  --no-verify bypasses pre-push hooks (Codex review, default-branch
  checks). Routine pushes should not bypass these.

  This is the documented emergency path. To use it:
    1. Set TOUCHSTONE_EMERGENCY=1 in the environment for this push.
    2. The next PR you open MUST include an "Emergency-bypass disclosure"
       section explaining what was bypassed and why.

  See principles/git-workflow.md ("Emergency path").
EOF
  exit 2
fi

# Allowed under emergency override — log for the next PR to disclose.
log_dir=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
  log_dir="$cwd/.touchstone"
else
  log_dir=".touchstone"
fi
log_file="$log_dir/emergency-bypass.log"
mkdir -p "$log_dir" 2>/dev/null || true
{
  printf '%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$command"
} >> "$log_file" 2>/dev/null || true

echo "emergency-disclosure: TOUCHSTONE_EMERGENCY=1 — push allowed; logged to ${log_file#"$cwd/"} for next-PR disclosure" >&2
exit 0
