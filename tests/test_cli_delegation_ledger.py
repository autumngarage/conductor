from __future__ import annotations

from click.testing import CliRunner

from conductor.cli import main
from conductor.providers import CallResponse
from conductor.router import RankedCandidate, RouteDecision


def _response(provider: str = "fake", model: str = "fake-model") -> CallResponse:
    return CallResponse(
        text="ok",
        provider=provider,
        model=model,
        duration_ms=42,
        usage={
            "input_tokens": 3,
            "output_tokens": 2,
            "thinking_tokens": None,
            "cached_tokens": None,
            "effort": "medium",
            "thinking_budget": None,
        },
        cost_usd=None,
        raw={},
    )


class FakeProvider:
    name = "fake"
    default_model = "fake-model"
    supported_tools = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
    enforces_exec_tool_permissions = True

    def call(self, *args, **kwargs):
        return _response()

    def exec(self, *args, **kwargs):
        return _response()

    def health_probe(self, timeout_sec: float = 30.0):
        return True, None

    def endpoint_url(self):
        return None


class FakeCouncilProvider:
    def __init__(self):
        self.calls = 0

    def call(self, *args, **kwargs):
        self.calls += 1
        return _response(provider="openrouter", model=kwargs.get("model") or "synth")


def _decision(provider: str = "codex") -> RouteDecision:
    ranked = RankedCandidate(
        name=provider,
        tier="frontier",
        tier_rank=4,
        matched_tags=("code",),
        tag_score=1,
        cost_score=0.0,
        latency_ms=1,
        health_penalty=0.0,
        combined_score=1.0,
    )
    return RouteDecision(
        provider=provider,
        prefer="best",
        effort="medium",
        thinking_budget=0,
        tier="frontier",
        task_tags=("code",),
        matched_tags=("code",),
        tools_requested=("Read",),
        sandbox="none",
        ranked=(ranked,),
        candidates_skipped=(),
    )


def test_call_records_ledger_event(monkeypatch):
    events = []
    monkeypatch.setattr("conductor.cli.get_provider", lambda provider_id: FakeProvider())
    monkeypatch.setattr("conductor.cli.record_delegation", events.append)

    result = CliRunner().invoke(main, ["call", "--with", "fake", "--task", "hi"])

    assert result.exit_code == 0, result.output
    assert len(events) == 1
    assert events[0].command == "call"
    assert events[0].provider == "fake"
    assert events[0].status == "ok"


def test_exec_records_ledger_event(monkeypatch):
    events = []
    monkeypatch.setattr("conductor.cli.get_provider", lambda provider_id: FakeProvider())
    monkeypatch.setattr("conductor.cli.record_delegation", events.append)

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "fake",
            "--task",
            "this is long enough for a test brief",
            "--no-preflight",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(events) == 1
    assert events[0].command == "exec"
    assert events[0].provider == "fake"
    assert events[0].status == "ok"


def test_ask_records_ledger_event(monkeypatch):
    events = []
    monkeypatch.setattr("conductor.cli.record_delegation", events.append)
    monkeypatch.setattr("conductor.cli.pick", lambda *args, **kwargs: ("codex", _decision()))
    monkeypatch.setattr(
        "conductor.cli._invoke_with_fallback",
        lambda *args, **kwargs: (_response(provider="codex", model="gpt-test"), []),
    )

    result = CliRunner().invoke(
        main,
        [
            "ask",
            "--kind",
            "code",
            "--task",
            "this is long enough for semantic exec",
            "--no-preflight",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(events) == 1
    assert events[0].command == "ask"
    assert events[0].provider == "codex"
    assert events[0].status == "ok"


def test_council_records_parent_and_member_events(monkeypatch):
    events = []
    fake_council = FakeCouncilProvider()
    monkeypatch.setattr("conductor.cli.record_delegation", events.append)
    monkeypatch.setattr(
        "conductor.cli._openrouter_council_provider",
        lambda **kwargs: fake_council,
    )

    result = CliRunner().invoke(
        main,
        ["ask", "--kind", "council", "--task", "answer this", "--json"],
    )

    assert result.exit_code == 0, result.output
    parent_events = [event for event in events if event.council_role == "parent"]
    child_events = [event for event in events if event.parent_delegation_id]
    assert len(parent_events) == 1
    assert parent_events[0].command == "council"
    assert parent_events[0].provider == "openrouter"
    assert child_events
