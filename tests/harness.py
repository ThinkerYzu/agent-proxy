"""A proxied session with the pty taken out: what a client submits, what it hears.

`Session` stands in for the io loop and the inner `claude` together. It calls
`Proxy` in the same places and the same order the real loop does — drain the
transcript, then decide whether a queued prompt may be typed — and lets a test
say exactly what the inner writes, when the footer flickers, and how much time
passes. What comes out is the byte stream a client would read, so tests built
on this assert about the protocol rather than about internals.

Shared by `tests/integration` (the live path) and `tests/requirements` (the
SPEC requirements and success criteria), which is why it lives here rather than
next to either.
"""

from __future__ import annotations

import io
import json

from agentproxy.fallback import FallbackEot
from agentproxy.liveness import Liveness
from agentproxy.orphans import OrphanReaper
from agentproxy.protocol import Emitter
from agentproxy.proxy import Proxy
from agentproxy.submit_queue import SubmitQueue
from agentproxy.turn_tracker import TurnTracker


HRULE = "─" * 80
IDLE_FOOTER = "  ? for shortcuts"
ACTIVE_FOOTER = "  esc to interrupt"


# ----------------------------------------------------------------------
# Transcript entries the inner writes
# ----------------------------------------------------------------------


def user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text: str = "ok") -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"output_tokens": 7},
        },
    }


def assistant_thinking(text: str = "weighing two options") -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": text}]},
    }


def assistant_tool_use(tool_use_id: str, description: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Bash",
                    "input": {"command": "make", "description": description},
                }
            ]
        },
    }


def tool_result(tool_use_id: str, content: str) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            ]
        },
    }


def background_launch(tool_use_id: str, task_id: str) -> dict:
    return tool_result(
        tool_use_id,
        f"Command running in background with ID: {task_id}. Output is being "
        "written to: /tmp/build.output.",
    )


def notification(
    task_id: str,
    tool_use_id: str,
    exit_code: int = 0,
    status: str = "completed",
    output_file: str | None = None,
) -> str:
    output_tag = f"<output-file>{output_file}</output-file>\n" if output_file else ""
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"{output_tag}"
        f"<status>{status}</status>\n"
        f'<summary>Background command "make" completed (exit code {exit_code})'
        "</summary>\n"
        "</task-notification>"
    )


def monitor_event(task_id: str, tool_use_id: str, event: str = "line") -> str:
    """A monitor firing an event: a notification with no `<status>` at all."""
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f'<summary>Monitor event: "watching"</summary>\n'
        f"<event>{event}</event>\n"
        "</task-notification>"
    )


def queue_operation(operation: str, content: str | None = None) -> dict:
    entry = {"type": "queue-operation", "operation": operation}
    if content is not None:
        entry["content"] = content
    return entry


def marker(subtype: str = "turn_duration", duration_ms: int = 1200) -> dict:
    return {"type": "system", "subtype": subtype, "durationMs": duration_ms}


# ----------------------------------------------------------------------
# The harness
# ----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Screen:
    def __init__(self, footer: str) -> None:
        self.display = [HRULE, "❯ Try \"fix the bug\"", HRULE, footer] + [""] * 36


class ProcessTree:
    """A scripted process tree standing in for `/proc` and `waitpid`.

    Which pids are the proxy's children, what each one's command line reads,
    and how each one ends, with no processes anywhere. `reaper()` builds an
    `OrphanReaper` reading from it, for a `Session` or on its own.
    """

    def __init__(self) -> None:
        self.children: list[int] = []
        self.commands: dict[int, str | None] = {}
        self.statuses: dict[int, int | None] = {}  # None = still running
        self.signals: list[tuple[int, int]] = []

    def spawn(self, pid: int, command: str | None = "sleep 5") -> None:
        """A process orphaned below the proxy: the kernel lists it as a child."""
        self.children.append(pid)
        self.commands[pid] = command
        self.statuses[pid] = None

    def exits(self, pid: int, code: int = 0) -> None:
        self.statuses[pid] = code << 8

    def dies_from(self, pid: int, sig: int) -> None:
        self.statuses[pid] = sig

    def reaper(self, exclude: tuple[int, ...] = ()) -> OrphanReaper:
        return OrphanReaper(
            exclude,
            list_children=lambda: list(self.children),
            read_command=self.commands.get,
            wait=self._wait,
            kill=self._kill,
            sleep=lambda _s: None,
        )

    def _wait(self, pid: int) -> int | None:
        status = self.statuses[pid]
        if status is not None:
            self.children.remove(pid)
        return status

    def _kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        # Everything scripted here dies to the first signal it is sent.
        self.statuses[pid] = sig


