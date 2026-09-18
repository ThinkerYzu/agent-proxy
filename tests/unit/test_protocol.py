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
    assert message["outstanding"] == {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}

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


def test_a_count_from_outside_the_tracker_is_merged_into_every_message():
    """`orphan` is read from the process tree, not the tracker's tables, and
    lands in `outstanding` like any other count, read at emission."""
    tracker = TurnTracker()
    orphans = {"orphan": 0}
    emitter = Emitter(tracker, out=io.StringIO(), extra_counts=lambda: dict(orphans))
    _, events = tracker.submit("hello")

    orphans["orphan"] = 2
    message = emitter.emit(events[0])
    assert message["outstanding"] == {
        "user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 2,
    }
    assert emitter.outstanding() == message["outstanding"]


def test_without_an_outside_source_the_key_is_absent_not_zero():
    """A `0` would claim knowledge the proxy does not have: on a host where it
    could not become a subreaper, it has simply never looked."""
    emitter = Emitter(TurnTracker(), out=io.StringIO())
    assert "orphan" not in emitter.outstanding()
