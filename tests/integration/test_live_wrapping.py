"""The live path, driven without a pty: what a client submits and what it hears.

The `Session` harness these tests drive lives in `tests/harness.py`, shared
with `tests/requirements`. It calls `Proxy` in the same places and the same
order the real io loop does, and lets a test say exactly what the inner writes
and when, so what comes out is the byte stream a client would read.

Three of the tests here are success criteria 2, 4 and 5, which are what Phase 2
is measured by. The rest of the criteria, and the functional requirements one
by one, are in `tests/requirements`.
"""

from __future__ import annotations

import pytest

from tests.harness import (
    Session,
    assistant_text,
    assistant_tool_use,
    background_launch,
    marker,
    notification,
    queue_operation,
    tool_result,
    user_text,
)


@pytest.fixture
def session() -> Session:
    return Session()



# ----------------------------------------------------------------------
# The client's side of the protocol
# ----------------------------------------------------------------------


def test_the_session_announces_itself_before_anything_else(session):
    first = session.messages()[0]
    assert first["type"] == "ready"
    assert first["session_id"] == "session-under-test"
    assert first["outstanding"] == {"user": 0, "unowned": 0, "background": 0}


def test_a_prompt_is_acknowledged_before_a_key_is_typed(session):
    """Requirement 2: the ack is the proxy's own work, not the inner's."""
    turn_id = session.submit("hello")
    assert session.of_type("ack")[-1]["turn"] == {"id": turn_id, "kind": "user"}
    assert session.of_type("ack")[-1]["outstanding"]["user"] == 1
    assert session.typed == []


def test_a_status_query_is_answered_and_never_typed(session):
    session.submit("hello")
    session.send({"type": "status"})
    state = session.of_type("state")[-1]
    assert [turn["state"] for turn in state["turns"]] == ["queued"]
    assert session.typed == []


def test_an_unusable_command_is_reported_and_typed_at_nobody(session):
    session.proxy.on_stdin_line("{not json")
    session.send({"type": "teleport"})
    session.send({"type": "submit", "text": "   "})
    assert len(session.of_type("error")) == 3
    session.run()
    assert session.typed == []


def test_a_command_is_answered_with_its_own_ref(session):
    """The client's handle on a command, echoed on whatever answers it."""
    session.send({"type": "submit", "text": "hello", "ref": "c-1"})
    session.send({"type": "status", "ref": "c-2"})
    session.send({"type": "submit", "text": "   ", "ref": "c-3"})
    assert session.of_type("ack")[-1]["ref"] == "c-1"
    assert session.of_type("state")[-1]["ref"] == "c-2"
    assert session.of_type("error")[-1]["ref"] == "c-3"


def test_a_rejected_submit_does_not_shift_the_acks_behind_it(session):
    """Why `ref` exists: counting acks binds the third prompt to the second.

    A `submit` the proxy will not act on produces an `error` and no `ack`, so
    the ordinal a client would otherwise match on is one short from there on —
    silently, and for the rest of the session.
    """
    session.send({"type": "submit", "text": "fix the leak", "ref": "c-1"})
    session.send({"type": "submit", "text": "   ", "ref": "c-2"})
    session.send({"type": "submit", "text": "now test it", "ref": "c-3"})

    acks = {message["ref"]: message["turn"]["id"] for message in session.of_type("ack")}
    assert acks == {"c-1": "t-1", "c-3": "t-2"}
    assert "c-2" not in acks
    # A client matching by position gives its second prompt the second ack.
    # That ack is the third prompt's.
    assert session.of_type("ack")[1]["ref"] == "c-3"


def test_a_command_without_a_ref_is_answered_without_one(session):
    """The field is optional; a client that does not correlate sees no change."""
    session.submit("hello")
    assert "ref" not in session.of_type("ack")[-1]


def test_every_message_carries_all_three_counts(session):
    """Requirement 5 — what makes a client that missed a line self-correcting."""
    session.submit("hello")
    session.run()
    session.answers("hello")
    session.run()
    assert all(
        set(m["outstanding"]) == {"user", "unowned", "background"}
        for m in session.messages()
    )
    assert [m["seq"] for m in session.messages()] == list(
        range(1, len(session.messages()) + 1)
    )


