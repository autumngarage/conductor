from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

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


def _fake_ship(worktree: Path, *, base_branch: str, auto_merge: bool):
    _ = (worktree, base_branch)
    if auto_merge:
        return "shipped", "https://github.com/example/repo/pull/123", "abc1234"
    return "pushed-not-merged", "https://github.com/example/repo/pull/123", None


def test_swarm_one_brief_succeeds_as_shipped(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

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
