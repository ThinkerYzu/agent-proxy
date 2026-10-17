"""`agent-proxy --agent codex`: spawn `codex app-server` and serve stdio.

The process boundary for the codex path, as `cli.serve` is for claude: it
owns the app-server's pipes, stdout and the signal handlers, runs startup in
DESIGN Decision 10's order, and hands every decision to `CodexProxy`.

Startup, before `ready`:

1. the arguments become a `CodexPlan`, or the session ends with an `error`
   and exit 2;
2. the proxy becomes the subreaper, and spawns the app-server without an
   API key in its environment;
3. `initialize`, then `account/read`: anything but a ChatGPT account is
   refused, since constraint 1 forbids per-token billing and a key saved by
   `codex login --with-api-key` survives the environment being cleaned;
4. the thread is opened (`thread/list` first for `--continue`), and its id
   is what `ready` names.
"""

from __future__ import annotations

import os
import select
import selectors
import signal
import subprocess
from importlib import metadata
from typing import IO, Optional, Sequence

from agentproxy import inner_codex, log, orphans
from agentproxy.app_server import AppServerError, AppServerPeer
from agentproxy.codex_proxy import CodexProxy
from agentproxy.codex_session import CodexSession
from agentproxy.orphans import OrphanReaper
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


# How long each startup request may take. `thread/start` took 0.8 s cold in
# the probes; this only has to catch an app-server that will never answer.
STARTUP_TIMEOUT_S = float(os.environ.get("AGENTPROXY_CODEX_STARTUP_S", "60"))
# The codex executable; a test points it at a fake app-server.
CODEX_BIN = os.environ.get("AGENTPROXY_CODEX_BIN", "codex")
# Grace between SIGTERM and SIGKILL for the app-server, as for the inner claude.
CLOSE_GRACE_S = 2.0
SELECT_TIMEOUT_S = 0.2

_ABORT_REASONS = {
    "server_eof": "codex app-server exited",
    "stdin_eof": "client closed stdin mid-turn",
    "signal": "interrupted by signal",
    "crashed": "the proxy stopped unexpectedly",
    "failed": "the codex session failed",
}


def _version() -> str:
    try:
        return metadata.version("agent-proxy")
    except metadata.PackageNotFoundError:
        return "0"


def _startup_error(stdout: IO[str], reason: str) -> int:
    """Report a session that could not be opened: one `error`, exit 2, no `ready`."""
    Emitter(TurnTracker(), out=stdout).emit({"type": "error", "error": reason})
    stdout.flush()
    return 2


def _close(server: subprocess.Popen) -> None:
    """Stop the app-server: SIGTERM, a grace period, then SIGKILL."""
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(CLOSE_GRACE_S)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def serve_codex(
    stdin: IO[bytes],
    stdout: IO[str],
    passthrough: Sequence[str] = (),
) -> int:
    """Run one codex-backed session, from spawn to the last message on stdout.

    Returns 0 for a session that ran and 2 for one that could not be opened
    (bad arguments, no codex, the wrong account, an unknown thread), which
    is reported as a lone `error` with no `ready` before it.
    """
    cwd = os.getcwd()
    try:
        plan = inner_codex.plan(passthrough, cwd, binary=CODEX_BIN)
    except inner_codex.CodexArgumentError as bad:
        return _startup_error(stdout, str(bad))

    subreaper_problem = orphans.become_subreaper()
    if subreaper_problem is not None:
        log.log_text("proxy", f"not a subreaper, orphans uncounted: {subreaper_problem}")

    argv = plan.server_argv
    try:
        server = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=inner_codex.build_env(dict(os.environ)),
        )
    except FileNotFoundError:
        return _startup_error(stdout, f"{argv[0]!r} not found on PATH")
    assert server.stdin is not None and server.stdout is not None
    server_out = server.stdout.fileno()

    def write(data: bytes) -> None:
        server.stdin.write(data)
        server.stdin.flush()

    def read(timeout: float) -> Optional[bytes]:
        ready, _, _ = select.select([server_out], [], [], timeout)
        if not ready:
            return b""
        data = os.read(server_out, 65536)
        return data or None

    peer = AppServerPeer(write)
    try:
        thread_id = _open_thread(peer, plan, read, cwd)
    except (AppServerError, OSError) as failure:
        _close(server)
        return _startup_error(stdout, str(failure))

    tracker = TurnTracker()
    reaper = None if subreaper_problem else OrphanReaper(exclude=(server.pid,))
    proxy = CodexProxy(
        tracker=tracker,
        emitter=Emitter(
            tracker,
            out=stdout,
            extra_counts=(lambda: {"orphan": reaper.count()}) if reaper else None,
        ),
        queue=SubmitQueue(),
        session=CodexSession(tracker, thread_id),
        peer=peer,
        reaper=reaper,
        kill_orphans=os.environ.get("AGENTPROXY_KILL_ORPHANS", "").lower() in ("1", "true", "yes"),
    )
    proxy.announce_ready(thread_id)
    # Whatever the server said while startup waited on its own answers.
    proxy.on_messages(peer.feed(b""))

    outcome = "crashed"
    try:
        outcome = _run(proxy, peer, stdin, server_out)
    except Exception as failure:  # noqa: BLE001 — reported, then re-raised
        proxy.fail(f"proxy failed: {failure}")
        raise
    finally:
        _close(server)
        stdout.flush()

    if outcome != "stop" or proxy.has_unfinished_work:
        log.log_text("proxy", f"loop ended: {outcome}")
        proxy.fail(_ABORT_REASONS.get(outcome, outcome))
    stdout.flush()
    return 0


