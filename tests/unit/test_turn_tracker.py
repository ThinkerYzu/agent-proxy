"""TurnTracker rules, driven by hand-built transcript entries.

The entry builders below mirror the shapes seen in a real session; the
fixture replay in `tests/integration` covers the same rules against a whole
session's worth of them.
"""

from __future__ import annotations

import pytest

from agentproxy.turn_tracker import (
    CANCEL_HELD,
    CANCEL_INTERRUPT,
    CANCEL_WITHDRAWN,
    KIND_UNOWNED,
    KIND_USER,
    STATE_ENDED,
    STATE_QUEUED,
    STATE_RUNNING,
    CancelError,
    TurnTracker,
)


# ----------------------------------------------------------------------
# Entry builders
# ----------------------------------------------------------------------


def user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text: str = "ok", output_tokens: int = 0) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"output_tokens": output_tokens},
        },
    }


def assistant_tool_use(tool_use_id: str, description: str, background: bool) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {
                        "command": "true",
                        "description": description,
                        "run_in_background": background,
                    },
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


def marker(subtype: str = "turn_duration", duration_ms: int = 1000) -> dict:
    return {"type": "system", "subtype": subtype, "durationMs": duration_ms}


def interruption(for_tool_use: bool = False) -> dict:
    """The `user` entry `Esc` makes the inner write, once the model had produced
    something. A list holding one text block, never a plain string."""
    text = "[Request interrupted by user for tool use]" if for_tool_use else "[Request interrupted by user]"
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def declined_tool(tool_use_id: str) -> dict:
    return tool_result(
        tool_use_id,
        "The user doesn't want to proceed with this tool use. The tool use was rejected.",
    )


def notification(
    task_id: str,
    tool_use_id: str,
    exit_code: int = 0,
    status: str = "completed",
    output_file: str | None = None,
) -> str:
    output_tag = f"<output-file>{output_file}</output-file>\n" if output_file else ""
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"{output_tag}"
        f"<status>{status}</status>\n"
        f'<summary>Background command "work" completed (exit code {exit_code})</summary>\n'
        "</task-notification>"
    )


def monitor_event(task_id: str, tool_use_id: str, event: str = "line") -> str:
    """A monitor firing an event: a notification with no `<status>` at all."""
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f'<summary>Monitor event: "watching"</summary>\n'
        f"<event>{event}</event>\n"
        "</task-notification>"
    )


def task_stop_result(tool_use_id: str, task_id: str, command: str = "true") -> dict:
    """`TaskStop`'s own tool_result confirming it stopped `task_id`.

    Structured JSON in real sessions, but recognised by the same `"Successfully
    stopped task: <id>"` wording either way — see `_STOP_RE`.
    """
    content = (
        f'{{"message":"Successfully stopped task: {task_id} ({command})",'
        f'"task_id":"{task_id}","task_type":"local_bash","command":"{command}"}}'
    )
    return tool_result(tool_use_id, content)


def queue_operation(operation: str, content: str | None = None) -> dict:
    entry = {"type": "queue-operation", "operation": operation}
    if content is not None:
        entry["content"] = content
    return entry


