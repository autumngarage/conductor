from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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


def _configure_cortex_pr_merged(repo: Path) -> None:
    template = repo / ".cortex" / "templates" / "journal" / "pr-merged.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# PR #{{ nnn }} merged\n", encoding="utf-8")
    (repo / ".cortex" / "protocol.md").write_text(
        "T1.9 requires journal/pr-merged.md\n",
        encoding="utf-8",
    )


def _swarm_manifests(repo: Path) -> list[Path]:
    return sorted((repo / ".cache" / "conductor" / "swarm" / "runs").glob("*.json"))


def _preflight_overlap_list(plan: dict[str, object], key: str) -> list[dict[str, object]]:
    overlaps = plan["overlaps"]
    assert isinstance(overlaps, dict)
    value = overlaps[key]
    assert isinstance(value, list)
    return cast("list[dict[str, object]]", value)


def _manual_swarm_manifest(
    repo: Path,
    *,
    tasks: list[dict[str, object]],
    base_ref: str | None = None,
    run_id: str = "20260101T000000000001Z-retry",
) -> Path:
    manifest_path = repo / ".cache" / "conductor" / "swarm" / "runs" / f"{run_id}.json"
    payload = cli._swarm_manifest_payload(
        run_id=run_id,
        repo_root=repo,
        base_branch="main",
        base_ref=base_ref or _git(repo, "rev-parse", "main").stdout.strip(),
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_ms=100,
        tasks=tasks,
    )
    cli._write_swarm_manifest(manifest_path, payload)
    return manifest_path


def test_new_swarm_run_id_preserves_subsecond_ordering() -> None:
    first = cli._new_swarm_run_id(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC))
    second = cli._new_swarm_run_id(datetime(2026, 1, 1, 0, 0, 0, 2, tzinfo=UTC))

    assert first.startswith("20260101T000000000001Z-")
    assert second.startswith("20260101T000000000002Z-")
    assert first < second


def _commit_change(worktree: Path, filename: str, message: str) -> None:
    (worktree / filename).write_text(f"{message}\n", encoding="utf-8")
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-m", message)


def _swarm_original_body(body: str) -> str:
    return body.split(f"\n\n{cli.SWARM_DELIVERY_CONTRACT}", 1)[0]


def _fake_exec_factory(*, fail_on: set[str] | None = None, no_change_on: set[str] | None = None):
    fail_on = fail_on or set()
    no_change_on = no_change_on or set()

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        body = _swarm_original_body(kwargs["body"])
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


def _ranked_provider(name: str, score: float) -> cli.RankedCandidate:
    return cli.RankedCandidate(
        name=name,
        tier="frontier",
        tier_rank=4,
        matched_tags=("tool-use",),
        tag_score=1,
        cost_score=0.0,
        latency_ms=1,
        health_penalty=0.0,
        combined_score=score,
    )


def _route_decision(*providers: str, tags: tuple[str, ...] = ("tool-use",)) -> cli.RouteDecision:
    ranked = tuple(
        _ranked_provider(provider, float(len(providers) - index))
        for index, provider in enumerate(providers)
    )
    return cli.RouteDecision(
        provider=providers[0],
        prefer="balanced",
        effort="medium",
        thinking_budget=0,
        tier="frontier",
        task_tags=tags,
        matched_tags=("tool-use",),
        tools_requested=tuple(cli.VALID_TOOLS),
        sandbox="none",
        ranked=ranked,
        candidates_skipped=(),
    )


def _fake_ship(worktree: Path, *, base_branch: str, branch: str, auto_merge: bool):
    _ = (worktree, base_branch, branch)
    if auto_merge:
        return cli.SwarmShipOutcome(
            "shipped",
            "https://github.com/example/repo/pull/123",
            "abc1234",
            merge_status="MERGED",
        )
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
        return cli.SwarmShipOutcome(
            "shipped",
            "https://github.com/example/repo/pull/123",
            merge_sha,
            merge_status="MERGED",
        )

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


def test_swarm_metrics_aggregate_mixed_task_statuses() -> None:
    metrics = cli._swarm_metrics_from_task_records(
        [
            {"status": "shipped", "commits": 2},
            {"status": "failed", "commits": 1},
            {"status": "provider-failed", "commits": 0},
            {"status": "no-changes", "commits": 0},
            {"status": "review-blocked", "commits": 3},
            {"status": "pushed-not-merged", "commits": 4},
        ],
        duration_ms=3_600_000,
    )

    assert metrics == {
        "schema_version": 1,
        "total_tasks": 6,
        "shipped_count": 1,
        "failed_count": 1,
        "provider_failed_count": 1,
        "no_changes_count": 1,
        "review_blocked_count": 1,
        "needs_human_conflict_resolution_count": 0,
        "pushed_not_merged_count": 1,
        "duration_ms": 3_600_000,
        "per_status_counts": {
            "failed": 1,
            "no-changes": 1,
            "provider-failed": 1,
            "pushed-not-merged": 1,
            "review-blocked": 1,
            "shipped": 1,
        },
        "total_commits": 10,
        "completed_tasks_per_hour": 2.0,
    }


@pytest.mark.parametrize("duration_ms", [0, None])
def test_swarm_metrics_zero_or_missing_duration_has_zero_throughput(
    duration_ms: int | None,
) -> None:
    metrics = cli._swarm_metrics_from_task_records(
        [{"status": "shipped", "commits": 1}],
        duration_ms=duration_ms,
    )

    assert metrics["duration_ms"] == duration_ms
    assert metrics["completed_tasks_per_hour"] == 0.0


def test_swarm_preflight_detects_duplicate_file_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Edit `src/conductor/session_log.py`.")
    second = _brief(repo, "second.md", "Update src/conductor/session_log.py too.")

    plan = cli._swarm_preflight_plan((str(first), str(second)), repo_root=repo)

    assert _preflight_overlap_list(plan, "file_paths") == [
        {"value": "src/conductor/session_log.py", "briefs": [str(first), str(second)]}
    ]


