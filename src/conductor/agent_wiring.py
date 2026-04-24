"""Agent-integration wiring for `conductor init`.

Detects agent instruction files (CLAUDE.md, AGENTS.md, …) and writes
managed artifacts — a canonical delegation-guidance file, a slash command,
subagent definitions — so Claude Code (and, in later slices, Codex / Cursor /
Gemini CLI) can delegate to Conductor providers without the user
hand-wiring anything.

Two file-ownership conventions:
  1. Fully managed files carry a `managed-by: conductor vX.Y.Z` marker in
     their first line (HTML comment) or in YAML frontmatter. On unwire
     they are deleted whole.
  2. Sentinel-block injection into user-owned files (CLAUDE.md, AGENTS.md):
     a conductor block bounded by ``<!-- conductor:begin vX -->`` and
     ``<!-- conductor:end -->``. Unwire removes only the block; user
     content outside the markers is untouched.

Paths can be overridden for tests via the ``CONDUCTOR_HOME`` and
``CLAUDE_HOME`` env vars — otherwise the standard user home locations are
used.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Marker formats — the two shapes of "conductor owns this".
# --------------------------------------------------------------------------- #

MANAGED_COMMENT_PREFIX = "<!-- managed-by: conductor v"
MANAGED_FRONTMATTER_KEY = "managed-by: conductor v"

SENTINEL_BEGIN_PREFIX = "<!-- conductor:begin v"
SENTINEL_END = "<!-- conductor:end -->"

_SENTINEL_BLOCK_RE = re.compile(
    r"(?:\n?)" + re.escape("<!-- conductor:begin v") + r"[^>]*-->\n"
    r".*?\n" + re.escape(SENTINEL_END) + r"\n?",
    flags=re.DOTALL,
)


def _managed_comment(version: str) -> str:
    return (
        f"<!-- managed-by: conductor v{version} — "
        f"do not edit; run `conductor init --unwire` to remove -->"
    )


def _sentinel_begin(version: str) -> str:
    return f"<!-- conductor:begin v{version} -->"


# --------------------------------------------------------------------------- #
# Paths — ~/.conductor/ and ~/.claude/ with test-time overrides.
# --------------------------------------------------------------------------- #


def conductor_home() -> Path:
    """Where conductor's canonical guidance lives (``~/.conductor/``)."""
    override = os.environ.get("CONDUCTOR_HOME")
    if override:
        return Path(override)
    return Path.home() / ".conductor"


