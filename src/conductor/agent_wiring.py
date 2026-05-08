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

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion
from packaging.version import parse as parse_version

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

_PROJECT_MARKERS = frozenset(
    {
        ".git",
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
    }
)
_REPO_SCOPED_KINDS = frozenset(
    {
        "agents-md-import",
        "gemini-md-import",
        "claude-md-repo-import",
        "cursor-rule",
    }
)
_USER_SCOPED_KINDS = frozenset(
    {
        "guidance",
        "slash-command",
        "subagent",
        "claude-md-import",
    }
)
_VERSION_SCAN_BYTES = 512


@dataclass(frozen=True)
class UserScopeVersionDecision:
    path: Path
    kind: str
    version: str | None
    stale: bool
    reason: str


@dataclass(frozen=True)
class RepoScopeVersionDecision:
    path: Path
    kind: str
    version: str | None
    stale: bool
    reason: str


@dataclass(frozen=True)
class RefreshReport:
    refreshed: tuple[Path, ...]
    skipped: tuple[tuple[Path, str], ...]


def _managed_comment(version: str) -> str:
    version = _canonical_wiring_version(version)
    return (
        f"<!-- managed-by: conductor v{version} — "
        f"do not edit; run `conductor init --unwire` to remove -->"
    )


def _sentinel_begin(version: str) -> str:
    version = _canonical_wiring_version(version)
    return f"<!-- conductor:begin v{version} -->"


def _normalize_generated_text(text: str) -> str:
    """Strip line-end whitespace from conductor-generated artifacts."""
    return "\n".join(line.rstrip(" \t") for line in text.splitlines()) + "\n"


def _frontmatter_line(key: str, value: str) -> str:
    if value == "":
        return f'{key}: ""'
    return f"{key}: {value}"


def _canonical_wiring_version(version: str) -> str:
    value = version.strip()
    if re.match(r"^v\d", value):
        value = value[1:]
    return value.split("+", 1)[0]


def _wiring_versions_match(left: str, right: str) -> bool:
    return _canonical_wiring_version(left) == _canonical_wiring_version(right)


def _wiring_version_is_older(version: str | None, *, binary_version: str) -> bool:
    if version is None:
        return False
    current = parse_version(_canonical_wiring_version(binary_version))
    try:
        artifact_version = parse_version(_canonical_wiring_version(version))
    except InvalidVersion:
        return True
    return artifact_version < current


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
        cwd / ".cursor" / "rules" / "conductor-delegation.mdc": "cursor-rule",
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


def _user_scope_candidate_artifacts() -> dict[Path, str]:
    """User-scope files conductor refreshes automatically after upgrades."""
    return {
        path: kind
        for path, kind in _candidate_artifacts().items()
        if kind in _USER_SCOPED_KINDS
    }


def _user_scope_candidate_sentinel_paths() -> dict[Path, str]:
    return {
        path: kind
        for path, kind in _candidate_sentinel_paths().items()
        if kind in _USER_SCOPED_KINDS
    }


def _repo_scope_candidate_artifacts(cwd: Path) -> dict[Path, str]:
    return {
        path: kind
        for path, kind in _candidate_artifacts(cwd=cwd).items()
        if kind in _REPO_SCOPED_KINDS
    }


