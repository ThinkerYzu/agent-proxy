"""The twelve functional requirements of the SPEC, one test group each.

These are traceability tests: each one names the requirement it stands for and
asserts what that requirement promises a client, on the protocol's own terms —
what is on the wire, not how the tracker reached it. Where a mechanism is
already covered elsewhere (`tests/unit/test_turn_tracker.py` for the rules,
`tests/integration/test_live_wrapping.py` for the live path), the test here
asserts the requirement over a whole session instead of repeating the unit.

| Requirement | Test |
|---|---|
| 1 — three counters | `test_requirement_1_the_three_counts_are_tracked_apart` |
| 2 — immediate acknowledgement | `test_requirement_2_*` (three tests) |
| 3 — turn kind on every turn message | `test_requirement_3_*` (two tests) |
| 4 — end-of-turn is explicit, identified, typed | `test_requirement_4_*` (two tests) |
| 5 — counts on every message | `test_requirement_5_*` (two tests) |
| 6 — turn identity | `test_requirement_6_*` (two tests) |
| 7 — background task lifecycle | `test_requirement_7_*` (two tests) |
| 8 — state query / resync | `test_requirement_8_*` (two tests) |
| 9 — queued-turn visibility | `test_requirement_9_queued_and_running_are_told_apart` |
| 10 — errors and termination | `test_requirement_10_*` (three tests) |
| 11 — no turn may hang without an end-of-turn | `test_requirement_11_*` (four tests) |
| 12 — session identity stated first, and always | `test_requirement_12_*` (three tests) |

"""

from __future__ import annotations

import io
import json

import pytest

from agentproxy.protocol import Emitter
from agentproxy.turn_tracker import TurnTracker

from tests.harness import (
    Session,
    assistant_text,
    assistant_thinking,
    assistant_tool_use,
    background_launch,
    marker,
    notification,
    queue_operation,
    tool_result,
    user_text,
)
from tests.requirements import invariants
from tests.requirements.reference_client import ReferenceClient


@pytest.fixture
def session() -> Session:
    return Session()


# ----------------------------------------------------------------------
# Session shapes the tests below share
# ----------------------------------------------------------------------


def start_turn(session: Session, prompt: str, ref: object = None) -> str:
    """Submit a prompt and drive it as far as running; return its turn id."""
    turn_id = session.submit(prompt, ref=ref)
    session.run()
    session.footer_idle = False
    session.writes(user_text(prompt), assistant_text("on it"))
    session.run()
    return turn_id


def finish_turn(session: Session) -> None:
    """Close the running turn the way an ordinary turn closes: on its marker."""
    session.writes(marker())
    session.footer_idle = True
    session.run()


def launch_task(session: Session, tool_use_id: str, task_id: str) -> None:
    session.writes(
        assistant_tool_use(tool_use_id, "run the build"),
        background_launch(tool_use_id, task_id),
    )
    session.run()


def complete_task_while_idle(session: Session, task_id: str, tool_use_id: str) -> None:
    """The completion that wakes an unowned turn, and that turn's whole life."""
    session.footer_idle = False
    session.writes(
        queue_operation("enqueue", notification(task_id, tool_use_id)),
        queue_operation("dequeue"),
        user_text(notification(task_id, tool_use_id)),
        assistant_text("The build finished."),
        marker(),
    )
    session.footer_idle = True
    session.run()


# ----------------------------------------------------------------------
# Requirement 1 — the proxy maintains three counts at all times
# ----------------------------------------------------------------------


def test_requirement_1_the_three_counts_are_tracked_apart(session):
    """All three non-zero at once, then each falling on its own signal.

    They are not three views of one number: a queued prompt is outstanding
    before anything is typed, an unowned turn is outstanding without any
    prompt at all, and a background task is outstanding across turn
    boundaries. This drives a session to the state where all three are up
    together, which is the state neither front-end can represent today.
    """
    start_turn(session, "start two builds")
    launch_task(session, "toolu_1", "bg1")
    launch_task(session, "toolu_2", "bg2")
    finish_turn(session)
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 2}

    # bg1 finishing while the inner is idle wakes a turn nobody asked for; two
    # prompts submitted into it are acknowledged and wait at the keyboard.
    session.footer_idle = False
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1")),
        queue_operation("dequeue"),
        user_text(notification("bg1", "toolu_1")),
        assistant_text("The first build finished."),
    )
    session.run()
    session.submit("A")
    session.submit("B")
    session.run()
    assert session.tracker.outstanding() == {"user": 2, "unowned": 1, "background": 1}
    assert session.typed == ["start two builds"]

    session.writes(marker())
    session.footer_idle = True
    session.run()
    assert session.tracker.outstanding()["unowned"] == 0

    session.footer_idle = False
    session.answers("A")
    session.footer_idle = True
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("B"),
        assistant_text("done"),
        queue_operation("enqueue", notification("bg2", "toolu_2")),
        queue_operation("remove", notification("bg2", "toolu_2")),
        marker(),
    )
    session.footer_idle = True
    session.run()
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}
    invariants.all_of_them(session.messages())