def claude_home() -> Path:
    """Claude Code's user-scoped config directory (``~/.claude/``)."""
    override = os.environ.get("CLAUDE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude"


# --------------------------------------------------------------------------- #
# Detection — what agent surface exists in this environment?
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentArtifact:
    """One managed or candidate artifact conductor can write/has written."""

    path: Path
    kind: str                 # "guidance" | "slash-command" | "subagent" | "claude-md-import"
    version: str | None       # present if currently managed
    owned: bool               # True if conductor-managed right now


@dataclass(frozen=True)
class Detection:
    """Result of scanning the environment for Claude Code + instruction files."""

    claude_cli_on_path: bool
    claude_home: Path
    claude_home_exists: bool
    claude_user_md: Path             # ~/.claude/CLAUDE.md — may or may not exist
    claude_user_md_exists: bool
    conductor_home: Path
    agents_md: Path                  # ./AGENTS.md in cwd — may or may not exist
    agents_md_exists: bool
    gemini_md: Path                  # ./GEMINI.md in cwd
    gemini_md_exists: bool
    claude_md_repo: Path              # ./CLAUDE.md in cwd (repo-scope)
    claude_md_repo_exists: bool
    cursor_rules_dir: Path            # ./.cursor/rules/ in cwd
    cursor_rules_dir_exists: bool
    managed: tuple[AgentArtifact, ...] = field(default_factory=tuple)

    @property
    def claude_detected(self) -> bool:
        """True if Claude Code is installed or has been configured before."""
        return self.claude_cli_on_path or self.claude_home_exists


def detect(*, cwd: Path | None = None) -> Detection:
    """Scan the current environment for Claude Code + any managed files.

    Args:
        cwd: where to look for repo-scoped files (AGENTS.md, GEMINI.md,
            CLAUDE.md, .cursor/rules/). Defaults to the process's cwd;
            tests pass an explicit ``tmp_path`` for isolation.
    """
    ch = claude_home()
    ch_exists = ch.is_dir()
    cli_on_path = shutil.which("claude") is not None
    user_md = ch / "CLAUDE.md"
    work_dir = cwd or Path.cwd()
    agents_md = work_dir / "AGENTS.md"
    gemini_md = work_dir / "GEMINI.md"
    claude_md_repo = work_dir / "CLAUDE.md"
    cursor_rules_dir = work_dir / ".cursor" / "rules"

    managed: list[AgentArtifact] = []

    # Fully-managed files: read the managed-by marker; skip anything we
    # didn't write.
    for path, kind in _candidate_artifacts(cwd=work_dir).items():
        if not path.exists():
            continue
        version = read_managed_version(path)
        if version is None:
            continue
        managed.append(AgentArtifact(path=path, kind=kind, version=version, owned=True))

    # Sentinel-block files (user-owned; only our block is ours).
    for path, kind in _candidate_sentinel_paths(cwd=work_dir).items():
        if not path.exists() or not _has_sentinel_block(path):
            continue
        managed.append(
            AgentArtifact(path=path, kind=kind, version=_block_version(path), owned=True)
        )

    return Detection(
        claude_cli_on_path=cli_on_path,
        claude_home=ch,
        claude_home_exists=ch_exists,
        claude_user_md=user_md,
        claude_user_md_exists=user_md.exists(),
        conductor_home=conductor_home(),
        agents_md=agents_md,
        agents_md_exists=agents_md.exists(),
        gemini_md=gemini_md,
        gemini_md_exists=gemini_md.exists(),
        claude_md_repo=claude_md_repo,
        claude_md_repo_exists=claude_md_repo.exists(),
        cursor_rules_dir=cursor_rules_dir,
        cursor_rules_dir_exists=cursor_rules_dir.is_dir(),
        managed=tuple(managed),
    )


def _candidate_artifacts(*, cwd: Path | None = None) -> dict[Path, str]:
    """Fully-managed paths conductor may own (written whole, deleted whole)."""
    ch = claude_home()
    conductor = conductor_home()
    cwd = cwd or Path.cwd()
    return {
        conductor / "delegation-guidance.md": "guidance",
        ch / "commands" / "conductor.md": "slash-command",
        ch / "agents" / "kimi-long-context.md": "subagent",
        ch / "agents" / "gemini-web-search.md": "subagent",
        ch / "agents" / "codex-coding-agent.md": "subagent",
        ch / "agents" / "ollama-offline.md": "subagent",
        ch / "agents" / "conductor-auto.md": "subagent",
        cwd / ".cursor" / "rules" / "conductor-delegation.md": "cursor-rule",
    }


def _candidate_sentinel_paths(*, cwd: Path | None = None) -> dict[Path, str]:
    """User-owned files where conductor injects sentinel blocks only.

    User-scope (cwd-independent):
      - ``~/.claude/CLAUDE.md`` — Claude Code global config.

    Repo-scope (under ``cwd``):
      - ``./AGENTS.md``  — Codex / Cursor / Zed / shared convention.
      - ``./GEMINI.md``  — Gemini CLI convention.
      - ``./CLAUDE.md``  — Claude Code repo-scope config.
    """
    cwd = cwd or Path.cwd()
    return {
        claude_home() / "CLAUDE.md": "claude-md-import",
        cwd / "AGENTS.md": "agents-md-import",
        cwd / "GEMINI.md": "gemini-md-import",
        cwd / "CLAUDE.md": "claude-md-repo-import",
    }


# --------------------------------------------------------------------------- #
# Managed-file helpers — write/read/detect ownership.
# --------------------------------------------------------------------------- #


def read_managed_version(path: Path) -> str | None:
    """Return the conductor version that owns ``path``, or None if unmanaged.

    Accepts either an HTML-comment marker on line 1 or a ``managed-by:``
    key inside a YAML frontmatter block at the top of the file.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text:
        return None

    first_line = text.splitlines()[0]
    if first_line.startswith(MANAGED_COMMENT_PREFIX):
        return _extract_version(first_line, MANAGED_COMMENT_PREFIX)

    # Try YAML frontmatter.
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            block = text[4:end]
            for line in block.splitlines():
                line = line.strip()
                if line.startswith(MANAGED_FRONTMATTER_KEY):
                    return _extract_version(line, MANAGED_FRONTMATTER_KEY)
    return None


def is_managed_file(path: Path) -> bool:
    return read_managed_version(path) is not None


def write_managed_markdown(path: Path, body: str, *, version: str) -> None:
    """Write ``body`` to ``path`` with a managed-by HTML comment prepended.

    Overwrites only if the file is already conductor-managed or does not
    exist. User-owned files at the path are left alone; caller decides
    what to do about that (typically: warn and skip).
    """
    if path.exists() and not is_managed_file(path):
        raise UserOwnedFileError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _managed_comment(version) + "\n"
    path.write_text(header + body, encoding="utf-8")


def write_managed_frontmatter(
    path: Path,
    frontmatter: dict[str, str],
    body: str,
    *,
    version: str,
) -> None:
    """Write a file with YAML frontmatter; inject ``managed-by`` key.

    Used for Claude Code artifacts (slash commands, subagents) where the
    file format is frontmatter-then-body.
    """
    if path.exists() and not is_managed_file(path):
        raise UserOwnedFileError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if key == "managed-by":
            continue
        lines.append(f"{key}: {value}")
    # managed-by last: the user sees name/description/etc. first when they
    # open the file; our marker sits at the bottom of the frontmatter.
    lines.append(f"managed-by: conductor v{version}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")


class UserOwnedFileError(Exception):
    """Raised when a path conductor would write to exists and is not managed."""

    def __init__(self, path: Path):
        super().__init__(
            f"{path} exists but is not conductor-managed; refusing to overwrite"
        )
        self.path = path


# --------------------------------------------------------------------------- #
# Sentinel-block helpers — inject into user-owned files (CLAUDE.md, AGENTS.md).
# --------------------------------------------------------------------------- #


def _has_sentinel_block(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return SENTINEL_BEGIN_PREFIX in text and SENTINEL_END in text


def _block_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    idx = text.find(SENTINEL_BEGIN_PREFIX)
    if idx == -1:
        return None
    rest = text[idx + len(SENTINEL_BEGIN_PREFIX) :]
    end = rest.find(" -->")
    if end == -1:
        return None
    return rest[:end].strip()


def inject_sentinel_block(path: Path, content: str, *, version: str) -> None:
    """Add or replace conductor's sentinel block in ``path``.

    If the file doesn't exist, it's created with only the block. If the
    file exists without a block, the block is appended (preserving all
    existing content). If the file already has a conductor block, the
    block is replaced in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    block = (
        f"{_sentinel_begin(version)}\n"
        f"{content.rstrip()}\n"
        f"{SENTINEL_END}\n"
    )

    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return

    existing = path.read_text(encoding="utf-8")
    if _SENTINEL_BLOCK_RE.search(existing):
        # Replace in place (including its trailing newline), keeping the
        # single-trailing-newline invariant.
        replaced = _SENTINEL_BLOCK_RE.sub("\n" + block, existing, count=1)
        path.write_text(replaced, encoding="utf-8")
        return

    # No existing block — append with a leading blank line for legibility.
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + block, encoding="utf-8")