class Session:
    """One proxied session: a scripted inner `claude` and a stand-in io loop."""

    def __init__(self, tree: ProcessTree | None = None, kill_orphans: bool = False) -> None:
        self.clock = Clock()
        self.out = io.StringIO()
        self.tracker = TurnTracker()
        # A `ProcessTree` makes this proxy a subreaper with a scripted set of
        # children; without one the session has no orphan count, as on a host
        # where the flag cannot be set.
        self.tree = tree
        reaper = tree.reaper() if tree is not None else None
        self.queue = SubmitQueue()
        self.activity = {"last_pty": self.clock.now, "last_activity": self.clock.now}
        self._entries: list[dict] = []
        self._payloads: list[dict] = []
        self._read = 0
        self.typed: list[str] = []
        self.footer_idle = True
        # A background task's output file, in memory: tests set
        # `session.task_outputs[path] = "..."` before the task_end that reads it.
        self.task_outputs: dict[str, str] = {}
        # `emitter.outstanding()` is every count the wire carries, orphans
        # included; `tracker.outstanding()` is the transcript's share alone.
        self.emitter = Emitter(
            self.tracker,
            out=self.out,
            extra_counts=(lambda: {"orphan": reaper.count()}) if reaper else None,
        )
        self.proxy = Proxy(
            tracker=self.tracker,
            emitter=self.emitter,
            queue=self.queue,
            liveness=Liveness(clock=self.clock),
            fallback=FallbackEot(clock=self.clock),
            read_entries=self._read_entries,
            read_output=self.task_outputs.get,
            activity=self.activity,
            reaper=reaper,
            kill_orphans=kill_orphans,
        )
        self.proxy.announce_ready("session-under-test")
        # Two frames, because the inner is not considered ready for input until
        # it has painted a second one (see Liveness.has_settled).
        self.tick()
        self.tick()

    # -- the client's side --------------------------------------------

    def submit(self, text: str, ref: object = None) -> str:
        """Send a `submit` and return the turn id the ack carried."""
        command = {"type": "submit", "text": text}
        if ref is not None:
            command["ref"] = ref
        self.send(command)
        return self.messages()[-1]["turn"]["id"]

    def send(self, command: dict) -> None:
        payload = self.proxy.on_stdin_line(json.dumps(command))
        if payload is not None:
            self._payloads.append(payload)

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def new_messages(self) -> list[dict]:
        """Everything written since the last call — one client read, in effect."""
        messages = self.messages()
        fresh = messages[self._read :]
        self._read = len(messages)
        return fresh

    def resume_reading_at(self, index: int) -> list[dict]:
        """Point the client's reader at `index` and hand back the rest.

        A client that stopped reading and came back sees the stream from
        wherever it resumes, not from where it left off — everything in
        between went past unread. `new_messages` continues from there.
        """
        self._read = index
        return self.new_messages()

    def of_type(self, message_type: str) -> list[dict]:
        return [m for m in self.messages() if m["type"] == message_type]

    # -- the inner claude's side --------------------------------------

    def writes(self, *entries: dict) -> None:
        """The inner appends entries to its transcript; the next tick reads them."""
        self._entries.extend(entries)

    def answers(self, prompt: str, *, text: str = "ok") -> None:
        """The whole of an ordinary turn: the echoed prompt, a reply, a marker."""
        self.writes(user_text(prompt), assistant_text(text), marker())

    def _read_entries(self) -> list[dict]:
        entries, self._entries = self._entries, []
        return entries

    # -- the loop ------------------------------------------------------

    def tick(self, advance: float = 0.0, pty_quiet: bool = False) -> None:
        """One pass of the io loop, in the order `cli.py` wires it.

        A frame arriving is also pty bytes arriving, so the clock the fallback
        tier reads for silence moves with it — unless `pty_quiet` says the
        inner painted nothing, which is the state `fallback:quiet` exists for.
        """
        self.clock.advance(advance)
        if not pty_quiet:
            self.activity["last_pty"] = self.clock.now
        self.proxy.on_commit(Screen(IDLE_FOOTER if self.footer_idle else ACTIVE_FOOTER))
        self.proxy.on_iteration()
        if self._payloads and self.proxy.is_ready_to_send():
            payload = self._payloads.pop(0)
            self.typed.append(payload["message"]["content"])
            self.proxy.on_typed(payload)

    def run(self, ticks: int = 4, advance: float = 0.0, pty_quiet: bool = False) -> None:
        for _ in range(ticks):
            self.tick(advance, pty_quiet)
