"""Replay a recorded session and check it against what really happened.

The fixture is session `08ff12b4-bc39-4459-b2d7-cdabf527f276`, chosen because
it holds all three background-completion shapes and three `/compact` runs —
see SPEC Appendix A. Its real boundaries are known from the transcript: 24
`turn_duration` markers, 3 `compact_boundary` markers, 26 human prompts, and
one turn woken by a background task finishing while the agent was idle.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agentproxy.replay import load_prompts, replay, summarise
from agentproxy.turn_tracker import KIND_UNOWNED, KIND_USER, STATE_ENDED


FIXTURE = Path(__file__).parent.parent / "fixtures" / "session-08ff12b4.jsonl"


@pytest.fixture(scope="module")
def replayed():
    return replay(FIXTURE, load_prompts(FIXTURE.with_suffix(".prompts.json")))


def test_every_counter_drains_to_zero(replayed):
    assert replayed.outstanding() == {"user": 0, "unowned": 0, "background": 0}


def test_turn_count_and_kinds_match_the_recorded_session(replayed):
    summary = summarise(replayed)
    assert summary["turns"] == 27
    assert summary["user_turns"] == 26
    assert summary["unowned_turns"] == 1
    assert summary["unfinished_turns"] == 0


def test_every_turn_ended_on_a_marker(replayed):
    assert summarise(replayed)["bases"] == {
        "marker:turn_duration": 24,
        "marker:compact_boundary": 3,
    }


def test_the_unowned_turn_names_the_task_that_woke_it(replayed):
    unowned = [t for t in replayed.turns if t.kind == KIND_UNOWNED]
    assert len(unowned) == 1
    assert unowned[0].caused_by == "by3c686m9"


def test_all_three_background_tasks_are_seen_and_closed(replayed):
    assert {t.id for t in replayed.tasks} == {"bunha2ebm", "bl60azfkq", "by3c686m9"}
    assert all(task.state == "done" for task in replayed.tasks)
    assert all(task.exit_code == 0 for task in replayed.tasks)


def test_no_turn_ends_twice_and_ids_are_unique(replayed):
    ids = [turn.id for turn in replayed.turns]
    assert len(ids) == len(set(ids))
    assert all(turn.state == STATE_ENDED for turn in replayed.turns)


def test_the_emitted_stream_is_well_formed():
    """Every line is one message, numbered from 1, with the counts on it."""
    out = io.StringIO()
    replay(FIXTURE, load_prompts(FIXTURE.with_suffix(".prompts.json")), out=out)
    messages = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert all(set(m["outstanding"]) == {"user", "unowned", "background"} for m in messages)
    assert messages[-1]["outstanding"] == {"user": 0, "unowned": 0, "background": 0}


def test_each_user_turn_is_acknowledged_before_it_starts():
    """A client's id reaches it in the ack, ahead of any message using it."""
    out = io.StringIO()
    tracker = replay(FIXTURE, load_prompts(FIXTURE.with_suffix(".prompts.json")), out=out)
    messages = [json.loads(line) for line in out.getvalue().splitlines()]
    acked = set()
    for message in messages:
        if message["type"] == "ack":
            acked.add(message["turn"]["id"])
        elif message.get("turn", {}).get("kind") == KIND_USER:
            assert message["turn"]["id"] in acked
    assert len(acked) == sum(1 for t in tracker.turns if t.kind == KIND_USER)