def _open_thread(peer: AppServerPeer, plan: inner_codex.CodexPlan, read, cwd: str) -> str:
    """Steps 3 and 4 of startup; returns the thread id `ready` will name."""
    peer.call(
        "initialize",
        {"clientInfo": {"name": "agent-proxy", "version": _version()}},
        read,
        STARTUP_TIMEOUT_S,
    )
    peer.notify("initialized")
    account = peer.call("account/read", {}, read, STARTUP_TIMEOUT_S).get("account") or {}
    if account.get("type") != "chatgpt":
        kind = account.get("type") or "no account"
        raise AppServerError(
            f"codex is signed in with {kind}, not ChatGPT; agent-proxy will not "
            "bill per token (run `codex login` with ChatGPT)"
        )
    if plan.latest:
        found = peer.call("thread/list", inner_codex.list_params(cwd), read, STARTUP_TIMEOUT_S)
        plan = inner_codex.with_latest(plan, found)
    opened = peer.call(plan.open_method, plan.open_params, read, STARTUP_TIMEOUT_S)
    thread_id = (opened.get("thread") or {}).get("id")
    if not isinstance(thread_id, str):
        raise AppServerError(f"{plan.open_method} returned no thread id")
    return thread_id


def _run(proxy: CodexProxy, peer: AppServerPeer, stdin: IO[bytes], server_out: int) -> str:
    """The loop: stdin, the app-server's stdout and the signal pipe, one thread.

    Returns why it stopped: `stop` (client gone, nothing owed), `stdin_eof`,
    `server_eof`, `signal` or `failed`. Stdin is read raw and split here,
    for `io_loop`'s reason: a buffered reader can hold a line `select`
    cannot see.
    """
    sel = selectors.DefaultSelector()
    sel.register(stdin, selectors.EVENT_READ, "stdin")
    sel.register(server_out, selectors.EVENT_READ, "server")
    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)
    sel.register(sig_r, selectors.EVENT_READ, "signal")

    def on_signal(_signo, _frame):
        try:
            os.write(sig_w, b"\x00")
        except OSError:
            pass

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    stdin_buffer = b""
    stdin_open = True
    try:
        while True:
            for key, _ in sel.select(SELECT_TIMEOUT_S):
                if key.data == "signal":
                    return "signal"
                if key.data == "server":
                    data = os.read(server_out, 65536)
                    if not data:
                        return "server_eof"
                    proxy.on_messages(peer.feed(data))
                elif key.data == "stdin":
                    data = os.read(stdin.fileno(), 65536)
                    if not data:
                        sel.unregister(stdin)
                        stdin_open = False
                        proxy.on_stdin_eof()
                        continue
                    *lines, stdin_buffer = (stdin_buffer + data).split(b"\n")
                    for raw in lines:
                        text = raw.strip().decode("utf-8", errors="replace")
                        if text:
                            log.log_text("stdin.in", text)
                            proxy.on_stdin_line(text)
            proxy.on_tick()
            if proxy.failed:
                return "failed"
            if not stdin_open and proxy.should_stop():
                return "stop"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        sel.close()
        os.close(sig_r)
        os.close(sig_w)