# ----------------------------------------------------------------------
# Typing, and when the proxy refuses to
# ----------------------------------------------------------------------


def test_a_prompt_is_typed_once_the_inner_is_idle_and_settled(session):
    session.submit("hello")
    session.run()
    assert session.typed == ["hello"]


def test_nothing_is_typed_into_a_busy_inner(session):
    session.footer_idle = False
    session.submit("hello")
    session.run()
    assert session.typed == []


def test_a_second_prompt_waits_for_the_first_turn_to_end(session):
    first = session.submit("first")
    session.run()
    session.writes(user_text("first"), assistant_text())
    session.run()

    session.submit("second")
    session.run()
    assert session.typed == ["first"]
    assert session.tracker.turn(first).state == "running"

    session.writes(marker())
    session.run()
    assert session.typed == ["first", "second"]


def test_a_typed_prompt_holds_the_keyboard_until_the_transcript_shows_it(session):
    """Two prompts in flight at once leave one binding decision to tell them
    apart, which is how a turn ends up matched to the wrong id."""
    session.submit("first")
    session.submit("second")
    session.run()
    assert session.typed == ["first"]
    assert not session.proxy.submit_landed()

    session.writes(user_text("first"))
    session.run()
    assert session.proxy.submit_landed()


# ----------------------------------------------------------------------
# SPEC success criterion 4 — no premature end-of-turn
# ----------------------------------------------------------------------


def test_a_mid_turn_footer_flicker_produces_one_end_of_turn_at_the_real_end(session):
    """The failure that desynced an earlier client: the footer settles idle between
    an assistant text block and the next tool call, and the wrapper calls it
    the end of the turn.
    """
    turn_id = session.submit("build it")
    session.run()
    session.footer_idle = False
    session.writes(user_text("build it"), assistant_text("Starting the build."))
    session.run()

    # The flicker: the panel reads idle while the inner decides its next call.
    session.footer_idle = True
    session.run(ticks=3, advance=1.0)
    assert session.of_type("turn_end") == []

    session.footer_idle = False
    session.writes(assistant_tool_use("toolu_1", "build"), tool_result("toolu_1", "done"))
    session.run()
    assert session.of_type("turn_end") == []

    session.writes(marker())
    session.footer_idle = True
    session.run()
    ends = session.of_type("turn_end")
    assert len(ends) == 1
    assert ends[0]["turn"]["id"] == turn_id
    assert ends[0]["basis"] == "marker:turn_duration"


def test_a_turn_no_marker_closes_still_ends(session):
    """Tier 2, the everyday path: an unrecognised turn shape must degrade to a
    late end-of-turn, never to silence (requirement 11)."""
    turn_id = session.submit("/some-command-we-do-not-know")
    session.run()
    session.footer_idle = False
    session.writes(user_text("/some-command-we-do-not-know"), assistant_text())
    session.run()

    session.footer_idle = True
    session.run(ticks=12, advance=1.0)
    ends = session.of_type("turn_end")
    assert [e["turn"]["id"] for e in ends] == [turn_id]
    assert ends[0]["basis"] == "fallback:idle"
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}


# ----------------------------------------------------------------------
# SPEC success criterion 2 — counters return to zero
# ----------------------------------------------------------------------


