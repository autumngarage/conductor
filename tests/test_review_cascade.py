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
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

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


def _write_config(repo: Path, reviewers: list[str], on_error: str = "fail-open") -> None:
    body = textwrap.dedent(f"""
        [codex_review]
        max_iterations = 1
        max_diff_lines = 5000
        cache_clean_reviews = false
        safe_by_default = false
        mode = "review-only"
        on_error = "{on_error}"
        unsafe_paths = []

        [review]
        enabled = true
        reviewers = {reviewers!r}
    """).lstrip("\n")
    (repo / ".codex-review.toml").write_text(body)


# ---------------------------------------------------------------------------
# fake reviewer factories
# ---------------------------------------------------------------------------

FAKE_CODEX_RATE_LIMIT = '''
#!/usr/bin/env bash
case "$1" in
  login)
    if [ "$2" = "status" ]; then exit 0; fi
    ;;
  exec)
    echo 'ERROR: {"type":"error","status":429,"error":{"type":"rate_limit_exceeded","message":"Too many requests"}}' >&2
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
