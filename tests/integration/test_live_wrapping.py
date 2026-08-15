"""The live path, driven without a pty: what a client submits and what it hears.

`Session` below stands in for the io loop and the inner `claude` together. It
calls `Proxy` in the same places and the same order the real loop does — drain
the transcript, then decide whether a queued prompt may be typed — and lets a
test say exactly what the inner writes and when. What comes out is the byte
stream a client would read, so these are assertions about the protocol rather
than about internals.

Three of the tests here are [SPEC success criteria](../../../../proj_docs/agent-proxy/SPEC.md)
2, 4 and 5, which are what Phase 2 is measured by.
"""

from __future__ import annotations

import io
import json

import pytest

from agentproxy.fallback import FallbackEot
from agentproxy.liveness import Liveness
from agentproxy.protocol import Emitter
from agentproxy.proxy import Proxy
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


HRULE = "─" * 80
IDLE_FOOTER = "  ? for shortcuts"
ACTIVE_FOOTER = "  esc to interrupt"


# ----------------------------------------------------------------------
# Transcript entries the inner writes
# ----------------------------------------------------------------------


def user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text: str = "ok") -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"output_tokens": 7},
        },
    }


def assistant_tool_use(tool_use_id: str, description: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {"command": "make", "description": description},
                }
            ]
        },
    }


def tool_result(tool_use_id: str, content: str) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ]
        },
    }


def background_launch(tool_use_id: str, task_id: str) -> dict:
    return tool_result(
        tool_use_id,
        f"Command running in background with ID: {task_id}. Output is being "
        "written to: /tmp/build.output.",
    )


def notification(task_id: str, tool_use_id: str, exit_code: int = 0) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        "<status>completed</status>\n"
        f'<summary>Background command "make" completed (exit code {exit_code})'
        "</summary>\n"
        "</task-notification>"
    )


def queue_operation(operation: str, content: str | None = None) -> dict:
    entry = {"type": "queue-operation", "operation": operation}
    if content is not None:
        entry["content"] = content
    return entry


def marker(subtype: str = "turn_duration", duration_ms: int = 1200) -> dict:
    return {"type": "system", "subtype": subtype, "durationMs": duration_ms}


# ----------------------------------------------------------------------
# The harness
# ----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Screen:
    def __init__(self, footer: str) -> None:
        self.display = [HRULE, "❯ Try \"fix the bug\"", HRULE, footer] + [""] * 36


class Session:
    """One proxied session: a scripted inner `claude` and a stand-in io loop."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.out = io.StringIO()
        self.tracker = TurnTracker()
        self.queue = SubmitQueue()
        self.activity = {"last_pty": self.clock.now, "last_activity": self.clock.now}
        self._entries: list[dict] = []
        self._payloads: list[dict] = []
        self.typed: list[str] = []
        self.footer_idle = True
        self.proxy = Proxy(
            tracker=self.tracker,
            emitter=Emitter(self.tracker, out=self.out),
            queue=self.queue,
            liveness=Liveness(clock=self.clock),
            fallback=FallbackEot(clock=self.clock),
            read_entries=self._read_entries,
            activity=self.activity,
        )
        self.proxy.announce_ready("session-under-test")
        # Two frames, because the inner is not considered ready for input until
        # it has painted a second one (see Liveness.has_settled).
        self.tick()
        self.tick()

    # -- the client's side --------------------------------------------

    def submit(self, text: str) -> str:
        """Send a `submit` and return the turn id the ack carried."""
        self.send({"type": "submit", "text": text})
        return self.messages()[-1]["turn"]["id"]

    def send(self, command: dict) -> None:
        payload = self.proxy.on_stdin_line(json.dumps(command))
        if payload is not None:
            self._payloads.append(payload)

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def of_type(self, message_type: str) -> list[dict]:
        return [m for m in self.messages() if m["type"] == message_type]

    # -- the inner claude's side --------------------------------------

    def writes(self, *entries: dict) -> None:
        """The inner appends entries to its transcript; the next tick reads them."""
        self._entries.extend(entries)

    def answers(self, prompt: str, *, text: str = "ok") -> None:
        """The whole of an ordinary turn: the echoed prompt, a reply, a marker."""
        self.writes(user_text(prompt), assistant_text(text), marker())

    def _read_entries(self) -> list[dict]:
        entries, self._entries = self._entries, []
        return entries

    # -- the loop ------------------------------------------------------

    def tick(self, advance: float = 0.0) -> None:
        """One pass of the io loop, in the order `cli.py` wires it.

        A frame arriving is also pty bytes arriving, so the clock the fallback
        tier reads for silence moves with it.
        """
        self.clock.advance(advance)
        self.activity["last_pty"] = self.clock.now
        self.proxy.on_commit(Screen(IDLE_FOOTER if self.footer_idle else ACTIVE_FOOTER))
        self.proxy.on_iteration()
        if self._payloads and self.proxy.is_ready_to_send():
            payload = self._payloads.pop(0)
            self.typed.append(payload["message"]["content"])
            self.proxy.on_typed(payload)

    def run(self, ticks: int = 4, advance: float = 0.0) -> None:
        for _ in range(ticks):
            self.tick(advance)


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
    """The failure that desyncs bzdash today: the footer settles idle between
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
    """The other half of Decision 4: folded into the running turn, not woken
    as a new one — and the count has to fall either way."""
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
    it first — which positional matching, the mechanism bzdash uses today,
    cannot do."""
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