def remove_sentinel_block(path: Path) -> bool:
    """Remove conductor's sentinel block from ``path`` if present.

    Returns True if a block was removed. If the file becomes empty as a
    result (i.e. the block was the only content), the file is deleted.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if not _SENTINEL_BLOCK_RE.search(text):
        return False
    trimmed = _SENTINEL_BLOCK_RE.sub("", text, count=1)
    # Normalize: collapse any run of 3+ newlines produced by the removal.
    trimmed = re.sub(r"\n{3,}", "\n\n", trimmed)
    trimmed = trimmed.lstrip("\n")
    if trimmed.strip() == "":
        path.unlink()
    else:
        path.write_text(trimmed, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Unwire — remove every managed artifact conductor knows about.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnwireReport:
    removed: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]  # (path, reason)


def unwire(*, cwd: Path | None = None) -> UnwireReport:
    """Remove every managed file / sentinel block conductor has written.

    Fully-managed files (guidance, slash command, subagents) are deleted
    only if their header still marks them as conductor-managed. User-owned
    files (CLAUDE.md, AGENTS.md) have their sentinel block removed; the
    rest of the file is left alone.

    ``cwd`` controls where ``AGENTS.md`` is looked for. Defaults to the
    process's current working directory; callers running unwire across
    multiple repos must invoke from each repo (we never walk the tree).
    """
    removed: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    # Fully-managed files: delete whole, but only if marker still matches.
    for path, _kind in _candidate_artifacts(cwd=cwd).items():
        if not path.exists():
            continue
        if not is_managed_file(path):
            skipped.append((path, "not conductor-managed (hand-edited?)"))
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as e:
            skipped.append((path, f"unlink failed: {e}"))

    # Sentinel-block files (user-scope + repo-scope): strip the block only.
    for path, _kind in _candidate_sentinel_paths(cwd=cwd).items():
        if path.exists() and remove_sentinel_block(path):
            removed.append(path)

    return UnwireReport(removed=tuple(removed), skipped=tuple(skipped))


# --------------------------------------------------------------------------- #
# Wire — write every managed artifact for Claude Code.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WireReport:
    written: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]
    patched_claude_md: bool


def wire_claude_code(
    version: str,
    *,
    patch_claude_md: bool,
) -> WireReport:
    """Write the Slice A managed artifacts for Claude Code.

    Args:
        version: conductor version, stamped into every managed-by marker.
        patch_claude_md: if True, inject the ``@~/.conductor/…`` import
            line into ``~/.claude/CLAUDE.md`` via the sentinel block. If
            False, skip the CLAUDE.md edit (caller will print instructions
            for the user to do it manually).

    The caller is expected to have obtained consent before invoking this.
    """
    from conductor import _agent_templates as templates

    conductor_dir = conductor_home()
    ch = claude_home()

    written: list[Path] = []
    skipped: list[tuple[Path, str]] = []

    def _write_md(path: Path, body: str) -> None:
        try:
            write_managed_markdown(path, body, version=version)
            written.append(path)
        except UserOwnedFileError as e:
            skipped.append((e.path, "user-owned file; refusing to overwrite"))

    def _write_fm(path: Path, fm: dict[str, str], body: str) -> None:
        try:
            write_managed_frontmatter(path, fm, body, version=version)
            written.append(path)
        except UserOwnedFileError as e:
            skipped.append((e.path, "user-owned file; refusing to overwrite"))

    _write_md(conductor_dir / "delegation-guidance.md", templates.DELEGATION_GUIDANCE)

    _write_fm(
        ch / "commands" / "conductor.md",
        {
            "description": "Delegate a task to another LLM via the conductor CLI.",
            "argument-hint": "<provider> <task…>",
        },
        templates.SLASH_COMMAND_CONDUCTOR,
    )
    _write_fm(
        ch / "agents" / "kimi-long-context.md",
        {
            "name": "kimi-long-context",
            "description": (
                "Use for summarizing or analyzing large files / long contexts "
                "where a cheaper strong-tier model suffices. Delegates to Kimi "
                "via `conductor call --with kimi`."
            ),
            "tools": "Bash",
        },
        templates.SUBAGENT_KIMI_LONG_CONTEXT,
    )
    _write_fm(
        ch / "agents" / "gemini-web-search.md",
        {
            "name": "gemini-web-search",
            "description": (
                "Use for questions needing fresh web information. Delegates to "
                "Gemini (which has native web search) via `conductor call --with gemini`."
            ),
            "tools": "Bash",
        },
        templates.SUBAGENT_GEMINI_WEB_SEARCH,
    )
    _write_fm(
        ch / "agents" / "codex-coding-agent.md",
        {
            "name": "codex-coding-agent",
            "description": (
                "Use for heavy multi-file coding tasks where a tool-using agent "
                "loop is expected. Delegates to OpenAI Codex via `conductor exec "
                "--with codex` in a workspace-write sandbox."
            ),
            "tools": "Bash",
        },
        templates.SUBAGENT_CODEX_CODING_AGENT,
    )
    _write_fm(
        ch / "agents" / "ollama-offline.md",
        {
            "name": "ollama-offline",
            "description": (
                "Use for privacy-sensitive or offline-only tasks. Delegates to a "
                "local Ollama model via `conductor call --with ollama` — nothing "
                "leaves the machine."
            ),
            "tools": "Bash",
        },
        templates.SUBAGENT_OLLAMA_OFFLINE,
    )
    _write_fm(
        ch / "agents" / "conductor-auto.md",
        {
            "name": "conductor-auto",
            "description": (
                "Use when delegating but unsure which provider fits. Lets "
                "conductor's auto-router pick by task tags via `conductor call "
                "--auto --tags <…>`."
            ),
            "tools": "Bash",
        },
        templates.SUBAGENT_CONDUCTOR_AUTO,
    )

    patched = False
    if patch_claude_md:
        import_line = f"@{conductor_dir}/delegation-guidance.md"
        inject_sentinel_block(ch / "CLAUDE.md", import_line, version=version)
        patched = True

    return WireReport(
        written=tuple(written),
        skipped=tuple(skipped),
        patched_claude_md=patched,
    )


@dataclass(frozen=True)
class AgentsMdReport:
    path: Path
    patched: bool


def wire_agents_md(cwd: Path | None = None, *, version: str) -> AgentsMdReport:
    """Inject conductor's delegation block into ``./AGENTS.md`` in ``cwd``.

    AGENTS.md is the cross-tool convention (Codex, Cursor, Zed, …). It has
    no ``@`` import mechanism, so we inline the block via the sentinel
    pattern — user content outside the markers is preserved.

    The block content is self-contained (a quick reference plus a
    pointer to ``~/.conductor/delegation-guidance.md`` for the full
    text), so an agent that reads only AGENTS.md still gets enough.
    """
    from conductor import _agent_templates as templates

    cwd = cwd or Path.cwd()
    path = cwd / "AGENTS.md"
    inject_sentinel_block(path, templates.AGENTS_MD_BLOCK, version=version)
    return AgentsMdReport(path=path, patched=True)


def wire_gemini_md(cwd: Path | None = None, *, version: str) -> AgentsMdReport:
    """Inject conductor's delegation block into ``./GEMINI.md`` in ``cwd``.

    Gemini CLI reads GEMINI.md. Like AGENTS.md, no ``@`` import mechanism,
    so the block is inlined with the sentinel pattern.
    """
    from conductor import _agent_templates as templates

    cwd = cwd or Path.cwd()
    path = cwd / "GEMINI.md"
    inject_sentinel_block(path, templates.GEMINI_MD_BLOCK, version=version)
    return AgentsMdReport(path=path, patched=True)


def wire_claude_md_repo(cwd: Path | None = None, *, version: str) -> AgentsMdReport:
    """Inject an inline delegation block into repo-scope ``./CLAUDE.md``.

    Uses inlined content (identical body to AGENTS.md / GEMINI.md), NOT the
    ``@`` import used by ``wire_claude_code(patch_claude_md=True)`` for
    user-scope ``~/.claude/CLAUDE.md``.

    Why: repo-scope CLAUDE.md is typically tracked in git. An ``@``-import
    pointing at ``/Users/<name>/.conductor/…`` would travel into every other
    contributor's checkout and fail to resolve on their machine. Inline
    content is self-contained markdown that works regardless of where
    ``~/.conductor/`` lives on any given machine — the loss is that repos
    don't auto-pick-up upstream guidance changes the way user-scope does,
    but that's the correct trade for shared files.
    """
    from conductor import _agent_templates as templates

    cwd = cwd or Path.cwd()
    path = cwd / "CLAUDE.md"
    inject_sentinel_block(path, templates.AGENTS_MD_BLOCK, version=version)
    return AgentsMdReport(path=path, patched=True)


def wire_cursor(cwd: Path | None = None, *, version: str) -> AgentsMdReport:
    """Write Cursor's conductor-delegation rule at
    ``<cwd>/.cursor/rules/conductor-delegation.md``.

    Unlike the markdown instruction files above, Cursor rules are
    fully-managed — the file is conductor's whole (YAML frontmatter
    with ``description`` / ``globs`` / ``alwaysApply`` keys plus a body
    rendered from ``CURSOR_RULE_BODY``).
    """
    from conductor import _agent_templates as templates

    cwd = cwd or Path.cwd()
    path = cwd / ".cursor" / "rules" / "conductor-delegation.md"
    write_managed_frontmatter(
        path,
        {
            "description": (
                "Use when delegating or routing tasks to a different LLM via "
                "the conductor CLI — e.g., cheap second opinions, long-context "
                "reads, web search, or offline runs."
            ),
            "globs": "",
            "alwaysApply": "false",
        },
        templates.CURSOR_RULE_BODY,
        version=version,
    )
    return AgentsMdReport(path=path, patched=True)


# --------------------------------------------------------------------------- #
# Utility — extract a version string from a marker line.
# --------------------------------------------------------------------------- #


def _extract_version(line: str, prefix: str) -> str | None:
    after = line[len(prefix) :]
    # Stop at whitespace, a trailing HTML comment close, or punctuation.
    m = re.match(r"[^\s\-—>]+", after)
    return m.group(0) if m else None
