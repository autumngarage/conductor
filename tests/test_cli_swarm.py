from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from conductor import cli
from conductor.cli import main
from conductor.providers import CallResponse


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_returncode(repo: Path, *args: str) -> int:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _brief(repo: Path, name: str, body: str = "make a focused change") -> Path:
    path = repo / "tasks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _commit_change(worktree: Path, filename: str, message: str) -> None:
    (worktree / filename).write_text(f"{message}\n", encoding="utf-8")
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-m", message)


def _fake_exec_factory(*, fail_on: set[str] | None = None, no_change_on: set[str] | None = None):
    fail_on = fail_on or set()
    no_change_on = no_change_on or set()

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        body = kwargs["body"]
        if body in fail_on:
            raise cli._ExecPhaseError(
                exit_code=1,
                exit_status="cap-exit",
                message="max-iterations cap reached",
            )
        if body not in no_change_on:
            filename = f"{body.replace(' ', '-')}.txt"
            _commit_change(worktree, filename, body)
        return (
            CallResponse(
                text="done",
                provider="codex",
                model="codex",
                duration_ms=1,
            ),
            None,
            None,
        )

    return fake_exec


def _fake_ship(worktree: Path, *, base_branch: str, branch: str, auto_merge: bool):
    _ = (worktree, base_branch, branch)
    if auto_merge:
        return "shipped", "https://github.com/example/repo/pull/123", "abc1234"
    return "pushed-not-merged", "https://github.com/example/repo/pull/123", None


def _fake_merged_ship(repo: Path):
    def fake_ship(worktree: Path, *, base_branch: str, branch: str, auto_merge: bool):
        _ = (branch, auto_merge)
        tree = _git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
        merge_sha = _git(
            repo,
            "commit-tree",
            tree,
            "-p",
            base_branch,
            "-m",
            "squash merge",
        ).stdout.strip()
        _git(repo, "update-ref", f"refs/heads/{base_branch}", merge_sha)
        return "shipped", "https://github.com/example/repo/pull/123", merge_sha

    return fake_ship


@pytest.mark.parametrize(
    ("brief_body", "repo_slug", "expected"),
    [
        ("Issue: #358\n\nImplement the fix.", None, ["#358"]),
        (
            "Issue: https://github.com/autumngarage/conductor/issues/358",
            "autumngarage/conductor",
            ["#358"],
        ),
        ("Closes #358", None, ["#358"]),
        ("Handle numbers 358 and 2026-05-11, but no issue marker.", None, []),
    ],
)
def test_swarm_issue_closing_refs_from_brief_body(
    brief_body: str,
    repo_slug: str | None,
    expected: list[str],
) -> None:
    assert cli._swarm_issue_closing_refs(brief_body, repo_slug=repo_slug) == expected


def test_swarm_issue_closing_refs_accepts_only_matching_repo_refs() -> None:
    assert cli._swarm_issue_closing_refs(
        "# Issue: Brief from issue (autumngarage/conductor#358)",
        repo_slug="autumngarage/conductor",
    ) == ["#358"]
    assert cli._swarm_issue_closing_refs(
        "Issue: https://github.com/other/repo/issues/358",
        repo_slug="autumngarage/conductor",
    ) == []
    assert cli._swarm_issue_closing_refs(
        "Closes other/repo#358",
        repo_slug="autumngarage/conductor",
    ) == []
    assert cli._swarm_issue_closing_refs(
        "Issue: autumngarage/conductor#358",
        repo_slug=None,
    ) == []


def test_swarm_github_repo_slug_parses_origin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:autumngarage/conductor.git")

    assert cli._swarm_github_repo_slug(repo) == "autumngarage/conductor"


def test_run_swarm_git_timeout_surfaces_runtime_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out after 30s"):
        cli._run_swarm_git(["status"], cwd=None)


