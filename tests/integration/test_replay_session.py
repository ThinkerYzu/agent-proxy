"""Replay a whole session and check the boundaries that come back out.

The fixture is synthetic — `tools/make_synthetic_fixture.py` builds it — but
every entry shape in it was copied from session
`08ff12b4-bc39-4459-b2d7-cdabf527f276` (SPEC Appendix A) rather than invented.
Only the conversation is made up, and the tracker reads none of it.

The session is deliberately harder than the recorded one it was cut from:
nine turns, of which one is unowned; three background tasks spanning both
launch wordings and both completion contexts, one of them outstanding across
three consecutive turns; a task that exits nonzero; and the four-entry cluster
`/compact` writes after its boundary.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agentproxy.replay import load_prompts, replay, summarise
from agentproxy.turn_tracker import KIND_UNOWNED, KIND_USER, STATE_ENDED


FIXTURE = Path(__file__).parent.parent / "fixtures" / "synthetic-session.jsonl"


def replayed_stream():
    out = io.StringIO()
    tracker = replay(FIXTURE, load_prompts(FIXTURE.with_suffix(".prompts.json")), out=out)
    return tracker, [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.fixture(scope="module")
def replayed():
    return replay(FIXTURE, load_prompts(FIXTURE.with_suffix(".prompts.json")))


@pytest.fixture(scope="module")
def messages():
    return replayed_stream()[1]


def test_every_counter_drains_to_zero(replayed):
    assert replayed.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_turn_count_and_kinds_match_the_session(replayed):
    summary = summarise(replayed)
    assert summary["turns"] == 9
    assert summary["user_turns"] == 8
    assert summary["unowned_turns"] == 1
    assert summary["unfinished_turns"] == 0


def test_every_turn_ended_on_a_marker(replayed):
    assert summarise(replayed)["bases"] == {
        "marker:turn_duration": 8,
        "marker:compact_boundary": 1,
    }


def test_the_unowned_turn_names_the_task_that_woke_it(replayed):
    unowned = [t for t in replayed.turns if t.kind == KIND_UNOWNED]
    assert len(unowned) == 1
    assert unowned[0].caused_by == "bsynthimport"


def test_all_three_background_tasks_are_seen_and_closed(replayed):
    tasks = {task.id: task for task in replayed.tasks}
    assert set(tasks) == {"bsynthbench", "bsynthimport", "bsynthgrep"}
    assert all(task.state == "done" for task in tasks.values())
    # The promoted grep failed; its exit code has to survive to the client.
    assert tasks["bsynthgrep"].exit_code == 1
    assert tasks["bsynthbench"].exit_code == 0


def test_a_task_outlives_the_turn_that_launched_it(messages):
    """The case the counters exist for, and the one both clients get wrong.

    `bsynthimport` is launched in one turn and still running when that turn
    and the two after it end — so a client watching turn boundaries alone
    would call the agent idle three times while work was in flight.
    """
    ended_with_work_outstanding = [
        m["turn"]["id"]
        for m in messages
        if m["type"] == "turn_end" and m["outstanding"]["background"] > 0
    ]
    assert ended_with_work_outstanding == ["t-3", "t-4", "t-5", "t-9"]


def test_the_compact_cluster_leaves_no_turn_behind(replayed, messages):
    """Its candidate takes an id and is then dropped, unseen by the client."""
    ids = [turn.id for turn in replayed.turns]
    assert "t-8" not in ids
    assert all(m.get("turn", {}).get("id") != "t-8" for m in messages)


def test_no_turn_ends_twice_and_ids_are_unique(replayed):
    ids = [turn.id for turn in replayed.turns]
    assert len(ids) == len(set(ids))
    assert all(turn.state == STATE_ENDED for turn in replayed.turns)


def test_the_emitted_stream_is_well_formed(messages):
    """Every line is one message, numbered from 1, with the counts on it."""
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert all(set(m["outstanding"]) == {"user", "unowned", "background", "monitor", "agent"} for m in messages)
    assert messages[-1]["outstanding"] == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_each_user_turn_is_acknowledged_before_it_starts(replayed, messages):
    """A client's id reaches it in the ack, ahead of any message using it."""
    acked = set()
    for message in messages:
        if message["type"] == "ack":
            acked.add(message["turn"]["id"])
        elif message.get("turn", {}).get("kind") == KIND_USER:
            assert message["turn"]["id"] in acked
    assert len(acked) == sum(1 for t in replayed.turns if t.kind == KIND_USER)


def test_the_fixture_is_reproducible():
    """Regenerating must not change a byte, or the fixture cannot be reviewed."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
    from make_synthetic_fixture import build  # noqa: E402

    rebuilt = [json.dumps(entry, ensure_ascii=False) for entry in build().entries]
    assert rebuilt == FIXTURE.read_text(encoding="utf-8").splitlines()
