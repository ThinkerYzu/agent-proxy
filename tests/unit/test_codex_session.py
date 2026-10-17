"""`CodexSession`: codex app-server notifications, as this proxy's turns and tasks.

Every notification here is built from a shape the 2026-10-17 probes recorded
against `codex` 0.163.0 (SPEC finding 10), with the ids shortened and no
conversation from a real session.
"""

from __future__ import annotations

import pytest

from agentproxy.codex_session import (
    BASIS_COMPLETED,
    BASIS_FAILED,
    BASIS_INTERRUPTED,
    CodexSession,
)
from agentproxy.turn_tracker import TurnTracker

THREAD = "th-1"


@pytest.fixture
def tracker() -> TurnTracker:
    return TurnTracker()


@pytest.fixture
def session(tracker) -> CodexSession:
    return CodexSession(tracker, THREAD)


def _typed(tracker, text="Reply with just the word ok."):
    turn, _ = tracker.submit(text)
    tracker.mark_typed(turn)
    return turn


def _started(session, codex_id):
    return session.on_turn_started_response({"turn": {"id": codex_id, "status": "inProgress"}}, None)


def _note(method, thread=THREAD, **params):
    return method, {"threadId": thread, **params}


def _turn_started(codex_id, thread=THREAD):
    return _note("turn/started", thread, turn={"id": codex_id, "status": "inProgress"})


def _turn_completed(codex_id, status="completed", thread=THREAD, **extra):
    return _note("turn/completed", thread, turn={"id": codex_id, "status": status, **extra})


def _item(phase, item, codex_id, thread=THREAD):
    return _note(f"item/{phase}", thread, item=item, turnId=codex_id)


def _command(item_id="exec-1", status="inProgress", **extra):
    return {
        "type": "commandExecution",
        "id": item_id,
        "command": "/bin/bash -lc 'sleep 47'",
        "cwd": "/w",
        "processId": "47738",
        "source": "unifiedExecStartup",
        "status": status,
        **extra,
    }


def _feed(session, *notes):
    events = []
    for method, params in notes:
        events += session.on_notification(method, params)
    return events


def _types(events):
    return [e["type"] for e in events]


# ----------------------------------------------------------------------
# Binding and ending
# ----------------------------------------------------------------------


def test_the_response_binds_the_typed_turn_by_id(tracker, session):
    turn = _typed(tracker)
    assert _started(session, "u1") == [{"type": "turn_start", "turn": turn.ref()}]
    assert session.codex_turn_of(turn.id) == "u1"
    # The notification that follows the response announces nothing new.
    assert _feed(session, _turn_started("u1")) == []


def test_a_turn_started_before_its_response_is_still_the_users(tracker, session):
    turn = _typed(tracker)
    assert _feed(session, _turn_started("u1")) == []
    assert _started(session, "u1") == [{"type": "turn_start", "turn": turn.ref()}]
    assert tracker.outstanding()["unowned"] == 0


def test_a_completed_turn_ends_with_its_duration_and_tokens(tracker, session):
    turn = _typed(tracker)
    _started(session, "u1")
    events = _feed(
        session,
        _item("completed", {"type": "agentMessage", "id": "m1", "text": "ok", "phase": "final_answer"}, "u1"),
        _note(
            "thread/tokenUsage/updated",
            turnId="u1",
            tokenUsage={"last": {"outputTokens": 5}, "total": {"outputTokens": 5}},
        ),
        _turn_completed("u1", durationMs=2040),
    )
    assert events == [
        {"type": "text", "turn": turn.ref(), "text": "ok"},
        {
            "type": "turn_end",
            "turn": turn.ref(),
            "basis": BASIS_COMPLETED,
            "outcome": "success",
            "usage": {"duration_ms": 2040, "output_tokens": 5},
        },
    ]
    assert not any(tracker.outstanding().values())


