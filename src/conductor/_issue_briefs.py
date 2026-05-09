from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class IssueBriefError(RuntimeError):
    """Raised when a GitHub issue cannot be resolved into a brief."""


_EXPLICIT_ISSUE_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>[1-9]\d*)$")
_ISSUE_NUMBER_RE = re.compile(r"^[1-9]\d*$")
_GIT_REMOTE_TIMEOUT_SEC = 5.0
_GH_ISSUE_TIMEOUT_SEC = 30.0


def build_issue_brief(
    issue: str,
    *,
    comment_limit: int = 10,
    cwd: str | Path | None = None,
) -> str:
    owner_repo, number = _resolve_issue_target(issue, cwd=cwd)
    issue_data = _fetch_issue(owner_repo, number, cwd=cwd)
    return _render_issue_brief(owner_repo, number, issue_data, comment_limit=comment_limit)


def append_operator_context(issue_brief: str, operator_text: str | None) -> str:
    if operator_text is None:
        return issue_brief
    operator_text = operator_text.strip()
    if not operator_text:
        return issue_brief
    return f"{issue_brief.rstrip()}\n\n## Operator-supplied additional context\n\n{operator_text}\n"


def _resolve_issue_target(issue: str, *, cwd: str | Path | None) -> tuple[str, str]:
    issue = issue.strip()
    explicit = _EXPLICIT_ISSUE_RE.match(issue)
    if explicit:
        return explicit.group("repo"), explicit.group("number")
    if not _ISSUE_NUMBER_RE.match(issue):
        raise IssueBriefError(
            "--issue must be an issue number like 239 or a target like owner/repo#239."
        )
    return _owner_repo_from_origin(cwd=cwd), issue


def _owner_repo_from_origin(*, cwd: str | Path | None) -> str:
    command = ["git", "config", "--get", "remote.origin.url"]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SEC,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise IssueBriefError(
            "--issue <N> requires a GitHub origin remote. "
            f"Could not run {' '.join(command)}" + (f": {detail}" if detail else ".")
        ) from e
    except FileNotFoundError as e:
        raise IssueBriefError("--issue <N> requires git to resolve the current repository.") from e
    except subprocess.TimeoutExpired as e:
        raise IssueBriefError(
            f"--issue <N> timed out after {_GIT_REMOTE_TIMEOUT_SEC:.0f}s while "
            "resolving the current repository origin."
        ) from e

    origin = result.stdout.strip()
    owner_repo = _parse_github_owner_repo(origin)
    if owner_repo is None:
        raise IssueBriefError(
            f"--issue <N> requires remote.origin.url to point at GitHub; got {origin!r}."
        )
    return owner_repo


def _parse_github_owner_repo(origin: str) -> str | None:
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^http://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, origin)
        if match:
            return match.group("repo")
    return None


def _fetch_issue(owner_repo: str, number: str, *, cwd: str | Path | None) -> dict[str, Any]:
    command = [
        "gh",
        "issue",
        "view",
        number,
        "--repo",
        owner_repo,
        "--json",
        "title,body,labels,comments",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GH_ISSUE_TIMEOUT_SEC,
            check=True,
        )
    except FileNotFoundError as e:
        raise IssueBriefError("--issue requires the gh CLI; install via brew install gh.") from e
    except subprocess.TimeoutExpired as e:
        raise IssueBriefError(
            f"timed out after {_GH_ISSUE_TIMEOUT_SEC:.0f}s fetching GitHub issue "
            f"{owner_repo}#{number}. Run {' '.join(command)} to inspect manually."
        ) from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise IssueBriefError(
            f"could not fetch GitHub issue {owner_repo}#{number}. "
            f"Run {' '.join(command)} to inspect the failure" + (f": {detail}" if detail else ".")
        ) from e

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise IssueBriefError(
            f"gh returned invalid JSON for {owner_repo}#{number}. "
            f"Run {' '.join(command)} to inspect the output: {e}"
        ) from e
    if not isinstance(payload, dict):
        raise IssueBriefError(f"gh returned an unexpected response for {owner_repo}#{number}.")
    return payload


def _render_issue_brief(
    owner_repo: str,
    number: str,
    issue_data: dict[str, Any],
    *,
    comment_limit: int,
) -> str:
    title = str(issue_data.get("title") or f"Issue #{number}").strip()
    body = str(issue_data.get("body") or "").strip()
    labels = _label_names(issue_data.get("labels"))
    comments = _recent_comments(issue_data.get("comments"), limit=comment_limit)

    parts = [f"# Issue: {title} ({owner_repo}#{number})"]
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")
    if body:
        parts.append(body)
    if comments:
        parts.append(f"## Recent comments (last {len(comments)})")
        for comment in comments:
            parts.append(_render_comment(comment))
    return "\n\n".join(parts).rstrip() + "\n"


def _label_names(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


def _recent_comments(comments: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(comments, list) or limit <= 0:
        return []
    return [comment for comment in comments[-limit:] if isinstance(comment, dict)]


def _render_comment(comment: dict[str, Any]) -> str:
    author = comment.get("author")
    if isinstance(author, dict):
        author_login = str(author.get("login") or "unknown")
    else:
        author_login = str(author or "unknown")
    created_at = str(comment.get("createdAt") or comment.get("created_at") or "unknown")
    body = str(comment.get("body") or "").strip()
    return f"### @{author_login} at {created_at}\n{body}".rstrip()
