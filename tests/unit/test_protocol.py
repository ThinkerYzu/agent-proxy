"""The envelope: what every message carries, and in what order."""

from __future__ import annotations

import io
import json

from agentproxy.protocol import Emitter
from agentproxy.turn_tracker import TurnTracker


def test_every_message_carries_seq_and_all_three_counts():
    tracker = TurnTracker()
    out = io.StringIO()
    emitter = Emitter(tracker, out=out)
    turn, events = tracker.submit("hello")

    message = emitter.emit(events[0])
    assert list(message) == ["seq", "type", "turn", "outstanding"]
    assert message["seq"] == 1
    assert message["outstanding"] == {"user": 1, "unowned": 0, "background": 0}

    written = json.loads(out.getvalue().splitlines()[0])
    assert written == message


def test_counts_are_read_at_emission_not_at_event_creation():
    """An event built before another turn was accepted still reports the total."""
    tracker = TurnTracker()
    emitter = Emitter(tracker, out=io.StringIO())
    _, first_events = tracker.submit("first")
    tracker.submit("second")
    assert emitter.emit(first_events[0])["outstanding"]["user"] == 2


def test_messages_that_belong_to_no_turn_omit_the_turn_field():
    tracker = TurnTracker()
    emitter = Emitter(tracker, out=io.StringIO())
    message = emitter.emit({"type": "error", "error": "inner claude exited"})
    assert "turn" not in message
    assert message["error"] == "inner claude exited"


def test_seq_counts_every_line_written():
    tracker = TurnTracker()
    emitter = Emitter(tracker, out=io.StringIO())
    messages = emitter.emit_all(
        [{"type": "ready", "session_id": "s"}, {"type": "error", "error": "x"}]
    )
    assert [m["seq"] for m in messages] == [1, 2]
    assert emitter.seq == 2