def test_a_failed_turn_carries_codex_s_own_reason(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    (end,) = _feed(session, _turn_completed("u1", "failed", error={"message": "usage limit reached"}))
    assert end["basis"] == BASIS_FAILED
    assert end["outcome"] == "error"
    assert end["error"] == "usage limit reached"


def test_a_refused_turn_start_ends_the_typed_turn(tracker, session):
    turn = _typed(tracker)
    (end,) = session.on_turn_started_response(None, {"code": -32600, "message": "thread not loaded"})
    assert end["turn"] == turn.ref()
    assert end["outcome"] == "error"
    assert end["error"] == "thread not loaded"
    assert tracker.typed_turn is None


def test_a_turn_nobody_asked_for_is_unowned(tracker, session):
    (start,) = _feed(session, _turn_started("w1"))
    assert start["turn"]["kind"] == "unowned"
    (end,) = _feed(session, _turn_completed("w1"))
    assert end["turn"] == start["turn"]
    assert not any(tracker.outstanding().values())


def test_a_prompt_folded_into_an_unowned_turn_ends_with_it(tracker, session):
    """The race in Decision 10: an unowned turn starts between `turn/start`
    and its response, and codex answers with the unowned turn's id."""
    (start,) = _feed(session, _turn_started("w1"))
    turn = _typed(tracker)
    assert _started(session, "w1") == []
    ends = _feed(session, _turn_completed("w1"))
    assert [e["turn"]["id"] for e in ends] == [start["turn"]["id"], turn.id]
    assert not any(tracker.outstanding().values())


def test_another_thread_is_not_this_session(tracker, session):
    """A subagent's own turns arrive on the same stream under its thread id."""
    assert _feed(session, _turn_started("s1", thread="sub-th"), _turn_completed("s1", thread="sub-th")) == []
    assert not any(tracker.outstanding().values())


def test_a_system_error_on_the_thread_marks_the_session_failed(session):
    _feed(session, _note("thread/status/changed", status={"type": "systemError"}))
    assert session.failure is not None


# ----------------------------------------------------------------------
# Items
# ----------------------------------------------------------------------


def test_a_command_is_a_tool_call_named_by_its_item_type(tracker, session):
    turn = _typed(tracker)
    _started(session, "u1")
    use, result = _feed(
        session,
        _item("started", _command(), "u1"),
        _item("completed", _command(status="completed", exitCode=0, aggregatedOutput="", durationMs=9), "u1"),
    )
    assert use == {
        "type": "tool_use",
        "turn": turn.ref(),
        "tool": "commandExecution",
        "input": {
            "command": "/bin/bash -lc 'sleep 47'",
            "cwd": "/w",
            "processId": "47738",
            "source": "unifiedExecStartup",
        },
        "tool_use_id": "exec-1",
    }
    assert result == {
        "type": "tool_result",
        "turn": turn.ref(),
        "tool_use_id": "exec-1",
        "content": "",
        "is_error": False,
    }


def test_a_non_zero_exit_or_a_declined_change_is_an_error_result(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    events = _feed(
        session,
        _item("completed", _command(status="completed", exitCode=2, aggregatedOutput="no"), "u1"),
        _item("completed", {"type": "fileChange", "id": "fc-1", "changes": [], "status": "declined"}, "u1"),
    )
    assert [e["is_error"] for e in events] == [True, True]


def test_a_file_change_result_carries_the_item(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    change = {
        "type": "fileChange",
        "id": "fc-1",
        "changes": [{"path": "/w/probe-note.txt", "kind": {"type": "add"}, "diff": "hello\n"}],
        "status": "completed",
    }
    (result,) = _feed(session, _item("completed", change, "u1"))
    assert result["content"] == {"changes": change["changes"], "status": "completed"}


def test_the_prompt_and_empty_reasoning_produce_nothing(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    events = _feed(
        session,
        _item("started", {"type": "userMessage", "id": "p1", "content": []}, "u1"),
        _item("completed", {"type": "userMessage", "id": "p1", "content": []}, "u1"),
        _item("started", {"type": "reasoning", "id": "r1", "summary": [], "content": []}, "u1"),
        _item("completed", {"type": "reasoning", "id": "r1", "summary": [], "content": []}, "u1"),
    )
    assert events == []


def test_reasoning_with_a_summary_is_thinking(tracker, session):
    turn = _typed(tracker)
    _started(session, "u1")
    events = _feed(
        session,
        _item("completed", {"type": "reasoning", "id": "r1", "summary": ["Checking the file"], "content": []}, "u1"),
    )
    assert events == [{"type": "thinking", "turn": turn.ref(), "text": "Checking the file"}]


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------


def test_a_command_an_interrupted_turn_left_running_is_a_task_until_it_ends(tracker, session):
    """SPEC finding 10's `interrupt-watch` probe: the `sleep 47` outlived its
    turn and codex reported it 45 s later, under the dead turn's id."""
    turn = _typed(tracker)
    _started(session, "u1")
    tracker.cancel(turn.id)
    _feed(session, _item("started", _command(), "u1"))
    task_start, end = _feed(session, _turn_completed("u1", "interrupted"))
    assert task_start == {
        "type": "task_start",
        "task": {"id": "exec-1", "kind": "shell"},
        "summary": None,
        "command": "/bin/bash -lc 'sleep 47'",
    }
    assert end["basis"] == BASIS_INTERRUPTED
    assert end["outcome"] == "cancelled"
    assert tracker.outstanding()["background"] == 1

    (task_end,) = _feed(
        session,
        _item("completed", _command(status="completed", exitCode=0, aggregatedOutput="", durationMs=46967), "u1"),
    )
    assert task_end == {
        "type": "task_end",
        "task": {"id": "exec-1", "kind": "shell"},
        "status": "completed",
        "exit_code": 0,
        "output": "",
    }
    assert not any(tracker.outstanding().values())


def test_an_interrupt_nobody_asked_for_is_an_error(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    (end,) = _feed(session, _turn_completed("u1", "interrupted"))
    assert end["outcome"] == "error"


def test_a_subagent_is_an_agent_task_keyed_by_its_thread(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    activity = {"type": "subAgentActivity", "agentThreadId": "sub-th", "agentPath": "/root/pong"}
    events = _feed(
        session,
        _item("started", {**activity, "id": "call_1", "kind": "started"}, "u1"),
        _item("completed", {**activity, "id": "call_1", "kind": "started"}, "u1"),
    )
    assert _types(events) == ["task_start"]
    assert events[0]["task"] == {"id": "sub-th", "kind": "agent"}
    assert events[0]["summary"] == "/root/pong"
    assert tracker.outstanding()["agent"] == 1
    events = _feed(
        session,
        _turn_started("s1", thread="sub-th"),
        _item("completed", {**activity, "id": "subagent-completed-s1", "kind": "completed"}, "u1"),
    )
    assert _types(events) == ["task_end"]
    assert tracker.outstanding()["agent"] == 0


def test_an_interrupted_subagent_is_stopped(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    activity = {"type": "subAgentActivity", "agentThreadId": "sub-th", "agentPath": "/root/x"}
    _feed(session, _item("completed", {**activity, "id": "a", "kind": "started"}, "u1"))
    (end,) = _feed(session, _item("completed", {**activity, "id": "b", "kind": "interrupted"}, "u1"))
    assert end["status"] == "stopped"


def test_content_for_an_ended_turn_is_dropped(tracker, session):
    _typed(tracker)
    _started(session, "u1")
    _feed(session, _turn_completed("u1"))
    late = _item("completed", {"type": "agentMessage", "id": "m9", "text": "late"}, "u1")
    assert _feed(session, late) == []
