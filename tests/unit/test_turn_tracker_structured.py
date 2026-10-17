"""`TurnTracker`'s structured side: facts an inner agent states, not entries it writes."""

from __future__ import annotations

import pytest

from agentproxy.turn_tracker import (
    KIND_UNOWNED,
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    STATE_RUNNING,
    TASK_AGENT,
    TASK_SHELL,
    TurnTracker,
)


@pytest.fixture
def tracker() -> TurnTracker:
    return TurnTracker()


def _typed(tracker: TurnTracker, text: str = "hello"):
    turn, _ = tracker.submit(text)
    tracker.mark_typed(turn)
    return turn


def test_binding_the_typed_turn_starts_it_without_comparing_any_text(tracker):
    turn = _typed(tracker)
    assert tracker.bind_typed() == [{"type": "turn_start", "turn": turn.ref()}]
    assert tracker.running_turn is turn
    assert tracker.typed_turn is None
    assert turn.state == STATE_RUNNING


def test_binding_with_nothing_typed_changes_nothing(tracker):
    assert tracker.bind_typed() == []
    assert tracker.running_turn is None


def test_binding_while_another_turn_runs_is_refused(tracker):
    _, _ = tracker.open_unowned()
    _typed(tracker)
    assert tracker.bind_typed() == []
    assert tracker.typed_turn is not None


def test_an_unowned_turn_counts_until_it_is_ended(tracker):
    turn, events = tracker.open_unowned()
    assert turn.kind == KIND_UNOWNED
    assert events == [{"type": "turn_start", "turn": turn.ref()}]
    assert tracker.outstanding()["unowned"] == 1
    tracker.end_turn(turn.id, "codex:completed", OUTCOME_SUCCESS)
    assert tracker.outstanding()["unowned"] == 0


def test_an_unowned_turn_cannot_open_while_one_runs(tracker):
    tracker.open_unowned()
    assert tracker.open_unowned() == (None, [])
    assert tracker.outstanding()["unowned"] == 1


def test_ending_a_turn_reports_its_basis_usage_and_error(tracker):
    turn = _typed(tracker)
    tracker.bind_typed()
    (event,) = tracker.end_turn(
        turn.id, "codex:failed", OUTCOME_ERROR, usage={"duration_ms": 5856}, error="quota"
    )
    assert event == {
        "type": "turn_end",
        "turn": turn.ref(),
        "basis": "codex:failed",
        "outcome": OUTCOME_ERROR,
        "usage": {"duration_ms": 5856},
        "error": "quota",
    }
    assert tracker.running_turn is None
    assert tracker.outstanding()["user"] == 0


def test_a_turn_end_without_an_error_carries_no_error_key(tracker):
    turn = _typed(tracker)
    tracker.bind_typed()
    (event,) = tracker.end_turn(turn.id, "codex:completed", OUTCOME_SUCCESS)
    assert "error" not in event


def test_a_typed_turn_can_be_ended_before_it_ever_ran(tracker):
    turn = _typed(tracker)
    assert tracker.end_turn(turn.id, "fallback:unmatched", OUTCOME_SUCCESS)
    assert tracker.typed_turn is None


def test_ending_an_ended_or_unknown_turn_is_ignored(tracker):
    turn = _typed(tracker)
    tracker.bind_typed()
    tracker.end_turn(turn.id, "codex:interrupted", OUTCOME_CANCELLED)
    assert tracker.end_turn(turn.id, "codex:completed", OUTCOME_SUCCESS) == []
    assert tracker.end_turn("t-99", "codex:completed", OUTCOME_SUCCESS) == []


def test_a_task_counts_under_its_kind_until_it_ends(tracker):
    assert tracker.start_task("exec-1", TASK_SHELL, command="sleep 47") == [
        {
            "type": "task_start",
            "task": {"id": "exec-1", "kind": TASK_SHELL},
            "summary": None,
            "command": "sleep 47",
        }
    ]
    tracker.start_task("01a1-thread", TASK_AGENT, summary="/root/pong")
    assert tracker.outstanding()["background"] == 1
    assert tracker.outstanding()["agent"] == 1
    assert tracker.end_task("exec-1", "completed", 0, output="done") == [
        {
            "type": "task_end",
            "task": {"id": "exec-1", "kind": TASK_SHELL},
            "status": "completed",
            "exit_code": 0,
            "output": "done",
        }
    ]
    assert tracker.outstanding()["background"] == 0


def test_a_repeated_task_start_counts_once(tracker):
    tracker.start_task("exec-1", TASK_SHELL)
    assert tracker.start_task("exec-1", TASK_SHELL) == []
    assert tracker.outstanding()["background"] == 1


def test_ending_a_task_never_started_or_already_ended_is_ignored(tracker):
    assert tracker.end_task("exec-9", "completed") == []
    tracker.start_task("exec-1", TASK_SHELL)
    tracker.end_task("exec-1", "completed")
    assert tracker.end_task("exec-1", "failed") == []


def test_a_session_failure_closes_structured_turns_and_tasks_too(tracker):
    turn = _typed(tracker)
    tracker.bind_typed()
    tracker.start_task("exec-1", TASK_SHELL)
    tracker.fail_session("app-server exited")
    assert not any(tracker.outstanding().values())
    assert turn.outcome == OUTCOME_ERROR
