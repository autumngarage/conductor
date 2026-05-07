"""Local git-state predicates for branch and worktree cleanup.

The branch scan is capped by recency for performance: by default only the
50 most recently updated non-default local branches are checked with the
tree-equivalence predicate. Callers receive ``BranchScanLimit`` metadata and
must surface it when the cap hides older branches from detection.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_BRANCH_SCAN_LIMIT = 50
DEFAULT_KEEP_WORKTREE_DAYS = 7


@dataclass(frozen=True)
class GitStateError(RuntimeError):
    """Raised when local git state cannot be read safely."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None
    branch: str | None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    lock_reason: str | None = None


@dataclass(frozen=True)
class StaleBranch:
    name: str
    reason: str
    last_commit: str


@dataclass(frozen=True)
class AbandonedWorktree:
    path: Path
    branch: str | None
    reason: str
    last_commit: str | None
    last_commit_age_days: int | None
    clean: bool


@dataclass(frozen=True)
class ProtectedRef:
    kind: str
    name: str
    reason: str


@dataclass(frozen=True)
class BranchScanLimit:
    checked: int
    total: int
    limit: int | None

    @property
    def capped(self) -> bool:
        return self.limit is not None and self.total > self.checked


@dataclass(frozen=True)
class GitCleanupPlan:
    default_branch: str
    current_path: Path
    current_branch: str | None
    stale_branches: list[StaleBranch]
    abandoned_worktrees: list[AbandonedWorktree]
    protected: list[ProtectedRef]
    branch_scan: BranchScanLimit


