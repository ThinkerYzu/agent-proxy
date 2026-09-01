"""The `agent-proxy` entry point: spawn the inner `claude` and serve stdio.

    echo '{"type":"submit","text":"Reply PONG"}' | agent-proxy

Reads line-delimited commands on stdin, drives an interactive `claude` on a
pty, and writes the turn-aware protocol to stdout. Unrecognised arguments are
forwarded to the inner `claude`, so a client passes `--resume` or `--model`
straight through.

This module is the process boundary and nothing else — it owns the pty, the
transcript file, stdout and the signal handlers, and hands every decision to
`Proxy`. To read what the proxy *does*, start there; to replay a recorded
session through the tracker with no process at all, see `replay.py`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import IO, Optional, Sequence

from agentproxy import inner_claude, log
from agentproxy.fallback import FallbackEot
from agentproxy.io_loop import (
    RESUBMIT_QUIESCENCE_S,
    SEND_QUIESCENCE_S,
    run as run_loop,
)
from agentproxy.liveness import PROMPT_ANSWERS, Liveness
from agentproxy.protocol import Emitter
from agentproxy.proxy import Proxy
from agentproxy.pty_driver import PtyDriver
from agentproxy.screen_model import ScreenModel
from agentproxy.submit_queue import SubmitQueue
from agentproxy.transcript_tailer import TranscriptTailer
from agentproxy.turn_tracker import TurnTracker


# Why the loop ended, in the words the closing `error` message uses. `stop` is
# in here for one case only: the client closed stdin while a background task
# was still running, which `should_stop` deliberately does not wait for — the
# client that would hear about it has already gone.
_ABORT_REASONS = {
    "stop": "client closed stdin with background work still running",
    "crashed": "the proxy stopped unexpectedly",
    "pty_eof": "inner claude exited",
    "stdin_eof": "client closed stdin mid-turn",
    "signal": "interrupted by signal",
    "timeout": "inner claude went silent",
}


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="agent-proxy",
        description=(
            "Turn-aware JSON stream around an interactive claude TUI. Reads "
            "{\"type\":\"submit\",\"text\":...} and {\"type\":\"status\"} on "
            "stdin; writes turn_start / turn_end / text / tool_use and the "
            "three outstanding counts on stdout. Unrecognised arguments are "
            "forwarded to the inner `claude`."
        ),
    )


def _read_task_output(path: str) -> Optional[str]:
    """The tail of a background task's own output file, or None if unreadable.

    Read as bytes and only the last few KB, so a large build log costs one
    bounded seek-and-read rather than loading the whole file just to keep the
    last 256 characters `Proxy` will actually use.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            data = handle.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _open_tailer(session_id: str, tail_mode: str, started_at: datetime) -> TranscriptTailer:
    """Point the tailer at the transcript the inner `claude` is about to write.

    Every session id is known before the spawn, so the path is always
    deterministic and the old directory-diff is gone. Must
    still be constructed *before* the inner starts, so a resume seeks past the
    file's existing contents rather than past whatever the inner has appended
    by the time we look.
    """
    return TranscriptTailer.for_known_path(
        os.getcwd(),
        session_id,
        start_at_end=(tail_mode == "from_end"),
        skip_before=started_at if tail_mode == "fork" else None,
    )


def serve(
    stdin: IO[bytes],
    stdout: IO[str],
    passthrough: Sequence[str] = (),
) -> int:
    """Run one proxied session, from spawn to the last message on stdout.

    Returns 0 for a session that ran, 2 for session flags that name no single
    session. That is the one case where `ready` is not the first message,
    because there is no session to be ready for: the failure is reported as a
    lone `error` and nothing is spawned.
    """
    # Stamped before the spawn so a forked session's backfill — which carries
    # the original's timestamps — is entirely in the past by this clock.
    started_at = datetime.now(timezone.utc)
    try:
        session_id, tail_mode, argv = inner_claude.resolve_session(passthrough)
    except inner_claude.SessionArgumentError as bad_flags:
        Emitter(TurnTracker(), out=stdout).emit(
            {"type": "error", "error": str(bad_flags)}
        )
        stdout.flush()
        return 2
    tailer = _open_tailer(session_id, tail_mode, started_at)

    driver = PtyDriver(
        inner_claude.build_argv(argv), env=inner_claude.build_env(dict(os.environ))
    )

    def _answer_dialog(prompt: str) -> None:
        for keys in PROMPT_ANSWERS.get(prompt, ()):
            driver.write(keys)

    tracker = TurnTracker()
    # Shared with the io loop, which stamps `last_pty` on every pty read. The
    # fallback tier reads it to tell a silent inner from a busy one.
    activity: dict[str, float] = {}
    proxy = Proxy(
        tracker=tracker,
        emitter=Emitter(tracker, out=stdout),
        queue=SubmitQueue(),
        liveness=Liveness(on_prompt=_answer_dialog),
        fallback=FallbackEot(),
        read_entries=tailer.read_new,
        read_output=_read_task_output,
        activity=activity,
    )

    # `on_query` answers the inner's terminal-capability probes (DA1,
    # XTVERSION). Without it, claude >= 2.1.162 never enables mode-2026
    # synchronised output, no frame ever commits, and the send gate — which
    # waits on an idle frame — stays shut forever.
    model = ScreenModel(on_commit=proxy.on_commit, on_query=driver.write)

    def _tick() -> None:
        # Poll the screen as well as taking committed frames: the inner paints
        # its first dialog before enabling mode-2026, so a trust prompt can sit
        # on a frame that never commits and would otherwise never be answered.
        proxy.on_commit(model.screen)
        proxy.on_iteration()

    proxy.announce_ready(session_id)

    # Starts at "crashed" so that an exception out of the loop is treated as
    # the abort it is, rather than inheriting the clean-stop path's `/exit`.
    outcome = "crashed"
    try:
        outcome = run_loop(
            driver,
            model,
            stdin=stdin,
            on_stdin_line=proxy.on_stdin_line,
            on_user_message=proxy.on_typed,
            on_stdin_eof=proxy.on_stdin_eof,
            on_iteration=_tick,
            send_initial_when=proxy.is_ready_to_send,
            confirm_send_when=proxy.submit_landed,
            send_quiescence_s=SEND_QUIESCENCE_S,
            resubmit_quiescence_s=RESUBMIT_QUIESCENCE_S,
            stop_when=proxy.should_stop,
            activity=activity,
        )
    except Exception as failure:  # noqa: BLE001 — reported, then re-raised
        proxy.fail(f"proxy failed: {failure}")
        raise
    finally:
        # `/exit\r` is only safe on a clean stop. Any other outcome means a
        # turn may be in flight, and the inner appends "/exit" to the pending
        # user message when it writes the transcript — corrupting the session
        # a client may later resume. SIGTERM alone is enough there.
        driver.close(send_exit=(outcome == "stop"))
        stdout.flush()

    # Requirement 11 at the process boundary: whatever cut the loop short,
    # every turn it opened still gets an end-of-turn. A clean stop with
    # nothing outstanding says nothing at all.
    if outcome != "stop" or proxy.has_unfinished_work:
        log.log_text("proxy", f"loop ended: {outcome}")
        proxy.fail(_ABORT_REASONS.get(outcome, outcome))
    stdout.flush()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _, passthrough = _build_parser().parse_known_args(argv)
    return serve(sys.stdin.buffer, sys.stdout, passthrough=passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