def test_swarm_preflight_detects_shared_subsystems(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Adjust swarm resume behavior.")
    second = _brief(repo, "second.md", "Audit swarm retry behavior.")

    plan = cli._swarm_preflight_plan((str(first), str(second)), repo_root=repo)

    assert _preflight_overlap_list(plan, "subsystems") == [
        {"value": "swarm", "briefs": [str(first), str(second)]}
    ]


def test_swarm_preflight_detects_duplicate_issue_refs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Issue: #369\n\nImplement part one.")
    second = _brief(repo, "second.md", "Issue: #369\n\nImplement part two.")

    plan = cli._swarm_preflight_plan((str(first), str(second)), repo_root=repo)

    assert _preflight_overlap_list(plan, "issue_refs") == [
        {"value": "#369", "briefs": [str(first), str(second)]}
    ]


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


def test_ship_swarm_pr_classifies_pre_push_validation_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", "main")
    _commit_change(worktree, "feature.txt", "feature")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "open-pr.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] != "bash":
            return real_run(args, **kwargs)
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(
                "pre-push hook failed\n"
                "failed command: bash scripts/touchstone-run.sh validate\n"
                "FAILED tests/test_cli_swarm.py::test_example\n"
            ),
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    outcome = cli._ship_swarm_pr(
        worktree,
        base_branch="main",
        branch="feat/swarm/foo",
        auto_merge=True,
    )

    assert outcome.status == "validation-failed"
    validation_failure = outcome.validation_failure
    assert validation_failure is not None
    output_tail = validation_failure["output_tail"]
    assert isinstance(output_tail, str)
    assert validation_failure["failed_command"] == (
        "bash scripts/touchstone-run.sh validate"
    )
    assert "FAILED tests/test_cli_swarm.py::test_example" in output_tail
    assert validation_failure["retry_command"] == (
        f"cd {worktree} && bash scripts/touchstone-run.sh validate"
    )


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
    monkeypatch.setattr(
        cli,
        "_swarm_pr_inspection",
        lambda *_args, **_kwargs: ("MERGED", "merge123", None),
    )

    outcome = cli._ship_swarm_pr(
        worktree,
        base_branch="main",
        branch="feat/swarm/foo",
        auto_merge=True,
    )
    status, pr_url, merge_sha = outcome

    assert status == "shipped"
    assert pr_url == "https://github.com/example/repo/pull/123"
    assert merge_sha == "merge123"
    assert outcome.merge_status == "MERGED"
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


def test_ship_swarm_pr_treats_github_merged_as_authoritative(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", "main")
    _commit_change(worktree, "feature.txt", "feature")
    scripts = repo / "scripts"
    scripts.mkdir()
    open_pr = scripts / "open-pr.sh"
    open_pr.write_text(
        "#!/usr/bin/env bash\n"
        'printf "https://github.com/example/repo/pull/123\\n"\n'
        'printf "local cleanup failed\\n" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    open_pr.chmod(0o755)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_swarm_pr_inspection",
        lambda *_args, **_kwargs: ("MERGED", "merge123", None),
    )

    outcome = cli._ship_swarm_pr(
        worktree,
        base_branch="main",
        branch="feat/swarm/foo",
        auto_merge=True,
    )

    assert outcome.status == "shipped"
    assert outcome.pr_url == "https://github.com/example/repo/pull/123"
    assert outcome.detail == "merge123"
    assert outcome.merge_status == "MERGED"


def test_swarm_one_brief_succeeds_as_shipped(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    seen: dict[str, object] = {}

    def fake_exec(**kwargs):
        seen.update(kwargs)
        worktree = Path(kwargs["cwd"])
        _commit_change(worktree, "foo.txt", "foo")
        return (
            CallResponse(text="done", provider=kwargs["provider_id"], model="test", duration_ms=1),
            None,
            None,
        )

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
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
    assert seen["allow_completion_stretch"] is True
    assert "Conductor swarm delivery contract:" in str(seen["body"])
    assert "Commit all intended changes" in str(seen["body"])
    assert not Path(payload["tasks"][0]["worktree"]).exists()
    assert (
        _git_returncode(repo, "show-ref", "--verify", "--quiet", "refs/heads/feat/swarm/foo")
        == 1
    )


def test_swarm_text_output_uses_orchestrated_manifest_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge"],
    )

    assert result.exit_code == 0, result.output
    assert "shipped:" in result.output
    assert "\nready-to-ship:" not in result.output
    assert "ok=True shipped=1 failed=0 no-changes=0 provider-failed=0" in result.output


def test_swarm_writes_manifest_for_successful_run(monkeypatch, tmp_path: Path) -> None:
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
    manifests = _swarm_manifests(repo)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == manifests[0].stem
    assert manifest["repo_root"] == str(repo)
    assert manifest["base_branch"] == "main"
    assert manifest["base_ref"]
    assert manifest["started_at"]
    assert manifest["ended_at"]
    assert isinstance(manifest["duration_ms"], int)
    assert manifest["ok"] is True
    assert manifest["shipped_count"] == 1
    assert manifest["failed_count"] == 0
    assert manifest["provider_failed_count"] == 0
    assert manifest["no_changes_count"] == 0
    assert manifest["metrics"]["schema_version"] == 1
    assert manifest["metrics"]["total_tasks"] == 1
    assert manifest["metrics"]["shipped_count"] == 1
    assert manifest["metrics"]["failed_count"] == 0
    assert manifest["metrics"]["provider_failed_count"] == 0
    assert manifest["metrics"]["no_changes_count"] == 0
    assert manifest["metrics"]["review_blocked_count"] == 0
    assert manifest["metrics"]["pushed_not_merged_count"] == 0
    assert manifest["metrics"]["duration_ms"] == manifest["duration_ms"]
    assert manifest["metrics"]["per_status_counts"] == {"shipped": 1}
    assert manifest["metrics"]["total_commits"] == 1
    assert manifest["metrics"]["completed_tasks_per_hour"] >= 0.0
    assert manifest["tasks"][0]["brief"] == str(brief)
    assert manifest["tasks"][0]["branch"] == "feat/swarm/foo"
    assert manifest["tasks"][0]["status"] == "shipped"
    assert manifest["tasks"][0]["commits"] == 1
    assert manifest["tasks"][0]["pr_url"] == "https://github.com/example/repo/pull/123"
    assert manifest["tasks"][0]["merge_sha"]
    assert manifest["tasks"][0]["cleanup_status"] == "removed"


def test_swarm_keeps_merged_pr_success_when_worktree_already_removed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())

    def ship_and_remove_worktree(
        worktree: Path,
        *,
        base_branch: str,
        branch: str,
        auto_merge: bool,
    ) -> cli.SwarmShipOutcome:
        assert auto_merge is True
        _ = branch
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
        _git(repo, "worktree", "remove", str(worktree))
        return cli.SwarmShipOutcome(
            "shipped",
            "https://github.com/example/repo/pull/123",
            merge_sha,
            merge_status="MERGED",
            inspection_warning="worktree was already removed before post-merge inspection",
        )

    monkeypatch.setattr(cli, "_ship_swarm_pr", ship_and_remove_worktree)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    task = payload["tasks"][0]
    assert payload["ok"] is True
    assert task["status"] == "shipped"
    assert task.get("failure_reason") is None

    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    manifest_task = manifest["tasks"][0]
    assert manifest_task["status"] == "shipped"
    assert manifest_task["merge_status"] == "MERGED"
    assert manifest_task["cleanup_status"] == "removed"
    assert manifest_task["inspection_warning"] == (
        "worktree was already removed before post-merge inspection"
    )
    assert manifest_task.get("failure_reason") is None
    assert not Path(manifest_task["worktree"]).exists()


def test_swarm_manifest_task_records_keep_cleanup_warning(tmp_path: Path) -> None:
    worktree = tmp_path / "removed"

    records = cli._swarm_manifest_task_records(
        [
            cli.SwarmTaskResult(
                brief="tasks/foo.md",
                branch="feat/swarm/foo",
                worktree=str(worktree),
                status="shipped",
                commits=1,
                pr_url="https://github.com/example/repo/pull/123",
                merge_sha="abc123",
                duration_ms=100,
                merge_status="MERGED",
                inspection_warning="inspect warning",
                cleanup_warning="cleanup warning",
            )
        ]
    )

    assert records[0]["merge_status"] == "MERGED"
    assert records[0]["inspection_warning"] == "inspect warning"
    assert records[0]["cleanup_warning"] == "cleanup warning"
    assert records[0]["cleanup_status"] == "removed"


def test_swarm_auto_routes_lane_with_task_tags(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    calls: list[str] = []

    def fake_pick(task_tags, **kwargs):
        assert task_tags == ["tool-use", "strong-reasoning"]
        assert kwargs["tools"] == frozenset(cli.VALID_TOOLS)
        decision = _route_decision("codex", tags=tuple(task_tags))
        return object(), decision

    def fake_exec(**kwargs):
        calls.append(kwargs["provider_id"])
        worktree = Path(kwargs["cwd"])
        _commit_change(worktree, "auto.txt", "auto")
        return (
            CallResponse(text="done", provider=kwargs["provider_id"], model="test", duration_ms=1),
            None,
            None,
        )

    monkeypatch.setattr(cli, "pick", fake_pick)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--tags", "strong-reasoning", "--brief", str(brief), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["codex"]
    payload = json.loads(result.stdout)
    assert "provider" not in payload["tasks"][0]
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["selected_provider"] == "codex"
    assert manifest["tasks"][0]["provider"] == "codex"
    assert manifest["tasks"][0]["fallback_provider"] is None
    assert manifest["tasks"][0]["fallback_reason"] is None


def test_swarm_auto_falls_back_and_records_provider_details(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "provider fallback")
    monkeypatch.chdir(repo)
    calls: list[str] = []

    def fake_pick(task_tags, **kwargs):
        _ = kwargs
        decision = _route_decision("codex", "gemini", tags=tuple(task_tags))
        return object(), decision

    def fake_exec(**kwargs):
        provider_id = kwargs["provider_id"]
        calls.append(provider_id)
        if provider_id == "codex":
            raise cli._ExecPhaseError(
                exit_code=1,
                exit_status="error",
                message="codex websocket disconnected during model refresh",
            )
        worktree = Path(kwargs["cwd"])
        _commit_change(worktree, "fallback.txt", "fallback")
        return (
            CallResponse(text="done", provider=provider_id, model="test", duration_ms=1),
            None,
            None,
        )

    monkeypatch.setattr(cli, "pick", fake_pick)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "auto", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["codex", "gemini"]
    manifest_path = _swarm_manifests(repo)[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert task["status"] == "shipped"
    assert task["selected_provider"] == "codex"
    assert task["provider"] == "gemini"
    assert task["fallback_provider"] == "gemini"
    assert task["fallback_reason"] == "codex websocket disconnected during model refresh"

    status_result = CliRunner().invoke(main, ["swarm", "status", str(manifest_path)])
    assert status_result.exit_code == 0, status_result.output
    assert "provider=gemini fallback=gemini" in status_result.output
    assert "fallback_reason=codex websocket disconnected during model refresh" in (
        status_result.output
    )


def test_swarm_pinned_provider_does_not_fallback(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "provider outage")
    monkeypatch.chdir(repo)
    calls: list[str] = []

    def fail_pick(*args, **kwargs):
        _ = (args, kwargs)
        raise AssertionError("pinned swarm provider must not use router fallback")

    def fake_exec(**kwargs):
        calls.append(kwargs["provider_id"])
        raise cli._ExecPhaseError(
            exit_code=1,
            exit_status="error",
            message="codex websocket disconnected during model refresh",
        )

    monkeypatch.setattr(cli, "pick", fail_pick)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--json"],
    )

    assert result.exit_code == 1
    assert calls == ["codex"]
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert task["status"] == "provider-failed"
    assert task["selected_provider"] == "codex"
    assert task["provider"] == "codex"
    assert task["fallback_provider"] is None
    assert task["fallback_reason"] is None


def test_swarm_queues_cortex_post_merge_bookkeeping_for_configured_repo(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _configure_cortex_pr_merged(repo)
    brief = _brief(repo, "issue-365.md", "Issue: #365\n\nmake a focused change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "tasks",
        "ok",
        "shipped_count",
        "failed_count",
        "no_changes_count",
    }
    assert "Cortex post-merge bookkeeping required" in result.stderr
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    bookkeeping = manifest["post_merge_bookkeeping"]
    assert bookkeeping["schema_version"] == 1
    assert bookkeeping["required"] is True
    assert bookkeeping["status"] == "follow-up-brief-queued"
    assert bookkeeping["follow_up_command"].startswith("conductor swarm --provider codex")
    assert bookkeeping["merged_prs"][0]["pr_number"] == "123"
    assert bookkeeping["merged_prs"][0]["closed_issues"] == ["#365"]
    assert bookkeeping["merged_prs"][0]["merge_sha"] == manifest["tasks"][0]["merge_sha"]
    context_path = Path(bookkeeping["context_path"])
    assert context_path.exists()
    context = context_path.read_text(encoding="utf-8")
    assert "PR #123: https://github.com/example/repo/pull/123" in context
    assert "closes issues: #365" in context
    assert f"merge commit: {manifest['tasks'][0]['merge_sha']}" in context
    assert "validation status: swarm auto-merge completed" in context
    assert "known residual risks: none recorded by swarm" in context
    assert ".cortex/journal/" in context
    assert ".cortex/state.md" in context


def test_swarm_cortex_bookkeeping_reads_relative_brief_from_repo_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _configure_cortex_pr_merged(repo)
    _brief(repo, "issue-365.md", "Issue: #365\n\nmake a focused change")
    subdir = repo / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    entry = cli._swarm_cortex_bookkeeping_entry(
        {
            "status": "shipped",
            "pr_url": "https://github.com/example/repo/pull/123",
            "brief": "tasks/issue-365.md",
            "branch": "feat/swarm/issue-365",
            "merge_sha": "abc1234",
            "commits": 1,
        },
        repo_root=repo,
        repo_slug="example/repo",
    )

    assert entry is not None
    assert entry["closed_issues"] == ["#365"]


def test_swarm_cortex_manual_commands_guard_existing_journal_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"

    commands = cli._swarm_cortex_manual_commands(
        run_id="20260511T000000000001Z-test",
        manifest_path=manifest_path,
        merged_prs=[
            {
                "pr_number": "123",
                "pr_url": "https://github.com/example/repo/pull/123",
            }
        ],
        hook_path=None,
    )

    assert any("test ! -e .cortex/journal/" in command for command in commands)
    assert any("already exists; inspect it before writing" in command for command in commands)
    assert any(
        command.startswith("cp .cortex/templates/journal/pr-merged.md")
        for command in commands
    )


def test_swarm_status_reports_cortex_post_merge_follow_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _configure_cortex_pr_merged(repo)
    brief = _brief(repo, "issue-365.md", "Issue: #365\n\nmake a focused change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))
    runner = CliRunner()

    run_result = runner.invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )
    assert run_result.exit_code == 0, run_result.output
    manifest_path = _swarm_manifests(repo)[0]

    status_result = runner.invoke(main, ["swarm", "status", str(manifest_path)])

    assert status_result.exit_code == 0, status_result.output
    assert "cortex-post-merge: follow-up-brief-queued" in status_result.output
    assert "context:" in status_result.output
    assert "command: conductor swarm --provider codex" in status_result.output


def test_swarm_does_not_queue_cortex_bookkeeping_when_not_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "issue-365.md", "Issue: #365\n\nmake a focused change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert "Cortex post-merge bookkeeping required" not in result.stderr
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["post_merge_bookkeeping"]["required"] is False
    assert manifest["post_merge_bookkeeping"]["status"] == "not-configured"


def test_swarm_manifest_contains_preflight_plan(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Touch src/conductor/session_log.py")
    second = _brief(repo, "second.md", "Also touch src/conductor/session_log.py")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_run_exec_phase_dispatch",
        _fake_exec_factory(no_change_on={first.read_text(), second.read_text()}),
    )

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
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["preflight"]["schema_version"] == 1
    assert manifest["preflight"]["overlaps"]["file_paths"] == [
        {"value": "src/conductor/session_log.py", "briefs": [str(first), str(second)]}
    ]


def test_swarm_json_stdout_omits_preflight_plan(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Touch src/conductor/session_log.py")
    second = _brief(repo, "second.md", "Also touch src/conductor/session_log.py")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_run_exec_phase_dispatch",
        _fake_exec_factory(no_change_on={first.read_text(), second.read_text()}),
    )

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
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"tasks", "ok", "shipped_count", "failed_count", "no_changes_count"}
    assert "preflight" not in payload


def test_swarm_preflight_only_writes_plan_without_worktrees(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "first.md", "Touch src/conductor/session_log.py")
    second = _brief(repo, "second.md", "Also touch src/conductor/session_log.py")
    monkeypatch.chdir(repo)

    def fail_exec(**kwargs):
        _ = kwargs
        raise AssertionError("preflight-only must not execute workers")

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fail_exec)

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
            "--preflight-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "swarm preflight: 2 lane(s), 1 warning(s)" in result.output
    assert "file paths:" in result.output
    assert not (repo / ".cache" / "conductor" / "swarm" / "first").exists()
    assert not (repo / ".cache" / "conductor" / "swarm" / "second").exists()
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["preflight"]["has_warnings"] is True


def test_swarm_writes_manifest_for_failed_run(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "fail change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli,
        "_run_exec_phase_dispatch",
        _fake_exec_factory(fail_on={"fail change"}),
    )
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 1
    manifests = _swarm_manifests(repo)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["ok"] is False
    assert manifest["failed_count"] == 1
    assert manifest["provider_failed_count"] == 0
    assert manifest["tasks"][0]["status"] == "failed"
    assert manifest["tasks"][0]["failure_reason"] == "max-iterations cap reached"
    assert manifest["tasks"][0]["cleanup_status"] == "preserved"
    assert Path(manifest["tasks"][0]["worktree"]).exists()


def test_swarm_validation_failed_manifest_resume_and_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "validation change")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())

    def validation_failed_ship(
        worktree: Path,
        *,
        base_branch: str,
        branch: str,
        auto_merge: bool,
    ) -> cli.SwarmShipOutcome:
        _ = (base_branch, auto_merge)
        output = (
            "pre-push hook failed\n"
            "failed check: bash scripts/touchstone-run.sh validate\n"
            "FAILED tests/test_cli_swarm.py::test_swarm_validation\n"
        )
        validation_failure = cli._swarm_validation_failure_from_output(
            output=output,
            worktree=worktree,
            branch=branch,
            push_command=f"git push -u origin {branch}",
        )
        assert validation_failure is not None
        return cli.SwarmShipOutcome(
            cli.SWARM_VALIDATION_FAILED_STATUS,
            None,
            output,
            validation_failure=validation_failure,
        )

    monkeypatch.setattr(cli, "_ship_swarm_pr", validation_failed_ship)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["failed_count"] == 1
    assert payload["tasks"][0]["status"] == "validation-failed"
    manifest_path = _swarm_manifests(repo)[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert task["status"] == "validation-failed"
    assert task["cleanup_status"] == "preserved"
    assert task["validation_failure"]["failed_command"] == (
        "bash scripts/touchstone-run.sh validate"
    )
    assert "FAILED tests/test_cli_swarm.py::test_swarm_validation" in task[
        "validation_failure"
    ]["output_tail"]
    assert task["validation_failure"]["suggested_next_command"] == (
        f"cd {task['worktree']} && bash scripts/touchstone-run.sh validate"
    )
    assert Path(task["worktree"]).exists()

    status = runner.invoke(main, ["swarm", "status", str(manifest_path)])
    assert status.exit_code == 0, status.output
    assert "validation-failed:" in status.output
    assert "failed check: bash scripts/touchstone-run.sh validate" in status.output
    assert (
        f"retry command: cd {task['worktree']} && bash scripts/touchstone-run.sh validate"
        in status.output
    )

    resume = runner.invoke(main, ["swarm", "resume", str(manifest_path), "1"])
    assert resume.exit_code == 0, resume.output
    assert f"worktree: {task['worktree']}" in resume.output
    assert "failed check: bash scripts/touchstone-run.sh validate" in resume.output
    assert (
        f"retry validation: cd {task['worktree']} && bash scripts/touchstone-run.sh validate"
        in resume.output
    )
    assert "re-enter worker: conductor exec --with codex" in resume.output


@pytest.mark.parametrize(
    ("mutate", "command", "expected"),
    [
        (
            lambda manifest: manifest["tasks"][0].__setitem__("branch", 123),
            "status",
            "tasks[0].branch is not a string",
        ),
        (
            lambda manifest: manifest["tasks"][0].__setitem__("commits", "1"),
            "resume",
            "tasks[0].commits is not an int",
        ),
        (
            lambda manifest: manifest["tasks"].append("not-a-task"),
            "status",
            "manifest.tasks[1] is not an object",
        ),
        (
            lambda manifest: manifest["tasks"][0].__setitem__(
                "conflict_state",
                {
                    "conflicted_files": ["README.md", 404],
                    "recovery_commands": ["git status"],
                },
            ),
            "status",
            "tasks[0].conflict_state.conflicted_files[1] is not a string",
        ),
    ],
)
def test_swarm_manifest_commands_reject_malformed_values(
    tmp_path: Path,
    mutate,
    command: str,
    expected: str,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    manifest_path = _manual_swarm_manifest(
        repo,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(repo / ".cache" / "conductor" / "swarm" / "foo"),
                "status": "failed",
                "commits": 1,
                "failure_reason": "needs recovery",
            }
        ],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    args = ["swarm", command, str(manifest_path)]
    if command == "resume":
        args.append("1")

    result = CliRunner().invoke(main, args)

    assert result.exit_code != 0
    assert expected in result.output


def test_swarm_classifies_exec_phase_provider_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "provider outage")
    monkeypatch.chdir(repo)

    def provider_failure(**kwargs):
        _ = kwargs
        raise cli._ExecPhaseError(
            exit_code=1,
            exit_status="error",
            message="codex websocket disconnected during model refresh",
        )

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", provider_failure)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        [
            "swarm",
            "--provider",
            "codex",
            "--brief",
            str(brief),
            "--remove-worktrees-on-failure",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["failed_count"] == 0
    assert payload["tasks"][0]["status"] == "provider-failed"
    assert payload["tasks"][0]["failure_reason"] == (
        "codex websocket disconnected during model refresh"
    )
    assert Path(payload["tasks"][0]["worktree"]).exists()
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["failed_count"] == 0
    assert manifest["provider_failed_count"] == 1
    assert manifest["metrics"]["provider_failed_count"] == 1
    assert manifest["metrics"]["per_status_counts"] == {"provider-failed": 1}
    assert manifest["tasks"][0]["cleanup_status"] == "preserved"


