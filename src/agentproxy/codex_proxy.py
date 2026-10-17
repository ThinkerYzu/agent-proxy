"""The wiring between the client's stdio protocol and a `codex app-server` thread.

The codex counterpart of `proxy.py`, and much smaller, because codex states
what `claude` makes `Proxy` infer: there is no screen to read, no composer
to keep clean and no transcript to match prompts against. What is left is
deciding when a prompt may go out, asking codex to interrupt a turn, the two
fallbacks that still apply, and the orphans. Every decision is here and no
process I/O is: `cli.serve_codex` owns the pipes, and the tests drive this
with a fake server's messages.

One prompt goes out at a time, and only with nothing running. Codex folds a
`turn/start` sent mid-turn into the running turn, so holding the next prompt
until the turn ends is what keeps one prompt one turn (SPEC C2).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from agentproxy import log
from agentproxy.app_server import AppServerPeer, Message, Notification, Response, ServerRequest
from agentproxy.codex_session import CodexSession
from agentproxy.commands import CANCEL, STATUS, SUBMIT, Rejection, parse_command, tagged
from agentproxy.fallback import BASIS_STALLED, BASIS_UNMATCHED, STALL_AFTER_S, UNMATCHED_AFTER_S
from agentproxy.orphans import OrphanReaper, orphan_end_event
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import (
    CANCEL_INTERRUPT,
    CANCEL_WITHDRAWN,
    CancelError,
    TurnTracker,
)


AGENT_CODEX = "codex"

# `task_end.output` is a preview, as on the claude path.
TASK_OUTPUT_MAX_CHARS = 256

# What the proxy answers each request codex can make of a client. None can be
# served, since no person is there to approve or answer, and every one must
# be answered: an unanswered request is a turn that never ends. Each is the
# refusal its own schema defines.
_REFUSALS: dict[str, dict] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "applyPatchApproval": {"decision": "denied"},
    "execCommandApproval": {"decision": "denied"},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "cancel"},
    "item/tool/call": {"success": False, "contentItems": []},
}
# JSON-RPC "method not found", for a request with no refusal of its own.
_METHOD_NOT_FOUND = -32601


class CodexProxy:
    """One codex thread, seen from the client's side of the pipe."""

    def __init__(
        self,
        tracker: TurnTracker,
        emitter: Emitter,
        queue: SubmitQueue,
        session: CodexSession,
        peer: AppServerPeer,
        reaper: Optional[OrphanReaper] = None,
        kill_orphans: bool = False,
        clock: Callable[[], float] = time.monotonic,
        stall_after_s: float = STALL_AFTER_S,
        unmatched_after_s: float = UNMATCHED_AFTER_S,
    ) -> None:
        self._tracker = tracker
        self._emitter = emitter
        self._queue = queue
        self._session = session
        self._peer = peer
        self._reaper = reaper
        self._kill_orphans = kill_orphans
        self._clock = clock
        self._stall_after_s = stall_after_s
        self._unmatched_after_s = unmatched_after_s
        self._stdin_closed = False
        self._failed = False
        # The `turn/start` waiting for its response, and when it was sent:
        # the second term of the send gate, and the unmatched clock.
        self._turn_start_id: Optional[int] = None
        self._typed_at: Optional[float] = None
        self._last_heard = clock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def announce_ready(self, thread_id: str) -> None:
        """Tell the client the session is up: always the first message."""
        self._emit([{"type": "ready", "session_id": thread_id, "agent": AGENT_CODEX}])

    def fail(self, reason: str) -> None:
        """Close out a session that cannot continue, leaving nothing outstanding.

        The same closing as `Proxy.fail`: the running turn, everything queued,
        every task, then the orphans, abandoned or killed.
        """
        if self._failed:
            return
        self._failed = True
        log.log_text("codex-proxy", f"session failed: {reason}")
        self._queue.drain()
        self._turn_start_id = None
        self._emit(self._tracker.fail_session(reason))
        if self._reaper is not None:
            self._emit([orphan_end_event(e) for e in self._reaper.shutdown(kill=self._kill_orphans)])

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def has_unfinished_work(self) -> bool:
        return any(self._emitter.outstanding().values())

    # ------------------------------------------------------------------
    # Client side
    # ------------------------------------------------------------------

    def on_stdin_line(self, line: str) -> None:
        """Handle one client command, answering it before codex hears anything."""
        command = parse_command(line)
        if isinstance(command, Rejection):
            self._reject(command.reason, command.ref)
        elif command.kind == SUBMIT:
            turn, events = self._tracker.submit(command.text)
            self._emit(tagged(events, command.ref))
            self._queue.accept(turn)
            self._send_if_ready()
        elif command.kind == STATUS:
            state = {"type": "state", **self._tracker.snapshot()}
            if self._reaper is not None:
                state["orphans"] = [o.to_wire() for o in self._reaper.alive()]
            self._emit(tagged([state], command.ref))
        else:
            assert command.kind == CANCEL
            self._cancel(command.turn, command.ref)

    def on_stdin_eof(self) -> None:
        """The client closed its end; finish what is in flight and stop."""
        self._stdin_closed = True

    def _cancel(self, turn_id: str, ref: object) -> None:
        """Answer a `cancel` by the turn's state, as `Proxy._cancel` does.

        Running: `turn/interrupt`, and the turn ends when codex says it was
        interrupted. Typed (its `turn/start` unanswered): held, and the
        interrupt goes out once the response binds it.
        """
        try:
            action, events = self._tracker.cancel(turn_id)
        except CancelError as why:
            self._reject(str(why), ref)
            return
        turn = self._tracker.turn(turn_id)
        assert turn is not None
        self._emit(tagged([{"type": "ack", "turn": turn.ref()}], ref))
        if action == CANCEL_WITHDRAWN:
            self._queue.withdraw(turn_id)
        self._emit(events)
        if action == CANCEL_INTERRUPT:
            self._interrupt_if_due()

    def _interrupt_if_due(self) -> None:
        """Send the one `turn/interrupt` a cancelled running turn is owed."""
        turn = self._tracker.running_turn
        if turn is None or not turn.cancel_requested or turn.interrupt_sent:
            return
        codex_id = self._session.codex_turn_of(turn.id)
        if codex_id is None:
            return
        turn.interrupt_sent = True
        log.log_text("codex-proxy", f"{turn.id}: turn/interrupt {codex_id}")
        self._peer.request(
            "turn/interrupt", {"threadId": self._session.thread_id, "turnId": codex_id}
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def is_ready_to_send(self) -> bool:
        """True when the next prompt may go to codex: nothing running, nothing in flight."""
        return (
            not self._failed
            and self._tracker.running_turn is None
            and self._tracker.typed_turn is None
            and self._turn_start_id is None
        )

    def _send_if_ready(self) -> None:
        """Send the next queued prompt as a `turn/start`, if the gate is open."""
        if self._queue.is_empty or not self.is_ready_to_send():
            return
        turn = self._queue.take()
        if turn is None:
            return
        self._tracker.mark_typed(turn)
        self._typed_at = self._clock()
        self._turn_start_id = self._peer.request(
            "turn/start",
            {
                "threadId": self._session.thread_id,
                "input": [{"type": "text", "text": turn.text or ""}],
            },
        )
        log.log_text("codex-proxy", f"{turn.id}: turn/start sent")

    # ------------------------------------------------------------------
    # Server side
    # ------------------------------------------------------------------

    def on_messages(self, messages: list[Message]) -> None:
        """Act on what the app-server said since the last call."""
        for message in messages:
            self._last_heard = self._clock()
            if isinstance(message, Response):
                self._on_response(message)
            elif isinstance(message, ServerRequest):
                self._refuse(message)
            elif isinstance(message, Notification):
                self._emit(self._session.on_notification(message.method, message.params))
            if self._session.failure is not None:
                self.fail(self._session.failure)
                return
        self._interrupt_if_due()
        self._send_if_ready()

    def _on_response(self, response: Response) -> None:
        if response.method == "turn/start" and response.id == self._turn_start_id:
            self._turn_start_id = None
            self._typed_at = None
            self._emit(self._session.on_turn_started_response(response.result, response.error))
        elif response.error is not None:
            log.log_text(
                "codex-proxy", f"{response.method} failed: {response.error.get('message')}"
            )

    def _refuse(self, request: ServerRequest) -> None:
        """Answer a request no client can serve, with the refusal codex expects."""
        refusal = _REFUSALS.get(request.method)
        log.log_text("codex-proxy", f"refusing server request {request.method}")
        if refusal is None:
            self._peer.respond_error(
                request.id, _METHOD_NOT_FOUND, f"agent-proxy does not serve {request.method}"
            )
        else:
            self._peer.respond(request.id, dict(refusal))

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def on_tick(self) -> None:
        """One loop tick: orphans, the two fallbacks, and the next prompt."""
        self._poll_orphans()
        now = self._clock()
        running = self._tracker.running_turn
        typed = self._tracker.typed_turn
        if running is not None and now - self._last_heard >= self._stall_after_s:
            log.log_text("codex-proxy", f"{running.id} ended on {BASIS_STALLED}")
            self._emit(self._tracker.end_running_turn(BASIS_STALLED))
        elif (
            typed is not None
            and self._typed_at is not None
            and now - self._typed_at >= self._unmatched_after_s
        ):
            # A late answer then finds no typed turn and is only logged.
            log.log_text("codex-proxy", f"{typed.id}: turn/start unanswered; {BASIS_UNMATCHED}")
            self._turn_start_id = None
            self._typed_at = None
            self._emit(self._tracker.end_typed_turn(BASIS_UNMATCHED))
        self._send_if_ready()

    def should_stop(self) -> bool:
        """True when the client has gone and nothing is owed to it, as `Proxy.should_stop`."""
        return (
            self._stdin_closed
            and self._queue.is_empty
            and self._tracker.running_turn is None
            and self._tracker.typed_turn is None
        )

    def _poll_orphans(self) -> None:
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
        for event in events:
            if event.get("type") == "task_end" and isinstance(event.get("output"), str):
                event["output"] = event["output"][-TASK_OUTPUT_MAX_CHARS:] or None
            self._emitter.emit(event)

    def _reject(self, reason: str, ref: object = None) -> None:
        log.log_text("codex-proxy", f"rejected: {reason}")
        self._emit(tagged([{"type": "error", "error": reason}], ref))
