"""The SPEC's success criteria, as tests.

| Criterion | Where it is verified |
|---|---|
| 1 — the inference code is gone | in the clients, which this repo does not change. What makes it possible is verified here instead: `ReferenceClient` needs no ordinal, no latch and no reconciliation timer, and criteria 3 and 6 below drive it through the cases those three mechanisms exist to survive |
| 2 — counters return to zero | `tests/integration/test_live_wrapping.py::test_a_queued_prompt_a_background_task_and_an_unowned_turn_all_drain` |
| 3 — no desync over a long session | here — `test_criterion_3_*` |
| 4 — no premature end-of-turn | `tests/integration/test_live_wrapping.py::test_a_mid_turn_footer_flicker_produces_one_end_of_turn_at_the_real_end` |
| 5 — a client's end-of-turn is its own | `tests/integration/test_live_wrapping.py::test_each_client_wait_resolves_on_its_own_turn_id` |
| 6 — reconnect is correct | here — `test_criterion_6_*` |

"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.harness import (
    Session,
    assistant_text,
    assistant_tool_use,
    background_launch,
    marker,
    notification,
    queue_operation,
    user_text,
)
from tests.requirements import invariants
from tests.requirements.reference_client import ReferenceClient


# Turns the soak submits. Every fifth one queues a second prompt behind it and
# every fourth launches a background task, so the session runs well over the
# thirty turns the criterion asks for.
TURNS = 32


@pytest.fixture
def session() -> Session:
    return Session()


def pump(
    session: Session,
    client: ReferenceClient,
    ticks: int = 4,
    advance: float = 0.0,
    pty_quiet: bool = False,
) -> None:
    """Run the loop, feed the client what came out, and check the two agree.

    The comparison lands at a tick boundary because that is where the two are
    comparable: inside a batch the counts are the batch's final ones, ahead of
    the events the client has read (see `test_functional.py`'s requirement 5).
    """
    for _ in range(ticks):
        session.tick(advance, pty_quiet)
        for message in session.new_messages():
            client.consume(message)
        assert client.derived == client.counts, (
            f"client sees {client.derived}, proxy says {client.counts}"
        )
        assert client.counts == session.tracker.outstanding()


# ----------------------------------------------------------------------
# Criterion 3 — no desync over a long session
# ----------------------------------------------------------------------


def test_criterion_3_a_long_mixed_session_never_desyncs(session):
    """Forty-odd turns of the three kinds mixed, checked at every step.

    The client derives the counts a second time — from `ack`,
    `turn_start`, `turn_end`, `task_start` and `task_end` alone — and every
    tick compares that derivation against the counts the proxy stamped. One
    missed decrement anywhere in the run puts the two out of step and stays
    that way, which is the failure mode of the ordinal and the latch this
    protocol replaces: they desync permanently on a single stray event.

    The session mixes everything that made them fail: prompts queued behind a
    running turn, background tasks outliving the turn that launched them,
    completions folded into a running turn, unowned turns woken by the others,
    and turns that end on the fallback tier rather than on a marker.
    """
    client = ReferenceClient()
    mine: list[str] = []

    for index in range(TURNS):
        prompt = f"prompt {index}"
        ref = f"c-{index}"
        mine.append(ref)
        session.submit(prompt, ref=ref)
        pump(session, client)

        session.footer_idle = False
        session.writes(user_text(prompt), assistant_text(f"working on {index}"))
        pump(session, client)

        task = tool = None
        if index % 4 == 0:
            task, tool = f"bg{index}", f"toolu_{index}"
            session.writes(
                assistant_tool_use(tool, f"build {index}"),
                background_launch(tool, task),
            )
            pump(session, client)

        queued_ref = None
        if index % 5 == 3:
            queued_ref = f"q-{index}"
            mine.append(queued_ref)
            session.submit(f"queued behind {index}", ref=queued_ref)
            pump(session, client)

        # Half the tasks finish mid-turn, which folds them into the running
        # turn and wakes nothing; the other half finish while the inner is
        # idle, and each of those wakes an unowned turn.
        folded = task is not None and index % 8 == 0
        if folded:
            session.writes(
                queue_operation("enqueue", notification(task, tool)),
                queue_operation("remove", notification(task, tool)),
            )
            pump(session, client)

        if index % 7 == 6:
            session.footer_idle = True
            pump(session, client, ticks=12, advance=1.0)
        else:
            session.writes(marker())
            session.footer_idle = True
            pump(session, client)

        if queued_ref is not None:
            pump(session, client)
            session.footer_idle = False
            session.answers(f"queued behind {index}")
            session.footer_idle = True
            pump(session, client)

        if task is not None and not folded:
            session.footer_idle = False
            session.writes(
                queue_operation("enqueue", notification(task, tool)),
                queue_operation("dequeue"),
                user_text(notification(task, tool)),
                assistant_text(f"build {index} finished"),
                marker(),
            )
            session.footer_idle = True
            pump(session, client)

    messages = session.messages()
    invariants.all_of_them(messages)

    ends = [m for m in messages if m["type"] == "turn_end"]
    assert len(ends) >= 30
    assert Counter(m["turn"]["kind"] for m in ends)["unowned"] >= 3
    bases = Counter(m["basis"] for m in ends)
    assert bases["marker:turn_duration"] > 0 and bases["fallback:idle"] > 0

    assert client.derived == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}
    assert all(client.is_finished(ref) for ref in mine)
    assert client.expected_seq == len(messages) + 1


def test_criterion_3_matching_by_position_would_have_drifted(session):
    """Why the run above is a test of ids rather than of arithmetic.

    A client counting `turn_end` messages and taking the Nth as the answer to
    its Nth prompt is right only while nothing else ever ends. Here foreign
    ends — an unowned turn's — arrive in between, so the ordinal a client
    would have matched on is already wrong by the second prompt, and stays
    wrong for the rest of the session.
    """
    client = ReferenceClient()
    session.submit("A", ref="c-A")
    pump(session, client)
    session.footer_idle = False
    session.writes(user_text("A"), assistant_text("on it"))
    pump(session, client)
    session.writes(
        assistant_tool_use("toolu_1", "build"), background_launch("toolu_1", "bg1")
    )
    pump(session, client)
    session.writes(marker())
    session.footer_idle = True
    pump(session, client)

    session.footer_idle = False
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("dequeue"),
        user_text(notification("bg1", "toolu_1")),
        assistant_text("the build finished"),
        marker(),
    )
    session.footer_idle = True
    pump(session, client)

    session.submit("B", ref="c-B")
    pump(session, client)
    session.footer_idle = False
    session.answers("B")
    session.footer_idle = True
    pump(session, client)

    ends = [m["turn"]["id"] for m in session.of_type("turn_end")]
    assert len(ends) == 3
    assert ends[0] == client.turn_for("c-A")
    assert ends[2] == client.turn_for("c-B")
    # The client's second prompt is the third end-of-turn. An ordinal reads
    # the second, which belongs to a turn the client never asked for.
    assert ends[1] not in {client.turn_for("c-A"), client.turn_for("c-B")}
    assert client.is_finished("c-B")


# ----------------------------------------------------------------------
# Criterion 6 — reconnect is correct
# ----------------------------------------------------------------------


def read_along(session: Session, client: ReferenceClient, ticks: int = 4) -> None:
    """Run the loop with the client reading, the way it does before it drops out."""
    for _ in range(ticks):
        session.tick()
        for message in session.new_messages():
            client.consume(message)


def drop_out_mid_turn(session: Session, client: ReferenceClient) -> int:
    """Read as far as two acked prompts, then stop; return where reading stopped.

    Everything after that point goes past unread: the running turn ends, the
    background task it launched completes, and the unowned turn woken by that
    completion is still running when the client comes back — with the client's
    second prompt queued behind it the whole time.
    """
    session.submit("look into the crash", ref="c-1")
    read_along(session, client)
    session.footer_idle = False
    session.writes(user_text("look into the crash"), assistant_text("on it"))
    read_along(session, client)
    session.writes(
        assistant_tool_use("toolu_1", "build"), background_launch("toolu_1", "bg1")
    )
    read_along(session, client)
    session.submit("and then summarise", ref="c-2")
    read_along(session, client, ticks=1)
    assert client.turn_for("c-2") is not None
    stopped_at = len(session.messages())

    session.writes(marker())
    session.footer_idle = True
    session.run()
    session.footer_idle = False
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("dequeue"),
        user_text(notification("bg1", "toolu_1")),
        assistant_text("the build finished"),
    )
    session.run()
    return stopped_at


def test_criterion_6_a_reconnecting_client_rebuilds_from_the_state_reply(session):
    """It missed an unknown number of messages and needs none of them.

    The reply is authoritative on its own: the counts, what is running, what
    is queued behind it, and which background command is still going. The one
    thing the client keeps across the gap is its own `ref`-to-id map, which is
    what lets the wait it was already in resolve when the end finally comes.
    """
    client = ReferenceClient()
    stopped_at = drop_out_mid_turn(session, client)
    missed = len(session.messages()) - stopped_at
    assert missed > 0

    session.send({"type": "status", "ref": "resync-1"})
    resumed = session.resume_reading_at(len(session.messages()) - 1)
    assert [m["type"] for m in resumed] == ["state"]
    state = resumed[0]

    client.resync_to(state)
    client.consume(state)
    assert state["ref"] == "resync-1"
    assert client.derived == session.tracker.outstanding()
    assert client.derived == {"user": 1, "unowned": 1, "background": 0, "monitor": 0, "agent": 0}
    assert client.queued == {client.turn_for("c-2")}
    assert client.running != client.queued

    # Reading from here on is enough to reach zero, and the prompt submitted
    # before the gap resolves on its own id — which the client still holds,
    # because the id was issued to it before the gap.
    session.writes(marker())
    session.footer_idle = True
    read_along(session, client)
    session.footer_idle = False
    session.answers("and then summarise")
    session.footer_idle = True
    read_along(session, client)

    assert client.is_finished("c-2")
    assert not client.is_finished("c-1")  # its end went past while it was away
    assert client.derived == client.counts == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_criterion_6_a_client_with_no_history_at_all_is_correct_too(session):
    """The reply depends on nothing the client has previously seen.

    A brand-new `ReferenceClient` — no `ref` map, no memory of the session —
    adopts the same state and agrees with the proxy. It cannot know which
    turns were its own, and does not need to: what it gates its composer on is
    the counts, and those are all in the reply.
    """
    watcher = ReferenceClient()
    drop_out_mid_turn(session, watcher)
    session.send({"type": "status"})
    state = session.of_type("state")[-1]

    newcomer = ReferenceClient()
    newcomer.adopt(state)
    assert newcomer.derived == session.tracker.outstanding()
    assert newcomer.derived == {"user": 1, "unowned": 1, "background": 0, "monitor": 0, "agent": 0}
    assert len(newcomer.running) == 1 and len(newcomer.queued) == 1
    assert newcomer.open_unowned == newcomer.running
    assert newcomer.turn_of == {}