def test_swarm_classifies_runtime_provider_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)

    def provider_failure(**kwargs):
        _ = kwargs
        raise RuntimeError("provider HTTP 503: request timed out")

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", provider_failure)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["status"] == "provider-failed"
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["failure_reason"] == "provider HTTP 503: request timed out"


def test_swarm_sequential_internal_error_updates_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)

    def fake_run_swarm_task(**kwargs):
        _ = kwargs
        raise ValueError("boom")

    monkeypatch.setattr(cli, "_run_swarm_task", fake_run_swarm_task)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 1
    manifests = _swarm_manifests(repo)
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["ended_at"]
    assert manifest["ok"] is False
    assert manifest["failed_count"] == 1
    assert manifest["provider_failed_count"] == 0
    assert manifest["tasks"][0]["status"] == "failed"
    assert manifest["tasks"][0]["failure_reason"] == "internal swarm task error: boom"


def test_swarm_does_not_write_manifest_for_preflight_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    missing_brief = repo / "tasks" / "missing.md"
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(missing_brief), "--json"],
    )

    assert result.exit_code == 2
    assert _swarm_manifests(repo) == []


def test_swarm_rejects_reserved_manifest_worktree_slug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "runs.md")
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--json"],
    )

    assert result.exit_code == 2
    assert "reserved swarm worktree slug 'runs'" in result.output
    assert _swarm_manifests(repo) == []


