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
