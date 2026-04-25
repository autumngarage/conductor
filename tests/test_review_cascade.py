"""Regression test for scripts/codex-review.sh fallback cascade.

Reproduces the silent-failure bug this PR fixes: previously, when codex
exited non-zero (usage limit, rate limit, transient API error), the
script's stderr was discarded and the fail-open path exited 0 with only
"reviewer exit N" logged — the push proceeded without a real review.

These tests assert the new behavior: a runtime failure on one reviewer
falls through to the next available reviewer, and the failure cause is
surfaced in the cascade chain summary.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    path.chmod(0o755)


def _make_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with two commits so MERGE_BASE..HEAD has a diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\n")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\nfeature line\n")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, env=env, check=True)
    return repo


def _run_script(repo: Path, fakes_dir: Path, extra_env: dict | None = None,
                timeout: int = 60) -> subprocess.CompletedProcess:
    # Scrub pre-commit/pre-push hook signals that may leak in from the test
    # runner's environment (e.g. when pytest itself runs under a pre-push
    # hook). The script's `should_skip_pre_push_review` would otherwise
    # think we're pushing a non-default branch and skip review entirely.
    inherited = {k: v for k, v in os.environ.items()
                 if not (k.startswith("PRE_COMMIT") or k.startswith("CODEX_REVIEW")
                         or k == "TOUCHSTONE_REVIEWER")}
    env = {
        **inherited,
        "PATH": f"{fakes_dir}:{os.environ.get('PATH', '')}",
        "CODEX_REVIEW_BASE": "HEAD~1",  # avoid origin fetch
        "CODEX_REVIEW_MODE": "review-only",
        "CODEX_REVIEW_DISABLE_CACHE": "1",
        "CODEX_REVIEW_TIMEOUT": "5",  # keep tests fast; we never exercise the real budget
        "TOUCHSTONE_ROOT": str(repo),
        "NO_COLOR": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT_PATH)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_config(repo: Path, reviewers: list[str], on_error: str = "fail-open",
                  mode: str = "review-only") -> None:
    body = textwrap.dedent(f"""
        [codex_review]
        max_iterations = 1
        max_diff_lines = 5000
        cache_clean_reviews = false
        safe_by_default = true
        mode = "{mode}"
        on_error = "{on_error}"
        unsafe_paths = []

        [review]
        enabled = true
        reviewers = {reviewers!r}
    """).lstrip("\n")
    (repo / ".codex-review.toml").write_text(body)
    # Commit so the worktree is clean before review runs — otherwise the
    # config file shows up as untracked and trips WORKTREE_DIRTY_BEFORE_REVIEW.
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", ".codex-review.toml"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, env=env, check=True)


# ---------------------------------------------------------------------------
# fake reviewer factories
# ---------------------------------------------------------------------------

FAKE_CODEX_RATE_LIMIT = r'''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    msg='{"type":"error","status":429,'
    msg="${msg}\"error\":{\"type\":\"rate_limit_exceeded\","
    msg="${msg}\"message\":\"Too many requests\"}}"
    echo "ERROR: $msg" >&2
    exit 1
    ;;
esac
exit 1
'''

FAKE_CODEX_MALFORMED = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "I reviewed your code and it looks fine but I will not emit the sentinel."
    exit 0
    ;;
esac
exit 1
'''

FAKE_CODEX_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "ERROR: usage limit exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

FAKE_CLAUDE_CLEAN = '''
#!/usr/bin/env bash
case "$1" in
  auth)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  -p)
    echo "Looks fine."
    echo "CODEX_REVIEW_CLEAN"
    exit 0
    ;;
esac
exit 1
'''

FAKE_CLAUDE_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  auth)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  -p)
    echo "ERROR: 401 unauthorized" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex writes a file (simulating a partial edit) then exits non-zero
# without ever emitting CODEX_REVIEW_FIXED. In fix mode the cascade must
# discard this partial edit before falling through, otherwise claude would
# review-or-bless un-blessed work.
FAKE_CODEX_PARTIAL_EDIT = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    # Simulate codex starting an edit then crashing mid-flight.
    echo "partial edit from codex that will never be blessed" >> README
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex commits its work then fails — the worktree is clean but HEAD
# moved without the script's auto-fix path blessing it. The cascade must
# detect this and abort.
FAKE_CODEX_COMMITS_THEN_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "edit from codex" >> README
    git -c user.name=t -c user.email=t@t add README
    git -c user.name=t -c user.email=t@t commit -q -m "unauthorized commit by codex"
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''

# Codex stashes its work then fails — clean tree, unchanged HEAD, but a
# new stash entry hides reviewer-authored state.
FAKE_CODEX_STASHES_THEN_FAILS = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo "edit from codex" >> README
    git stash push -q -m "hidden by codex" -- README
    echo "ERROR: rate_limit_exceeded" >&2
    exit 1
    ;;
esac
exit 1
'''


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_usage_limit_falls_through_to_next_reviewer(tmp_path: Path) -> None:
    """Codex hits a rate limit; cascade falls through to claude, which clears."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_RATE_LIMIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "falling back to Claude" in result.stdout
    assert "codex:usage-limit" in result.stdout
    assert "claude:clean" in result.stdout
    assert "ALL CLEAR" in result.stdout