def test_swarm_status_latest_and_explicit_lookup(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))
    runner = CliRunner()

    run_result = runner.invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert run_result.exit_code == 0, run_result.output
    manifest_path = _swarm_manifests(repo)[0]
    run_id = manifest_path.stem

    latest_result = runner.invoke(main, ["swarm", "status", "--latest"])
    assert latest_result.exit_code == 0, latest_result.output
    assert f"swarm run {run_id} (ok)" in latest_result.output
    assert "shipped=1 failed=0 no-changes=0" in latest_result.output
    assert (
        "metrics: total=1 review-blocked=0 pushed-not-merged=0 commits=1 completed/hour="
        in latest_result.output
    )
    assert "provider-failed=0" in latest_result.output
    assert "status-counts: shipped=1" in latest_result.output
    assert "shipped:" in latest_result.output
    assert "phase=shipping" in latest_result.output

    id_result = runner.invoke(main, ["swarm", "status", run_id])
    assert id_result.exit_code == 0, id_result.output
    assert f"manifest: {manifest_path}" in id_result.output

    path_result = runner.invoke(main, ["swarm", "status", str(manifest_path)])
    assert path_result.exit_code == 0, path_result.output
    assert f"swarm run {run_id} (ok)" in path_result.output


def test_swarm_manifest_records_lane_phase_progress(monkeypatch, tmp_path: Path) -> None:
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
    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert task["last_phase"] == "shipping"
    assert isinstance(task["last_progress_at"], str)
    phases = [event["phase"] for event in task["phase_history"]]
    assert phases == ["starting", "editing", "testing", "committing", "reviewing", "shipping"]


