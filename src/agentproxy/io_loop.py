"""Single-threaded `selectors` loop over stdin + pty fd + a signal self-pipe.

All state mutations happen on the loop thread, so no locking is required.

``run(driver, model, ...)`` drains the pty into the screen model, optionally
writes one or more preloaded user messages, and exits when ``stop_when`` says
so (or the pty / stdin closes, or a signal arrives). The stdin -> pty path is
wired here too, so the same loop serves both a scripted run and the live
proxy.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import time
from typing import IO, Callable, Iterable, Optional

from agentproxy import log
from agentproxy.input_encoder import (
    SUBMIT_DELAY,
    encode_user_message_chunks,
)
from agentproxy.pty_driver import PtyDriver
from agentproxy.screen_model import ScreenModel


# Submit-confirmation defaults. claude >= 2.1.195 can silently eat the submit
# CR if it arrives while the inner is still finishing startup — most reliably
# when an MCP-server-init banner ("N MCP servers need authentication") paints
# an idle-looking footer before the inner can accept a submit. The bracketed
# paste lands in the input box intact, but the bare `\r` is dropped, so the
# turn never goes active and stalls until `max_duration`. When a confirmation
# predicate is supplied, we re-send the submit CR every RESUBMIT_INTERVAL_S
# until the inner is observed to go active, bounded by MAX_RESUBMITS. Extra
# CRs sent before the inner is ready are eaten the same way; the first one to
# land once it is ready submits the already-present paste text. Tunable via
# env for diagnostics.
RESUBMIT_INTERVAL_S = float(os.environ.get("CLAUDEJSON_RESUBMIT_INTERVAL_S", "0.6"))
MAX_RESUBMITS = int(os.environ.get("CLAUDEJSON_MAX_RESUBMITS", "10"))

# Quiescence gates, keyed on real inner *output* silence (no pty bytes), not on
# the idle/active footer reading — which a `--resume` repaint can leave stuck
# stale (the inner repaints its resumed conversation, paints an active footer,
# then stops emitting frames, so `is_idle` reads False forever even though the
# inner is sitting idle at the prompt). A genuinely busy turn keeps painting its
# spinner (~1 Hz), so it never goes quiet this long; an idle/stuck inner does.
#
#  - SEND_QUIESCENCE_S: hold an outbound message until the inner has been quiet
#    this long, so a resume repaint finishes before we paste. Without it the
#    paste lands mid-repaint and the next repaint frame wipes it from the box,
#    so the submit CR then submits an empty box and the turn never starts.
#  - RESUBMIT_QUIESCENCE_S: only re-send / re-paste once the inner has gone
#    quiet, so we never spam a busy turn and we don't re-paste during the
#    pre-render window (after this long of silence the box state is stable —
#    if our text isn't there, it was genuinely wiped, not just not-yet-rendered).
# Both default to 0.0 in `run()` (opt-in; existing callers/tests unaffected);
# `cli.py` passes these values.
SEND_QUIESCENCE_S = float(os.environ.get("CLAUDEJSON_SEND_QUIESCENCE_S", "0.7"))
RESUBMIT_QUIESCENCE_S = float(os.environ.get("CLAUDEJSON_RESUBMIT_QUIESCENCE_S", "1.5"))


Encoder = Callable[[dict], tuple[bytes, bytes]]
StopPredicate = Callable[[], bool]
ReadyPredicate = Callable[[], bool]
MessageCallback = Callable[[dict], None]
EofCallback = Callable[[], None]
TickCallback = Callable[[], None]
LineHandler = Callable[[str], Optional[dict]]


def run(
    driver: PtyDriver,
    model: ScreenModel,
    *,
    stdin: Optional[IO[bytes]] = None,
    encoder: Encoder = encode_user_message_chunks,
    initial_messages: Optional[Iterable[dict]] = None,
    send_initial_when: Optional[ReadyPredicate] = None,
    on_stdin_line: Optional[LineHandler] = None,
    on_user_message: Optional[MessageCallback] = None,
    on_stdin_eof: Optional[EofCallback] = None,
    on_iteration: Optional[TickCallback] = None,
    stop_when: Optional[StopPredicate] = None,
    confirm_send_when: Optional[ReadyPredicate] = None,
    resubmit_when: Optional[ReadyPredicate] = None,
    resubmit_interval: float = RESUBMIT_INTERVAL_S,
    max_resubmits: int = MAX_RESUBMITS,
    send_quiescence_s: float = 0.0,
    resubmit_quiescence_s: float = 0.0,
    select_timeout: float = 0.2,
    max_duration: Optional[float] = None,
    activity: Optional[dict] = None,
    idle_timeout_when: Optional[ReadyPredicate] = None,
) -> str:
    """Drive the pty until a termination condition fires.

    Returns one of:
      - ``"stop"`` — ``stop_when`` returned True.
      - ``"pty_eof"`` — pty read returned ``b""`` (inner claude exited).
      - ``"stdin_eof"`` — stdin closed before any other condition.
      - ``"signal"`` — SIGINT or SIGTERM was caught.
      - ``"timeout"`` — ``max_duration`` seconds passed with no new activity.
        ``max_duration`` is a *continuous idle* budget, not a total-runtime
        cap: it measures elapsed time since ``activity["last_activity"]``
        was last bumped, not since ``run()`` was called. It is only checked
        while ``idle_timeout_when`` (if given) returns True — pass it so the
        idle clock is only live during an actual in-flight turn, not while
        genuinely idle between turns waiting on stdin.

    ``initial_messages`` are encoded and written to the pty as soon as
    ``send_initial_when`` (if given) returns True, or immediately if no
    gate predicate is provided. They are sent exactly once.

    ``on_stdin_line`` hands the caller each stdin line before this loop does
    anything with it, and the caller returns the message to queue for the
    pty — or ``None`` to queue nothing. It is what lets a caller speak a
    stdin protocol that is not "every line is a prompt": agent-proxy answers
    a `status` command itself and reports a malformed line as an `error`,
    neither of which may reach the inner claude's keyboard. Without it, the
    loop parses the line as JSON and queues it as-is.

    ``activity``, if supplied, is a caller-owned dict this loop reads/writes
    in place — pass the same dict a caller's own callbacks (e.g. transcript
    or tool-call handlers) also write ``last_activity`` into, so those events
    reset the idle-timeout clock too. Without it, a fresh dict is created and
    nothing outside this loop can reset the clock (equivalent to the old
    fixed-from-start deadline).
    """
    # Unified outbound queue: all messages (initial + stdin-sourced) wait
    # in here until `send_initial_when` (used as the universal ready
    # predicate) reports the inner claude is idle. Sending mid-turn would
    # land the paste into a non-existent input panel — the keystrokes get
    # swallowed and the turn never advances.
    pending_messages: list[dict] = list(initial_messages or [])
    # Deferred submit CRs: list[(due_at_monotonic, bytes)]. We write the
    # bracketed-paste block immediately, then schedule the submit CR a few
    # tens of milliseconds later — see input_encoder.SUBMIT_DELAY.
    deferred_writes: list[tuple[float, bytes]] = []
    # Submit-confirmation state (see RESUBMIT_INTERVAL_S). Active only while
    # `confirm_send_when` is wired and a message has been sent but the inner
    # has not yet been observed to go active. `confirm` holds the per-send
    # bookkeeping: the submit bytes to re-send, when the next re-send is due,
    # and how many re-sends remain.
    confirm: dict[str, object] = {
        "pending": False,
        "submit": b"",
        "next_at": 0.0,
        "left": 0,
    }
    # Ground-truth inner-output activity: monotonic timestamp of the last
    # non-empty pty read. The quiescence gates (send / resubmit) measure
    # silence against this rather than the footer reading. Boxed so the nested
    # helpers can mutate it.
    #
    # `last_activity` is a distinct clock backing the idle-timeout: unlike
    # `last_pty` it is never bumped by this loop on its own (raw pty bytes —
    # e.g. a spinner redraw — aren't proof of real progress), only by an
    # external caller writing into the shared `activity` dict when a genuine
    # message/tool-call event occurs. If no caller ever does that, it just
    # sits at its initial value and `max_duration` behaves like the old
    # fixed-from-start deadline.
    if activity is None:
        activity = {}
    now0 = time.monotonic()
    activity.setdefault("last_pty", now0)
    activity.setdefault("last_activity", now0)

    # Partial stdin line, held until its newline arrives. Stdin is read with
    # `os.read` on the raw fd rather than `readline()`, and that is not a
    # style choice: `readline()` on a buffered stream pulls the whole
    # available chunk into Python's own buffer and hands back one line. The
    # rest is then invisible to `select`, which watches the fd, so a second
    # message sent in the same write sits unread until *more* bytes arrive —
    # observed as a queued prompt stranded for 100 s until the client closed
    # the pipe. Splitting the lines here keeps the fd the only buffer.
    stdin_buffer = b""

    sel = selectors.DefaultSelector()
    sel.register(driver.fd, selectors.EVENT_READ, "pty")
    stdin_open = False
    if stdin is not None:
        sel.register(stdin, selectors.EVENT_READ, "stdin")
        stdin_open = True

    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)
    sel.register(sig_r, selectors.EVENT_READ, "signal")

    def _on_signal(_signo, _frame):
        try:
            os.write(sig_w, b"\x00")
        except OSError:
            pass

    prev_handlers: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev_handlers[sig] = signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # Not on the main thread / unsupported platform — skip.
            pass

    def _send(msg: dict) -> None:
        if on_user_message is not None:
            on_user_message(msg)
        paste, submit = encoder(msg)
        driver.write(paste)
        now = time.monotonic()
        deferred_writes.append((now + SUBMIT_DELAY, submit))
        # Arm submit confirmation: if the submit isn't confirmed shortly after
        # the first CR, re-send (CR, or the whole paste if the box lost our
        # text) until it lands or we exhaust the budget. See RESUBMIT_INTERVAL_S.
        if confirm_send_when is not None:
            confirm["pending"] = True
            confirm["submit"] = submit
            confirm["next_at"] = now + SUBMIT_DELAY + resubmit_interval
            confirm["left"] = max_resubmits

    def _drain_confirm() -> None:
        if not confirm["pending"]:
            return
        # confirm_send_when is non-None whenever confirm can be pending.
        assert confirm_send_when is not None
        if confirm_send_when():
            confirm["pending"] = False
            log.log_text("parser", "submit confirmed")
            return
        now = time.monotonic()
        if now < float(confirm["next_at"]):
            return
        # Only re-send once the inner has gone quiet (no pty output) for
        # resubmit_quiescence_s. This is ground-truth "inner is not busy",
        # robust to a stale idle/active footer reading: a resume repaint can
        # leave is_idle stuck False even though the inner is sitting idle, which
        # is exactly when we MUST retry. A genuinely busy turn keeps painting
        # its spinner, so it never goes quiet this long and we never spam it.
        # (Supersedes the old `resubmit_when=is_idle` gate, which the stale
        # reading disarmed.) `resubmit_when`, if still supplied, is honoured too.
        if resubmit_quiescence_s > 0 and (now - activity["last_pty"]) < resubmit_quiescence_s:
            confirm["next_at"] = now + resubmit_interval
            return
        if resubmit_when is not None and not resubmit_when():
            confirm["next_at"] = now + resubmit_interval
            return
        if int(confirm["left"]) <= 0:
            # Budget exhausted — stop re-sending and let the turn's
            # max_duration / loop-end guard surface the failure rather than
            # spinning forever.
            confirm["pending"] = False
            log.log_text("parser", "submit confirmation exhausted")
            return
        left = int(confirm["left"]) - 1
        # Re-send the submit CR only — never re-paste. A bare CR into a box that
        # still holds our text submits it (the eaten-CR case we recover); a CR
        # into an empty/idle box is a harmless no-op. Re-pasting, by contrast,
        # would submit a *duplicate turn* whenever confirmation races — e.g. a
        # turn completes before the box-clear confirm observes the paste, so
        # `paste_seen` never latches and the retry keeps firing — observed as
        # repeated identical turns. Resends stay safe; the send-quiescence gate
        # (not re-paste) is what avoids the resume-repaint box-wipe.
        driver.write(confirm["submit"])  # type: ignore[arg-type]
        log.log_text("parser", f"submit not confirmed — resending CR ({left} left)")
        confirm["left"] = left
        confirm["next_at"] = now + resubmit_interval

    def _maybe_drain_pending() -> None:
        """Drain pending messages while the ready predicate (if any) is True.

        Sends one message per check so the inner claude has a chance to
        leave the idle state (e.g. show a spinner) before we evaluate the
        predicate again for the next message.
        """
        while pending_messages:
            if send_initial_when is not None and not send_initial_when():
                return
            # Hold the message until the inner has been quiet (no pty output)
            # for send_quiescence_s, so a `--resume` repaint finishes before we
            # paste. Pasting mid-repaint lets the next repaint frame wipe the
            # box; the submit CR then submits nothing and the turn never starts.
            if send_quiescence_s > 0 and (
                time.monotonic() - activity["last_pty"]
            ) < send_quiescence_s:
                return
            _send(pending_messages.pop(0))
            # After a send the screen will go non-idle on the next frame;
            # stop draining now and let the next loop iteration re-check.
            return

    def _drain_deferred() -> None:
        now = time.monotonic()
        # Stable iteration: pop ready items in-place.
        i = 0
        while i < len(deferred_writes):
            due_at, payload = deferred_writes[i]
            if due_at <= now:
                driver.write(payload)
                deferred_writes.pop(i)
            else:
                i += 1

    def _take_line(raw: bytes) -> None:
        """Turn one complete stdin line into a queued message, if it is one."""
        stripped = raw.strip()
        if not stripped:
            return
        text = stripped.decode("utf-8", errors="replace")
        log.log_text("stdin.in", text)
        if on_stdin_line is not None:
            msg = on_stdin_line(text)
            if msg is not None:
                pending_messages.append(msg)
            return
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            # Invalid JSON on stdin is ignored here; a caller that wants to
            # report it takes `on_stdin_line` instead.
            return
        pending_messages.append(msg)

    def _should_stop() -> bool:
        # Don't honour stop_when while there's still outbound traffic
        # queued: messages waiting for is_idle, a submit CR still waiting
        # on its SUBMIT_DELAY, or a submit not yet confirmed landed.
        # Otherwise we could drop an unsent message on shutdown.
        if pending_messages or deferred_writes or confirm["pending"]:
            return False
        return stop_when is not None and stop_when()

    def _idle_timeout_live() -> bool:
        return max_duration is not None and (
            idle_timeout_when is None or idle_timeout_when()
        )

    try:
        while True:
            if _should_stop():
                return "stop"
            if _idle_timeout_live() and (
                time.monotonic() - float(activity["last_activity"]) >= max_duration
            ):
                return "timeout"

            if on_iteration is not None:
                on_iteration()
            _maybe_drain_pending()
            _drain_deferred()
            _drain_confirm()

            timeout = select_timeout
            if _idle_timeout_live():
                idle_remaining = max_duration - (
                    time.monotonic() - float(activity["last_activity"])
                )
                timeout = max(0.0, min(timeout, idle_remaining))
            # Shrink the select timeout so a due deferred submit fires promptly.
            if deferred_writes:
                next_due = min(due_at for due_at, _ in deferred_writes)
                timeout = max(0.0, min(timeout, next_due - time.monotonic()))
            # Likewise wake in time for a due submit re-send.
            if confirm["pending"]:
                timeout = max(0.0, min(timeout, float(confirm["next_at"]) - time.monotonic()))

            events = sel.select(timeout=timeout)
            for key, _ in events:
                tag = key.data
                if tag == "pty":
                    data = driver.read(65536)
                    if not data:
                        return "pty_eof"
                    activity["last_pty"] = time.monotonic()
                    model.feed(data)
                elif tag == "stdin":
                    assert stdin is not None
                    chunk = os.read(stdin.fileno(), 65536)
                    if not chunk:
                        _take_line(stdin_buffer)
                        stdin_buffer = b""
                        sel.unregister(stdin)
                        stdin_open = False
                        if on_stdin_eof is not None:
                            on_stdin_eof()
                        # Don't immediately return — let the in-flight turn
                        # drain into model.feed before the caller's stop_when
                        # or pty EOF takes us out.
                        continue
                    stdin_buffer += chunk
                    while b"\n" in stdin_buffer:
                        line, stdin_buffer = stdin_buffer.split(b"\n", 1)
                        _take_line(line)
                elif tag == "signal":
                    try:
                        os.read(sig_r, 4096)
                    except OSError:
                        pass
                    # Flush any pending submit CRs before exiting. Without
                    # this, driver.close() provides the \r via "/exit\r",
                    # fusing "/exit" into the in-flight message and corrupting
                    # the session transcript.
                    for _due_at, payload in deferred_writes:
                        try:
                            driver.write(payload)
                        except OSError:
                            pass
                    deferred_writes.clear()
                    return "signal"

            if not stdin_open and stdin is not None and stop_when is None:
                # stdin EOF and no other termination hook — let the in-flight
                # turn finish, then exit when the pty has nothing more to say.
                # (We loop once more so any final pty bytes drain via the
                # selector; that path returns "pty_eof".)
                # To avoid spinning, give the pty a moment.
                if not events:
                    return "stdin_eof"
    finally:
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        try:
            sel.close()
        except Exception:
            pass
        for fd in (sig_r, sig_w):
            try:
                os.close(fd)
            except OSError:
                pass
