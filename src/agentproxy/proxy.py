"""The wiring between the client's stdio protocol and the inner `claude`.

Everything that decides *what* the proxy says lives here; everything that
touches a process — spawning the pty, reading its bytes, writing stdout — is
in `cli.py`. That split is what lets Phase 2's behaviour be tested without a
live `claude`: the tests drive a `Proxy` with a stub transcript source and
hand-built frames, and read the same messages a client would.

The io loop calls into this in five places — a stdin line arrives, a prompt is
about to be typed, a frame settles, a tick passes, stdin closes — and asks it
two questions: may a prompt be sent, and may the loop stop.

Cancellation is the one thing here that writes to the pty of its own accord,
through the injected `send_keys`: one `Esc` to interrupt a running turn, and
`Esc` `Esc` to clear the composer when the inner hands the cancelled prompt
back instead of writing an interruption entry.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

from agentproxy import log
from agentproxy.commands import ANSWER, CANCEL, STATUS, SUBMIT, Rejection, parse_command, tagged
from agentproxy.input_hook import denial
from agentproxy.user_input import InputRequests
from agentproxy.fallback import BASIS_UNMATCHED, FallbackEot
from agentproxy.liveness import Liveness
from agentproxy.orphans import OrphanReaper, orphan_end_event
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import (
    CANCEL_INTERRUPT,
    CANCEL_WITHDRAWN,
    CancelError,
    TurnTracker,
)


# The name `ready` gives this backend's inner agent.
AGENT_CLAUDE = "claude"

# `task_end.output` is a preview, not a log dump — the wire carries the last
# this many characters of the task's output file, which is where a build or
# test log's own verdict actually lands.
TASK_OUTPUT_MAX_CHARS = 256

ESC = b"\x1b"
# The inner's own gesture for emptying a composer that holds text: `Esc`
# shows "Esc again to clear", and a second `Esc` inside that hint's window
# clears it. Sent only while the composer is seen to hold the cancelled
# prompt — on an empty composer the same two keys open the rewind picker.
CLEAR_COMPOSER = (ESC, ESC)
# After sending the clear, wait this long before reading the composer again:
# the next tick's frame may predate the repaint, and re-sending into a
# composer that has in fact just emptied is the rewind-picker case above.
CLEAR_SETTLE_S = 0.6
# How many times to try the clear before giving up and reopening the gate,
# loudly. A gate held shut forever is a wedged session; a prompt typed on top
# of leftover text is caught downstream as an unmatched turn.
CLEAR_ATTEMPTS = 3


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
        send_keys: Optional[Callable[[Sequence[bytes]], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        inputs: Optional[InputRequests] = None,
        bridge=None,
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
        # Writes keystrokes that are not a prompt: the cancel `Esc` and the
        # composer clear. `cli.py` paces them the way it paces dialog answers.
        # None means the proxy cannot interrupt; `cancel` still withdraws.
        self._send_keys = send_keys
        self._clock = clock
        self._stdin_closed = False
        self._inputs = inputs
        self._bridge = bridge
        self._session_id = None
        self._hook_connections = {}
        self._emitter.input_requests = inputs
        # Set when a cancelled prompt was seen handed back to the composer.
        # While set the send gate stays shut and each idle frame is a chance
        # to clear it; `_clear_sent_at` throttles the retries.
        self._composer_dirty: Optional[str] = None
        self._clear_sent_at: Optional[float] = None
        self._clear_attempts = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def announce_ready(self, session_id: str) -> None:
        """Tell the client the session is up, and which one it is.

        Always the first message on the stream, and its `session_id` is always
        a real id — it is what a client records to resume this conversation in
        a later process. `agent` says which inner agent the id belongs to: a
        session id resumes only under the agent that made it.
        """
        self._session_id = session_id
        ready = {"type": "ready", "session_id": session_id, "agent": AGENT_CLAUDE}
        if self._inputs is not None:
            ready["capabilities"] = {"user_input": True}
        self._emit([ready])

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
        if self._inputs is not None:
            self._emit(self._inputs.close("error"))
        self._emit(self._tracker.fail_session(reason))
        if self._reaper is not None:
            self._emit(
                [orphan_end_event(e) for e in self._reaper.shutdown(kill=self._kill_orphans)]
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
        command = parse_command(line)
        if isinstance(command, Rejection):
            return self._reject(command.reason, command.ref)
        if command.kind == SUBMIT:
            turn, events = self._tracker.submit(command.text)
            self._emit(tagged(events, command.ref))
            return self._queue.accept(turn)
        if command.kind == STATUS:
            state = {"type": "state", **self._tracker.snapshot()}
            if self._inputs is not None:
                state["input_requests"] = self._inputs.snapshot()
            if self._reaper is not None:
                state["orphans"] = [o.to_wire() for o in self._reaper.alive()]
            self._emit(tagged([state], command.ref))
            return None
        if command.kind == ANSWER:
            return self._answer(command)
        assert command.kind == CANCEL
        return self._cancel(command.turn, command.ref)

    def _answer(self, command) -> None:
        if self._inputs is None:
            return self._reject("user input is not enabled", command.ref)
        try:
            request, native = self._inputs.accept(command.request, command.answers, command.dismiss)
        except ValueError as exc:
            return self._reject(str(exc), command.ref)
        self._emit(tagged([{"type": "ack", "turn": request.turn, "request": request.id}], command.ref))
        self._fallback.note_turn_start()
        self._activity["last_activity"] = self._clock()
        self._emit(self._inputs.send(request, native, "dismissed" if command.dismiss else "answered"))

    def _on_hook(self, connection, payload) -> None:
        tool_id = payload.get("tool_use_id")
        if (payload.get("session_id") != self._session_id
            or payload.get("tool_name") != "AskUserQuestion"
            or not isinstance(tool_id, str) or not tool_id):
            connection.respond(denial("agent-proxy cannot attribute this question to its session"))
            self._reject("question hook has no valid main-session tool identity")
            return
        key = ("claude", tool_id)
        event = payload.get("hook_event_name")
        if event in ("PostToolUse", "PostToolUseFailure"):
            self._emit(self._inputs.resolve(key, "error" if event == "PostToolUseFailure" else None))
            connection.respond({})
            return
        if event != "PreToolUse" or not isinstance(payload.get("tool_input"), dict):
            connection.respond(denial("unsupported question hook"))
            return
        if self._inputs.seen(key):
            connection.respond(denial("duplicate question hook"))
            return
        self._emit(self._tracker.confirm_hook_turn())
        turn = self._tracker.running_turn
        original = payload["tool_input"]

        def deliver(native, reason):
            self._hook_connections.pop(connection, None)
            if native is None:
                connection.respond(denial(f"agent-proxy question {reason}"))
            else:
                connection.respond({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "updatedInput": {**original, "answers": {q: ", ".join(a) for q, a in native.items()}},
                }})
        try:
            request_event = self._inputs.add(key, turn.ref(), original.get("questions"), "claude", True, deliver)
        except ValueError as exc:
            connection.respond(denial(str(exc)))
            self._reject(str(exc))
            return
        self._hook_connections[connection] = key
        self._emit([request_event])
        if self._stdin_closed or turn.cancel_requested:
            self._emit(self._inputs.close("cancelled" if turn.cancel_requested else "dismissed", turn.id))

    def _hook_disconnected(self, connection) -> None:
        key = self._hook_connections.pop(connection, None)
        if key is not None:
            self._emit(self._inputs.resolve(key, "error"))
            # Fail the session rather than fall through to an unseen native selector.
            raise RuntimeError("Claude question hook disconnected before receiving an answer")

    def _cancel(self, turn_id: str, ref: object) -> None:
        """Answer a `cancel`: `ack` it and act on the turn's state.

        The ack says the request was taken, not that the turn has ended —
        that is the `turn_end`, which for a queued turn follows in the same
        batch and for a running one arrives when the inner is seen to stop.
        """
        try:
            action, events = self._tracker.cancel(turn_id)
        except CancelError as why:
            return self._reject(str(why), ref)
        turn = self._tracker.turn(turn_id)
        assert turn is not None
        self._emit(tagged([{"type": "ack", "turn": turn.ref()}], ref))
        if self._inputs is not None:
            self._emit(self._inputs.close("cancelled", turn_id))
        if action == CANCEL_WITHDRAWN:
            self._queue.withdraw(turn_id)
            log.log_text("proxy", f"{turn_id} withdrawn before it was typed")
        self._emit(events)
        if action == CANCEL_INTERRUPT:
            self._interrupt_if_due()
        return None

    def _interrupt_if_due(self) -> None:
        """Send the one `Esc` a cancelled running turn is owed, once.

        Called on the cancel itself and on every tick, so a turn cancelled
        while its prompt was still being typed is interrupted the moment the
        transcript binds it — and never a second time.
        """
        turn = self._tracker.running_turn
        if turn is None or not turn.cancel_requested or turn.interrupt_sent:
            return
        turn.interrupt_sent = True
        if self._send_keys is None:
            log.log_text("proxy", f"{turn.id}: cancel requested but no way to send Esc")
            return
        log.log_text("proxy", f"{turn.id}: sending Esc")
        self._send_keys((ESC,))

    def on_stdin_eof(self) -> None:
        """The client closed its end; finish what is in flight and stop."""
        self._stdin_closed = True
        if self._inputs is not None:
            self._emit(self._inputs.close("dismissed"))

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def is_ready_to_send(self) -> bool:
        """True when a queued prompt may be typed into the TUI right now.

        Five conditions. The fourth is the one that is easy to miss: a
        prompt already typed but not yet seen in the transcript still owns the
        keyboard. Typing past it would leave two prompts in flight with one
        binding decision to tell them apart, which is how a turn ends up
        matched to the wrong id. The fifth came with cancellation: a composer
        still holding a cancelled prompt would have the next one typed onto
        its end and submitted as one — observed, and it binds as an unowned
        turn while the client's own sits unmatched.
        """
        return (
            self._liveness.is_idle
            and self._liveness.has_settled
            and self._tracker.running_turn is None
            and self._tracker.typed_turn is None
            and self._composer_dirty is None
        )

    def on_typed(self, message: dict) -> bool:
        """The keystrokes for a prompt are about to go out; False says type nothing.

        None from the queue means the payload's turn was withdrawn by a
        `cancel` while it waited, so the io loop drops it instead of typing
        a prompt whose turn has already ended.
        """
        turn = self._queue.take(message)
        if turn is None:
            return False
        self._fallback.note_typed()
        self._tracker.mark_typed(turn)
        return True

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
        """Read a settled frame for liveness, and feed the fallback clock.

        Since cancellation the frame answers one more question, and only
        after an `Esc` has gone out: is the cancelled prompt sitting on the
        composer row? That is the inner's silent ending — nothing reaches the
        transcript — so the turn ends here, and the composer is marked for
        clearing before anything else may be typed.
        """
        self._liveness.on_commit(screen)
        self._fallback.note_frame(self._liveness.is_idle)
        if not self._liveness.is_idle:
            return
        turn = self._tracker.running_turn
        if (
            turn is not None
            and turn.interrupt_sent
            and turn.text
            and self._liveness.composer_holds(turn.text)
        ):
            log.log_text("proxy", f"{turn.id}: prompt handed back to the composer")
            self._composer_dirty = turn.text
            self._clear_sent_at = None
            self._clear_attempts = 0
            self._emit(self._tracker.end_restored_turn())
        self._clear_composer_if_dirty()

    def _clear_composer_if_dirty(self) -> None:
        """Empty a composer the inner left the cancelled prompt in, then reopen the gate.

        Only on an idle frame that still shows the text — never blind, since
        the same keys on an empty composer open the rewind picker — and not
        again until `CLEAR_SETTLE_S` after the last try, because the frame
        under the next tick may predate the repaint. Gives up after
        `CLEAR_ATTEMPTS`: a gate held shut forever is the wedge this project
        exists to remove, and a prompt typed onto leftover text is at least
        caught downstream as an unmatched turn.
        """
        if self._composer_dirty is None:
            return
        now = self._clock()
        if self._clear_sent_at is not None and now - self._clear_sent_at < CLEAR_SETTLE_S:
            return
        if not self._liveness.composer_holds(self._composer_dirty):
            log.log_text("proxy", "composer clear")
            self._composer_dirty = None
            return
        if self._clear_attempts >= CLEAR_ATTEMPTS or self._send_keys is None:
            log.log_text(
                "proxy",
                "composer still holds the cancelled prompt and will not clear; "
                "reopening the gate anyway",
            )
            self._composer_dirty = None
            return
        self._clear_attempts += 1
        self._clear_sent_at = now
        log.log_text("proxy", f"clearing the composer (attempt {self._clear_attempts})")
        self._send_keys(CLEAR_COMPOSER)

    def on_iteration(self) -> None:
        """One loop tick: drain the transcript, look for orphans, check the fallback tier."""
        for entry in self._read_entries():
            self._fallback.note_transcript()
            self._activity["last_activity"] = time.monotonic()
            self._emit(self._tracker.on_entry(entry))
        if self._bridge is not None:
            self._bridge.poll(self._on_hook, self._hook_disconnected)
        if self._inputs is not None:
            self._emit(self._inputs.tick())
            if any(r.blocking and r.state == "pending" for r in self._inputs.requests.values()):
                self._activity["last_activity"] = self._clock()
        self._interrupt_if_due()
        self._poll_orphans()

        running = self._tracker.running_turn
        typed = self._tracker.typed_turn
        if typed is None:
            self._fallback.note_typed_cleared()
        if running is None and typed is None:
            return
        if running is not None and self._inputs is not None and self._inputs.blocks(running.id):
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
            + [orphan_end_event(e) for e in ended]
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
            if self._inputs is not None:
                if event.get("type") == "tool_result":
                    self._emit(self._inputs.resolve(("claude", event.get("tool_use_id")),
                                                   "error" if event.get("is_error") else None))
                elif event.get("type") == "turn_end":
                    self._emit(self._inputs.close("backend_closed", event["turn"]["id"]))
                elif event.get("type") == "input_resolved":
                    self._fallback.note_turn_start()
                    self._activity["last_activity"] = self._clock()
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
        self._emit(tagged([{"type": "error", "error": reason}], ref))
        return None
