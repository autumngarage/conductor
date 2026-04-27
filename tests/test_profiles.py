from __future__ import annotations

from click.testing import CliRunner

from conductor.cli import PROFILE_PRECEDENCE_TEXT, main
from conductor.providers import CallResponse
from conductor.router import RankedCandidate, RouteDecision


def _fake_response(provider: str = "codex") -> CallResponse:
    return CallResponse(
        text="ok",
        provider=provider,
        model="stub-model",
        duration_ms=10,
        usage={},
        cost_usd=0.0,
        raw={},
    )


def _fake_decision(provider: str = "codex") -> RouteDecision:
    candidate = RankedCandidate(
        name=provider,
        tier="frontier",
        tier_rank=4,
        matched_tags=("coding",),
        tag_score=1,
        cost_score=0.01,
        latency_ms=1000,
        health_penalty=0.0,
        combined_score=10.0,
    )
    return RouteDecision(
        provider=provider,
        prefer="best",
        effort="high",
        thinking_budget=4000,
        tier="frontier",
        task_tags=("coding",),
        matched_tags=("coding",),
        tools_requested=("Read",),
        sandbox="workspace-write",
        ranked=(candidate,),
        candidates_skipped=(),
    )


def test_call_profile_applies_builtin_defaults(mocker):
    pick_mock = mocker.patch(
        "conductor.cli.pick",
        return_value=("codex", _fake_decision()),
    )
    mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response(), []),
    )

    result = CliRunner().invoke(
        main,
        ["call", "--auto", "--profile", "coding", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert pick_mock.call_args.args[0] == ["coding", "tool-use"]
    assert pick_mock.call_args.kwargs["prefer"] == "best"
    assert pick_mock.call_args.kwargs["effort"] == "high"


def test_exec_profile_applies_builtin_sandbox(mocker):
    pick_mock = mocker.patch(
        "conductor.cli.pick",
        return_value=("codex", _fake_decision()),
    )
    mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response(), []),
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--profile", "coding", "--tools", "Read", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert pick_mock.call_args.kwargs["sandbox"] == "workspace-write"


def test_user_profile_overrides_builtin(mocker, monkeypatch, tmp_path):
    profiles_file = tmp_path / "profiles.toml"
    profiles_file.write_text(
        '[profiles.coding]\n'
        'prefer = "balanced"\n'
        'effort = "low"\n'
        'tags = "cheap"\n'
        'sandbox = "read-only"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CONDUCTOR_PROFILES_FILE", str(profiles_file))

    pick_mock = mocker.patch(
        "conductor.cli.pick",
        return_value=("codex", _fake_decision()),
    )
    mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response(), []),
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--profile", "coding", "--tools", "Read", "--task", "hi"],
    )

    assert result.exit_code == 0, result.output
    assert pick_mock.call_args.args[0] == ["cheap"]
    assert pick_mock.call_args.kwargs["prefer"] == "balanced"
    assert pick_mock.call_args.kwargs["effort"] == "low"
    assert pick_mock.call_args.kwargs["sandbox"] == "read-only"


def test_unknown_profile_raises_usage_error():
    result = CliRunner().invoke(
        main,
        ["exec", "--auto", "--profile", "does-not-exist", "--task", "hi"],
    )

    assert result.exit_code == 2
    assert "unknown profile" in result.output.lower()


def test_explicit_flags_override_profile_defaults(mocker):
    pick_mock = mocker.patch(
        "conductor.cli.pick",
        return_value=("codex", _fake_decision()),
    )
    mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response(), []),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--profile",
            "review",
            "--prefer",
            "best",
            "--effort",
            "high",
            "--tags",
            "coding,tool-use",
            "--sandbox",
            "workspace-write",
            "--tools",
            "Read",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pick_mock.call_args.args[0] == ["coding", "tool-use"]
    assert pick_mock.call_args.kwargs["prefer"] == "best"
    assert pick_mock.call_args.kwargs["effort"] == "high"
    assert pick_mock.call_args.kwargs["sandbox"] == "workspace-write"


def test_profile_env_cli_precedence(mocker, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_PREFER", "fastest")
    monkeypatch.setenv("CONDUCTOR_EFFORT", "low")
    monkeypatch.setenv("CONDUCTOR_TAGS", "cheap")
    monkeypatch.setenv("CONDUCTOR_SANDBOX", "strict")

    pick_mock = mocker.patch(
        "conductor.cli.pick",
        return_value=("codex", _fake_decision()),
    )
    mocker.patch(
        "conductor.cli._invoke_with_fallback",
        return_value=(_fake_response(), []),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--auto",
            "--profile",
            "review",
            "--effort",
            "high",
            "--tags",
            "coding,tool-use",
            "--tools",
            "Read",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert pick_mock.call_args.args[0] == ["coding", "tool-use"]
    assert pick_mock.call_args.kwargs["prefer"] == "fastest"
    assert pick_mock.call_args.kwargs["effort"] == "high"
    assert pick_mock.call_args.kwargs["sandbox"] == "strict"


def test_profiles_list_and_show_smoke(monkeypatch, tmp_path):
    profiles_file = tmp_path / "profiles.toml"
    profiles_file.write_text(
        '[profiles.local]\n'
        'prefer = "balanced"\n'
        'effort = "medium"\n'
        'tags = "cheap"\n'
        'sandbox = "read-only"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CONDUCTOR_PROFILES_FILE", str(profiles_file))

    runner = CliRunner()
    list_result = runner.invoke(main, ["profiles", "list"])
    show_result = runner.invoke(main, ["profiles", "show", "coding"])

    assert list_result.exit_code == 0, list_result.output
    assert "coding" in list_result.output
    assert "local" in list_result.output

    assert show_result.exit_code == 0, show_result.output
    assert "coding" in show_result.output
    assert "workspace-write" in show_result.output
    assert PROFILE_PRECEDENCE_TEXT in show_result.output
