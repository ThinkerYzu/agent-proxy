"""A codex-backed session with the app-server replaced by `FakeAppServer`, in memory.

The codex counterpart of `harness.py`: a real `CodexProxy`, `CodexSession`,
`TurnTracker`, `SubmitQueue`, `Emitter` and `AppServerPeer`, with the
subprocess taken out and a fake clock in. Each `tick` lets the fake server
move on one step, hands the proxy whatever it said, and runs the proxy's own
tick. No subreaper: the orphan count is the kernel's and is tested apart
(Phase 4), and setting the flag in the test process would adopt every other
test's orphans.
"""

from __future__ import annotations

import io
import json

from agentproxy.app_server import AppServerPeer
from agentproxy.codex_proxy import CodexProxy
from agentproxy.codex_session import CodexSession
from agentproxy.protocol import Emitter
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker
from tests.fake_app_server import FakeAppServer

TICK_S = 0.05


class CodexSessionUnderTest:
    """One proxied codex session, driven a tick at a time."""

    def __init__(self, stall_after_s: float = 1800.0, unmatched_after_s: float = 60.0) -> None:
        self.server = FakeAppServer()
        self.out = io.StringIO()
        self.now = 0.0
        self._read = 0
        self.tracker = TurnTracker()
        self.peer = AppServerPeer(self.server.receive)
        self.peer.request("thread/start", {"cwd": "/w", "approvalPolicy": "never"})
        (opened,) = [m for m in self.peer.feed(self.server.take_output()) if hasattr(m, "result")]
        thread_id = opened.result["thread"]["id"]
        self.emitter = Emitter(self.tracker, out=self.out)
        self.proxy = CodexProxy(
            tracker=self.tracker,
            emitter=self.emitter,
            queue=SubmitQueue(),
            session=CodexSession(self.tracker, thread_id),
            peer=self.peer,
            clock=lambda: self.now,
            stall_after_s=stall_after_s,
            unmatched_after_s=unmatched_after_s,
        )
        self.proxy.announce_ready(thread_id)
        self.proxy.on_messages(self.peer.feed(self.server.take_output()))

    def send(self, command: dict) -> None:
        self.proxy.on_stdin_line(json.dumps(command))

    def submit(self, text: str, ref: object = None) -> None:
        command = {"type": "submit", "text": text}
        if ref is not None:
            command["ref"] = ref
        self.send(command)

    def tick(self, ticks: int = 1, server: bool = True) -> None:
        """Advance `ticks` steps; with `server` False the app-server says nothing."""
        for _ in range(ticks):
            if server:
                self.server.tick()
                self.proxy.on_messages(self.peer.feed(self.server.take_output()))
            self.now += TICK_S
            self.proxy.on_tick()

    def run_until_idle(self, max_ticks: int = 200) -> None:
        """Tick until nothing is outstanding, or fail."""
        for _ in range(max_ticks):
            self.tick()
            if not self.proxy.has_unfinished_work:
                return
        raise AssertionError(f"still busy after {max_ticks} ticks: {self.emitter.outstanding()}")

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def new_messages(self) -> list[dict]:
        everything = self.messages()
        fresh, self._read = everything[self._read:], len(everything)
        return fresh

    def turn_starts_sent(self) -> list[dict]:
        return [m for m in self.server.received if m.get("method") == "turn/start"]
