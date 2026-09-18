"""The FIFO between "the client submitted" and "the inner was ready for it"."""

from __future__ import annotations

import pytest

from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


@pytest.fixture
def tracker() -> TurnTracker:
    return TurnTracker()


@pytest.fixture
def queue() -> SubmitQueue:
    return SubmitQueue()


def test_an_accepted_prompt_becomes_the_message_the_pty_is_given(tracker, queue):
    turn, _ = tracker.submit("what does load_timeout_id do?")
    message = queue.accept(turn)
    assert message == {
        "type": "user",
        "message": {"role": "user", "content": "what does load_timeout_id do?"},
        "turn": turn.id,
    }
    assert len(queue) == 1


def test_prompts_are_typed_in_the_order_they_were_accepted(tracker, queue):
    first, _ = tracker.submit("first")
    second, _ = tracker.submit("second")
    queue.accept(first)
    queue.accept(second)
    assert queue.take() is first
    assert queue.take() is second
    assert queue.is_empty


def test_taking_from_an_empty_queue_is_survivable(queue):
    """The inner echoing something the proxy never typed must not raise."""
    assert queue.take() is None


def test_a_mismatched_payload_is_reported_but_still_pops(tracker, queue, monkeypatch):
    """A desync between the two queues has to be loud, not silent."""
    logged: list[str] = []
    monkeypatch.setattr(
        "agentproxy.submit_queue.log.log_text",
        lambda channel, text: logged.append(text),
    )
    turn, _ = tracker.submit("mine")
    queue.accept(turn)
    popped = queue.take({"type": "user", "message": {"content": "not mine"}})
    assert popped is turn
    assert any("does not match" in line for line in logged)


def test_draining_empties_the_queue_and_hands_back_what_was_owed(tracker, queue):
    first, _ = tracker.submit("first")
    second, _ = tracker.submit("second")
    queue.accept(first)
    queue.accept(second)
    assert queue.drain() == [first, second]
    assert queue.is_empty


# ----------------------------------------------------------------------
# Withdrawal — a queued prompt taken back before it was typed
# ----------------------------------------------------------------------


def test_a_withdrawn_turn_leaves_the_queue_and_its_payload_types_nothing(tracker, queue):
    """The io loop still holds the payload; when its time comes, `take`
    answers None so nothing is typed for a turn that has already ended."""
    first, _ = tracker.submit("first")
    second, _ = tracker.submit("second")
    first_payload = queue.accept(first)
    second_payload = queue.accept(second)
    assert queue.withdraw(first.id) is first
    assert len(queue) == 1
    assert queue.take(first_payload) is None
    assert queue.take(second_payload) is second
    assert queue.is_empty


def test_withdrawing_what_is_not_waiting_changes_nothing(tracker, queue):
    """A turn already typed, or never accepted, is past taking back here."""
    turn, _ = tracker.submit("mine")
    payload = queue.accept(turn)
    assert queue.take(payload) is turn
    assert queue.withdraw(turn.id) is None
    assert queue.withdraw("t-99") is None


def test_take_finds_a_payloads_turn_by_id_not_by_position(tracker, queue):
    """With the middle turn withdrawn, the third payload still meets its own
    turn — the whole point of the payload naming it."""
    turns = [tracker.submit(f"prompt {i}")[0] for i in range(3)]
    payloads = [queue.accept(t) for t in turns]
    queue.withdraw(turns[1].id)
    assert queue.take(payloads[0]) is turns[0]
    assert queue.take(payloads[1]) is None
    assert queue.take(payloads[2]) is turns[2]
