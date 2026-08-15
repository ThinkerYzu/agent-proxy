"""Thin wrapper around `ptyprocess.PtyProcess` for the inner `claude` TUI.

Geometry is locked at spawn (120 cols x 40 rows) and never resized; `SIGWINCH`
from the outer caller is intentionally not forwarded. The pty master fd is
flipped non-blocking so the `selectors` loop can drain it on EVENT_READ
without risk of stalling the single-threaded loop.

Hardening flags are applied here in `argv` / `env` rather than at the call
site so the wrapper has a single, auditable hardening surface.
"""

from __future__ import annotations

import errno
import os
import signal
import time
from typing import Optional, Sequence

import ptyprocess

from agentproxy import log


DEFAULT_COLS = 120
DEFAULT_ROWS = 40


class PtyDriver:
    """Owns the inner-`claude` pty pair; exposes non-blocking IO."""

    def __init__(
        self,
        argv: Sequence[str],
        env: dict[str, str],
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
    ) -> None:
        self._proc = ptyprocess.PtyProcess.spawn(
            list(argv),
            env=dict(env),
            dimensions=(rows, cols),
        )
        self._fd = self._proc.fd
        os.set_blocking(self._fd, False)

    @property
    def fd(self) -> int:
        return self._fd

    @property
    def pid(self) -> int:
        return self._proc.pid

    def read(self, n: int) -> bytes:
        """Non-blocking read. Returns ``b""`` on EAGAIN or on pty EOF."""
        try:
            data = os.read(self._fd, n)
        except BlockingIOError:
            return b""
        except OSError as exc:
            # Linux raises EIO on the master side after the child closes its
            # controlling tty; treat that as EOF rather than an error.
            if exc.errno in (errno.EIO,):
                return b""
            raise
        log.log_bytes("pty.in", data)
        return data

    def write(self, data: bytes) -> int:
        log.log_bytes("pty.out", data)
        return os.write(self._fd, data)

    def send_signal(self, sig: int) -> None:
        try:
            self._proc.kill(sig)
        except (OSError, ProcessLookupError):
            pass

    def is_alive(self) -> bool:
        return self._proc.isalive()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Block (up to ``timeout`` seconds) for the child to exit.

        Returns the exit status, or ``None`` if the timeout expires while the
        child is still alive.
        """
        if timeout is None:
            self._proc.wait()
            return self._proc.exitstatus
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._proc.isalive():
                return self._proc.exitstatus
            try:
                time.sleep(0.05)
            except (KeyboardInterrupt, SystemExit):
                # Signal delivered during grace-period sleep — stop waiting;
                # the caller's signal handler will run after we return.
                break
        return None

    def close(self, grace: float = 2.0, send_exit: bool = True) -> Optional[int]:
        """Graceful shutdown: optionally send ``/exit\\r`` first, then ``SIGTERM``.

        ``send_exit=False`` skips the ``/exit\\r`` write and goes straight to
        SIGTERM. Use this when a turn is in flight: the inner TUI accumulates
        PTY bytes during an API call and would append "/exit" to the pending
        user message when writing the transcript, corrupting the session.

        Returns the child's exit status if it terminated within the grace
        window plus a brief post-SIGTERM wait, otherwise ``None``.
        """
        if self._proc.isalive():
            if send_exit:
                try:
                    os.write(self._fd, b"/exit\r")
                except OSError:
                    pass
                self.wait(timeout=grace)
        if self._proc.isalive():
            self.send_signal(signal.SIGTERM)
            self.wait(timeout=1.0)
        try:
            self._proc.close(force=True)
        except OSError:
            pass
        return self._proc.exitstatus
