"""The wiring between the client's stdio protocol and the inner `claude`.

Everything that decides *what* the proxy says lives here; everything that
touches a process — spawning the pty, reading its bytes, writing stdout — is
in `cli.py`. That split is what lets Phase 2's behaviour be tested without a
live `claude`: the tests drive a `Proxy` with a stub transcript source and
hand-built frames, and read the same messages a client would.

The io loop calls into this in five places — a stdin line arrives, a prompt is
about to be typed, a frame settles, a tick passes, stdin closes — and asks it
two questions: may a prompt be sent, and may the loop stop.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from agentproxy import log
from agentproxy.fallback import FallbackEot
from agentproxy.liveness import Liveness
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


def _tagged(events: list[dict], ref: object) -> list[dict]:
    """Stamp a command's `ref` on the messages answering it.

    Echoed verbatim, whatever the client sent, and omitted entirely when it
    sent nothing — the field is optional, so a client that does not correlate
    sees exactly the stream it saw before.
    """
    if ref is None:
        return events
    for event in events:
        event["ref"] = ref
    return events


class Proxy:
    """One inner-`claude` session, seen from the client's side of the pipe."""

    def __init__(
        self,
        tracker: TurnTracker,
        emitter: Emitter,
        queue: SubmitQueue,
        liveness: Liveness,
        fallback: FallbackEot,
        read_entries: Callable[[], list[dict]],
        activity: dict,
    ) -> None:
        self._tracker = tracker
        self._emitter = emitter
        self._queue = queue
        self._liveness = liveness
        self._fallback = fallback
        self._read_entries = read_entries
        # Shared with the io loop, which stamps `last_pty` on every read and
        # reads `last_activity` for its own idle ceiling. Both clocks are
        # monotonic seconds.
        self._activity = activity
        self._stdin_closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def announce_ready(self, session_id: str) -> None:
        """Tell the client the session is up, and which one it is.

        Always the first message on the stream, and its `session_id` is always
        a real id — it is what a client records to resume this conversation in
        a later process.
        """
        self._emit([{"type": "ready", "session_id": session_id}])

    def fail(self, reason: str) -> None:
        """Close out a session that cannot continue.

        Ends the running turn, everything queued behind it, and every
        background task, so nothing is left for a client to wait on. Stops
        the proxy typing into an inner `claude` that is gone.
        """
        log.log_text("proxy", f"session failed: {reason}")
        self._queue.drain()
        self._emit(self._tracker.fail_session(reason))

    @property
    def has_unfinished_work(self) -> bool:
        return any(self._tracker.outstanding().values())

    # ------------------------------------------------------------------
    # Client side
    # ------------------------------------------------------------------

    def on_stdin_line(self, line: str) -> Optional[dict]:
        """Handle one client command; return the prompt to type, if any.

        `submit` is acknowledged here, before anything reaches the inner
        `claude` — the ack is the client's handle on the turn and it must not
        wait on the pty. `status` is answered here too and
        never reaches the keyboard; so is anything the proxy cannot parse.

        A command may carry a `ref` of the client's choosing, which comes back
        on the message answering it. Without one the only correspondence
        between a command and its answer is positional — the same ordinal
        matching this protocol rules out for end-of-turn, and it fails the same
        way here:
        a rejected `submit` produces an `error` rather than an `ack`, and a
        client counting acks then binds every later prompt to the wrong turn.
        """
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            return self._reject("stdin line is not JSON")
        if not isinstance(command, dict):
            return self._reject("stdin line is not a JSON object")

        kind = command.get("type")
        ref = command.get("ref")
        if kind == "submit":
            text = command.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._reject("submit needs a non-empty 'text'", ref)
            turn, events = self._tracker.submit(text)
            self._emit(_tagged(events, ref))
            return self._queue.accept(turn)
        if kind == "status":
            self._emit(
                _tagged([{"type": "state", **self._tracker.snapshot()}], ref)
            )
            return None
        return self._reject(f"unknown command type: {kind!r}", ref)

    def on_stdin_eof(self) -> None:
        """The client closed its end; finish what is in flight and stop."""
        self._stdin_closed = True

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def is_ready_to_send(self) -> bool:
        """True when a queued prompt may be typed into the TUI right now.

        Four conditions, and the last is the one that is easy to miss: a
        prompt already typed but not yet seen in the transcript still owns the
        keyboard. Typing past it would leave two prompts in flight with one
        binding decision to tell them apart, which is how a turn ends up
        matched to the wrong id.
        """
        return (
            self._liveness.is_idle
            and self._liveness.has_settled
            and self._tracker.running_turn is None
            and self._tracker.typed_turn is None
        )

    def on_typed(self, message: dict) -> None:
        """The keystrokes for a prompt are going out now."""
        turn = self._queue.take(message)
        if turn is not None:
            self._tracker.mark_typed(turn)

    def submit_landed(self) -> bool:
        """True once the typed prompt has been recognised in the transcript.

        This is what the io loop's submit confirmation waits on, and the
        transcript is a better witness than the screen: the inner `claude`
        writing the prompt as an entry *is* the submit landing, where a
        cleared input box is an inference about a repaint. Until then the loop
        re-sends the submit CR, which a busy inner never sees (its quiescence
        gate holds) and an idle one accepts.
        """
        return self._tracker.typed_turn is None

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def on_commit(self, screen) -> None:
        """Read a settled frame for liveness, and feed the fallback clock."""
        self._liveness.on_commit(screen)
        self._fallback.note_frame(self._liveness.is_idle)

    def on_iteration(self) -> None:
        """One loop tick: drain the transcript, then check the fallback tier."""
        for entry in self._read_entries():
            self._fallback.note_transcript()
            self._activity["last_activity"] = time.monotonic()
            self._emit(self._tracker.on_entry(entry))

        if self._tracker.running_turn is None:
            return
        # Absent `last_pty` means the io loop has not read yet, which is the
        # opposite of silence — default to now rather than to the epoch, so a
        # missing clock can never look like a minute of quiet.
        last_pty = self._activity.get("last_pty") or time.monotonic()
        basis = self._fallback.basis(last_pty)
        if basis is not None:
            log.log_text(
                "proxy", f"{self._tracker.running_turn.id} ended on {basis}"
            )
            self._emit(self._tracker.end_running_turn(basis))

    def should_stop(self) -> bool:
        """True when the client has gone and nothing is owed to it.

        A background task still running does not hold the session open: the
        client that would hear about it has already closed its end.
        """
        return (
            self._stdin_closed
            and self._queue.is_empty
            and self._tracker.running_turn is None
            and self._tracker.typed_turn is None
        )

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    def _emit(self, events: list[dict]) -> None:
        """Write events out, restarting the fallback clock on each turn start.

        A turn's clock has to start when the turn does, not when the last one
        ended — otherwise a turn opening after a quiet stretch inherits it and
        the fallback fires on the tick it began.
        """
        for event in events:
            if event.get("type") == "turn_start":
                self._fallback.note_turn_start()
            self._emitter.emit(event)

    def _reject(self, reason: str, ref: object = None) -> Optional[dict]:
        """Report a command the proxy will not act on, and type nothing.

        `ref` is the rejected command's own, when it had one — a rejection is
        an answer like any other, and a client that cannot attribute it is
        left waiting for an `ack` that will never come.
        """
        log.log_text("proxy", f"rejected: {reason}")
        self._emit(_tagged([{"type": "error", "error": reason}], ref))
        return None
