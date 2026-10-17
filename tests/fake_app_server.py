"""A stand-in for `codex app-server`, speaking the message shapes the probes recorded.

Built from shapes, not captured (the fixture rule in TESTING-GUIDE): every
message below has the form `codex` 0.163.0 sent in SPEC finding 10's probes,
with short ids and no conversation from a real session. It also reproduces
the three codex behaviours the design depends on: a `turn/start` sent
mid-turn is folded into the running turn; an interrupted turn's command
keeps running and is reported later under the dead turn's id; and a
subagent's own turns arrive on the same stream under its thread id.

What a turn does is chosen by its prompt, a space-separated list of steps:

    reply <word>    an agent message
    run <ticks>     a command that takes that many ticks to finish
    approve         ask for approval, then reply with the decision received
    spawn           a subagent that answers on the next tick
    fail            end the turn `failed`
    wake            after this turn ends, start one nobody asked for

Time is ticks: `tick()` advances one. In-process, the test harness ticks it;
as a program (`python fake_app_server.py app-server`), it ticks every 50 ms
over stdio, for tests that run the real `agent-proxy` binary.

`FAKE_CODEX_ACCOUNT` sets the account `account/read` reports (default
`chatgpt`; `apiKey` or `none` to test the refusal).
"""

from __future__ import annotations

import json
import os
import select
import sys
from typing import Callable, Optional

THREAD = "th-fake"
KNOWN_THREADS = {THREAD, "th-old"}