def types_of(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def launch_background(tracker: TurnTracker, task_id: str, tool_use_id: str) -> list[dict]:
    """Run the two entries that put a background command on the wire."""
    events = tracker.on_entry(assistant_tool_use(tool_use_id, "work", background=True))
    events += tracker.on_entry(
        tool_result(
            tool_use_id,
            f"Command running in background with ID: {task_id}. Output is being "
            "written to: /tmp/x.output.",
        )
    )
    return events


@pytest.fixture
def tracker() -> TurnTracker:
    return TurnTracker()


def running_user_turn(tracker: TurnTracker, text: str = "hello"):
    """Submit, type and bind one prompt; return its turn."""
    turn, _ = tracker.submit(text)
    tracker.mark_typed(turn)
    tracker.on_entry(user_text(text))
    return turn


# ----------------------------------------------------------------------
# Submission and binding
# ----------------------------------------------------------------------


def test_submit_counts_before_anything_is_typed(tracker):
    turn, events = tracker.submit("hello")
    assert types_of(events) == ["ack"]
    assert events[0]["turn"] == {"id": turn.id, "kind": KIND_USER}
    assert turn.state == STATE_QUEUED
    assert tracker.outstanding() == {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_matching_entry_starts_the_user_turn(tracker):
    turn, _ = tracker.submit("what does load_timeout_id do?")
    tracker.mark_typed(turn)
    events = tracker.on_entry(user_text("what does load_timeout_id do?"))
    assert types_of(events) == ["turn_start"]
    assert events[0]["turn"]["kind"] == KIND_USER
    assert turn.state == STATE_RUNNING


def test_binding_ignores_whitespace_the_tui_rewrapped(tracker):
    turn, _ = tracker.submit("fix   the\nbug")
    tracker.mark_typed(turn)
    tracker.on_entry(user_text("fix the bug"))
    assert turn.state == STATE_RUNNING


def test_unmatched_entry_leaves_the_typed_prompt_outstanding(tracker):
    turn, _ = tracker.submit("mine")
    tracker.mark_typed(turn)
    tracker.on_entry(user_text("not mine"))
    tracker.on_entry(assistant_text())
    assert turn.state == STATE_QUEUED
    assert tracker.outstanding() == {"user": 1, "unowned": 1, "background": 0, "monitor": 0, "agent": 0}


def test_a_prompt_the_tui_ate_a_trailing_backslash_from_still_binds(tracker):
    """The TUI reads a trailing `\\` as a line continuation and drops it.

    The prompt reaches `claude` one character shorter than it was typed, and
    before this was handled the entry never matched: the turn stayed queued,
    `outstanding.user` stuck at 1, and the session took no further prompt.
    """
    turn, _ = tracker.submit("do we have docs on glyph rendering?\\")
    tracker.mark_typed(turn)
    tracker.on_entry(user_text("do we have docs on glyph rendering?"))
    assert turn.state == STATE_RUNNING
    assert tracker.outstanding() == {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_end_typed_turn_closes_a_prompt_the_transcript_never_showed(tracker):
    """The only way out for a typed turn that no entry will ever match."""
    turn, _ = tracker.submit("mine")
    tracker.mark_typed(turn)
    tracker.on_entry(user_text("not mine"))
    tracker.on_entry(assistant_text())
    tracker.on_entry(marker())
    assert tracker.outstanding()["user"] == 1

    events = tracker.end_typed_turn("fallback:unmatched")
    assert types_of(events) == ["turn_end"]
    assert events[0]["basis"] == "fallback:unmatched"
    assert turn.state == STATE_ENDED
    assert tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}
    assert tracker.typed_turn is None


def test_end_typed_turn_does_nothing_with_no_prompt_outstanding(tracker):
    assert tracker.end_typed_turn("fallback:unmatched") == []


def test_queued_prompt_binds_after_the_unowned_turn_ends(tracker):
    turn, _ = tracker.submit("mine")
    tracker.mark_typed(turn)
    tracker.on_entry(user_text("not mine"))
    tracker.on_entry(assistant_text())
    tracker.on_entry(marker())
    tracker.on_entry(user_text("mine"))
    assert turn.state == STATE_RUNNING
    assert tracker.outstanding() == {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


# ----------------------------------------------------------------------
# Unowned turns and the candidate rule
# ----------------------------------------------------------------------


def test_unowned_turn_is_announced_only_once_the_model_works(tracker):
    assert tracker.on_entry(user_text("<task-notification/>")) == []
    assert tracker.outstanding()["unowned"] == 0
    events = tracker.on_entry(assistant_text())
    assert types_of(events) == ["turn_start", "text"]
    assert events[0]["turn"]["kind"] == KIND_UNOWNED
    assert tracker.outstanding()["unowned"] == 1


def test_compact_cluster_never_becomes_a_turn(tracker):
    """The entries `/compact` writes after its boundary must not open a turn.

    They are user-shaped, they arrive with nothing running, and no model work
    follows them — so the candidate they open is dropped when the next real
    prompt binds.
    """
    compact, _ = tracker.submit("/compact")
    tracker.mark_typed(compact)
    tracker.on_entry(user_text("/compact"))
    tracker.on_entry(marker("compact_boundary"))
    assert compact.state == STATE_ENDED

    for text in (
        "This session is being continued from a previous conversation…",
        "<local-command-caveat>Caveat: …</local-command-caveat>",
        "<command-name>/compact</command-name>",
        "<local-command-stdout>Compacted</local-command-stdout>",
    ):
        assert tracker.on_entry(user_text(text)) == []

    following, _ = tracker.submit("carry on")
    tracker.mark_typed(following)
    events = tracker.on_entry(user_text("carry on"))
    assert types_of(events) == ["turn_start"]
    assert [t.kind for t in tracker.turns] == [KIND_USER, KIND_USER]


def test_a_user_shaped_entry_arriving_mid_turn_starts_nothing(tracker):
    """A skill injection, a hook's output, anything written into a live turn.

    The rule needs no list of these: a turn is already running, so nothing
    here can open one. Modelled rather than captured — the recorded session
    the fixture came from contains no mid-turn injection.
    """
    turn = running_user_turn(tracker)
    tracker.on_entry(assistant_text())
    assert tracker.on_entry(user_text("<command-name>/widget-lint</command-name>")) == []
    assert tracker.running_turn is turn
    assert len(tracker.turns) == 1
    tracker.on_entry(marker())
    assert tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_consecutive_unmatched_entries_fold_into_one_turn(tracker):
    tracker.on_entry(user_text("first"))
    tracker.on_entry(user_text("second"))
    tracker.on_entry(assistant_text())
    assert len(tracker.turns) == 1
    assert tracker.outstanding()["unowned"] == 1


# ----------------------------------------------------------------------
# End of turn
# ----------------------------------------------------------------------


def test_marker_ends_the_turn_and_names_its_basis(tracker):
    turn = running_user_turn(tracker)
    tracker.on_entry(assistant_text(output_tokens=42))
    events = tracker.on_entry(marker(duration_ms=25658))
    assert types_of(events) == ["turn_end"]
    assert events[0]["basis"] == "marker:turn_duration"
    assert events[0]["outcome"] == "success"
    assert events[0]["usage"] == {"duration_ms": 25658, "output_tokens": 42}
    assert tracker.outstanding()["user"] == 0


def test_compact_boundary_ends_the_turn_too(tracker):
    running_user_turn(tracker, "/compact")
    events = tracker.on_entry(marker("compact_boundary"))
    assert events[0]["basis"] == "marker:compact_boundary"


def test_repeated_marker_opens_no_phantom_turn(tracker):
    running_user_turn(tracker)
    tracker.on_entry(marker())
    assert tracker.on_entry(marker()) == []
    assert len(tracker.turns) == 1


def test_fallback_ends_a_turn_no_marker_closed(tracker):
    turn = running_user_turn(tracker)
    events = tracker.end_running_turn("fallback:idle")
    assert types_of(events) == ["turn_end"]
    assert events[0]["basis"] == "fallback:idle"
    assert turn.state == STATE_ENDED
    assert tracker.end_running_turn("fallback:idle") == []


# ----------------------------------------------------------------------
# Cancellation — the interruption marker, and cancel by state
# ----------------------------------------------------------------------


def test_the_interruption_entry_ends_the_running_turn_as_cancelled(tracker):
    turn = running_user_turn(tracker)
    tracker.cancel(turn.id)
    tracker.on_entry(assistant_text("half an answer", output_tokens=9))
    events = tracker.on_entry(interruption())
    assert types_of(events) == ["turn_end"]
    assert events[0]["basis"] == "marker:interrupted"
    assert events[0]["outcome"] == "cancelled"
    assert events[0]["usage"] == {"output_tokens": 9}
    assert tracker.outstanding()["user"] == 0


def test_the_tool_form_follows_the_declined_tool_result_inside_the_turn(tracker):
    """An aborted tool is declined first, in a `tool_result` that belongs to
    the turn, and the interruption entry comes after it."""
    turn = running_user_turn(tracker)
    tracker.cancel(turn.id)
    tracker.on_entry(assistant_tool_use("toolu_1", "sleep", background=False))
    declined = tracker.on_entry(declined_tool("toolu_1"))
    assert types_of(declined) == ["tool_result"]
    assert declined[0]["turn"]["id"] == turn.id
    events = tracker.on_entry(interruption(for_tool_use=True))
    assert [(e["type"], e["basis"], e["outcome"]) for e in events] == [
        ("turn_end", "marker:interrupted", "cancelled")
    ]


def test_an_interruption_nobody_asked_for_still_ends_the_turn(tracker):
    """The proxy is the only thing typing, so this cannot happen — but if it
    did, the turn is over either way, and the log says who did not ask."""
    running_user_turn(tracker)
    events = tracker.on_entry(interruption())
    assert events[0]["outcome"] == "cancelled"


def test_a_turn_duration_after_the_interruption_is_a_late_marker(tracker):
    """Older inners wrote one after the interruption entry; it finds no turn
    running and opens nothing."""
    running_user_turn(tracker)
    tracker.on_entry(interruption())
    assert tracker.on_entry(marker()) == []
    assert len(tracker.turns) == 1


def test_an_interruption_with_nothing_running_opens_no_turn(tracker):
    """The entry is shaped like a prompt, but it is the end of something,
    never the start — no candidate, no phantom."""
    assert tracker.on_entry(interruption()) == []
    assert tracker.on_entry(assistant_text()) != []  # a real turn can still open
    assert len(tracker.turns) == 1


def test_an_interruption_drops_an_unconfirmed_candidate(tracker):
    tracker.on_entry(user_text("something the inner wrote"))
    assert tracker.on_entry(interruption()) == []
    assert tracker.turns == []


def test_a_prompt_worded_like_the_marker_is_still_a_prompt(tracker):
    """The marker is a list of text blocks; a client's prompt is a string."""
    turn = running_user_turn(tracker, "[Request interrupted by user]")
    assert turn.state == STATE_RUNNING


def test_cancelling_a_queued_turn_withdraws_and_ends_it_at_once(tracker):
    turn, _ = tracker.submit("later")
    action, events = tracker.cancel(turn.id)
    assert action == CANCEL_WITHDRAWN
    assert [(e["type"], e["basis"], e["outcome"]) for e in events] == [
        ("turn_end", "cancel:withdrawn", "cancelled")
    ]
    assert turn.state == STATE_ENDED
    assert tracker.outstanding()["user"] == 0


def test_cancelling_a_running_turn_asks_for_the_interrupt_and_ends_nothing(tracker):
    turn = running_user_turn(tracker)
    action, events = tracker.cancel(turn.id)
    assert action == CANCEL_INTERRUPT
    assert events == []
    assert turn.cancel_requested and not turn.interrupt_sent
    assert turn.state == STATE_RUNNING


def test_cancelling_a_typed_turn_is_held_until_it_binds(tracker):
    """Typed but not yet in the transcript: an `Esc` now would land on an
    idle composer. The request is kept and acted on once the turn runs."""
    turn, _ = tracker.submit("hello")
    tracker.mark_typed(turn)
    action, events = tracker.cancel(turn.id)
    assert (action, events) == (CANCEL_HELD, [])
    assert turn.state == STATE_QUEUED
    tracker.on_entry(user_text("hello"))
    assert tracker.running_turn is turn and turn.cancel_requested


def test_a_second_cancel_for_the_same_turn_asks_for_no_second_esc(tracker):
    turn = running_user_turn(tracker)
    tracker.cancel(turn.id)
    assert tracker.cancel(turn.id) == (CANCEL_HELD, [])


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda t: "t-99", "no turn"),
        (lambda t: running_user_turn(t).id and t.end_running_turn("marker:turn_duration") and "t-1", "already ended"),
    ],
)
def test_a_cancel_that_cannot_be_acted_on_is_an_error(tracker, setup, reason):
    turn_id = setup(tracker)
    with pytest.raises(CancelError, match=reason):
        tracker.cancel(turn_id)