# ----------------------------------------------------------------------
# Requirement 2 — a prompt is acknowledged immediately
# ----------------------------------------------------------------------


def test_requirement_2_the_ack_needs_no_tick_of_the_loop(session):
    """Bounded by the proxy's own processing, not the inner's latency.

    Nothing has run between the `submit` and this assertion — no frame, no
    transcript read, no keystroke — so the ack cannot have waited on the inner
    for anything.
    """
    before = len(session.messages())
    turn_id = session.submit("what does load_timeout_id do?", ref="c-1")
    fresh = session.messages()[before:]
    assert [m["type"] for m in fresh] == ["ack"]
    assert fresh[0]["turn"] == {"id": turn_id, "kind": "user"}
    assert fresh[0]["ref"] == "c-1"
    assert fresh[0]["outstanding"]["user"] == 1
    assert session.typed == []


def test_requirement_2_a_prompt_into_a_busy_inner_is_acked_just_the_same(session):
    """The ack states the resulting count, queued turns included.

    A client blocked on the inner would learn nothing here for as long as the
    turn runs — which is the window in which its composer has to decide what
    to show.
    """
    start_turn(session, "first")
    second = session.submit("second", ref="c-2")
    third = session.submit("third", ref="c-3")
    acks = {m["ref"]: m for m in session.of_type("ack") if "ref" in m}
    assert acks["c-2"]["outstanding"]["user"] == 2
    assert acks["c-3"]["outstanding"]["user"] == 3
    assert session.typed == ["first"]
    assert session.tracker.running_turn.id not in {second, third}


def test_requirement_2_the_command_token_comes_back_on_whatever_answers_it(session):
    """`ref` is the client's handle: on the ack, on the state, on the error.

    Without it the correspondence is positional, and a rejected `submit` —
    answered by an `error` and no `ack` — shifts every later prompt onto the
    wrong turn, silently and for the rest of the session.
    """
    session.send({"type": "submit", "text": "fix the leak", "ref": "c-1"})
    session.send({"type": "submit", "text": "   ", "ref": "c-2"})
    session.send({"type": "status", "ref": "c-3"})
    session.send({"type": "submit", "text": "now test it", "ref": "c-4"})

    client = ReferenceClient()
    for message in session.messages():
        client.consume(message)
    assert client.turn_for("c-1") == "t-1"
    assert client.turn_for("c-2") is None and "c-2" in client.rejected
    assert client.turn_for("c-4") == "t-2"
    assert session.of_type("state")[-1]["ref"] == "c-3"


# ----------------------------------------------------------------------
# Requirement 3 — every turn message says what kind of turn it belongs to
# ----------------------------------------------------------------------


def test_requirement_3_every_turn_message_carries_its_kind(session):
    """Prose, reasoning, tool calls and results, on both kinds of turn."""
    start_turn(session, "look into it")
    session.writes(
        assistant_thinking("two ways to read this"),
        assistant_tool_use("toolu_1", "grep"),
        tool_result("toolu_1", "3 hits"),
        assistant_text("Three hits."),
    )
    session.run()
    launch_task(session, "toolu_2", "bg1")
    finish_turn(session)
    complete_task_while_idle(session, "bg1", "toolu_2")

    carried = [m for m in session.messages() if "turn" in m]
    assert {m["turn"]["kind"] for m in carried} == {"user", "unowned"}
    assert {m["type"] for m in carried} >= {
        "ack",
        "turn_start",
        "thinking",
        "tool_use",
        "tool_result",
        "text",
        "turn_end",
    }
    invariants.turn_identity(session.messages())