def test_malformed_sentinel_falls_through(tmp_path: Path) -> None:
    """Codex emits non-sentinel output (a contract violation, not a crash);
    cascade still falls through to claude per the silent-fail-prevention spirit."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_MALFORMED)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "did not match the expected sentinel contract" in result.stdout
    assert "falling back to Claude" in result.stdout
    assert "codex:malformed" in result.stdout
    assert "claude:clean" in result.stdout


def test_cascade_exhausted_records_chain(tmp_path: Path) -> None:
    """Both reviewers fail; cascade-exhausted path records the chain and
    fail-open allows the push (preserving today's exit-policy semantics).
    The bug this PR fixes is *visibility*, not the exit policy itself —
    the failure reason is now in the chain instead of swallowed."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_FAILS)

    result = _run_script(repo, fakes)

    # fail-open: cascade exhausted → exit 0 with loud message
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "All reviewers in the cascade failed" in result.stdout
    assert "codex:usage-limit" in result.stdout
    assert "claude:auth" in result.stdout
    assert "cascade exhausted" in result.stdout


def test_cascade_exhausted_blocks_when_fail_closed(tmp_path: Path) -> None:
    """Same as above but with on_error=fail-closed — push must be blocked."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], on_error="fail-closed")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_FAILS)

    result = _run_script(repo, fakes)

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "All reviewers in the cascade failed" in result.stdout
    assert "blocking push" in result.stderr or "blocking push" in result.stdout


def test_fix_mode_discards_partial_edits_before_fallthrough(tmp_path: Path) -> None:
    """In fix mode, a failed reviewer may have written partial edits before
    crashing. The cascade must NOT pass that dirty worktree to the next
    reviewer — otherwise claude could bless or auto-commit work codex
    never blessed with FIXED."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_PARTIAL_EDIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "Discarding partial edits" in result.stdout
    assert "falling back to Claude" in result.stdout
    # Worktree must be clean after the cascade — codex's un-blessed edit
    # should have been reverted before claude saw it.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
    )
    assert status.stdout == "", (
        f"Worktree should be clean post-cascade; saw: {status.stdout!r}\n"
        f"This means a failed reviewer's partial edits leaked through to the next reviewer."
    )


def test_fix_mode_aborts_on_unauthorized_commits(tmp_path: Path) -> None:
    """A reviewer that commits its own work leaves a clean worktree at a
    HEAD the script never blessed. Cascade must detect HEAD movement that
    doesn't match the auto-fix counter and abort."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_COMMITS_THEN_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "unauthorized commits" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_on_reviewer_stash(tmp_path: Path) -> None:
    """A reviewer that stashes its work leaves a clean worktree and
    unchanged HEAD, but the stash hides reviewer-authored state. Cascade
    must detect stash list growth and abort."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_STASHES_THEN_FAILS)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "added stash entries" in result.stdout
    assert "Refusing to fall through" in result.stdout


def test_fix_mode_aborts_when_pre_review_worktree_dirty(tmp_path: Path) -> None:
    """If the user already had uncommitted changes before review started,
    we cannot safely distinguish their work from reviewer edits on failure.
    Refuse to fall through and block the push, so the user can sort it out."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"], mode="fix")
    # Plant a pre-review uncommitted change.
    (repo / "README").write_text("base\nfeature line\nuser edit\n")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_PARTIAL_EDIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes, extra_env={"CODEX_REVIEW_MODE": "fix"})

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "refusing to fall through" in result.stdout
    assert "mixed user/reviewer state" in result.stdout


def test_stderr_tail_surfaced_on_failure(tmp_path: Path) -> None:
    """Reviewer stderr is no longer discarded — the rate-limit message
    must appear in the user-visible output so they know *why* it failed."""
    repo = _make_repo(tmp_path)
    _write_config(repo, ["codex", "claude"])

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _write_executable(fakes / "codex", FAKE_CODEX_RATE_LIMIT)
    _write_executable(fakes / "claude", FAKE_CLAUDE_CLEAN)

    result = _run_script(repo, fakes)

    assert "rate_limit_exceeded" in result.stdout, (
        "Codex stderr (rate-limit JSON) should be tailed into user output, "
        "not silenced. This is the core silent-failure bug.\n"
        f"stdout={result.stdout!r}"
    )