def test_an_unowned_turn_cannot_be_cancelled(tracker):
    """A handed-back prompt is recognised by the text the proxy typed, which
    an unowned turn does not have — so the silent ending could not be seen."""
    tracker.on_entry(user_text("wake-up"))
    tracker.on_entry(assistant_text())
    unowned = tracker.running_turn
    assert unowned.kind == KIND_UNOWNED
    with pytest.raises(CancelError, match="unowned"):
        tracker.cancel(unowned.id)


def test_the_restored_prompt_ends_the_cancelled_turn_on_screen_evidence(tracker):
    turn = running_user_turn(tracker)
    assert tracker.end_restored_turn() == []  # not cancelled: nothing to end
    tracker.cancel(turn.id)
    events = tracker.end_restored_turn()
    assert [(e["type"], e["basis"], e["outcome"]) for e in events] == [
        ("turn_end", "cancel:restored", "cancelled")
    ]
    assert tracker.end_restored_turn() == []


# ----------------------------------------------------------------------
# Background tasks
# ----------------------------------------------------------------------


def test_launch_and_mid_turn_completion_leave_no_extra_turn(tracker):
    """The `remove` case: a task finishing mid-turn is folded into that turn."""
    running_user_turn(tracker)
    events = launch_background(tracker, "bg1", "toolu_1")
    assert types_of(events) == ["tool_use", "task_start", "tool_result"]
    assert events[1]["summary"] == "work"
    assert events[1]["command"] == "true"
    assert tracker.outstanding()["background"] == 1

    events = tracker.on_entry(queue_operation("enqueue", notification("bg1", "toolu_1")))
    assert types_of(events) == ["task_end"]
    assert events[0]["exit_code"] == 0
    tracker.on_entry(queue_operation("remove", notification("bg1", "toolu_1")))
    tracker.on_entry(marker())
    assert len(tracker.turns) == 1
    assert tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_task_start_carries_the_command_that_launched_it(tracker):
    """`command` comes from the same `tool_use` block `summary` is cached from."""
    running_user_turn(tracker)
    tracker.on_entry(assistant_tool_use("toolu_1", "run the linter", background=True))
    events = tracker.on_entry(
        tool_result("toolu_1", "Command running in background with ID: bg1.")
    )
    assert events[0]["command"] == "true"