def test_requirement_3_a_background_task_is_not_a_turn_kind(session):
    """The requirement's third kind is a message type, not a `turn.kind`.

    A background task has no turn to belong to — it outlives the one that
    launched it, and finishing may start one, fold into one, or start nothing.
    So `task_start` and `task_end` carry no `turn` field,
    and `background` never appears as a kind.
    """
    start_turn(session, "start the build")
    launch_task(session, "toolu_1", "bg1")
    finish_turn(session)
    complete_task_while_idle(session, "bg1", "toolu_1")

    for message in session.messages():
        if message["type"] in {"task_start", "task_end"}:
            assert "turn" not in message
    assert all(
        m["turn"]["kind"] in {"user", "unowned"}
        for m in session.messages()
        if "turn" in m
    )


# ----------------------------------------------------------------------
# Requirement 4 — end-of-turn is explicit, identified and typed
# ----------------------------------------------------------------------


def test_requirement_4_each_end_names_its_turn_and_its_evidence(session):
    """Three turns, three bases: a marker, the fallback tier, and a failure."""
    first = start_turn(session, "first")
    finish_turn(session)

    second = start_turn(session, "second")
    session.footer_idle = True
    session.run(ticks=12, advance=1.0)

    third = start_turn(session, "third")
    session.proxy.fail("inner claude exited")

    ends = {m["turn"]["id"]: m for m in session.of_type("turn_end")}
    assert [ends[t]["basis"] for t in (first, second, third)] == [
        "marker:turn_duration",
        "fallback:idle",
        "error",
    ]
    assert [ends[t]["outcome"] for t in (first, second, third)] == [
        "success",
        "success",
        "error",
    ]
    invariants.end_of_turn_is_typed(session.messages())


def test_requirement_4_a_client_never_has_to_judge_whether_an_end_was_real(session):
    """One end per turn, at the real end — not one per quiet moment.

    The turn below goes idle-looking twice in the middle: after its first
    answer, and again between two tool calls. Deciding whether either was the
    end is the judgement call this requirement removes, and getting it wrong
    is the premature `result` that desynced an earlier client.
    """
    turn_id = start_turn(session, "build it")
    for _ in range(2):
        session.footer_idle = True
        session.run(ticks=3, advance=1.0)
        assert session.of_type("turn_end") == []
        session.footer_idle = False
        session.writes(assistant_tool_use("toolu_x", "step"), tool_result("toolu_x", "ok"))
        session.run()
    finish_turn(session)
    ends = session.of_type("turn_end")
    assert [m["turn"]["id"] for m in ends] == [turn_id]
    assert ends[0]["basis"] == "marker:turn_duration"


# ----------------------------------------------------------------------
# Requirement 5 — every message carries all three counts
# ----------------------------------------------------------------------


def test_requirement_5_the_counts_are_on_every_message_of_every_type(session):
    """Including the ones belonging to no turn — `ready`, `error`, `state`."""
    session.send({"type": "teleport"})
    session.send({"type": "status"})
    start_turn(session, "hello")
    launch_task(session, "toolu_1", "bg1")
    finish_turn(session)

    types = {m["type"] for m in session.messages()}
    assert types >= {"ready", "error", "state", "ack", "turn_start", "task_start"}
    invariants.envelope(session.messages())


def test_requirement_5_a_batch_carries_the_counts_it_ends_with():
    """The counts lead the events inside one batch; they never lag.

    Every message a single transcript entry produces is stamped after that
    entry has been folded into the tables, so all of them carry the counts as
    of the end of the batch. A client that derives its own counts from events
    must therefore compare them at a batch boundary, never mid-batch — and
    since `outstanding` is absolute and restated on the next message, reading
    the counts rather than deriving them is always correct.
    """
    tracker = TurnTracker()
    emitter = Emitter(tracker, out=io.StringIO())
    running, _ = tracker.submit("start the build")
    tracker.mark_typed(running)
    emitter.emit_all(tracker.on_entry(user_text("start the build")))
    tracker.submit("queued behind it")

    # One entry, two events: the task is registered and finished at once, so
    # `task_start` is already stamped with the count `task_end` leaves behind.
    batch = emitter.emit_all(
        tracker.on_entry(queue_operation("enqueue", notification("bg9", "toolu_9")))
    )
    assert [m["type"] for m in batch] == ["task_start", "task_end"]
    assert {m["outstanding"]["background"] for m in batch} == {0}

    failure = emitter.emit_all(tracker.fail_session("inner claude exited"))
    assert [m["type"] for m in failure] == ["error", "turn_end", "turn_end"]
    assert all(m["outstanding"] == {"user": 0, "unowned": 0, "background": 0} for m in failure)