def test_ship_swarm_pr_timeout_preserves_detected_pr_url(monkeypatch, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(cli, "_swarm_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_ensure_swarm_branch_checked_out", lambda *_args: None)

    def fake_run(args, **kwargs):
        assert kwargs["timeout"] == cli.SWARM_SHIP_TIMEOUT_SEC
        raise subprocess.TimeoutExpired(
            args,
            kwargs["timeout"],
            output="created https://github.com/example/repo/pull/123",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    status, pr_url, reason = cli._ship_swarm_pr(
        tmp_path,
        base_branch="main",
        branch="feat/swarm/foo",
        auto_merge=True,
    )

    assert status == "review-blocked"
    assert pr_url == "https://github.com/example/repo/pull/123"
    assert reason is not None
    assert "open-pr.sh timed out" in reason


def test_ship_swarm_pr_repairs_detached_head_before_open_pr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", "main")
    _commit_change(worktree, "feature.txt", "feature")
    head_sha = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "checkout", "--detach", "HEAD")

    scripts = repo / "scripts"
    scripts.mkdir()
    open_pr = scripts / "open-pr.sh"
    open_pr.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "branch=%s\\n" "$(git branch --show-current)"\n'
        'printf "https://github.com/example/repo/pull/123\\n"\n',
        encoding="utf-8",
    )
    open_pr.chmod(0o755)
    monkeypatch.setattr(cli, "_swarm_pr_merge_sha", lambda *_args, **_kwargs: "merge123")

    status, pr_url, merge_sha = cli._ship_swarm_pr(
        worktree,
        base_branch="main",
        branch="feat/swarm/foo",
        auto_merge=True,
    )

    assert status == "shipped"
    assert pr_url == "https://github.com/example/repo/pull/123"
    assert merge_sha == "merge123"
    assert _git(worktree, "branch", "--show-current").stdout.strip() == "feat/swarm/foo"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == head_sha


def test_ship_swarm_pr_refuses_named_branch_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", "main")
    _commit_change(worktree, "feature.txt", "feature")
    _git(worktree, "checkout", "-b", "scratch")

    with pytest.raises(RuntimeError, match="refusing to retarget a named branch"):
        cli._ensure_swarm_branch_checked_out(worktree, "feat/swarm/foo")

    assert _git(worktree, "branch", "--show-current").stdout.strip() == "scratch"