def _repo_scope_candidate_sentinel_paths(cwd: Path) -> dict[Path, str]:
    return {
        path: kind
        for path, kind in _candidate_sentinel_paths(cwd=cwd).items()
        if kind in _REPO_SCOPED_KINDS
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


def user_scope_version_decisions(
    *,
    binary_version: str,
) -> tuple[UserScopeVersionDecision, ...]:
    """Scan user-scope managed files for stale conductor version stamps.

    The startup auto-refresh path calls this before eligible CLI commands, so
    it intentionally reads only a small prefix from each candidate file.
    Missing or user-owned files are not stale; only conductor-managed stamps
    older than the running binary trigger a refresh.
    """
    decisions: list[UserScopeVersionDecision] = []
    for path, kind in _user_scope_candidate_artifacts().items():
        version = _read_managed_version_prefix(path)
        decisions.append(
            _user_scope_decision(
                path=path,
                kind=kind,
                version=version,
                exists=path.is_file(),
                binary_version=binary_version,
            )
        )
    for path, kind in _user_scope_candidate_sentinel_paths().items():
        version = _read_sentinel_version_prefix(path)
        decisions.append(
            _user_scope_decision(
                path=path,
                kind=kind,
                version=version,
                exists=path.is_file(),
                binary_version=binary_version,
            )
        )
    return tuple(decisions)


def is_user_scope_stale(*, binary_version: str) -> bool:
    """Return True when any user-scope conductor-managed file is stale."""
    return any(
        decision.stale
        for decision in user_scope_version_decisions(binary_version=binary_version)
    )


def repo_scope_version_decisions(
    cwd: Path,
    *,
    binary_version: str,
) -> tuple[RepoScopeVersionDecision, ...]:
    """Scan repo-scope managed files for stale conductor version stamps.

    Startup auto-refresh calls this on eligible CLI commands, so the scan is
    intentionally bounded to the conventional repo-scope files in ``cwd`` and
    reads only a small prefix from each existing candidate.
    """
    root = cwd.resolve()
    if not _is_inside_git_repo(root):
        return ()

    decisions: list[RepoScopeVersionDecision] = []
    for path, kind in _repo_scope_candidate_sentinel_paths(root).items():
        exists = path.is_file()
        version, reason = _read_repo_sentinel_version_prefix(path)
        decisions.append(
            _repo_scope_decision(
                path=path,
                kind=kind,
                version=version,
                exists=exists,
                binary_version=binary_version,
                reason=reason,
            )
        )
    for path, kind in _repo_scope_candidate_artifacts(root).items():
        exists = path.is_file()
        version = _read_managed_version_prefix(path)
        decisions.append(
            _repo_scope_decision(
                path=path,
                kind=kind,
                version=version,
                exists=exists,
                binary_version=binary_version,
                reason=None,
            )
        )
    return tuple(decisions)


def is_repo_scope_stale(cwd: Path) -> bool:
    """Return True when any repo-scope conductor-managed file is stale."""
    from conductor import __version__

    return any(
        decision.stale
        for decision in repo_scope_version_decisions(cwd, binary_version=__version__)
    )


def refresh_repo_scope(cwd: Path, *, version: str) -> RefreshReport:
    """Refresh stale repo-scope conductor-managed files in ``cwd``."""
    refreshed: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    root = cwd.resolve()

    for decision in repo_scope_version_decisions(root, binary_version=version):
        if not decision.stale:
            if decision.reason not in {
                "missing",
                "not conductor-managed",
                "current",
                "import-mode",
            }:
                skipped.append((decision.path, decision.reason))
            continue
        try:
            _refresh_repo_scope_path(decision.kind, cwd=root, version=version)
            refreshed.append(decision.path)
        except OSError as e:
            skipped.append((decision.path, f"refresh failed: {e}"))
        except UserOwnedFileError as e:
            skipped.append((e.path, "user-owned file; refusing to overwrite"))

    return RefreshReport(refreshed=tuple(refreshed), skipped=tuple(skipped))


def _repo_scope_decision(
    *,
    path: Path,
    kind: str,
    version: str | None,
    exists: bool,
    binary_version: str,
    reason: str | None,
) -> RepoScopeVersionDecision:
    if reason is not None:
        return RepoScopeVersionDecision(path, kind, version, False, reason)
    if version is None:
        detail = "missing" if not exists else "not conductor-managed"
        return RepoScopeVersionDecision(path, kind, None, False, detail)
    stale = _wiring_version_is_older(version, binary_version=binary_version)
    detail = "stale" if stale else "current"
    return RepoScopeVersionDecision(path, kind, version, stale, detail)


def _refresh_repo_scope_path(kind: str, *, cwd: Path, version: str) -> None:
    if kind == "agents-md-import":
        wire_agents_md(cwd=cwd, version=version)
        return
    if kind == "gemini-md-import":
        wire_gemini_md(cwd=cwd, version=version)
        return
    if kind == "claude-md-repo-import":
        wire_claude_md_repo(cwd=cwd, version=version)
        return
    if kind == "cursor-rule":
        wire_cursor(cwd=cwd, version=version)
        return
    raise ValueError(f"unsupported repo integration kind: {kind}")


def _user_scope_decision(
    *,
    path: Path,
    kind: str,
    version: str | None,
    exists: bool,
    binary_version: str,
) -> UserScopeVersionDecision:
    if version is None:
        reason = "missing" if not exists else "not conductor-managed"
        return UserScopeVersionDecision(path, kind, None, False, reason)
    stale = _wiring_version_is_older(version, binary_version=binary_version)
    reason = "stale" if stale else "current"
    return UserScopeVersionDecision(path, kind, version, stale, reason)


def _read_prefix(path: Path) -> str | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return fh.read(_VERSION_SCAN_BYTES)


def _read_managed_version_prefix(path: Path) -> str | None:
    text = _read_prefix(path)
    if not text:
        return None
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if first_line.startswith(MANAGED_COMMENT_PREFIX):
        return _extract_version(first_line, MANAGED_COMMENT_PREFIX)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(MANAGED_FRONTMATTER_KEY):
            return _extract_version(stripped, MANAGED_FRONTMATTER_KEY)
    return None


def _read_sentinel_version_prefix(path: Path) -> str | None:
    text = _read_prefix(path)
    if not text:
        return None
    idx = text.find(SENTINEL_BEGIN_PREFIX)
    if idx == -1:
        return None
    rest = text[idx + len(SENTINEL_BEGIN_PREFIX) :]
    end = rest.find(" -->")
    if end == -1:
        return None
    return rest[:end].strip()


def _read_repo_sentinel_version_prefix(path: Path) -> tuple[str | None, str | None]:
    text = _read_prefix(path)
    if not text:
        return None, None
    idx = text.find(SENTINEL_BEGIN_PREFIX)
    if idx == -1:
        return None, None
    marker_end = text.find("-->", idx)
    if marker_end == -1:
        return None, "malformed sentinel"
    body_prefix = text[marker_end + len("-->") :].lstrip()
    if body_prefix.startswith("@"):
        return None, "import-mode"
    version = text[idx + len(SENTINEL_BEGIN_PREFIX) : marker_end].strip()
    return version or None, None


def _is_inside_git_repo(cwd: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (cwd, *cwd.parents))


def project_root_for_wiring_notice(cwd: Path | None = None) -> Path | None:
    """Return the nearest project root worth checking for agent wiring.

    This intentionally uses marker files instead of recursively scanning the
    machine. The startup notice is advisory and must stay cheap.
    """
    start = (cwd or Path.cwd()).resolve()
    candidates = (start, *start.parents)
    for candidate in candidates:
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate
    return None


def agent_wiring_notice(
    *,
    cwd: Path | None = None,
    current_version: str,
    include_missing: bool = False,
) -> tuple[str, str] | None:
    """Return a one-time notice key/message if repo wiring needs refresh.

    ``include_missing`` is meant for interactive shells. Non-interactive
    callers get stale managed-block warnings, but not missing-block warnings,
    because a repo may deliberately not use agent instruction files.
    """
    root = project_root_for_wiring_notice(cwd)
    if root is None:
        return None

    detection = detect(cwd=root)
    current_version_key = _canonical_wiring_version(current_version)
    stale: list[str] = []
    for artifact in detection.managed:
        if artifact.kind not in _REPO_SCOPED_KINDS:
            continue
        if artifact.version and not _wiring_versions_match(
            artifact.version, current_version
        ):
            try:
                display = artifact.path.relative_to(root)
            except ValueError:
                display = artifact.path
            stale.append(f"{display} has conductor v{artifact.version}")

    agents_current = any(
        artifact.kind == "agents-md-import"
        and artifact.version
        and _wiring_versions_match(artifact.version, current_version)
        for artifact in detection.managed
    )
    agents_missing = not agents_current

    if stale:
        reason = "stale"
        detail = "; ".join(stale)
    elif include_missing and agents_missing:
        reason = "missing"
        detail = "AGENTS.md has no current conductor delegation block"
    else:
        return None

    key = _notice_key(root=root, current_version=current_version_key, reason=reason)
    message = (
        "[conductor] This repo's Conductor agent instructions are "
        f"{'out of date' if reason == 'stale' else 'missing'}.\n"
        f"[conductor] {detail}.\n"
        "[conductor] Refresh them with: conductor init --yes"
    )
    return key, message


def should_emit_agent_wiring_notice(key: str) -> bool:
    """Return True once per notice key, persisting the seen marker best-effort."""
    path = _notice_seen_path(key)
    try:
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seen\n", encoding="utf-8")
    except OSError:
        # Broken cache must not break a conductor invocation. If the marker
        # cannot be written, emit the notice for this process.
        return True
    return True


def _notice_key(*, root: Path, current_version: str, reason: str) -> str:
    raw = f"{root.resolve()}\0{current_version}\0{reason}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _notice_seen_path(key: str) -> Path:
    from conductor.offline_mode import _cache_dir

    return _cache_dir() / "agent-wiring-notices" / f"{key}.seen"


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
    path.write_text(_normalize_generated_text(header + body), encoding="utf-8")


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
        lines.append(_frontmatter_line(key, value))
    # managed-by last: the user sees name/description/etc. first when they
    # open the file; our marker sits at the bottom of the frontmatter.
    lines.append(f"managed-by: conductor v{_canonical_wiring_version(version)}")
    lines.append("---")
    path.write_text(
        _normalize_generated_text("\n".join(lines) + "\n\n" + body),
        encoding="utf-8",
    )


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

    clean_content = _normalize_generated_text(content).rstrip("\n")
    block = (
        f"{_sentinel_begin(version)}\n"
        f"{clean_content}\n"
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
                "--with codex`."
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
    """Inject conductor's delegation @-import into repo-scope ``./CLAUDE.md``.

    The block content is a single ``@~/.conductor/delegation-guidance.md``
    import line. Claude Code expands ``~`` per-user, so the same checked-in
    block resolves correctly on every contributor's machine. Drift goes to
    zero: ``brew upgrade conductor`` updates the user-scope target, and
    every repo's import re-resolves to the new content automatically.
    """
    cwd = cwd or Path.cwd()
    path = cwd / "CLAUDE.md"
    import_line = "@~/.conductor/delegation-guidance.md"
    inject_sentinel_block(path, import_line, version=version)
    return AgentsMdReport(path=path, patched=True)


def wire_cursor(cwd: Path | None = None, *, version: str) -> AgentsMdReport:
    """Write Cursor's conductor-delegation rule at
    ``<cwd>/.cursor/rules/conductor-delegation.mdc``.

    Unlike the markdown instruction files above, Cursor rules are
    fully-managed — the file is conductor's whole (YAML frontmatter
    with ``description`` / ``globs`` / ``alwaysApply`` keys plus a body
    rendered from ``CURSOR_RULE_BODY``).
    """
    from conductor import _agent_templates as templates

    cwd = cwd or Path.cwd()
    path = cwd / ".cursor" / "rules" / "conductor-delegation.mdc"
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