def test_task_end_carries_the_notifications_output_file(tracker):
    """`output_file` is a path here — `TurnTracker` does no I/O — for `Proxy`
    to resolve into `output` before the message reaches a client."""
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    events = tracker.on_entry(
        user_text(notification("bg1", "toolu_1", output_file="/tmp/bg1.output"))
    )
    assert types_of(events) == ["task_end"]
    assert events[0]["output_file"] == "/tmp/bg1.output"


def test_a_task_with_no_command_input_reports_command_as_null(tracker):
    """A monitor or subagent's launching `tool_use` has no `command` field at
    all — unlike Bash, whose real shape every other test here stands in for."""
    running_user_turn(tracker)
    tracker.on_entry(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Monitor",
                        "input": {"pattern": "Unit tests: (success|failure)"},
                    }
                ]
            },
        }
    )
    events = tracker.on_entry(
        tool_result(
            "toolu_1",
            "Monitor started (task bmon, timeout 60000ms). You will be notified "
            "on each event.",
        )
    )
    assert events[0]["task"]["kind"] == "monitor"
    assert events[0]["command"] is None


def test_idle_completion_starts_an_unowned_turn_that_names_its_cause(tracker):
    """The `dequeue` case: the same completion, with no turn to fold into."""
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    tracker.on_entry(marker())

    tracker.on_entry(queue_operation("enqueue", notification("bg1", "toolu_1")))
    tracker.on_entry(queue_operation("dequeue"))
    tracker.on_entry(user_text(notification("bg1", "toolu_1")))
    events = tracker.on_entry(assistant_text())
    assert types_of(events) == ["turn_start", "text"]
    assert events[0]["turn"]["kind"] == KIND_UNOWNED
    assert events[0]["caused_by"] == "bg1"

    tracker.on_entry(marker())
    assert tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_a_foreground_command_promoted_on_timeout_is_a_task_too(tracker):
    running_user_turn(tracker)
    tracker.on_entry(assistant_tool_use("toolu_1", "find sources", background=False))
    events = tracker.on_entry(
        tool_result(
            "toolu_1",
            "Command did not complete within its 120s timeout and was moved to "
            "the background (ID: bg9). Output is being written to: /tmp/x.output.",
        )
    )
    assert types_of(events) == ["task_start", "tool_result"]
    assert tracker.outstanding()["background"] == 1


