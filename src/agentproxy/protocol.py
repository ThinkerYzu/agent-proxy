"""The message envelope and the writer that puts it on stdout.

Every message the proxy emits carries `seq`, `type`, every `outstanding`
count, and (when it belongs to a turn) that turn's `id` and `kind`. The
counts come from `TurnTracker.outstanding()` at the moment of writing rather
than from the caller, so nothing can emit a count that has gone stale. A
count that is not the tracker's (`orphan`, read from the process tree) is
merged in the same way, through `extra_counts`.

Field order is fixed so a human reading the stream, or a diff of two captured
runs, sees the envelope before the payload.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Callable, Iterable, Optional

from agentproxy import log
from agentproxy.turn_tracker import TurnTracker


_ENVELOPE_KEYS = ("seq", "type", "turn", "outstanding")


class Emitter:
    """Numbers, stamps and writes protocol messages for one session.

    `seq` counts from 1 across everything this emitter writes, so a client
    that sees a gap knows it dropped a line.
    """

    def __init__(
        self,
        tracker: TurnTracker,
        out: Optional[IO[str]] = None,
        extra_counts: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._tracker = tracker
        self._out = out if out is not None else sys.stdout
        self._extra_counts = extra_counts
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def outstanding(self) -> dict:
        """Every count, right now: the tracker's, plus any source outside it.

        The one place the counts are assembled, so what `has_unfinished_work`
        tests and what a message carries can never disagree. `extra_counts`
        is None when there is no such source, and its keys are then simply
        absent from the wire, never a `0` claiming knowledge the proxy does
        not have.
        """
        counts = self._tracker.outstanding()
        if self._extra_counts is not None:
            counts.update(self._extra_counts())
        return counts

    def emit(self, event: dict) -> dict:
        """Write one event as a complete message; return what was written.

        `event` supplies `type`, an optional `turn`, and that type's own
        fields, as `TurnTracker` builds them. The returned dict is the
        message itself, which is what tests assert against.
        """
        self._seq += 1
        message = {"seq": self._seq, "type": event["type"]}
        if event.get("turn") is not None:
            message["turn"] = event["turn"]
        message["outstanding"] = self.outstanding()
        for key, value in event.items():
            if key not in _ENVELOPE_KEYS:
                message[key] = value
        line = json.dumps(message, ensure_ascii=False)
        log.log_text("stdout.out", line)
        self._out.write(line + "\n")
        self._out.flush()
        return message

    def emit_all(self, events: Iterable[dict]) -> list[dict]:
        return [self.emit(event) for event in events]
