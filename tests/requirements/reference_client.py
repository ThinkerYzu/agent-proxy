"""A client with no inference layer, used to check the protocol keeps its word.

This is the client [API.md](../../API.md) describes, written out: it claims a
turn id from the `ref` it sent, resolves each wait on `turn.id`, and gates its
composer on `outstanding`. There is no ordinal, no latch and no reconciliation
timer in it — those are the three mechanisms this protocol exists to delete
from a real front-end, and a client that needs none of them is what makes
deleting them possible.

What makes it a test instrument rather than a sample is that it derives the
the counts a second time, from the events alone, and every test that drives
it compares that derivation against the counts the proxy stamped on the same
messages. The id path and the counter path are independent routes to the same
answer, so a disagreement between them is a desync caught at the message that
caused it.

**Compare the two at a tick boundary, not mid-batch.** All the messages one
transcript entry produces carry that entry's *final* counts, so inside a batch
the counts legitimately run ahead of the events a client has read — see
`test_functional.py::test_requirement_5_a_batch_carries_the_counts_it_ends_with`.
"""

from __future__ import annotations

import collections

from typing import Optional


EMPTY = {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


class ReferenceClient:
    """One client's whole view of a session, rebuilt from the message stream."""

    def __init__(self) -> None:
        self.expected_seq = 1
        self.turn_of: dict[object, str] = {}
        self.rejected: set = set()
        self.open_user: set[str] = set()
        self.open_unowned: set[str] = set()
        self.open_tasks: dict[str, str] = {}   # task id -> kind
        self.ended: dict[str, dict] = {}
        self.started: set[str] = set()
        # Only a `state` reply says which turns are queued and which are
        # running; the event stream says a turn is running by starting it.
        self.queued: set[str] = set()
        self.running: set[str] = set()
        self.counts: dict = dict(EMPTY)
        self.busy = False
        self.errors: list[str] = []

    # ------------------------------------------------------------------
    # Reading the stream
    # ------------------------------------------------------------------

    def consume(self, message: dict) -> None:
        """Apply one message, the way a front-end would on reading the line.

        Raises rather than returns on a gap in `seq`: a client that dropped a
        line has no way to notice otherwise, which is the whole reason the
        envelope numbers them.
        """
        assert message["seq"] == self.expected_seq, (
            f"seq gap: expected {self.expected_seq}, got {message['seq']}"
        )
        self.expected_seq += 1

        kind = message["type"]
        turn = message.get("turn") or {}
        if kind == "ack":
            self.open_user.add(turn["id"])
            if "ref" in message:
                self.turn_of[message["ref"]] = turn["id"]
        elif kind == "turn_start":
            self.started.add(turn["id"])
            if turn["kind"] == "unowned":
                self.open_unowned.add(turn["id"])
        elif kind == "turn_end":
            self.open_user.discard(turn["id"])
            self.open_unowned.discard(turn["id"])
            self.ended[turn["id"]] = message
        elif kind == "task_start":
            self.open_tasks[message["task"]["id"]] = message["task"]["kind"]
        elif kind == "task_end":
            self.open_tasks.pop(message["task"]["id"], None)
        elif kind == "state":
            self.adopt(message)
        elif kind == "error":
            self.errors.append(message["error"])
            if "ref" in message:
                self.rejected.add(message["ref"])

        self.counts = message["outstanding"]
        self.busy = any(message["outstanding"].values())

    def adopt(self, state: dict) -> None:
        """Rebuild the whole view from a `state` reply, discarding what was here.

        This is the reconnect path: the client has missed an unknown number of
        messages, so nothing it remembers about what is outstanding can be
        trusted, and the reply is authoritative on its own. The `ref` map
        survives — the ids in it were issued to this client and are still its
        own — which is what lets a resumed wait resolve.

        Every user turn in the reply is this client's, since a proxy serves
        exactly one client.
        """
        self.open_user = {t["id"] for t in state["turns"] if t["kind"] == "user"}
        self.open_unowned = {t["id"] for t in state["turns"] if t["kind"] == "unowned"}
        self.open_tasks = {task["id"]: task["kind"] for task in state["tasks"]}
        self.queued = {t["id"] for t in state["turns"] if t["state"] == "queued"}
        self.running = {t["id"] for t in state["turns"] if t["state"] == "running"}

    def resync_to(self, message: dict) -> None:
        """Resume reading a stream this client has not been following.

        A client that stopped reading and came back has no idea how many lines
        went past, so it re-bases its `seq` expectation on the first message it
        does see. Nothing else it knows comes from those messages.
        """
        self.expected_seq = message["seq"]

    # ------------------------------------------------------------------
    # What the client believes
    # ------------------------------------------------------------------

    @property
    def derived(self) -> dict:
        """The counts worked out from the events, never from the counts.

        Background work is split the way the proxy splits it — by `kind`, into
        disjoint buckets — so this can be compared with `outstanding` key for
        key. A kind this client has never heard of counts under `background`,
        which is what keeps the comparison honest when the proxy learns a new
        one before the client does.
        """
        kinds = collections.Counter(self.open_tasks.values())
        return {
            "user": len(self.open_user),
            "unowned": len(self.open_unowned),
            "background": len(self.open_tasks)
            - kinds["monitor"]
            - kinds["agent"],
            "monitor": kinds["monitor"],
            "agent": kinds["agent"],
        }

    def turn_for(self, ref: object) -> Optional[str]:
        """The turn id the `ack` for this command carried, if it was accepted."""
        return self.turn_of.get(ref)

    def is_finished(self, ref: object) -> bool:
        """Has *my* prompt's turn ended — not somebody's, mine."""
        turn_id = self.turn_of.get(ref)
        return turn_id is not None and turn_id in self.ended
