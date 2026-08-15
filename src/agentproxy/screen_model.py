"""pyte screen emulator with a frame-commit hook on mode-2026 ESU.

The inner `claude` brackets every visual update in `\\e[?2026h … \\e[?2026l`
(DEC private mode 2026, Synchronized Output). ESU (`\\e[?2026l`) is the signal
"the frame is settled, safe to read." We hook it on the Screen subclass so the
commit notification fires synchronously inside `Stream.feed()`, before any
following bytes from the same buffer advance the screen further.

The hook lives on the Screen rather than the Stream because pyte's Stream has
no `reset_mode` method — the dispatcher calls `listener.reset_mode(...)` on the
attached Screen, with bindings frozen at `attach()` time via `getattr`. So the
Screen-subclass override is what pyte actually invokes.
"""

from __future__ import annotations

from typing import Callable, Optional

import pyte

from agentproxy import terminal_queries


CommitCallback = Callable[["_CommitScreen"], None]
QueryResponder = Callable[[bytes], object]


class _CommitScreen(pyte.HistoryScreen):
    def __init__(
        self,
        columns: int,
        lines: int,
        history: int,
        on_commit: CommitCallback,
    ) -> None:
        super().__init__(columns, lines, history=history)
        self._on_commit = on_commit

    def reset_mode(self, *modes: int, private: bool = False) -> None:
        super().reset_mode(*modes, private=private)
        if private and 2026 in modes:
            self._on_commit(self)


class ScreenModel:
    """Pairs a `_CommitScreen` with a `pyte.ByteStream` and exposes `feed()`."""

    def __init__(
        self,
        on_commit: CommitCallback,
        cols: int = 120,
        rows: int = 40,
        history: int = 10_000,
        on_query: Optional[QueryResponder] = None,
    ) -> None:
        self.screen = _CommitScreen(cols, rows, history, on_commit)
        self.stream = pyte.ByteStream(self.screen)
        self._on_query = on_query

    def feed(self, data: bytes) -> None:
        # Reply to terminal capability queries before feeding the bytes
        # into pyte. Replying first means a query and its response are
        # adjacent in the log (`pty.in` -> `pty.out`) and removes any
        # chance pyte's parser eats the query without notifying us.
        # See terminal_queries.py for the why.
        if self._on_query is not None:
            for response in terminal_queries.scan(data):
                self._on_query(response)
        self.stream.feed(data)
