"""Accepted prompts, waiting their turn at the keyboard.

A client may submit whenever it likes; the inner `claude` accepts a prompt
only when it is idle and nothing is running, and it runs one turn at a time.
This queue is what sits between the two — a plain FIFO, because the protocol
has no cancellation: a prompt leaves it by being typed, or by the session
failing.

The queue holds turns; the io loop holds the pty payloads that `accept`
returns. They stay in step because each accepted prompt produces exactly one
payload and each send consumes exactly one, in order — and `take` checks that
the turn it pops is the one whose text was just typed, so a desync becomes a
log line rather than a prompt bound to the wrong id.
"""

from __future__ import annotations

from typing import Optional

from agentproxy import log
from agentproxy.turn_tracker import Turn


class SubmitQueue:
    """The prompts the proxy has acknowledged but not yet typed."""

    def __init__(self) -> None:
        self._waiting: list[Turn] = []

    def __len__(self) -> int:
        return len(self._waiting)

    @property
    def is_empty(self) -> bool:
        return not self._waiting

    def accept(self, turn: Turn) -> dict:
        """Queue an acknowledged turn; return the message the io loop types.

        The payload is the shape `input_encoder` expects, which is the
        stream-json user message claudejson's pty path already spoke — the
        proxy's own protocol stops at the pty boundary.
        """
        self._waiting.append(turn)
        return {
            "type": "user",
            "message": {"role": "user", "content": turn.text or ""},
        }

    def take(self, message: Optional[dict] = None) -> Optional[Turn]:
        """Pop the turn whose prompt is being typed right now.

        Called as the keystrokes go out, so the caller can tell `TurnTracker`
        which prompt is now awaiting its transcript entry. `message` is the
        payload the io loop is sending; when given, it is checked against the
        popped turn, since binding the wrong id would be silent otherwise.
        """
        if not self._waiting:
            log.log_text("queue", "a prompt was typed with the queue empty")
            return None
        turn = self._waiting.pop(0)
        if message is not None:
            typed = message.get("message", {}).get("content")
            if typed != turn.text:
                log.log_text(
                    "queue", f"typed text does not match {turn.id}: {typed!r}"
                )
        return turn

    def drain(self) -> list[Turn]:
        """Empty the queue and return what was in it, for the failure path.

        `TurnTracker.fail_session` is what ends those turns; this only stops
        the proxy from typing prompts into an inner `claude` that is gone.
        """
        waiting, self._waiting = self._waiting, []
        return waiting
