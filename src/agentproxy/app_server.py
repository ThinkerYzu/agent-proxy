"""JSON-RPC with a `codex app-server` child, over its stdin and stdout.

The wire only: request ids, matching each response to the request it
answers, telling a notification from a request the server makes of us, and
buffering partial lines. What any of it means is `codex_session.py`'s job.

The app-server speaks one JSON object per line and omits the `"jsonrpc":
"2.0"` member, so neither is sent or expected. Bytes are fed in as the loop
reads them, rather than read here, for the reason stdin is read raw
(`io_loop`): a buffered `readline` can hold a second message where `select`
cannot see it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from agentproxy import log


@dataclass
class Response:
    """The answer to a request this side made.

    `method` is the request's own, recorded when it was sent, so a caller
    can act on a response without keeping its own id table. Exactly one of
    `result` and `error` is set.
    """

    id: int
    method: str
    result: Optional[dict] = None
    error: Optional[dict] = None


@dataclass
class Notification:
    """Something the server reports, needing no answer."""

    method: str
    params: dict = field(default_factory=dict)


@dataclass
class ServerRequest:
    """Something the server asks of this side, which must be answered.

    An unanswered one leaves the server waiting, and a turn waiting on it
    never ends.
    """

    id: object
    method: str
    params: dict = field(default_factory=dict)


Message = Union[Response, Notification, ServerRequest]


class AppServerError(Exception):
    """A blocking call that failed: the server answered with an error, or never answered."""


class AppServerPeer:
    """One side of the JSON-RPC conversation with an app-server.

    `write` sends bytes to the server's stdin. Nothing here reads; the
    caller passes what it read to `feed`, or uses `call` before its own loop
    has started.
    """

    def __init__(self, write: Callable[[bytes], None]) -> None:
        self._write = write
        self._next_id = 0
        self._pending: dict[int, str] = {}
        self._buffer = b""
        # Messages `call` read past while waiting for its own response. The
        # loop takes them first, so nothing said during startup is lost.
        self._backlog: list[Message] = []

    @property
    def pending(self) -> dict[int, str]:
        """Requests sent and not yet answered, id to method."""
        return dict(self._pending)

    def request(self, method: str, params: Optional[dict] = None) -> int:
        """Send a request and return its id; the answer arrives through `feed`."""
        self._next_id += 1
        self._pending[self._next_id] = method
        message: dict = {"method": method, "id": self._next_id}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self._next_id

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a notification, which the server does not answer."""
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def respond(self, request_id: object, result: dict) -> None:
        """Answer a `ServerRequest`."""
        self._send({"id": request_id, "result": result})

    def respond_error(self, request_id: object, code: int, message: str) -> None:
        """Refuse a `ServerRequest` this side cannot serve at all."""
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def feed(self, data: bytes) -> list[Message]:
        """Take bytes read from the server and return the whole messages they complete.

        A partial last line is kept for the next call. A line that is not a
        JSON object, or a response to no request this side sent, is logged
        and skipped: one bad line must not stop the session.
        """
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        messages = self._backlog
        self._backlog = []
        for line in lines:
            message = self._parse(line)
            if message is not None:
                messages.append(message)
        return messages

    def call(
        self,
        method: str,
        params: Optional[dict],
        read: Callable[[float], bytes],
        timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict:
        """Send a request and wait for its result, for use before the loop starts.

        `read(seconds)` returns what the server wrote within that long, b""
        if nothing, or None once the server has closed its output. Other messages that arrive meanwhile are kept for the
        next `feed`. Raises `AppServerError` on an error response, on the
        server closing its stdout, or on no answer within `timeout`.
        """
        request_id = self.request(method, params)
        deadline = clock() + timeout
        held: list[Message] = []
        try:
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise AppServerError(f"{method}: no answer within {timeout:g}s")
                data = read(remaining)
                if data is None:
                    raise AppServerError(f"{method}: the app-server closed its output")
                for message in self.feed(data):
                    if isinstance(message, Response) and message.id == request_id:
                        if message.error is not None:
                            raise AppServerError(
                                f"{method}: {message.error.get('message', message.error)}"
                            )
                        return message.result or {}
                    held.append(message)
        finally:
            self._backlog = held + self._backlog

    def _send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        log.log_text("app-server.in", line)
        self._write(line.encode("utf-8") + b"\n")

    def _parse(self, line: bytes) -> Optional[Message]:
        """One line to one message, or None for a line that is not one."""
        if not line.strip():
            return None
        text = line.decode("utf-8", errors="replace")
        log.log_text("app-server.out", text)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            log.log_text("app-server", f"not JSON: {text[:200]}")
            return None
        if not isinstance(raw, dict):
            log.log_text("app-server", f"not a JSON object: {text[:200]}")
            return None
        method = raw.get("method")
        if "id" in raw and method is None:
            request_method = self._pending.pop(raw["id"], None)
            if request_method is None:
                log.log_text("app-server", f"response to no request: {text[:200]}")
                return None
            return Response(raw["id"], request_method, raw.get("result"), raw.get("error"))
        if not isinstance(method, str):
            log.log_text("app-server", f"no method: {text[:200]}")
            return None
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        if "id" in raw:
            return ServerRequest(raw["id"], method, params)
        return Notification(method, params)
