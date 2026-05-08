from __future__ import annotations

import pytest

from conductor.providers._tool_weights import ToolBudgetCounter, tool_call_weight


def _function_call(name: str, args: dict, call_id: str) -> dict:
    return {
        "type": "item.completed",
        "item": {
            "type": "function_call",
            "id": call_id,
            "name": name,
            "arguments": args,
        },
    }


def _function_output(output: str, call_id: str) -> dict:
    return {
        "type": "item.completed",
        "item": {
            "type": "function_call_output",
            "id": call_id,
            "output": output,
        },
    }


def test_tool_weight_table_for_mixed_session() -> None:
    counter = ToolBudgetCounter()
    events = [
        _function_call("Read", {"limit": 100}, "read-small"),
        _function_call("Read", {"limit": 300}, "read-large"),
        _function_call("Glob", {"pattern": "*.py"}, "glob"),
        _function_call("Edit", {"old_string": "a\n", "new_string": "b\n"}, "edit"),
        _function_call("Write", {"content": "x\n" * 101}, "write"),
        _function_call("Bash", {"command": "pytest"}, "bash"),
        _function_output("x" * 501, "bash"),
        _function_call("Mystery", {}, "unknown"),
    ]

    for event in events:
        counter.observe_event(event)

    assert counter.raw_count == 7
    assert counter.weighted_total == pytest.approx(6.7)


def test_many_small_reads_and_globs_fit_in_budget_30() -> None:
    counter = ToolBudgetCounter()

    for idx in range(47):
        name = "Read" if idx % 2 else "Glob"
        counter.observe_event(_function_call(name, {"limit": 20}, f"call-{idx}"))

    assert counter.raw_count == 47
    assert counter.weighted_total == pytest.approx(14.1)
    assert counter.weighted_total < 30


def test_heavy_bash_session_exhausts_budget_30() -> None:
    counter = ToolBudgetCounter()

    for idx in range(30):
        call_id = f"bash-{idx}"
        counter.observe_event(_function_call("Bash", {"command": "pytest"}, call_id))
        counter.observe_event(_function_output("x" * 501, call_id))

    assert counter.raw_count == 30
    assert counter.weighted_total == pytest.approx(45.0)
    assert counter.weighted_total >= 30


def test_typical_raw_30_session_still_fits_weighted_30() -> None:
    counter = ToolBudgetCounter()

    for idx in range(10):
        counter.observe_event(_function_call("Read", {"limit": 300}, f"read-{idx}"))
    for idx in range(10):
        counter.observe_event(
            _function_call(
                "Edit",
                {"old_string": "old\n" * 21, "new_string": "new\n" * 21},
                f"edit-{idx}",
            )
        )
    for idx in range(10):
        counter.observe_event(_function_call("Bash", {"command": "true"}, f"bash-{idx}"))
        counter.observe_event(_function_output("ok", f"bash-{idx}"))

    assert counter.raw_count == 30
    assert counter.weighted_total == pytest.approx(25.0)
    assert counter.weighted_total < 30


def test_read_and_bash_weights_can_be_refined_by_tool_output() -> None:
    assert tool_call_weight("Read", args={"limit": 20}) == pytest.approx(0.3)
    assert tool_call_weight("Read", result="x\n" * 201) == pytest.approx(1.0)
    assert tool_call_weight("Bash", args={"command": "pytest"}) == pytest.approx(0.5)
    assert tool_call_weight("Bash", result="x" * 501) == pytest.approx(1.5)