# ----------------------------------------------------------------------
# Requirement 6 — every turn has an id, and it is the id its end carries
# ----------------------------------------------------------------------


def test_requirement_6_an_id_reaches_the_client_before_anything_uses_it(session):
    """A user turn's in its ack, an unowned turn's in its `turn_start`."""
    start_turn(session, "start the build")
    launch_task(session, "toolu_1", "bg1")
    finish_turn(session)
    complete_task_while_idle(session, "bg1", "toolu_1")

    invariants.turn_identity(session.messages())
    unowned = [m for m in session.of_type("turn_start") if m["turn"]["kind"] == "unowned"]
    assert len(unowned) == 1
    assert unowned[0]["turn"]["id"] not in {m["turn"]["id"] for m in session.of_type("ack")}


def test_requirement_6_a_wait_resolves_on_the_id_the_ack_issued(session):
    """The client's whole correlation rule, exercised end to end.

    Two prompts and an unowned turn, so the end of the second prompt's turn is
    the third `turn_end` to arrive. Nothing about its position says so; the id
    does.
    """
    client = ReferenceClient()
    session.submit("A", ref="c-A")
    session.run()
    session.footer_idle = False
    session.writes(user_text("A"), assistant_text("on it"))
    session.run()
    launch_task(session, "toolu_1", "bg1")
    session.submit("B", ref="c-B")
    finish_turn(session)
    complete_task_while_idle(session, "bg1", "toolu_1")
    session.footer_idle = False
    session.answers("B")
    session.footer_idle = True
    session.run()

    for message in session.messages():
        client.consume(message)
    ends = [m["turn"]["id"] for m in session.of_type("turn_end")]
    assert len(ends) == 3
    assert ends[-1] == client.turn_for("c-B")
    assert client.is_finished("c-A") and client.is_finished("c-B")
    assert client.derived == {"user": 0, "unowned": 0, "background": 0}


# ----------------------------------------------------------------------
# Requirement 7 — the background-task lifecycle is reported
# ----------------------------------------------------------------------


def test_requirement_7_launch_and_completion_are_separate_events(session):
    """And the turn a completion wakes says which task woke it."""
    start_turn(session, "start the build")
    launch_task(session, "toolu_1", "bg1")
    finish_turn(session)
    complete_task_while_idle(session, "bg1", "toolu_1")

    assert [m["task"]["id"] for m in session.of_type("task_start")] == ["bg1"]
    assert [m["task"]["id"] for m in session.of_type("task_end")] == ["bg1"]
    assert session.of_type("task_start")[0]["summary"] == "run the build"
    assert session.of_type("task_end")[0]["exit_code"] == 0
    woken = [m for m in session.of_type("turn_start") if m["turn"]["kind"] == "unowned"]
    assert woken[0]["caused_by"] == "bg1"


def test_requirement_7_a_completion_that_wakes_nothing_is_still_reported(session):
    """Mid-turn, the notification is folded into the running turn.

    The count still has to fall, and it falls on `task_end` rather than on any
    turn message — which is why the background count is not derived from
    turns. This is the case ADE's outstanding-task reconciliation exists to
    paper over.
    """
    turn_id = start_turn(session, "start the build")
    launch_task(session, "toolu_1", "bg1")
    session.writes(
        queue_operation("enqueue", notification("bg1", "toolu_1", exit_code=3)),
        queue_operation("remove", notification("bg1", "toolu_1", exit_code=3)),
    )
    session.run()
    assert session.of_type("task_end")[0]["exit_code"] == 3
    assert [m["turn"]["id"] for m in session.of_type("turn_start")] == [turn_id]
    finish_turn(session)
    invariants.every_turn_ends(session.messages())


# ----------------------------------------------------------------------
# Requirement 8 — a client can ask for the authoritative state
# ----------------------------------------------------------------------


def test_requirement_8_the_reply_stands_on_its_own(session):
    """A client that has read nothing at all reaches the right view from it."""
    start_turn(session, "start the build")
    launch_task(session, "toolu_1", "bg1")
    session.submit("queued behind it")

    session.send({"type": "status", "ref": "q-1"})
    state = session.of_type("state")[-1]

    newcomer = ReferenceClient()
    newcomer.adopt(state)
    assert newcomer.derived == state["outstanding"] == session.tracker.outstanding()
    assert newcomer.derived == {"user": 2, "unowned": 0, "background": 1}
    assert state["ref"] == "q-1"