def test_swarm_status_latest_reports_active_and_stalled_phases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    now = cli._swarm_now_iso()
    old = (
        datetime.now(UTC) - timedelta(seconds=cli.DEFAULT_EXEC_MAX_STALL_SEC + 30)
    ).isoformat().replace("+00:00", "Z")
    run_id = "20260101T000000000001Z-progress"
    manifest_path = repo / ".cache" / "conductor" / "swarm" / "runs" / f"{run_id}.json"
    payload = cli._swarm_manifest_payload(
        run_id=run_id,
        repo_root=repo,
        base_branch="main",
        base_ref=_git(repo, "rev-parse", "main").stdout.strip(),
        started_at="2026-01-01T00:00:00Z",
        ended_at=None,
        duration_ms=None,
        tasks=[
            {
                "brief": "tasks/active.md",
                "branch": "feat/swarm/active",
                "worktree": str(repo / ".cache" / "conductor" / "swarm" / "active"),
                "status": "running",
                "commits": 0,
                "last_phase": "editing",
                "last_progress_at": now,
            },
            {
                "brief": "tasks/stalled.md",
                "branch": "feat/swarm/stalled",
                "worktree": str(repo / ".cache" / "conductor" / "swarm" / "stalled"),
                "status": "running",
                "commits": 0,
                "last_phase": "testing",
                "last_progress_at": old,
            },
        ],
    )
    cli._write_swarm_manifest(manifest_path, payload)

    result = CliRunner().invoke(main, ["swarm", "status", "--latest"])

    assert result.exit_code == 0, result.output
    assert "running: tasks/active.md -> feat/swarm/active active phase=editing" in result.output
    assert "running: tasks/stalled.md -> feat/swarm/stalled stalled phase=testing" in result.output


