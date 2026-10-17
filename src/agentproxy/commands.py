"""The client's stdin commands, parsed the same way for every inner agent.

A client cannot tell which agent is behind the proxy from how its commands
are answered: `submit`, `status` and `cancel` are accepted, and a malformed
line is rejected, with the same words whichever backend acts on them. So the
parsing lives here, apart from what each backend does with a command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Union


SUBMIT = "submit"
STATUS = "status"
CANCEL = "cancel"


@dataclass(frozen=True)
class Command:
    """One well-formed client command.

    `text` is set for a `submit` and `turn` for a `cancel`; both are already
    checked to be usable. `ref` is the client's own token, whatever it sent,
    or None when it sent none.
    """

    kind: str
    ref: object = None
    text: Optional[str] = None
    turn: Optional[str] = None


@dataclass(frozen=True)
class Rejection:
    """A line the proxy will not act on: the `error` text, and the `ref` to echo."""

    reason: str
    ref: object = None


def parse_command(line: str) -> Union[Command, Rejection]:
    """Turn one stdin line into a command, or into the reason it is refused.

    A refusal still carries the line's `ref` when the line got far enough to
    have one, since a client that cannot attribute an `error` waits forever
    for the `ack` it will never get.
    """
    try:
        command = json.loads(line)
    except json.JSONDecodeError:
        return Rejection("stdin line is not JSON")
    if not isinstance(command, dict):
        return Rejection("stdin line is not a JSON object")

    kind = command.get("type")
    ref = command.get("ref")
    if kind == SUBMIT:
        text = command.get("text")
        if not isinstance(text, str) or not text.strip():
            return Rejection("submit needs a non-empty 'text'", ref)
        return Command(SUBMIT, ref, text=text)
    if kind == STATUS:
        return Command(STATUS, ref)
    if kind == CANCEL:
        turn_id = command.get("turn")
        if not isinstance(turn_id, str) or not turn_id:
            return Rejection("cancel needs a 'turn' id", ref)
        return Command(CANCEL, ref, turn=turn_id)
    return Rejection(f"unknown command type: {kind!r}", ref)


def tagged(events: list[dict], ref: object) -> list[dict]:
    """Stamp a command's `ref` on the messages answering it.

    Echoed verbatim, whatever the client sent, and omitted entirely when it
    sent nothing. The field is optional, so a client that does not correlate
    sees exactly the stream it saw before.
    """
    if ref is None:
        return events
    for event in events:
        event["ref"] = ref
    return events