def test_a_queued_prompt_a_background_task_and_an_unowned_turn_all_drain(session):
    """The three kinds of outstanding work, in one session, back to zero.

    A prompt queued behind a running turn, a `run_in_background` command that
    outlives the turn that launched it, and the unowned turn its completion
    wakes — the shape both clients get wrong today.
    """
    first = session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("start the build"),
        assistant_tool_use("toolu_1", "run the build"),
        background_launch("toolu_1", "bg1"),
    )
    session.run()

    second = session.submit("meanwhile, explain the plan")
    session.writes(marker())
    session.footer_idle = True
    session.run()
    assert session.tracker.outstanding() == {"user": 1, "unowned": 0, "background": 1}
    assert session.typed == ["start the build", "meanwhile, explain the plan"]

    session.footer_idle = False
    session.answers("meanwhile, explain the plan")
    session.footer_idle = True
    session.run()
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 1}

    # The task finishes while the inner is idle, so it wakes a turn nobody
    # asked for.
    session.footer_idle = False
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("dequeue"),
        user_text(notification("bg1", "toolu_1")),
        assistant_text("The build finished."),
        marker(),
    )
    session.footer_idle = True
    session.run()

    unowned = [m for m in session.of_type("turn_start") if m["turn"]["kind"] == "unowned"]
    assert len(unowned) == 1
    assert unowned[0]["caused_by"] == "bg1"
    assert [m["turn"]["id"] for m in session.of_type("turn_end")] == [
        first,
        second,
        unowned[0]["turn"]["id"],
    ]
    assert session.messages()[-1]["outstanding"] == {
        "user": 0,
        "unowned": 0,
        "background": 0,
    }


def test_a_task_finishing_mid_turn_makes_no_turn_and_still_decrements(session):
    """The other half of a task's lifecycle: folded into the running turn, not
    woken as a new one — and the count has to fall either way."""
    session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("start the build"),
        assistant_tool_use("toolu_1", "run the build"),
        background_launch("toolu_1", "bg1"),
    )
    session.run()
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("remove", notification("bg1", "toolu_1")),
        marker(),
    )
    session.footer_idle = True
    session.run()
    assert len(session.of_type("turn_start")) == 1
    assert session.of_type("task_end")[-1]["exit_code"] == 0
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}


# ----------------------------------------------------------------------
# SPEC success criterion 5 — a client's end-of-turn is its own
# ----------------------------------------------------------------------


def test_each_client_wait_resolves_on_its_own_turn_id(session):
    """Prompt A runs, prompt B queues behind it, and an unowned turn fires in
    between. A client waiting on B must ignore the two end-of-turns that reach
    it first — which positional matching, the mechanism an earlier client
    used, cannot do."""
    first = session.submit("A")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("A"),
        assistant_tool_use("toolu_1", "check"),
        background_launch("toolu_1", "bg1"),
    )
    session.run()

    second = session.submit("B")
    assert first != second

    # A ends and the task's completion wakes an unowned turn in the same
    # breath, so the proxy never gets an idle moment to type B into.
    session.writes(
        marker(),
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("dequeue"),
        user_text(notification("bg1", "toolu_1")),
        assistant_text("The check came back clean."),
    )
    session.run()
    assert session.typed == ["A"]

    session.writes(marker())
    session.footer_idle = True
    session.run()
    session.footer_idle = False
    session.answers("B")
    session.footer_idle = True
    session.run()

    ends = session.of_type("turn_end")
    assert [e["turn"]["id"] for e in ends][0] == first
    assert [e["turn"]["id"] for e in ends][-1] == second
    assert [e["turn"]["kind"] for e in ends] == ["user", "unowned", "user"]
    # A client waiting on B sees three, and only one of them is its own.
    assert sum(1 for e in ends if e["turn"]["id"] == second) == 1
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}


# ----------------------------------------------------------------------
# The error path
# ----------------------------------------------------------------------


def test_a_dead_inner_claude_closes_the_running_turn_and_the_queue_behind_it(session):
    running = session.submit("first")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("first"),
        assistant_tool_use("toolu_1", "run the build"),
        background_launch("toolu_1", "bg1"),
    )
    session.run()
    queued = session.submit("second")

    session.proxy.fail("inner claude exited")

    ends = {e["turn"]["id"]: e for e in session.of_type("turn_end")}
    assert set(ends) == {running, queued}
    assert all(e["outcome"] == "error" for e in ends.values())
    assert session.of_type("task_end")[-1]["exit_code"] is None
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}
    assert not session.proxy.has_unfinished_work


def test_the_loop_may_stop_only_once_the_client_is_owed_nothing(session):
    session.submit("hello")
    session.run()
    session.proxy.on_stdin_eof()
    session.writes(user_text("hello"), assistant_text())
    session.run()
    assert not session.proxy.should_stop()

    session.writes(marker())
    session.run()
    assert session.proxy.should_stop()
