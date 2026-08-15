"""Properties every emitted stream must have, whatever the session did.

Each function here reads a whole captured stream and raises on the first
violation. They are separated from the tests that call them because the same
properties are asserted twice over: once against a short session built to
exercise one requirement, and once against the 30-turn soak of [success
criterion 3](../../../../proj_docs/agent-proxy/SPEC.md#success-criteria), where
what matters is that nothing drifts over a long run.

A note on `turn`: the field is absent from messages belonging to no turn —
`ready`, `error`, `state`, and the two task messages — and may also be absent
from a `tool_result` the transcript ran ahead of. Every check below therefore
reads it with `.get`, and none of them treats an absent `turn` as a violation.
"""

from __future__ import annotations

from collections import Counter


TURN_KINDS = {"user", "unowned"}


def envelope(messages: list[dict]) -> None:
    """`seq`, `type` and all three counts, on every line, numbered from 1.

    Requirements 1 and 5. The counts are what a client gates its composer on,
    so a message that omitted them, or carried a negative one, would put the
    UI in a state no later message could correct.
    """
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    for message in messages:
        assert isinstance(message.get("type"), str), message
        counts = message["outstanding"]
        assert set(counts) == {"user", "unowned", "background"}, message
        assert all(isinstance(value, int) and value >= 0 for value in counts.values())


def turn_identity(messages: list[dict]) -> None:
    """Every turn id is announced before it is used, and ends exactly once.

    Requirements 4 and 6. A user turn is announced by its `ack` and an unowned
    turn by its `turn_start`; a client that sees an id it was never given has
    no way to decide whether the turn is its own.
    """
    kinds: dict[str, str] = {}
    ended: Counter = Counter()
    for message in messages:
        turn = message.get("turn")
        if turn is None:
            continue
        turn_id, kind = turn["id"], turn["kind"]
        assert kind in TURN_KINDS, message
        if turn_id in kinds:
            assert kinds[turn_id] == kind, f"{turn_id} changed kind: {message}"
        else:
            announced = message["type"] == "ack" or (
                message["type"] == "turn_start" and kind == "unowned"
            )
            assert announced, f"{turn_id} used before it was announced: {message}"
            kinds[turn_id] = kind
        if message["type"] == "turn_end":
            ended[turn_id] += 1
    assert all(count == 1 for count in ended.values()), ended


def end_of_turn_is_typed(messages: list[dict]) -> None:
    """Every `turn_end` says which turn, what kind, on what evidence.

    Requirement 4. `basis` is also the design's own instrumentation — a
    `fallback:` rate above roughly zero means `claude` grew a turn shape worth
    teaching to `MARKER_BASIS` — so an end that did not name one would hide
    the blind spot it was reporting.
    """
    for message in messages:
        if message["type"] != "turn_end":
            continue
        basis = message["basis"]
        assert message["turn"]["kind"] in TURN_KINDS, message
        assert basis == "error" or basis.startswith(("marker:", "fallback:")), message
        assert message["outcome"] in {"success", "error"}, message
        assert isinstance(message["usage"], dict), message


def one_turn_at_a_time(messages: list[dict]) -> None:
    """No turn starts while another is still running (SPEC R4).

    This is what makes attribution unambiguous: a `text` or `tool_use` message
    can belong to only one turn, so the id on it is a fact rather than a
    guess. Note it says nothing about *queued* turns, of which there may be
    any number — a turn is running from its `turn_start`, not from its `ack`.
    """
    running = None
    for message in messages:
        if message["type"] == "turn_start":
            assert running is None, f"{message['turn']['id']} started under {running}"
            running = message["turn"]["id"]
        elif message["type"] == "turn_end" and message["turn"]["id"] == running:
            running = None


def every_turn_ends(messages: list[dict]) -> None:
    """Nothing the proxy announced is still outstanding at the end of the stream.

    Requirement 11, read over a finished session: every turn that was acked or
    started has a `turn_end`, and every task that started has a `task_end`. A
    late end is a recoverable client bug; a missing one hangs the UI forever,
    which is the failure this project exists to remove.
    """
    opened, ended = set(), set()
    tasks_opened, tasks_ended = set(), set()
    for message in messages:
        kind = message["type"]
        if kind in {"ack", "turn_start"}:
            opened.add(message["turn"]["id"])
        elif kind == "turn_end":
            ended.add(message["turn"]["id"])
        elif kind == "task_start":
            tasks_opened.add(message["task"]["id"])
        elif kind == "task_end":
            tasks_ended.add(message["task"]["id"])
    assert opened - ended == set(), f"turns left hanging: {sorted(opened - ended)}"
    assert tasks_opened - tasks_ended == set(), sorted(tasks_opened - tasks_ended)
    assert messages[-1]["outstanding"] == {"user": 0, "unowned": 0, "background": 0}


def all_of_them(messages: list[dict]) -> None:
    """Every invariant above, for a test that wants the lot."""
    envelope(messages)
    turn_identity(messages)
    end_of_turn_is_typed(messages)
    one_turn_at_a_time(messages)
    every_turn_ends(messages)
