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
from agentproxy.fallback import BASIS_UNMATCHED, FallbackEot
from agentproxy.liveness import Liveness
from agentproxy.orphans import OrphanExit, OrphanReaper
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


# `task_end.output` is a preview, not a log dump — the wire carries the last
# this many characters of the task's output file, which is where a build or
# test log's own verdict actually lands.
TASK_OUTPUT_MAX_CHARS = 256


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


def _orphan_end(ending: OrphanExit) -> dict:
    return {
        "type": "orphan_end",
        "process": ending.process.to_wire(),
        "status": ending.status,
        "exit_code": ending.exit_code,
        "signal": ending.signal,
    }


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
        read_output: Callable[[str], Optional[str]],
        activity: dict,
        reaper: Optional[OrphanReaper] = None,
        kill_orphans: bool = False,
    ) -> None:
        self._tracker = tracker
        self._emitter = emitter
        self._queue = queue
        self._liveness = liveness
        self._fallback = fallback
        self._read_entries = read_entries
        self._read_output = read_output
        # Shared with the io loop, which stamps `last_pty` on every read and
        # reads `last_activity` for its own idle ceiling. Both clocks are
        # monotonic seconds.
        self._activity = activity
        # None where the proxy could not become a subreaper: then no orphan is
        # ever seen, and no `orphan` key is ever on the wire.
        self._reaper = reaper
        self._kill_orphans = kill_orphans
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
        the proxy typing into an inner `claude` that is gone. Adopted orphans
        are closed out last (abandoned, or killed when the proxy was told to),
        so their count is zero on the final message like every other.
        """
        log.log_text("proxy", f"session failed: {reason}")
        self._queue.drain()
        self._emit(self._tracker.fail_session(reason))
        if self._reaper is not None:
            self._emit(
                [_orphan_end(e) for e in self._reaper.shutdown(kill=self._kill_orphans)]
            )

    @property
    def has_unfinished_work(self) -> bool:
        return any(self._emitter.outstanding().values())

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
            state = {"type": "state", **self._tracker.snapshot()}
            if self._reaper is not None:
                state["orphans"] = [o.to_wire() for o in self._reaper.alive()]
            self._emit(_tagged([state], ref))
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
        self._fallback.note_typed()
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
        """One loop tick: drain the transcript, look for orphans, check the fallback tier."""
        for entry in self._read_entries():
            self._fallback.note_transcript()
            self._activity["last_activity"] = time.monotonic()
            self._emit(self._tracker.on_entry(entry))
        self._poll_orphans()

        running = self._tracker.running_turn
        typed = self._tracker.typed_turn
        if typed is None:
            self._fallback.note_typed_cleared()
        if running is None and typed is None:
            return
        # Absent `last_pty` means the io loop has not read yet, which is the
        # opposite of silence — default to now rather than to the epoch, so a
        # missing clock can never look like a minute of quiet.
        last_pty = self._activity.get("last_pty") or time.monotonic()
        basis = self._fallback.basis(last_pty)
        if basis is None:
            return
        if running is not None:
            log.log_text("proxy", f"{running.id} ended on {basis}")
            self._emit(self._tracker.end_running_turn(basis))
            return
        # Ending an unrecognised typed prompt here is what stops one altered
        # prompt wedging the session.
        #
        # Left open, the turn holds `outstanding.user` above zero for the life
        # of the process, and `is_ready_to_send` refuses to type anything
        # behind it. What separates a lost prompt from one merely slow to be
        # picked up is the prompt's own age, not which clock fired: both look
        # like a quiet transcript with nothing running, seconds after typing.
        if not self._fallback.typed_is_lost():
            return
        log.log_text(
            "proxy",
            f"{typed.id} was typed but never seen in the transcript; "
            f"ended on {BASIS_UNMATCHED}",
        )
        self._emit(self._tracker.end_typed_turn(BASIS_UNMATCHED))

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

    def _poll_orphans(self) -> None:
        """Report every process adopted or reaped since the last tick.

        A count alone would not do: with the last orphan gone and no message
        behind it, a client gating its composer on `any(outstanding.values())`
        would stay gated until something unrelated was emitted. So an arrival
        and an ending are each a message, the way a task's are.
        """
        if self._reaper is None:
            return
        arrived, ended = self._reaper.poll()
        self._emit(
            [{"type": "orphan_start", "process": o.to_wire()} for o in arrived]
            + [_orphan_end(e) for e in ended]
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
            if event.get("type") == "task_end":
                self._resolve_output(event)
            self._emitter.emit(event)

    def _resolve_output(self, event: dict) -> None:
        """Replace a `task_end`'s `output_file` path with actual `output` text.

        The one place this read happens — `TurnTracker` does no I/O by design,
        so a task's output file is only a path until it reaches here.
        """
        path = event.pop("output_file", None)
        content = self._read_output(path) if path else None
        event["output"] = content[-TASK_OUTPUT_MAX_CHARS:] if content else None

    def _reject(self, reason: str, ref: object = None) -> Optional[dict]:
        """Report a command the proxy will not act on, and type nothing.

        `ref` is the rejected command's own, when it had one — a rejection is
        an answer like any other, and a client that cannot attribute it is
        left waiting for an `ack` that will never come.
        """
        log.log_text("proxy", f"rejected: {reason}")
        self._emit(_tagged([{"type": "error", "error": reason}], ref))
        return None
