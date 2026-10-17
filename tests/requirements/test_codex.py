"""Requirement 15 and criterion 9: a codex-backed proxy keeps the same promises.

Each test drives a real `CodexProxy` against `FakeAppServer`, which speaks the
shapes SPEC finding 10's probes recorded. They are codex twins of the claude
requirement tests, not generalisations of them, so a claude regression still
fails a claude-named test (IMPLEMENTATION-GUIDE step 36).
"""

from __future__ import annotations

import pytest

from tests.codex_harness import CodexSessionUnderTest
from tests.requirements import invariants
from tests.requirements.reference_client import ReferenceClient


@pytest.fixture
def session() -> CodexSessionUnderTest:
    return CodexSessionUnderTest()


def _of_type(messages, kind):
    return [m for m in messages if m["type"] == kind]


def _end_of(messages, turn_id):
    (end,) = [m for m in _of_type(messages, "turn_end") if m["turn"]["id"] == turn_id]
    return end


def test_requirement_15_ready_names_the_codex_thread_and_the_agent(session):
    first = session.messages()[0]
    assert (first["seq"], first["type"]) == (1, "ready")
    assert first["session_id"] == "th-fake"
    assert first["agent"] == "codex"


def test_requirement_15_background_and_monitor_exist_and_stay_at_zero(session):
    """Codex keeps no task list; the keys are there, so a client testing every
    count still works, and they say nothing is running there."""
    session.submit("reply ok")
    session.run_until_idle()
    for message in session.messages():
        assert message["outstanding"]["background"] == 0
        assert message["outstanding"]["monitor"] == 0


def test_codex_a_prompt_queued_behind_a_running_turn_gets_its_own_turn(session):
    """SPEC C2: codex would fold the second prompt into the first turn, so the
    proxy holds it until the first `turn/completed`."""
    session.submit("run 3 reply first", ref="a")
    session.tick()
    session.submit("reply second", ref="b")
    session.tick(2)
    assert len(session.turn_starts_sent()) == 1
    session.run_until_idle()

    messages = session.messages()
    first, second = [m["turn"]["id"] for m in _of_type(messages, "ack")]
    assert _end_of(messages, first)["outcome"] == "success"
    assert _end_of(messages, second)["outcome"] == "success"
    assert len(session.turn_starts_sent()) == 2
    texts = [(m["turn"]["id"], m["text"]) for m in _of_type(messages, "text")]
    assert texts == [(first, "first"), (second, "second")]
    invariants.all_of_them(messages)


def test_codex_a_completed_turn_ends_on_codex_s_own_status(session):
    session.submit("reply ok", ref="a")
    session.run_until_idle()
    (end,) = _of_type(session.messages(), "turn_end")
    assert end["basis"] == "codex:completed"
    assert end["usage"] == {"duration_ms": 100, "output_tokens": 3}


def test_codex_a_failed_turn_carries_codex_s_reason(session):
    session.submit("fail")
    session.run_until_idle()
    (end,) = _of_type(session.messages(), "turn_end")
    assert (end["basis"], end["outcome"], end["error"]) == ("codex:failed", "error", "fake failure")


def test_codex_a_queued_turn_cancelled_is_never_sent(session):
    session.submit("run 5", ref="a")
    session.submit("reply never", ref="b")
    session.tick()
    queued = _of_type(session.messages(), "ack")[1]["turn"]["id"]
    session.send({"type": "cancel", "turn": queued, "ref": "c"})
    session.run_until_idle()
    assert _end_of(session.messages(), queued)["basis"] == "cancel:withdrawn"
    assert len(session.turn_starts_sent()) == 1


