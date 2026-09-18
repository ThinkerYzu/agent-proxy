"""Accepted prompts, waiting their turn at the keyboard.

A client may submit whenever it likes; the inner `claude` accepts a prompt
only when it is idle and nothing is running, and it runs one turn at a time.
This queue is what sits between the two — a FIFO that a prompt leaves by
being typed, by the session failing, or by the client withdrawing it with
`cancel` before it was ever typed.

The queue holds turns; the io loop holds the pty payloads that `accept`
returns, one per accepted prompt. Each payload names its turn, so `take` finds
the turn by id rather than by position — which is what lets a withdrawn turn
leave the queue while its payload is still in the io loop's list: when that
payload's time comes, `take` answers None and the loop types nothing.
"""

from __future__ import annotations

from typing import Optional

from agentproxy import log
from agentproxy.turn_tracker import Turn


class SubmitQueue:
    """The prompts the proxy has acknowledged but not yet typed."""

    def __init__(self) -> None:
        self._waiting: list[Turn] = []
        self._withdrawn: set[str] = set()

    def __len__(self) -> int:
        return len(self._waiting)

    @property
    def is_empty(self) -> bool:
        return not self._waiting

    def accept(self, turn: Turn) -> dict:
        """Queue an acknowledged turn; return the message the io loop types.

        The payload is the shape `input_encoder` expects, which is the
        stream-json user message claudejson's pty path already spoke — the
        proxy's own protocol stops at the pty boundary. `turn` is this
        queue's own key on it, ignored by the encoder, and how `take` knows
        which turn a payload stands for.
        """
        self._waiting.append(turn)
        return {
            "type": "user",
            "message": {"role": "user", "content": turn.text or ""},
            "turn": turn.id,
        }

    def withdraw(self, turn_id: str) -> Optional[Turn]:
        """Drop a turn that is still waiting, so it is never typed.

        Returns the turn, or None if nothing waiting has that id — a turn
        already typed is past taking back this way. The payload the io loop
        still holds is answered by `take` with None when its time comes.
        """
        for index, turn in enumerate(self._waiting):
            if turn.id == turn_id:
                self._withdrawn.add(turn_id)
                return self._waiting.pop(index)
        return None

    def take(self, message: Optional[dict] = None) -> Optional[Turn]:
        """Pop the turn whose prompt is being typed right now.

        Called as the keystrokes are about to go out, so the caller can tell
        `TurnTracker` which prompt is now awaiting its transcript entry — or
        type nothing, when the answer is None. `message` is the payload the
        io loop is sending; the turn is found by the id it carries, and its
        text is checked against the turn's, since binding the wrong id would
        be silent otherwise. A payload with no id pops the head, as before.
        """
        turn_id = message.get("turn") if message is not None else None
        if turn_id in self._withdrawn:
            self._withdrawn.discard(turn_id)
            log.log_text("queue", f"{turn_id} was withdrawn; typing nothing")
            return None
        if not self._waiting:
            log.log_text("queue", "a prompt was typed with the queue empty")
            return None
        index = next(
            (i for i, t in enumerate(self._waiting) if t.id == turn_id), 0
        )
        if turn_id is not None and self._waiting[index].id != turn_id:
            log.log_text("queue", f"no waiting turn {turn_id}; popping the head")
        turn = self._waiting.pop(index)
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