def test_swarm_status_reports_provider_failed_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "provider-failed",
                "commits": 0,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "rate limit: quota exceeded",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "status", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert "failed=0 no-changes=0 provider-failed=1" in result.output
    assert "metrics: total=1 review-blocked=0 pushed-not-merged=0 commits=0" in result.output
    assert "provider-failed=1" in result.output
    assert "status-counts: provider-failed=1" in result.output
    assert "provider-failed:" in result.output


def test_swarm_retry_ship_updates_review_blocked_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", _fake_exec_factory())

    def review_blocked_ship(
        worktree: Path,
        *,
        base_branch: str,
        branch: str,
        auto_merge: bool,
    ):
        _ = (worktree, base_branch, branch, auto_merge)
        return "review-blocked", "https://github.com/example/repo/pull/123", "review flaked"

    monkeypatch.setattr(cli, "_ship_swarm_pr", review_blocked_ship)
    run_result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )
    assert run_result.exit_code == 1, run_result.output
    manifest_path = _swarm_manifests(repo)[0]
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert before["tasks"][0]["status"] == "review-blocked"
    assert before["metrics"]["review_blocked_count"] == 1

    def fail_if_exec_reruns(**kwargs):
        _ = kwargs
        raise AssertionError("retry-ship must not rerun implementation")

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fail_if_exec_reruns)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    retry_result = CliRunner().invoke(main, ["swarm", "retry-ship", manifest_path.stem, "1"])

    assert retry_result.exit_code == 0, retry_result.output
    assert "shipped:" in retry_result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert manifest["ok"] is True
    assert manifest["shipped_count"] == 1
    assert manifest["failed_count"] == 0
    assert manifest["metrics"]["shipped_count"] == 1
    assert manifest["metrics"]["review_blocked_count"] == 0
    assert manifest["metrics"]["per_status_counts"] == {"shipped": 1}
    assert manifest["metrics"]["total_commits"] == 1
    assert task["status"] == "shipped"
    assert task["pr_url"] == "https://github.com/example/repo/pull/123"
    assert task["merge_sha"]
    assert task["failure_reason"] is None
    assert task["cleanup_status"] == "removed"
    assert not Path(task["worktree"]).exists()


def test_swarm_resume_reports_failed_lane_with_worktree_and_commits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(worktree, "feature.txt", "feature")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": "https://github.com/example/repo/pull/123",
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "review flaked",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "resume", str(manifest_path), "1"])

    assert result.exit_code == 0, result.output
    assert f"manifest: {manifest_path}" in result.output
    assert f"run id: {manifest_path.stem}" in result.output
    assert "task index: 1" in result.output
    assert "status: review-blocked" in result.output
    assert f"brief: {brief}" in result.output
    assert "branch: feat/swarm/foo" in result.output
    assert f"worktree: {worktree}" in result.output
    assert "commits: 1" in result.output
    assert "pr url: https://github.com/example/repo/pull/123" in result.output
    assert "failure reason: review flaked" in result.output
    assert "worktree exists: yes" in result.output
    assert "worktree dirty: no" in result.output
    assert "worktree branch: feat/swarm/foo" in result.output
    assert f"retry shipping: conductor swarm retry-ship {manifest_path} 1" in result.output
    assert (
        f"rerun implementation: conductor swarm --provider codex "
        f"--base-branch main --brief {brief}"
    ) in result.output


def test_swarm_resume_reports_missing_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "failed",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "implementation failed",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "resume", str(manifest_path), "1"])

    assert result.exit_code == 0, result.output
    assert "worktree exists: no" in result.output
    assert "worktree dirty: unknown (worktree missing)" in result.output
    assert "worktree branch: unknown (worktree missing)" in result.output
    assert "retry shipping: unavailable until the worktree exists and has commits" in result.output


@pytest.mark.parametrize("selector_kind", ["index", "branch", "brief"])
def test_swarm_resume_selects_by_index_branch_or_brief(
    tmp_path: Path,
    monkeypatch,
    selector_kind: str,
) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "foo.md")
    second = _brief(repo, "bar.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    foo_worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    bar_worktree = repo / ".cache" / "conductor" / "swarm" / "bar"
    _git(repo, "worktree", "add", str(foo_worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(foo_worktree, "foo.txt", "foo")
    _git(repo, "worktree", "add", str(bar_worktree), "-b", "feat/swarm/bar", base_ref)
    _commit_change(bar_worktree, "bar.txt", "bar")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(first),
                "branch": "feat/swarm/foo",
                "worktree": str(foo_worktree),
                "status": "failed",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "push failed",
                "cleanup_status": "preserved",
            },
            {
                "brief": str(second),
                "branch": "feat/swarm/bar",
                "worktree": str(bar_worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": "https://github.com/example/repo/pull/124",
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "review flaked",
                "cleanup_status": "preserved",
            },
        ],
    )
    selector = {
        "index": "2",
        "branch": "feat/swarm/bar",
        "brief": str(second),
    }[selector_kind]

    result = CliRunner().invoke(main, ["swarm", "resume", str(manifest_path), selector])

    assert result.exit_code == 0, result.output
    assert "task index: 2" in result.output
    assert f"brief: {second}" in result.output
    assert "branch: feat/swarm/bar" in result.output
    assert "failure reason: review flaked" in result.output


def test_swarm_resume_retry_ship_delegates_to_retry_ship_update_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(worktree, "feature.txt", "feature")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": "https://github.com/example/repo/pull/123",
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "review flaked",
                "cleanup_status": "preserved",
            }
        ],
    )
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

    result = CliRunner().invoke(main, ["swarm", "resume", str(manifest_path), "1", "--retry-ship"])

    assert result.exit_code == 0, result.output
    assert "shipped:" in result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = manifest["tasks"][0]
    assert manifest["ok"] is True
    assert manifest["metrics"]["shipped_count"] == 1
    assert task["status"] == "shipped"
    assert task["cleanup_status"] == "removed"
    assert not worktree.exists()