def test_swarm_one_brief_succeeds_as_shipped(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["shipped_count"] == 1
    assert payload["tasks"][0]["status"] == "shipped"
    assert payload["tasks"][0]["commits"] == 1
    assert not Path(payload["tasks"][0]["worktree"]).exists()
    assert (
        _git_returncode(repo, "show-ref", "--verify", "--quiet", "refs/heads/feat/swarm/foo")
        == 1
    )


def test_swarm_ship_appends_closing_ref_to_last_commit_body(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "Issue: #358\n\nmake a focused change")
    monkeypatch.chdir(repo)

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        _commit_change(worktree, "feature.txt", "feature")
        return (
            CallResponse(
                text="done",
                provider="codex",
                model="codex",
                duration_ms=1,
            ),
            None,
            None,
        )

    def fake_ship(worktree: Path, *, base_branch: str, branch: str, auto_merge: bool):
        _ = (base_branch, branch, auto_merge)
        body = _git(worktree, "log", "-1", "--format=%b").stdout
        assert "Closes #358" in body
        return "shipped", "https://github.com/example/repo/pull/123", "abc1234"

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output


def test_swarm_ship_does_not_add_closing_ref_without_explicit_issue(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "Handle case 358 without an issue marker.")
    monkeypatch.chdir(repo)

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        _commit_change(worktree, "feature.txt", "feature")
        return (
            CallResponse(
                text="done",
                provider="codex",
                model="codex",
                duration_ms=1,
            ),
            None,
            None,
        )

    def fake_ship(worktree: Path, *, base_branch: str, branch: str, auto_merge: bool):
        _ = (base_branch, branch, auto_merge)
        body = _git(worktree, "log", "-1", "--format=%b").stdout
        assert "Closes #358" not in body
        return "shipped", "https://github.com/example/repo/pull/123", "abc1234"

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output


def test_swarm_two_briefs_both_succeed(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "foo.md", "first change")
    second = _brief(repo, "bar.md", "second change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        [
            "swarm",
            "--provider",
            "codex",
            "--brief",
            str(first),
            "--brief",
            str(second),
            "--auto-merge",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [task["status"] for task in payload["tasks"]] == ["shipped", "shipped"]


def test_swarm_second_failure_preserves_worktree(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "foo.md", "first change")
    second = _brief(repo, "bar.md", "fail change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_run_exec_phase_dispatch",
        _fake_exec_factory(fail_on={"fail change"}),
    )
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        [
            "swarm",
            "--provider",
            "codex",
            "--brief",
            str(first),
            "--brief",
            str(second),
            "--auto-merge",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert [task["status"] for task in payload["tasks"]] == ["shipped", "failed"]
    failed_worktree = Path(payload["tasks"][1]["worktree"])
    assert failed_worktree.exists()
    assert (
        _git_returncode(repo, "show-ref", "--verify", "--quiet", "refs/heads/feat/swarm/bar")
        == 0
    )
    assert payload["tasks"][1]["failure_reason"] == "max-iterations cap reached"


def test_swarm_no_commits_is_no_changes(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "noop.md", "no change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_run_exec_phase_dispatch",
        _fake_exec_factory(no_change_on={"no change"}),
    )
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["status"] == "no-changes"
    assert payload["tasks"][0]["commits"] == 0


def test_swarm_auto_merge_off_leaves_pushed_not_merged(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["tasks"][0]["status"] == "pushed-not-merged"
    assert payload["tasks"][0]["pr_url"] == "https://github.com/example/repo/pull/123"
    assert Path(payload["tasks"][0]["worktree"]).exists()
    assert (
        _git_returncode(repo, "show-ref", "--verify", "--quiet", "refs/heads/feat/swarm/foo")
        == 0
    )


def test_swarm_shipped_branch_delete_failure_is_reported(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))
    original_run_swarm_git = cli._run_swarm_git

    def fake_run_swarm_git(args, **kwargs):
        if args == ["branch", "-D", "feat/swarm/foo"]:
            raise RuntimeError("branch delete failed for test")
        return original_run_swarm_git(args, **kwargs)

    monkeypatch.setattr(cli, "_run_swarm_git", fake_run_swarm_git)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["status"] == "shipped"
    assert "branch delete failed for test" in payload["tasks"][0]["failure_reason"]
    assert "branch delete failed for test" in result.stderr
    assert not Path(payload["tasks"][0]["worktree"]).exists()
    assert (
        _git_returncode(repo, "show-ref", "--verify", "--quiet", "refs/heads/feat/swarm/foo")
        == 0
    )


def test_swarm_sanitizes_brief_stem_for_branch(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "Feature: Big Thing!!.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["branch"] == "feat/swarm/feature-big-thing"


def test_swarm_max_parallel_runs_tasks_concurrently(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "foo.md", "first change")
    second = _brief(repo, "bar.md", "second change")
    monkeypatch.chdir(repo)
    started: set[str] = set()
    started_lock = threading.Lock()
    both_started = threading.Event()

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        body = kwargs["body"]
        with started_lock:
            started.add(body)
            if len(started) == 2:
                both_started.set()
        if not both_started.wait(timeout=2):
            raise cli._ExecPhaseError(
                exit_code=1,
                exit_status="parallelism-missing",
                message="tasks did not overlap",
            )
        filename = f"{body.replace(' ', '-')}.txt"
        _commit_change(worktree, filename, body)
        return (
            CallResponse(
                text="done",
                provider="codex",
                model="codex",
                duration_ms=1,
            ),
            None,
            None,
        )

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        [
            "swarm",
            "--provider",
            "codex",
            "--brief",
            str(first),
            "--brief",
            str(second),
            "--max-parallel",
            "2",
            "--auto-merge",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [task["status"] for task in payload["tasks"]] == ["shipped", "shipped"]
    assert started == {"first change", "second change"}


def test_swarm_rejects_duplicate_brief_branch_slugs(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first/foo.md", "first change")
    second = _brief(repo, "second/foo.md", "second change")
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        main,
        [
            "swarm",
            "--provider",
            "codex",
            "--brief",
            str(first),
            "--brief",
            str(second),
        ],
    )

    assert result.exit_code == 2
    assert "both map to 'feat/swarm/foo'" in result.output