@pytest.mark.parametrize("status", ["failed", "killed", "stopped"])
def test_a_task_that_does_not_complete_still_ends(tracker, status):
    """The counter must drain on every terminal status, not just success.

    47 of the 309 background tasks in the surveyed corpus never report
    `completed` — they are killed, they fail, or they are stopped. Absorbing
    only `completed` left every one of them holding `outstanding.background`
    above zero for the life of the process, which is the stranded counter this
    protocol exists to make impossible.
    """
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    events = tracker.on_entry(
        user_text(notification("bg1", "toolu_1", status=status))
    )
    assert types_of(events) == ["task_end"]
    assert events[0]["status"] == status
    assert tracker.outstanding()["background"] == 0


def test_task_stop_ends_the_task_with_no_notification(tracker):
    """`TaskStop` resolves in-band; the count must fall without a notification.

    The model can stop its own background task and gets the confirmation back
    synchronously, in `TaskStop`'s own tool_result — no `<task-notification>`
    ever follows a stop. Unrecognised, this is a fifth way for
    `outstanding.background` to strand above zero for the life of the
    process, the same failure class Session 13 fixed for the four
    notification statuses.
    """
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    assert tracker.outstanding()["background"] == 1

    events = tracker.on_entry(task_stop_result("toolu_2", "bg1"))
    assert types_of(events) == ["task_end", "tool_result"]
    assert events[0]["task"] == {"id": "bg1", "kind": "shell"}
    assert events[0]["status"] == "stopped"
    assert tracker.outstanding()["background"] == 0


