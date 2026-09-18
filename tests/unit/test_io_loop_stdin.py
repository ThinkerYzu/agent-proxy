"""How the io loop reads its stdin, and the buffering trap underneath it."""

from __future__ import annotations

import os
from typing import Callable, Optional

from agentproxy.io_loop import run


class SilentPty:
    """A pty that never says anything, so only stdin drives the loop."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()

    @property
    def fd(self) -> int:
        return self._read_fd

    def read(self, _n: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        for fd in (self._read_fd, self._write_fd):
            os.close(fd)


class DeafScreen:
    def feed(self, data: bytes) -> None:
        pass


def drive(
    payload: bytes,
    expected: int,
    *,
    close_stdin: bool = False,
    on_iteration: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """Run the loop over `payload` on stdin and collect the lines it hands back.

    The writing end stays open unless `close_stdin` says otherwise, because
    holding it open is what makes this a test of reading rather than of EOF.
    `on_iteration` is called with the write fd and the tick number, for a test
    that needs to write more once the loop is already running.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    if close_stdin:
        os.close(write_fd)
    driver = SilentPty()
    seen: list[str] = []
    ticks = [0]

    def take(line: str) -> None:
        seen.append(line)
        return None

    def tick() -> None:
        ticks[0] += 1
        if on_iteration is not None:
            on_iteration(write_fd, ticks[0])

    # Buffered on purpose: `cli.py` hands the loop `sys.stdin.buffer`, and the
    # bug these tests guard lives in that buffering. A raw, unbuffered stream
    # would hide it.
    with open(read_fd, "rb") as stdin:
        try:
            run(
                driver,
                DeafScreen(),
                stdin=stdin,
                on_stdin_line=take,
                on_iteration=tick,
                stop_when=lambda: len(seen) >= expected,
                # A backstop only: the loop is meant to leave on `stop_when`,
                # and reaching this means it did not.
                max_duration=3.0,
                idle_timeout_when=lambda: True,
            )
        finally:
            if not close_stdin:
                os.close(write_fd)
            driver.close()
    return seen


def test_two_messages_written_at_once_are_both_read():
    """The regression that cost a live session 100 seconds.

    A client that submits twice and then goes quiet writes both lines in one
    go, and keeps its pipe open. Read through a buffered `readline()`, the
    second line lands in Python's buffer — where `select`, which watches the
    fd, cannot see it — and stays there until *more* bytes arrive. The queued
    prompt this whole protocol exists to serve was the case that broke.
    """
    lines = drive(b'{"type":"status"}\n{"type":"submit"}\n', expected=2)
    assert lines == ['{"type":"status"}', '{"type":"submit"}']


def test_a_line_split_across_two_writes_is_reassembled():
    def finish_the_line(write_fd: int, tick: int) -> None:
        if tick == 2:
            os.write(write_fd, b'tus"}\n')

    lines = drive(b'{"type":"sta', expected=1, on_iteration=finish_the_line)
    assert lines == ['{"type":"status"}']


def test_a_last_line_with_no_newline_is_not_dropped():
    """Nothing guarantees a client's final write ends in a newline."""
    lines = drive(b'{"type":"status"}', expected=1, close_stdin=True)
    assert lines == ['{"type":"status"}']


# ----------------------------------------------------------------------
# The send veto — a withdrawn turn's payload types nothing
# ----------------------------------------------------------------------


class RecordingPty(SilentPty):
    def __init__(self) -> None:
        super().__init__()
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)


def test_a_message_the_callback_vetoes_is_dropped_before_the_paste():
    """`on_user_message` answering False is a `cancel` that withdrew the
    turn while its payload waited: nothing reaches the pty, and the loop
    moves on to the next message rather than stalling on the dropped one."""
    read_fd, write_fd = os.pipe()
    os.write(
        write_fd,
        b'{"type":"user","message":{"role":"user","content":"withdrawn"}}\n'
        b'{"type":"user","message":{"role":"user","content":"kept"}}\n',
    )
    driver = RecordingPty()
    sent: list[str] = []

    def veto_withdrawn(msg: dict) -> Optional[bool]:
        text = msg["message"]["content"]
        sent.append(text)
        return False if text == "withdrawn" else None

    with open(read_fd, "rb") as stdin:
        try:
            run(
                driver,
                DeafScreen(),
                stdin=stdin,
                on_user_message=veto_withdrawn,
                stop_when=lambda: "kept" in sent,
                max_duration=3.0,
                idle_timeout_when=lambda: True,
            )
        finally:
            os.close(write_fd)
            driver.close()
    assert sent == ["withdrawn", "kept"]
    assert not any(b"withdrawn" in chunk for chunk in driver.written)
    assert any(b"kept" in chunk for chunk in driver.written)