class FakeAppServer:
    """The server side of the JSON-RPC conversation, one thread, in memory."""

    def __init__(self, account: Optional[str] = "chatgpt") -> None:
        self._account = account
        self._out: list[dict] = []
        self._buffer = b""
        self._now = 0
        self._due: list[tuple[int, Callable[[], None]]] = []
        self._turn_seq = 0
        self._item_seq = 0
        self._request_seq = 0
        self._active: Optional[str] = None
        self._steps_left: list[str] = []
        self._open_commands: dict[str, tuple[str, int]] = {}
        self._waiting_approval: dict[object, str] = {}
        self.thread_id: Optional[str] = None
        self.received: list[dict] = []
        self.answers: list[dict] = []

    # ------------------------------------------------------------------
    # The wire
    # ------------------------------------------------------------------

    def receive(self, data: bytes) -> None:
        """Bytes the proxy wrote to the server's stdin."""
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if line.strip():
                self._handle(json.loads(line))

    def take_output(self) -> bytes:
        """Everything the server has said since the last call, as wire bytes."""
        out, self._out = self._out, []
        return b"".join(json.dumps(m).encode() + b"\n" for m in out)

    def tick(self) -> None:
        self._now += 1
        due = [action for when, action in self._due if when <= self._now]
        self._due = [(when, action) for when, action in self._due if when > self._now]
        for action in due:
            action()

    def _later(self, ticks: int, action: Callable[[], None]) -> None:
        self._due.append((self._now + ticks, action))

    def _say(self, method: str, **params) -> None:
        self._out.append({"method": method, "params": params})

    def _answer(self, request_id, result=None, error=None) -> None:
        message = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self._out.append(message)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _handle(self, message: dict) -> None:
        self.received.append(message)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method is None:
            self.answers.append(message)
            self._on_answer(message)
            return
        if method == "initialize":
            self._answer(request_id, {"userAgent": "fake/0", "codexHome": "/tmp/fake"})
        elif method == "initialized":
            pass
        elif method == "account/read":
            account = None if self._account in (None, "none") else {"type": self._account}
            if account and account["type"] == "chatgpt":
                account.update(email=None, planType="business")
            self._answer(request_id, {"account": account, "requiresOpenaiAuth": True})
        elif method == "thread/list":
            self._answer(request_id, {"data": [{"id": "th-old"}], "nextCursor": None})
        elif method in ("thread/start", "thread/resume", "thread/fork"):
            wanted = params.get("threadId")
            if method != "thread/start" and wanted not in KNOWN_THREADS:
                self._answer(request_id, error={"code": -32600, "message": f"thread {wanted} not found"})
                return
            self.thread_id = THREAD if method != "thread/resume" else wanted
            self._answer(request_id, {"thread": {"id": self.thread_id, "status": {"type": "idle"}}})
            self._say("thread/started", thread={"id": self.thread_id})
        elif method == "turn/start":
            self._on_turn_start(request_id, params)
        elif method == "turn/interrupt":
            self._answer(request_id, {})
            if params.get("turnId") == self._active:
                self._end_turn("interrupted")
        else:
            self._answer(request_id, error={"code": -32601, "message": f"no {method}"})

    def _on_turn_start(self, request_id, params: dict) -> None:
        text = " ".join(part.get("text", "") for part in params.get("input") or [])
        if self._active is not None:
            # Codex folds a mid-turn prompt into the running turn.
            self._answer(request_id, {"turn": {"id": self._active, "status": "inProgress"}})
            self._user_message(self._active, text)
            return
        turn_id = self._open_turn()
        self._answer(request_id, {"turn": {"id": turn_id, "status": "inProgress"}})
        self._say_turn_started(turn_id)
        self._user_message(turn_id, text)
        self._steps_left = text.split()
        self._later(1, self._next_step)

    def _on_answer(self, message: dict) -> None:
        turn_id = self._waiting_approval.pop(message.get("id"), None)
        if turn_id is None or turn_id != self._active:
            return
        decision = (message.get("result") or {}).get("decision", "none")
        self._agent_message(turn_id, f"approval:{decision}")
        self._later(1, self._next_step)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def _open_turn(self) -> str:
        self._turn_seq += 1
        self._active = f"u{self._turn_seq}"
        self._say("thread/status/changed", threadId=self.thread_id, status={"type": "active", "activeFlags": []})
        return self._active

    def _say_turn_started(self, turn_id: str, thread: Optional[str] = None) -> None:
        self._say("turn/started", threadId=thread or self.thread_id, turn={"id": turn_id, "status": "inProgress", "items": []})

    def _item(self, phase: str, turn_id: str, item: dict, thread: Optional[str] = None) -> None:
        self._say(f"item/{phase}", threadId=thread or self.thread_id, turnId=turn_id, item=item)

    def _next_id(self, prefix: str) -> str:
        self._item_seq += 1
        return f"{prefix}-{self._item_seq}"

    def _user_message(self, turn_id: str, text: str) -> None:
        item = {"type": "userMessage", "id": self._next_id("p"), "clientId": None,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        self._item("started", turn_id, item)
        self._item("completed", turn_id, item)

    def _agent_message(self, turn_id: str, text: str, thread: Optional[str] = None) -> None:
        item = {"type": "agentMessage", "id": self._next_id("msg"), "text": "", "phase": "final_answer"}
        self._item("started", turn_id, item, thread)
        self._item("completed", turn_id, {**item, "text": text}, thread)
        self._say("thread/tokenUsage/updated", threadId=thread or self.thread_id, turnId=turn_id,
                  tokenUsage={"last": {"outputTokens": 3}, "total": {"outputTokens": 3}})

    def _next_step(self) -> None:
        turn_id = self._active
        if turn_id is None:
            return
        if not self._steps_left:
            self._end_turn("completed")
            return
        step = self._steps_left.pop(0)
        if step == "reply":
            self._agent_message(turn_id, self._steps_left.pop(0) if self._steps_left else "")
            self._later(1, self._next_step)
        elif step == "run":
            ticks = int(self._steps_left.pop(0)) if self._steps_left else 1
            item_id = self._next_id("exec")
            self._open_commands[item_id] = (turn_id, ticks)
            self._item("started", turn_id, self._command(item_id, "inProgress"))
            self._later(ticks, lambda: self._finish_command(item_id))
        elif step == "approve":
            self._request_seq += 1
            self._waiting_approval[self._request_seq] = turn_id
            self._out.append({"id": self._request_seq, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": self.thread_id, "turnId": turn_id, "itemId": "exec-a"}})
        elif step == "spawn":
            self._subagent(turn_id)
        elif step == "fail":
            self._end_turn("failed", error={"message": "fake failure"})
        elif step == "wake":
            self._wake_after = True
            self._later(1, self._next_step)
        else:
            self._later(1, self._next_step)

    def _command(self, item_id: str, status: str, **extra) -> dict:
        return {"type": "commandExecution", "id": item_id, "command": "/bin/bash -lc 'sleep 1'",
                "cwd": "/w", "processId": "4773", "source": "unifiedExecStartup", "status": status, **extra}

    def _finish_command(self, item_id: str) -> None:
        turn_id, ticks = self._open_commands.pop(item_id)
        self._item("completed", turn_id, self._command(item_id, "completed", exitCode=0,
                                                       aggregatedOutput="done\n", durationMs=ticks * 50))
        if turn_id == self._active:
            self._later(1, self._next_step)

    def _subagent(self, turn_id: str) -> None:
        sub_thread = "th-sub"
        activity = {"type": "subAgentActivity", "agentThreadId": sub_thread, "agentPath": "/root/pong",
                    "model": "fake", "reasoningEffort": "medium"}
        started = {**activity, "id": self._next_id("call"), "kind": "started"}
        self._item("started", turn_id, started)
        self._item("completed", turn_id, started)

        def answer() -> None:
            self._say_turn_started("s1", thread=sub_thread)
            self._agent_message("s1", "pong", thread=sub_thread)
            self._say("turn/completed", threadId=sub_thread, turn={"id": "s1", "status": "completed", "items": []})
            done = {**activity, "id": "subagent-completed-s1", "kind": "completed"}
            self._item("started", turn_id, done)
            self._item("completed", turn_id, done)
            self._later(1, self._next_step)

        self._later(1, answer)

    _wake_after = False

    def _end_turn(self, status: str, error: Optional[dict] = None) -> None:
        turn_id = self._active
        self._active = None
        self._steps_left = []
        self._say("thread/status/changed", threadId=self.thread_id, status={"type": "idle"})
        self._say("turn/completed", threadId=self.thread_id,
                  turn={"id": turn_id, "status": status, "error": error, "items": [], "durationMs": 100})
        if self._wake_after:
            self._wake_after = False
            self._later(2, self._wake)

    def _wake(self) -> None:
        if self._active is not None:
            return
        turn_id = self._open_turn()
        self._say_turn_started(turn_id)
        self._agent_message(turn_id, "woke")
        self._later(1, lambda: self._end_turn("completed"))


def main() -> int:
    """Serve over stdio, ticking every 50 ms, until stdin closes."""
    account = os.environ.get("FAKE_CODEX_ACCOUNT", "chatgpt")
    if "OPENAI_API_KEY" in os.environ or "CODEX_API_KEY" in os.environ:
        # What real codex would do with a key in its environment: bill it.
        account = "apiKey"
    server = FakeAppServer(account)
    stdin = sys.stdin.fileno()
    while True:
        ready, _, _ = select.select([stdin], [], [], 0.05)
        if ready:
            data = os.read(stdin, 65536)
            if not data:
                return 0
            server.receive(data)
        else:
            server.tick()
        out = server.take_output()
        if out:
            os.write(sys.stdout.fileno(), out)


if __name__ == "__main__":
    raise SystemExit(main())