def _git(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as e:
        raise GitStateError(f"`git {' '.join(args)}` failed to start: {e}") from e
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise GitStateError(f"`git {' '.join(args)}` failed{suffix}")
    return result


def git_root(*, cwd: str | Path | None = None) -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"], cwd=cwd).stdout.strip())


def default_branch(*, cwd: str | Path | None = None) -> str:
    candidates = (
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["rev-parse", "--verify", "--quiet", "main"],
        ["rev-parse", "--verify", "--quiet", "master"],
    )
    first = _git(candidates[0], cwd=cwd, check=False)
    if first.returncode == 0:
        raw = first.stdout.strip()
        if raw.startswith("origin/"):
            return raw.removeprefix("origin/")
        if raw:
            return raw
    for branch, cmd in (("main", candidates[1]), ("master", candidates[2])):
        if _git(cmd, cwd=cwd, check=False).returncode == 0:
            return branch
    raise GitStateError("could not resolve default branch (tried origin/HEAD, main, master)")


def current_branch(*, cwd: str | Path | None = None) -> str | None:
    raw = _git(["branch", "--show-current"], cwd=cwd).stdout.strip()
    return raw or None


def tree_equivalent_to(
    branch: str,
    base: str,
    *,
    cwd: str | Path | None = None,
) -> bool:
    """Return true when ``base`` currently contains ``branch``'s changed tree.

    The check mirrors ``scripts/cleanup-branches.sh``: compare every path the
    branch changed since merge-base, with rename detection disabled, against
    the current base tree. That catches squash-merge/cherry-pick shape while
    rejecting add-then-revert false positives.
    """
    merge_base = _git(["merge-base", base, branch], cwd=cwd, check=False)
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return False
    base_sha = merge_base.stdout.strip()
    paths = _git(
        ["diff", "--name-only", "--no-renames", "-z", base_sha, branch],
        cwd=cwd,
        check=False,
    )
    if paths.returncode != 0:
        return False
    for path in paths.stdout.split("\0"):
        if not path:
            continue
        diff = _git(["diff", "--quiet", base, branch, "--", path], cwd=cwd, check=False)
        if diff.returncode != 0:
            return False
    return True


def list_worktrees(*, cwd: str | Path | None = None) -> list[Worktree]:
    output = _git(["worktree", "list", "--porcelain"], cwd=cwd).stdout
    entries: list[Worktree] = []
    current: dict[str, object] = {}

    def flush() -> None:
        if not current:
            return
        path = current.get("path")
        if path is None:
            raise GitStateError("git worktree porcelain entry is missing path")
        head = current.get("head")
        branch = current.get("branch")
        lock_reason = current.get("lock_reason")
        entries.append(
            Worktree(
                path=Path(str(path)),
                head=head if isinstance(head, str) else None,
                branch=branch if isinstance(branch, str) else None,
                detached=bool(current.get("detached")),
                bare=bool(current.get("bare")),
                locked=bool(current.get("locked")),
                lock_reason=lock_reason if isinstance(lock_reason, str) else None,
            )
        )

    for raw_line in output.splitlines():
        if not raw_line:
            flush()
            current = {}
            continue
        key, _, value = raw_line.partition(" ")
        if key == "worktree":
            if current:
                flush()
                current = {}
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
            current["lock_reason"] = value or None
    flush()
    return entries


def _local_branches_by_recency(
    *,
    cwd: str | Path | None,
    exclude: set[str],
    limit: int | None,
) -> tuple[list[str], BranchScanLimit]:
    raw = _git(
        [
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            "refs/heads/",
        ],
        cwd=cwd,
    ).stdout
    all_branches = [line.strip() for line in raw.splitlines() if line.strip()]
    candidates = [branch for branch in all_branches if branch not in exclude]
    checked = candidates if limit is None else candidates[:limit]
    return checked, BranchScanLimit(
        checked=len(checked),
        total=len(candidates),
        limit=limit,
    )


def _last_commit(branch: str, *, cwd: str | Path | None) -> str:
    return _git(["rev-parse", "--short", branch], cwd=cwd).stdout.strip()


def _last_commit_datetime(ref: str, *, cwd: str | Path | None) -> datetime | None:
    result = _git(["log", "-1", "--format=%cI", ref], cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _is_clean(path: Path) -> bool:
    return _git(["status", "--porcelain"], cwd=path).stdout == ""


def _worktree_for_current_checkout(
    worktrees: list[Worktree],
    *,
    cwd: str | Path | None,
) -> Path:
    root = git_root(cwd=cwd).resolve()
    for worktree in worktrees:
        if worktree.path.resolve() == root:
            return worktree.path.resolve()
    return root


def find_stale_branches(
    *,
    base: str | None = None,
    branch_scan_limit: int | None = DEFAULT_BRANCH_SCAN_LIMIT,
    cwd: str | Path | None = None,
) -> list[StaleBranch]:
    plan = scan_git_state(
        base=base,
        keep_worktree_days=DEFAULT_KEEP_WORKTREE_DAYS,
        branch_scan_limit=branch_scan_limit,
        cwd=cwd,
    )
    return plan.stale_branches


def find_abandoned_worktrees(
    *,
    keep_days: int = DEFAULT_KEEP_WORKTREE_DAYS,
    base: str | None = None,
    cwd: str | Path | None = None,
) -> list[AbandonedWorktree]:
    plan = scan_git_state(
        base=base,
        keep_worktree_days=keep_days,
        branch_scan_limit=DEFAULT_BRANCH_SCAN_LIMIT,
        cwd=cwd,
    )
    return plan.abandoned_worktrees


def scan_git_state(
    *,
    base: str | None = None,
    keep_worktree_days: int = DEFAULT_KEEP_WORKTREE_DAYS,
    branch_scan_limit: int | None = DEFAULT_BRANCH_SCAN_LIMIT,
    cwd: str | Path | None = None,
) -> GitCleanupPlan:
    """Classify local branches and worktrees for cleanup.

    Branch tree-equivalence checks are O(N x diff). ``branch_scan_limit`` caps
    checks to the most recently updated local branches (default 50); callers
    must surface ``branch_scan.capped`` so older unchecked branches are not
    silently hidden.
    """
    if keep_worktree_days < 0:
        raise GitStateError("keep_worktree_days must be >= 0")
    if branch_scan_limit is not None and branch_scan_limit < 1:
        raise GitStateError("branch_scan_limit must be >= 1")

    resolved_base = base or default_branch(cwd=cwd)
    current = current_branch(cwd=cwd)
    worktrees = list_worktrees(cwd=cwd)
    current_path = _worktree_for_current_checkout(worktrees, cwd=cwd)
    worktree_branches = {w.branch for w in worktrees if w.branch}
    protected_names = {resolved_base, "main", "master", "HEAD"}
    if current:
        protected_names.add(current)

    stale: list[StaleBranch] = []
    protected: list[ProtectedRef] = []
    checked_branches, branch_scan = _local_branches_by_recency(
        cwd=cwd,
        exclude=protected_names,
        limit=branch_scan_limit,
    )
    for branch in checked_branches:
        if branch in worktree_branches:
            protected.append(ProtectedRef("branch", branch, "checked out in worktree"))
            continue
        if tree_equivalent_to(branch, resolved_base, cwd=cwd):
            stale.append(
                StaleBranch(
                    name=branch,
                    reason=f"squash-merged into {resolved_base}",
                    last_commit=_last_commit(branch, cwd=cwd),
                )
            )
        else:
            protected.append(ProtectedRef("branch", branch, "unique unmerged commits"))

    if current:
        protected.append(ProtectedRef("branch", current, "current checkout"))
    protected.append(ProtectedRef("branch", resolved_base, "default branch"))

    abandoned: list[AbandonedWorktree] = []
    cutoff = datetime.now(UTC) - timedelta(days=keep_worktree_days)
    for worktree in worktrees:
        path = worktree.path.resolve()
        label = str(path)
        if path == current_path:
            protected.append(ProtectedRef("worktree", label, "current checkout"))
            continue
        if worktree.locked:
            reason = "locked"
            if worktree.lock_reason:
                reason = f"locked: {worktree.lock_reason}"
            protected.append(ProtectedRef("worktree", label, reason))
            continue
        if worktree.branch == resolved_base:
            protected.append(ProtectedRef("worktree", label, "default branch"))
            continue
        if not path.exists():
            protected.append(
                ProtectedRef(
                    "worktree",
                    label,
                    "missing path; run git worktree prune",
                )
            )
            continue
        if not _is_clean(path):
            protected.append(ProtectedRef("worktree", label, "uncommitted changes"))
            continue
        ref = worktree.branch or worktree.head
        if ref is None:
            protected.append(ProtectedRef("worktree", label, "missing HEAD"))
            continue

        last_dt = _last_commit_datetime(ref, cwd=path)
        age_days = (
            max(0, (datetime.now(UTC) - last_dt).days)
            if last_dt is not None
            else None
        )
        last_commit = _last_commit(ref, cwd=path) if ref else None
        if worktree.branch and tree_equivalent_to(worktree.branch, resolved_base, cwd=cwd):
            abandoned.append(
                AbandonedWorktree(
                    path=path,
                    branch=worktree.branch,
                    reason=f"squash-merged into {resolved_base}",
                    last_commit=last_commit,
                    last_commit_age_days=age_days,
                    clean=True,
                )
            )
            continue
        if last_dt is not None and last_dt < cutoff:
            abandoned.append(
                AbandonedWorktree(
                    path=path,
                    branch=worktree.branch,
                    reason=f"last commit older than {keep_worktree_days} days",
                    last_commit=last_commit,
                    last_commit_age_days=age_days,
                    clean=True,
                )
            )
            continue
        protected.append(ProtectedRef("worktree", label, "recent or unique work"))

    return GitCleanupPlan(
        default_branch=resolved_base,
        current_path=current_path,
        current_branch=current,
        stale_branches=stale,
        abandoned_worktrees=abandoned,
        protected=protected,
        branch_scan=branch_scan,
    )