def test_codex_a_running_turn_cancelled_leaves_its_command_counted_until_it_ends(session):
    """SPEC C3 and finding 10: the interrupt ends the turn, the command runs
    on, and codex's late `item/completed` ends the task."""
    session.submit("run 6 reply never", ref="a")
    session.tick(2)
    turn = _of_type(session.messages(), "ack")[0]["turn"]["id"]
    session.send({"type": "cancel", "turn": turn})
    session.tick(2)

    messages = session.messages()
    end = _end_of(messages, turn)
    assert (end["basis"], end["outcome"]) == ("codex:interrupted", "cancelled")
    (task_start,) = _of_type(messages, "task_start")
    assert task_start["task"]["kind"] == "shell"
    assert end["outstanding"]["background"] == 1
    assert [m["method"] for m in session.server.received].count("turn/interrupt") == 1

    session.run_until_idle()
    (task_end,) = _of_type(session.messages(), "task_end")
    assert task_end["task"] == task_start["task"]
    assert (task_end["status"], task_end["exit_code"], task_end["output"]) == ("completed", 0, "done\n")
    assert not any(session.emitter.outstanding().values())
    assert not _of_type(session.messages(), "text")


def test_codex_a_subagent_is_an_agent_task_inside_its_turn(session):
    session.submit("spawn reply pong")
    session.run_until_idle()
    messages = session.messages()
    (start,) = _of_type(messages, "task_start")
    (end,) = _of_type(messages, "task_end")
    assert start["task"] == {"id": "th-sub", "kind": "agent"}
    assert start["outstanding"]["agent"] == 1
    assert end["outstanding"]["agent"] == 0
    # The subagent's own turn is not one of this session's.
    assert len(_of_type(messages, "turn_start")) == 1


def test_codex_an_approval_request_is_refused_never_left_waiting(session):
    session.submit("approve")
    session.run_until_idle()
    assert session.server.answers == [{"id": 1, "result": {"decision": "decline"}}]
    texts = [m["text"] for m in _of_type(session.messages(), "text")]
    assert texts == ["approval:decline"]


def test_codex_a_turn_nobody_asked_for_is_unowned(session):
    session.submit("wake reply ok")
    session.run_until_idle()
    session.tick(5)
    session.run_until_idle()
    kinds = [m["turn"]["kind"] for m in _of_type(session.messages(), "turn_start")]
    assert kinds == ["user", "unowned"]
    invariants.all_of_them(session.messages())


def test_codex_a_turn_the_server_goes_silent_on_still_ends():
    session = CodexSessionUnderTest(stall_after_s=1.0)
    session.submit("run 1000")
    session.tick(3)
    session.tick(30, server=False)
    (end,) = _of_type(session.messages(), "turn_end")
    assert end["basis"] == "fallback:stalled"


def test_codex_a_turn_start_never_answered_still_ends():
    session = CodexSessionUnderTest(unmatched_after_s=1.0)
    session.submit("reply ok")
    session.tick(30, server=False)
    (end,) = _of_type(session.messages(), "turn_end")
    assert end["basis"] == "fallback:unmatched"
    assert not any(session.emitter.outstanding().values())


def test_criterion_9_the_reference_client_cannot_tell_which_agent_it_reads(session):
    """Queued prompts, a cancel of each kind, a subagent, a command outliving
    its turn and an unowned turn, read by the same client the claude suites
    use, with its own derivation of the counts checked at every tick."""
    client = ReferenceClient()

    def pump(ticks=1):
        for _ in range(ticks):
            session.tick()
            for message in session.new_messages():
                client.consume(message)
            assert client.derived == client.counts, (client.derived, client.counts)
            assert client.counts == session.emitter.outstanding()

    for message in session.new_messages():
        client.consume(message)
    session.submit("spawn reply one", ref="r1")
    session.submit("reply two", ref="r2")
    session.submit("reply withdrawn", ref="r3")
    pump()
    session.send({"type": "cancel", "turn": client.turn_for("r3"), "ref": "x3"})
    pump(8)
    session.submit("run 6 reply cut", ref="r4")
    pump(3)
    session.send({"type": "cancel", "turn": client.turn_for("r4"), "ref": "x4"})
    pump(3)
    session.submit("wake reply five", ref="r5")
    pump(40)

    for ref in ("r1", "r2", "r3", "r4", "r5"):
        assert client.is_finished(ref), ref
    assert client.ended[client.turn_for("r3")]["basis"] == "cancel:withdrawn"
    assert client.ended[client.turn_for("r4")]["outcome"] == "cancelled"
    assert not client.errors
    assert not any(client.counts.values())
    invariants.all_of_them(session.messages())
