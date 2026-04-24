"""Template bodies for conductor's managed agent-integration artifacts.

Embedded as string literals (rather than package data files) so conductor
has no packaging-time dependency on non-Python resources. The `wizard`
flow stamps each artifact with the running conductor version in its
managed-by header.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Canonical delegation guidance — imported into ~/.claude/CLAUDE.md.
# --------------------------------------------------------------------------- #

DELEGATION_GUIDANCE = """# Conductor delegation

Conductor exposes other LLMs behind a uniform CLI (`conductor call`,
`conductor exec`). When a task is a better fit for a different model
than the one you're running as, delegate: run conductor, read back the
answer, present it to the user with attribution.

## When to delegate

**DO delegate when:**
- The task is **long-context reading or summarization** over a large file
  or many files (>50 KB combined). Kimi and Gemini are stronger per-dollar
  for broad reading than most flagship models.
- The task needs **fresh web information** — Gemini is the only conductor
  provider with native web search.
- The task is a **cheap second opinion** on a diff, a piece of code, or a
  design choice. Kimi gives you a fast, low-cost sanity check.
- The task is **privacy-sensitive** and should not leave the machine.
  Ollama runs locally.

**DON'T delegate when:**
- The task is mid-conversation reasoning where you hold active context.
  Passing it off loses that context.
- The user addressed *you* specifically.
- The task is short enough that the round-trip adds more latency than
  value.

## How to delegate

Single-turn call:

    conductor call --with <provider> --task "..."

Let the router pick by tags:

    conductor call --auto --tags long-context,cheap --task "..."

Pipe content in as the task:

    cat long-file.md | conductor call --with kimi --task "Summarize."

Multi-turn agent session with tools (inside a sandbox):

    conductor exec --with <provider> --tools Read,Grep,Edit \\
        --sandbox workspace-write --task "..."

Get JSON for scripting / piping into other tools:

    conductor call --with kimi --task "..." --json

## Providers at a glance

| Provider | Best for                     | Cost   | Notes                        |
|----------|------------------------------|--------|------------------------------|
| kimi     | long-context, cheap reviews  | $      | Cloudflare Workers AI        |
| gemini   | web search, multimodal       | $$     | Google AI Studio or gcloud   |
| claude   | strongest reasoning          | $$$    | your Claude subscription     |
| codex    | coding agent                 | $$$    | your ChatGPT subscription    |
| ollama   | private, offline             | free   | runs locally                 |

Discover what's currently configured:

    conductor list

## Subagents available

Conductor installs subagent definitions at `~/.claude/agents/`. Dispatch
to them via the Agent tool (`subagent_type`) for a cleaner delegation
than direct Bash calls.

- `kimi-long-context` — long-document summarization / broad reading
- `gemini-web-search` — questions needing fresh web information

For other providers, use the Bash path or the `/conductor` slash
command directly.

## Error handling

Conductor surfaces structured errors. When they come back, relay them
verbatim — most are user-actionable (missing API key, provider not
installed, rate limited). Don't paper over a `no provider...` error by
answering the question yourself; tell the user to run `conductor init`.
"""


# --------------------------------------------------------------------------- #
# /conductor slash command — loaded as a prompt with $ARGUMENTS substituted.
# --------------------------------------------------------------------------- #

SLASH_COMMAND_CONDUCTOR = """The user invoked `/conductor` with arguments:

$ARGUMENTS

The first token is the target — a provider name (`kimi`, `claude`,
`codex`, `gemini`, `ollama`) or the literal `auto` to let conductor's
router pick. Everything after the first token is the task.

Run the task through conductor using the Bash tool:

- If the first token is a provider name:

      conductor call --with <provider> --task "<the rest>"

- If the first token is `auto`:

      conductor call --auto --task "<the rest>"

- If the task clearly needs file tools (editing, grep, long-running
  agent work), prefer:

      conductor exec --with <provider> --tools Read,Grep,Edit \\
          --sandbox workspace-write --task "<the rest>"

Capture the provider's response. Present it to the user with a brief
"(from <provider>)" attribution. If conductor returns an error, show
the error verbatim — don't substitute your own answer.

If the user's arguments are ambiguous (e.g. just a task with no
provider), ask which provider to use before running anything.
"""


# --------------------------------------------------------------------------- #
# Subagent bodies — invoked via Claude Code's Agent tool.
# --------------------------------------------------------------------------- #

SUBAGENT_KIMI_LONG_CONTEXT = """You are a delegation subagent. Your job is to route
long-context reading tasks to Kimi via the `conductor` CLI and return the
answer — NOT to answer them yourself.

When invoked:

1. Take the user's task as given.
2. If the task references files, read them (use the Bash tool) and include
   the relevant contents in the prompt you pass to Kimi. Kimi supports
   1M-token contexts; you rarely need to truncate.
3. Run:

       conductor call --with kimi --task "<prompt>" --json

   Pipe content via stdin if the prompt is large:

       cat <file> | conductor call --with kimi --task "Summarize." --json

4. Parse the JSON, extract the `text` field, and return it verbatim
   prefixed with "From Kimi:". Include the `model` and `duration_ms` from
   the JSON as a one-line footer for transparency.

Kimi is strongest for: summarization, broad-reading across many files,
structural extraction from long transcripts, and cheap second-opinion
reviews.

If conductor errors:
- `no provider...` → kimi isn't configured. Tell the user to run
  `conductor init`.
- `rate-limited` → report the cooldown window; suggest retry.
- HTTP / network errors → pass through verbatim.

Never fall back to answering from your own training data. If Kimi isn't
available, say so plainly rather than substituting your own reasoning —
the user asked for Kimi specifically.
"""


SUBAGENT_GEMINI_WEB_SEARCH = """You are a delegation subagent. Your job is to route
web-search-requiring tasks to Gemini via the `conductor` CLI and return
the answer.

Gemini is the only conductor provider with native web search. Tasks that
need fresh information from the live web — news, recent docs, package
versions, live service status, anything your training data is stale on —
should go through you.

When invoked:

1. Craft a prompt that explicitly asks Gemini to use web search and cite
   its sources inline.
2. Run:

       conductor call --with gemini --task "<prompt>" --json

3. Parse the JSON, extract the `text` field, and return it verbatim
   prefixed with "From Gemini:". Preserve any URLs or citations Gemini
   includes — do not rewrite them.

If the user's question doesn't actually need the web (it's a coding
task, a reasoning task, a summary of material they already provided),
tell the parent agent to handle it directly instead of calling you —
you exist specifically for the web-search path.

If conductor errors:
- `no provider...` → gemini isn't configured. Tell the user to run
  `conductor init`.
- Rate limit / quota → report the cooldown or daily cap.
- HTTP / network errors → pass through verbatim.

Never fall back to answering from your own training data for a task
that needs current information. If Gemini isn't available, say so
plainly — stale answers labeled as fresh are worse than an explicit
"I can't reach Gemini right now."
"""