def test_a_failed_task_stop_ends_nothing(tracker):
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    events = tracker.on_entry(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "content": "Task bg1 not found",
                        "is_error": True,
                    }
                ]
            },
        }
    )
    assert types_of(events) == ["tool_result"]
    assert tracker.outstanding()["background"] == 1


def test_task_stop_for_an_already_ended_task_is_a_no_op(tracker):
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    tracker.on_entry(user_text(notification("bg1", "toolu_1")))
    assert tracker.outstanding()["background"] == 0

    events = tracker.on_entry(task_stop_result("toolu_2", "bg1"))
    assert types_of(events) == ["tool_result"]


def test_a_monitor_is_a_background_task(tracker):
    """A monitor is background work, announced in wording of its own.

    It gets an id from the same namespace as a shell command and ends through
    the same notification, so the only thing separating the two is what the
    launching tool_result says.
    """
    running_user_turn(tracker)
    tracker.on_entry(assistant_tool_use("toolu_1", "watch CI", background=False))
    events = tracker.on_entry(
        tool_result(
            "toolu_1",
            "Monitor started (task bwmkvgwju, timeout 3000000ms). You will be "
            "notified on each event.",
        )
    )
    assert types_of(events) == ["task_start", "tool_result"]
    assert events[0]["task"] == {"id": "bwmkvgwju", "kind": "monitor"}
    assert tracker.outstanding()["monitor"] == 1
    assert tracker.outstanding()["background"] == 0