def test_requirement_8_two_queries_in_flight_cannot_be_confused(session):
    """Which is what the token is for: the replies are told apart by `ref`."""
    start_turn(session, "start the build")
    session.send({"type": "status", "ref": "q-1"})
    session.submit("queued behind it")
    session.send({"type": "status", "ref": "q-2"})

    replies = {m["ref"]: m for m in session.of_type("state")}
    assert replies["q-1"]["outstanding"]["user"] == 1
    assert replies["q-2"]["outstanding"]["user"] == 2
    assert session.typed == ["start the build"]


# ----------------------------------------------------------------------
# Requirement 9 — queued work is distinguishable from running work
# ----------------------------------------------------------------------


def test_requirement_9_queued_and_running_are_told_apart(session):
    """`outstanding.user` says three; the state reply says which three."""
    running = start_turn(session, "first")
    second = session.submit("second")
    third = session.submit("third")
    session.send({"type": "status"})

    state = session.of_type("state")[-1]
    assert state["outstanding"]["user"] == 3
    by_state = {turn["id"]: turn["state"] for turn in state["turns"]}
    assert by_state == {running: "running", second: "queued", third: "queued"}

    # The second prompt is typed as soon as the first turn ends, but it is
    # still queued until the transcript shows it: a prompt in the inner's
    # keyboard buffer is not yet a turn the inner is working on.
    finish_turn(session)
    session.send({"type": "status"})
    typed_not_yet_bound = {t["id"]: t["state"] for t in session.of_type("state")[-1]["turns"]}
    assert typed_not_yet_bound == {second: "queued", third: "queued"}
    assert session.typed == ["first", "second"]

    session.footer_idle = False
    session.writes(user_text("second"), assistant_text("on it"))
    session.run()
    session.send({"type": "status"})
    after = {t["id"]: t["state"] for t in session.of_type("state")[-1]["turns"]}
    assert after == {second: "running", third: "queued"}


# ----------------------------------------------------------------------
# Requirement 10 — errors are typed messages that strand no counter
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, reason",
    [
        ("{not json", "not JSON"),
        ('"a string"', "not a JSON object"),
        ('{"type": "teleport"}', "unknown command"),
        ('{"type": "submit", "text": "   "}', "empty prompt"),
    ],
)
def test_requirement_10_an_unusable_command_is_reported_and_changes_nothing(
    session, line, reason
):
    before = session.tracker.outstanding()
    assert session.proxy.on_stdin_line(line) is None
    last = session.messages()[-1]
    assert last["type"] == "error", reason
    assert isinstance(last["error"], str)
    assert "turn" not in last
    assert session.tracker.outstanding() == before
    session.run()
    assert session.typed == []


def test_requirement_10_a_failure_closes_everything_it_killed(session):
    """The running turn, the queue behind it, and every unfinished task.

    R7 rules out client-initiated cancellation, but this involuntary path is
    still specified: nothing may be left for a client to wait on, or the
    counter it gates its composer on never returns to zero.
    """
    running = start_turn(session, "first")
    launch_task(session, "toolu_1", "bg1")
    queued = session.submit("second")

    session.proxy.fail("inner claude exited")

    tail = [m for m in session.messages() if m["type"] in {"error", "turn_end", "task_end"}]
    assert tail[0]["type"] == "error" and tail[0]["error"] == "inner claude exited"
    assert {m["turn"]["id"] for m in tail if m["type"] == "turn_end"} == {running, queued}
    assert tail[-1]["exit_code"] is None
    assert not session.proxy.has_unfinished_work
    invariants.every_turn_ends(session.messages())


def test_requirement_10_nothing_survives_a_failure_to_be_bound_later(session):
    """A closed turn cannot come back, however late a keystroke arrives.

    `fail` drains the queue as well as ending its turns, so the turn table has
    nothing left waiting to be matched. `cli.serve` only fails a session once
    the io loop has exited, so a prompt going out afterwards is not a path the
    process actually takes — but if one did, it must bind to nothing rather
    than resurrect a turn the client was already told had ended.
    """
    start_turn(session, "first")
    session.submit("second")
    session.proxy.fail("inner claude exited")
    assert session.queue.is_empty

    session.footer_idle = True
    session.run()
    assert session.tracker.typed_turn is None
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}
    assert len(session.of_type("turn_end")) == 2


# ----------------------------------------------------------------------
# Requirement 11 — no turn may hang without an end-of-turn
# ----------------------------------------------------------------------