def test_swarm_resume_reports_dirty_detached_worktree_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(worktree, "feature.txt", "feature")
    branch_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "checkout", "--detach", "HEAD")
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "failed",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "ship failed",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "resume", str(manifest_path), "1"])

    assert result.exit_code == 0, result.output
    assert "worktree dirty: yes" in result.output
    assert "dirty.txt" in result.output
    assert "worktree branch: detached at " in result.output
    assert _git(repo, "rev-parse", "feat/swarm/foo").stdout.strip() == branch_head
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "failed"


def test_swarm_retry_ship_refuses_missing_worktree(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(repo / ".cache" / "conductor" / "swarm" / "foo"),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": "https://github.com/example/repo/pull/123",
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "review flaked",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "retry-ship", str(manifest_path), "1"])

    assert result.exit_code == 1
    assert "worktree does not exist" in result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "review-blocked"


def test_swarm_retry_ship_refuses_no_commits_ahead(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "review-blocked",
                "commits": 0,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "ship failed",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "retry-ship", str(manifest_path), "1"])

    assert result.exit_code == 1
    assert "has no commits ahead of manifest base_ref" in result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "review-blocked"
    assert worktree.exists()


def test_swarm_retry_ship_refuses_detached_worktree_without_moving_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(worktree, "feature.txt", "feature")
    branch_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "checkout", "--detach", "HEAD")
    _commit_change(worktree, "detached.txt", "detached")
    detached_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert detached_head != branch_head
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "ship failed",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "retry-ship", str(manifest_path), "1"])

    assert result.exit_code == 1
    assert "detached HEAD" in result.output
    assert "refuses to retarget the saved branch" in result.output
    assert _git(repo, "rev-parse", "feat/swarm/foo").stdout.strip() == branch_head
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "review-blocked"


def test_swarm_retry_ship_refuses_dirty_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    _git(repo, "worktree", "add", str(worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(worktree, "feature.txt", "feature")
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(brief),
                "branch": "feat/swarm/foo",
                "worktree": str(worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "ship failed",
                "cleanup_status": "preserved",
            }
        ],
    )

    result = CliRunner().invoke(main, ["swarm", "retry-ship", str(manifest_path), "1"])

    assert result.exit_code == 1
    assert "worktree has uncommitted changes" in result.output
    assert "dirty.txt" in result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "review-blocked"


@pytest.mark.parametrize("selector_kind", ["index", "branch", "brief"])
def test_swarm_retry_ship_selects_by_index_branch_or_brief(
    monkeypatch,
    tmp_path: Path,
    selector_kind: str,
) -> None:
    repo = _repo(tmp_path)
    first = _brief(repo, "foo.md")
    second = _brief(repo, "bar.md")
    base_ref = _git(repo, "rev-parse", "main").stdout.strip()
    foo_worktree = repo / ".cache" / "conductor" / "swarm" / "foo"
    bar_worktree = repo / ".cache" / "conductor" / "swarm" / "bar"
    _git(repo, "worktree", "add", str(foo_worktree), "-b", "feat/swarm/foo", base_ref)
    _commit_change(foo_worktree, "foo.txt", "foo")
    _git(repo, "worktree", "add", str(bar_worktree), "-b", "feat/swarm/bar", base_ref)
    _commit_change(bar_worktree, "bar.txt", "bar")
    monkeypatch.chdir(repo)
    manifest_path = _manual_swarm_manifest(
        repo,
        base_ref=base_ref,
        tasks=[
            {
                "brief": str(first),
                "branch": "feat/swarm/foo",
                "worktree": str(foo_worktree),
                "status": "failed",
                "commits": 1,
                "pr_url": None,
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "push failed",
                "cleanup_status": "preserved",
            },
            {
                "brief": str(second),
                "branch": "feat/swarm/bar",
                "worktree": str(bar_worktree),
                "status": "review-blocked",
                "commits": 1,
                "pr_url": "https://github.com/example/repo/pull/124",
                "merge_sha": None,
                "duration_ms": 10,
                "failure_reason": "review flaked",
                "cleanup_status": "preserved",
            },
        ],
    )
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))
    selector = {
        "index": "2",
        "branch": "feat/swarm/bar",
        "brief": str(second),
    }[selector_kind]

    result = CliRunner().invoke(main, ["swarm", "retry-ship", str(manifest_path), selector])

    assert result.exit_code == 0, result.output
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "failed"
    assert manifest["tasks"][1]["status"] == "shipped"
    assert manifest["metrics"]["failed_count"] == 1
    assert manifest["metrics"]["shipped_count"] == 1
    assert manifest["metrics"]["total_commits"] == 2