def test_a_monitor_event_is_not_the_monitor_ending(tracker):
    """Events fire throughout a monitor's life; only a status ends it."""
    running_user_turn(tracker)
    tracker.on_entry(assistant_tool_use("toolu_1", "watch CI", background=False))
    tracker.on_entry(tool_result("toolu_1", "Monitor started (task bmon, timeout 60000ms)."))

    events = tracker.on_entry(user_text(monitor_event("bmon", "toolu_1")))
    assert types_of(events) == []
    assert tracker.outstanding()["monitor"] == 1

    events = tracker.on_entry(user_text(notification("bmon", "toolu_1", status="stopped")))
    assert types_of(events) == ["task_end"]
    assert tracker.outstanding()["monitor"] == 0


def test_an_async_subagent_is_a_background_task(tracker):
    """A subagent launched to run on its own is outstanding work too."""
    running_user_turn(tracker)
    tracker.on_entry(assistant_tool_use("toolu_1", "survey the corpus", background=False))
    events = tracker.on_entry(
        tool_result(
            "toolu_1",
            "Async agent launched successfully. (This tool result is internal "
            "metadata.)\nagentId: a7195bc33b72b2374 (internal ID)",
        )
    )
    assert types_of(events) == ["task_start", "tool_result"]
    assert events[0]["task"] == {"id": "a7195bc33b72b2374", "kind": "agent"}
    assert tracker.outstanding()["agent"] == 1
    assert tracker.outstanding()["background"] == 0


def test_an_event_for_an_unseen_monitor_still_announces_it(tracker):
    """So that a `caused_by` never names a task the client was not told about.

    A launch wording the tracker does not know leaves the task unregistered.
    The first notification of any kind — event or terminal — is the backstop.
    """
    running_user_turn(tracker)
    events = tracker.on_entry(user_text(monitor_event("bunseen", "toolu_9")))
    assert types_of(events) == ["task_start"]
    assert events[0]["task"] == {"id": "bunseen", "kind": "unknown"}
    assert tracker.outstanding()["background"] == 1


def test_a_completion_seen_twice_decrements_once(tracker):
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    first = tracker.on_entry(queue_operation("enqueue", notification("bg1", "toolu_1")))
    second = tracker.on_entry(user_text(notification("bg1", "toolu_1")))
    assert types_of(first) == ["task_end"]
    assert types_of(second) == []
    assert tracker.outstanding()["background"] == 0


def test_a_completion_with_no_launch_seen_still_balances(tracker):
    running_user_turn(tracker)
    events = tracker.on_entry(queue_operation("enqueue", notification("bg2", "toolu_9")))
    assert types_of(events) == ["task_start", "task_end"]
    assert tracker.outstanding()["background"] == 0


def test_a_failed_task_reports_its_exit_code(tracker):
    running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    events = tracker.on_entry(
        queue_operation("enqueue", notification("bg1", "toolu_1", exit_code=2))
    )
    assert events[0]["exit_code"] == 2


# ----------------------------------------------------------------------
# The error path
# ----------------------------------------------------------------------


def test_a_dead_inner_claude_closes_the_queue_behind_it(tracker):
    running = running_user_turn(tracker, "first")
    queued, _ = tracker.submit("second")
    events = tracker.fail_session("inner claude exited")
    assert types_of(events) == ["error", "turn_end", "turn_end"]
    assert {event["turn"]["id"] for event in events[1:]} == {running.id, queued.id}
    assert all(event["outcome"] == "error" for event in events[1:])
    assert tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


# ----------------------------------------------------------------------
# Derived state
# ----------------------------------------------------------------------


def test_ended_turns_stay_in_the_table_without_being_counted(tracker):
    running_user_turn(tracker)
    tracker.on_entry(marker())
    assert len(tracker.turns) == 1
    assert tracker.outstanding()["user"] == 0


def test_snapshot_lists_only_unfinished_work(tracker):
    turn = running_user_turn(tracker)
    launch_background(tracker, "bg1", "toolu_1")
    queued, _ = tracker.submit("later")
    snapshot = tracker.snapshot()
    assert [t["id"] for t in snapshot["turns"]] == [turn.id, queued.id]
    assert [t["state"] for t in snapshot["turns"]] == [STATE_RUNNING, STATE_QUEUED]
    assert [t["id"] for t in snapshot["tasks"]] == ["bg1"]