def test_requirement_11_an_unrecognised_turn_shape_ends_on_the_idle_tier(session):
    """The everyday tier-2 path: the marker set is open, so this is not an error."""
    turn_id = start_turn(session, "/some-command-we-do-not-know")
    session.footer_idle = True
    session.run(ticks=12, advance=1.0)
    ends = session.of_type("turn_end")
    assert [m["turn"]["id"] for m in ends] == [turn_id]
    assert ends[0]["basis"] == "fallback:idle"


def test_requirement_11_a_stale_active_footer_ends_the_turn_on_pty_silence(session):
    """`fallback:quiet`: the footer says working, and nothing has painted.

    A `--resume` repaint leaves the last frame reading active and then stops
    emitting frames, so the footer alone would hold the turn open forever. A
    running turn paints its spinner about once a second, so silence this long
    is ground truth whatever the footer says.
    """
    turn_id = start_turn(session, "resume and carry on")
    session.footer_idle = False
    session.run(ticks=8, advance=10.0, pty_quiet=True)
    ends = session.of_type("turn_end")
    assert [m["turn"]["id"] for m in ends] == [turn_id]
    assert ends[0]["basis"] == "fallback:quiet"


def test_requirement_11_the_ceiling_ends_a_turn_nothing_else_would(session):
    """`fallback:stalled`: frames still arriving, footer still active, and no
    transcript entry for half an hour. It can fire on a turn that is genuinely
    alive, which is why it is set far out — its whole job is to make "hangs
    forever" impossible."""
    turn_id = start_turn(session, "run the whole suite")
    session.footer_idle = False
    session.run(ticks=20, advance=100.0)
    ends = session.of_type("turn_end")
    assert [m["turn"]["id"] for m in ends] == [turn_id]
    assert ends[0]["basis"] == "fallback:stalled"
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}


def test_requirement_11_a_marker_arriving_after_the_fallback_opens_no_phantom(session):
    """The late signal a self-correcting counter has to survive.

    The fallback closed the turn; the marker it was waiting for turns up
    afterwards. Ended turns stay in the table precisely so this is recognised
    and dropped rather than treated as a turn nobody has seen the start of.
    """
    turn_id = start_turn(session, "slow one")
    session.footer_idle = True
    session.run(ticks=12, advance=1.0)
    assert len(session.of_type("turn_end")) == 1

    session.writes(marker())
    session.run()
    assert [m["turn"]["id"] for m in session.of_type("turn_end")] == [turn_id]
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0}
    invariants.all_of_them(session.messages())


# ----------------------------------------------------------------------
# Requirement 12 — session identity is stated first, and always
# ----------------------------------------------------------------------


def test_requirement_12_the_session_id_is_the_first_thing_a_client_reads(session):
    """Before any command is read or any key typed, the client is told which
    session it is talking to — which is what it records to resume later."""
    first = session.messages()[0]
    assert first["seq"] == 1
    assert first["type"] == "ready"
    assert first["session_id"] == "session-under-test"


def test_requirement_12_every_invocation_names_a_session(tmp_path):
    """"Always" is the load-bearing word: the invocations that could not name
    a session before are exactly the recovery ones, so a null id would be
    missing where it matters most.
    """
    from agentproxy import inner_claude
    from agentproxy.transcript_tailer import encode_cwd

    cwd = "/work/thing"
    project_dir = tmp_path / encode_cwd(cwd)
    project_dir.mkdir(parents=True)
    (project_dir / "prior.jsonl").write_text('{"type":"user"}\n')

    for argv in ([], ["--resume", "prior"], ["--continue"], ["--resume"],
                 ["--continue", "--fork-session"], ["--session-id", "named"]):
        session_id, _, rewritten = inner_claude.resolve_session(
            argv, cwd=cwd, projects_root=tmp_path
        )
        assert session_id, f"{argv} named no session"
        # And the inner is told the same id the client was, so the two cannot
        # be looking at different transcripts.
        assert session_id in rewritten


def test_requirement_12_an_unresolvable_invocation_says_so_and_starts_nothing():
    """The one case where `ready` is not first, because there is no session to
    be ready for. It is a lone `error` rather than a spawn that dies."""
    from agentproxy import cli

    out = io.StringIO()
    code = cli.serve(
        io.BytesIO(b""), out, passthrough=["--resume", "abc", "--session-id", "def"]
    )
    messages = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 2
    assert len(messages) == 1
    assert messages[0]["type"] == "error"
    assert "--fork-session" in messages[0]["error"]