def test_swarm_status_latest_uses_run_id_not_mtime(monkeypatch, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    runs_dir = repo / ".cache" / "conductor" / "swarm" / "runs"
    runs_dir.mkdir(parents=True)
    older = runs_dir / "20260101T000000000001Z-older.json"
    newer = runs_dir / "20260101T000000000002Z-newer.json"
    base_payload = {
        "schema_version": 1,
        "repo_root": str(repo),
        "base_branch": "main",
        "base_ref": "abc123",
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1,
        "tasks": [],
        "ok": True,
        "shipped_count": 0,
        "failed_count": 0,
        "no_changes_count": 0,
    }
    older.write_text(
        json.dumps({**base_payload, "run_id": older.stem}),
        encoding="utf-8",
    )
    newer.write_text(
        json.dumps({**base_payload, "run_id": newer.stem}),
        encoding="utf-8",
    )
    os.utime(older, (200, 200))
    os.utime(newer, (100, 100))

    result = CliRunner().invoke(main, ["swarm", "status", "--latest"])

    assert result.exit_code == 0, result.output
    assert f"swarm run {newer.stem} (ok)" in result.output


def test_repo_ignores_generated_conductor_cache() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert ".cache/conductor/" in gitignore


def test_swarm_json_stdout_contract_keeps_top_level_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
    assert set(payload) == {
        "tasks",
        "ok",
        "shipped_count",
        "failed_count",
        "no_changes_count",
    }


def test_swarm_merge_plan_orders_dependencies_and_records_overlap() -> None:
    tasks: list[dict[str, object]] = [
        {
            "brief": "tasks/app.md",
            "branch": "feat/swarm/app",
            "status": "ready-to-ship",
            "changed_files": ["src/app.py"],
        },
        {
            "brief": "tasks/core.md",
            "branch": "feat/swarm/core",
            "status": "ready-to-ship",
            "changed_files": ["src/app.py", "src/core.py"],
        },
        {
            "brief": "tasks/docs.md",
            "branch": "feat/swarm/docs",
            "status": "ready-to-ship",
            "changed_files": ["docs/usage.md"],
        },
    ]
    preflight: dict[str, object] = {
        "lanes": [
            {"brief": "tasks/app.md", "issue_refs": ["#1"], "depends_on": ["feat/swarm/core"]},
            {"brief": "tasks/core.md", "issue_refs": ["#1"], "depends_on": []},
            {"brief": "tasks/docs.md", "issue_refs": [], "depends_on": []},
        ]
    }

    plan = cli._swarm_merge_plan(tasks, preflight=preflight)
    lanes = cli._swarm_dict_list(plan.get("lanes"))

    assert [lane["branch"] for lane in lanes] == [
        "feat/swarm/core",
        "feat/swarm/app",
        "feat/swarm/docs",
    ]
    assert lanes[0]["overlaps_with"] == ["tasks/app.md"]
    assert lanes[1]["issue_refs"] == ["#1"]


def test_swarm_merge_plan_uses_lowest_shared_issue_group_deterministically() -> None:
    tasks: list[dict[str, object]] = [
        {
            "brief": "tasks/a.md",
            "branch": "feat/swarm/a",
            "status": "ready-to-ship",
            "changed_files": ["src/a.py"],
        },
        {
            "brief": "tasks/c.md",
            "branch": "feat/swarm/c",
            "status": "ready-to-ship",
            "changed_files": ["src/c.py"],
        },
        {
            "brief": "tasks/b.md",
            "branch": "feat/swarm/b",
            "status": "ready-to-ship",
            "changed_files": ["src/b.py"],
        },
    ]
    preflight: dict[str, object] = {
        "lanes": [
            {"brief": "tasks/a.md", "issue_refs": ["#2", "#1"], "depends_on": []},
            {"brief": "tasks/c.md", "issue_refs": ["#1"], "depends_on": []},
            {"brief": "tasks/b.md", "issue_refs": ["#2"], "depends_on": []},
        ]
    }

    plan = cli._swarm_merge_plan(tasks, preflight=preflight)
    lanes = cli._swarm_dict_list(plan.get("lanes"))

    assert lanes[0]["branch"] == "feat/swarm/a"


def test_swarm_auto_merge_marks_rebase_conflict_for_human_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "add conflict fixture")
    first = _brief(repo, "first.md", "first lane touches conflict.txt")
    second = _brief(repo, "second.md", "second lane touches conflict.txt")
    monkeypatch.chdir(repo)

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        body = _swarm_original_body(kwargs["body"])
        value = "first\n" if body.startswith("first lane") else "second\n"
        (worktree / "conflict.txt").write_text(value, encoding="utf-8")
        _git(worktree, "add", "conflict.txt")
        _git(worktree, "commit", "-m", body.split()[0])
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
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_merged_ship(repo))

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
    assert payload["tasks"][0]["status"] == "shipped"
    assert payload["tasks"][1]["status"] == cli.SWARM_CONFLICT_STATUS
    assert payload["failed_count"] == 1

    manifest = json.loads(_swarm_manifests(repo)[0].read_text(encoding="utf-8"))
    conflicted = manifest["tasks"][1]
    assert conflicted["status"] == cli.SWARM_CONFLICT_STATUS
    assert conflicted["conflict_state"]["conflicted_files"] == ["conflict.txt"]
    assert any(
        "git rebase --continue" in command
        for command in conflicted["conflict_state"]["recovery_commands"]
    )
    assert manifest["merge_plan"]["lanes"][0]["task_index"] == 1

    status = CliRunner().invoke(main, ["swarm", "status", str(_swarm_manifests(repo)[0])])
    assert status.exit_code == 0
    assert "conflicted files: conflict.txt" in status.output
    assert "recovery commands:" in status.output


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


def test_swarm_commits_dirty_worktree_before_shipping(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md", "make a focused change")
    monkeypatch.chdir(repo)
    merged_ship = _fake_merged_ship(repo)

    def fake_exec(**kwargs):
        worktree = Path(kwargs["cwd"])
        (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
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
        assert _git(worktree, "status", "--porcelain").stdout == ""
        assert _git(worktree, "log", "-1", "--format=%s").stdout.strip() == "swarm: foo"
        body = _git(worktree, "log", "-1", "--format=%b").stdout
        assert "Conductor swarm task:" in body
        assert "make a focused change" in body
        assert _git(worktree, "rev-list", "--count", f"{base_branch}..HEAD").stdout.strip() == "1"
        return merged_ship(
            worktree,
            base_branch=base_branch,
            branch=branch,
            auto_merge=auto_merge,
        )

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", fake_exec)
    monkeypatch.setattr(cli, "_ship_swarm_pr", fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief), "--auto-merge", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
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


def test_swarm_non_json_summary_includes_provider_failed_count(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, "foo.md")
    monkeypatch.chdir(repo)

    def provider_failure(**kwargs):
        _ = kwargs
        raise RuntimeError("provider HTTP 503: request timed out")

    monkeypatch.setattr(cli, "_run_exec_phase_dispatch", provider_failure)
    monkeypatch.setattr(cli, "_ship_swarm_pr", _fake_ship)

    result = CliRunner().invoke(
        main,
        ["swarm", "--provider", "codex", "--brief", str(brief)],
    )

    assert result.exit_code == 1
    assert "provider-failed:" in result.output
    assert "ok=False shipped=0 failed=0 no-changes=0 provider-failed=1" in result.output


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
        body = _swarm_original_body(kwargs["body"])
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
