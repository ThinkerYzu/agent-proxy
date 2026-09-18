"""The turn table, the background-task table, and the rules that move them.

Everything the protocol says about turns is decided here. The tracker takes
parsed transcript entries and client submissions, and returns the messages
those imply; it does no I/O and knows nothing about stdout, which is what
makes it testable by replaying a recorded session.

Three rules carry the design:

- **Provenance, not content.** The proxy is the only thing that
  types client prompts into the TUI, so a turn is `user` when its initiating
  entry matches the prompt we typed and `unowned` otherwise. No rule here
  inspects an entry to decide whether it "looks like" a prompt.
- **Two-tier end-of-turn.** Tier 1 — a recognised transcript
  marker — lives here. Tier 2, the TUI-idle fallback, arrives with the live
  pty and calls `end_running_turn` from the outside.
- **Derived counters.** `outstanding()` counts the tables; no
  integer is ever incremented, so no signal can be missed.

Events come back as plain dicts carrying `type`, an optional `turn`, and the
type's own fields. `protocol.Emitter` stamps `seq` and `outstanding` on the
way out — see there for the envelope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from agentproxy import log


KIND_USER = "user"
KIND_UNOWNED = "unowned"

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_ENDED = "ended"

TASK_RUNNING = "running"
TASK_DONE = "done"

# What kind of work the task is. All three are counted the same way — they are
# work outstanding that is not a turn — but a client showing them apart needs
# to be told which, and only the launch wording says.
TASK_SHELL = "shell"
TASK_MONITOR = "monitor"
TASK_AGENT = "agent"
TASK_UNKNOWN = "unknown"

# The statuses a `<task-notification>` uses to say the task is over. Every one
# of them must end the task: absorbing only `completed` left the other three
# stranding `outstanding.background` above zero for the life of the process,
# which is 47 of 309 tasks in the surveyed corpus. A notification with no
# status at all is a monitor firing an event, and ends nothing.
TERMINAL_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"

BASIS_ERROR = "error"

# The three ways a cancelled turn is seen to end. Each is evidence, not the
# client's request: the request only ever sends one `Esc`, and the turn ends
# when the inner is seen to have stopped — in the transcript when it writes
# an interruption entry, on the screen when it hands the prompt back instead.
BASIS_INTERRUPTED = "marker:interrupted"
BASIS_RESTORED = "cancel:restored"
BASIS_WITHDRAWN = "cancel:withdrawn"

# What the inner writes, as the text of a `user` entry, when `Esc` interrupts
# a turn that had already produced something. The second form follows a
# `tool_result` declining the aborted tool. A `user` entry rather than a
# `system` one, so it is matched on content the way a launch wording is; an
# interrupt that lands before the model has produced anything writes neither
# — the prompt is handed back to the composer and the transcript stays silent.
INTERRUPTION_TEXTS = frozenset(
    {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}
)

# What `cancel` decided to do, by the turn's state.
CANCEL_WITHDRAWN = "withdrawn"
CANCEL_INTERRUPT = "interrupt"
CANCEL_HELD = "held"

# Transcript markers that end a turn, mapped to the `basis` they report.
# Extensible by design: `compact_boundary` was found only by surveying 412
# transcripts, so the fallback tier exists for the marker nobody has met yet.
MARKER_BASIS = {
    "turn_duration": "marker:turn_duration",
    "compact_boundary": "marker:compact_boundary",
}

# A background task announces itself in the tool_result of the Bash call that
# started it, in one of two wordings: `run_in_background: true` produces
# "Command running in background with ID: <id>", and a foreground command that
# outlives its timeout produces "was moved to the background (ID: <id>)". The
# second shape means a task can appear without anyone having asked for one.
# What the inner writes in a tool_result when it puts work in the background.
# Keyed by wording because none of it is visible in the `tool_use`: the shell
# flag is invisible on a timeout promotion, and neither the monitor nor the
# subagent names its id anywhere else. Each entry yields the id the later
# `<task-notification>` will be keyed by.
_LAUNCH_RES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (TASK_SHELL, re.compile(r"background[^.]*?ID: ([A-Za-z0-9_-]+)")),
    (TASK_MONITOR, re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+)")),
    (
        TASK_AGENT,
        re.compile(r"Async agent launched successfully.{0,200}?agentId: ([A-Za-z0-9_-]+)", re.S),
    ),
)

_NOTIFICATION_TAG_RE = re.compile(
    r"<(task-id|tool-use-id|status|summary|output-file)>(.*?)</\1>", re.DOTALL
)
_EXIT_CODE_RE = re.compile(r"exit code (-?\d+)")

# `TaskStop`'s own tool_result, confirming a task the model stopped on
# purpose. This ending never produces a `<task-notification>` — the stop is
# confirmed in-band, synchronously — so it needs its own recognition, by
# wording, for the same reason a launch does: the tool name is invisible here.
_STOP_RE = re.compile(r"[Ss]uccessfully stopped task:?\s+([A-Za-z0-9_-]+)")

# Since `claude` 2.1.277 a bracketed paste — which is how every prompt reaches
# the inner — is recorded wrapped in a `pasted_content` block, closing tag
# with the same id attribute as the opening one:
#     \n\n<pasted_content id="fa0b">\n<the prompt>\n</pasted_content id="fa0b">\n
# Left in, no prompt matches its entry and every turn runs as unowned.
_PASTED_CONTENT_RE = re.compile(r"</?pasted_content\b[^>]*>")


class CancelError(Exception):
    """A `cancel` the tracker will not act on; the message is the reason."""


@dataclass
class Turn:
    """One unit of inner-`claude` work, from submission or wake-up to EOT."""

    id: str
    kind: str
    state: str
    text: Optional[str] = None
    caused_by: Optional[str] = None
    basis: Optional[str] = None
    outcome: Optional[str] = None
    output_tokens: int = 0
    # A client asked for this turn to be cancelled; `Proxy` sends the `Esc`
    # once the turn is running and records that it did in `interrupt_sent`,
    # so a turn cancelled while still being typed is interrupted the moment
    # it binds, and never twice.
    cancel_requested: bool = False
    interrupt_sent: bool = False

    def ref(self) -> dict:
        """The `turn` field every message belonging to this turn carries."""
        return {"id": self.id, "kind": self.kind}


@dataclass
class BackgroundTask:
    """Work the inner put in the background, tracked apart from any turn.

    Three things arrive here: a `run_in_background` shell command, a monitor,
    and an async subagent. `kind` says which, and each gets its own count in
    `outstanding` — a monitor that watches for the rest of the session reads
    very differently from a build that will finish. The lifecycle below is the
    same for all three.

    Completing while a turn runs folds the notification into that turn and
    starts nothing; completing while idle starts an unowned turn. The count
    must fall in both cases, which is why tasks have their own table and
    their own lifecycle.
    """

    id: str
    kind: str = TASK_UNKNOWN
    tool_use_id: Optional[str] = None
    summary: Optional[str] = None
    command: Optional[str] = None
    state: str = TASK_RUNNING
    status: Optional[str] = None
    exit_code: Optional[int] = None
    output_file: Optional[str] = None


class TurnTracker:
    """Holds every turn and background task of one inner-`claude` session.

    Callers drive it from two sides: `submit`/`mark_typed` as the client's
    prompts move through the proxy, and `on_entry` for each transcript entry
    the tailer reads. Both return lists of protocol events, in emission order.

    Ended turns stay in the table for the life of the session, so a late or
    repeated marker is recognised and ignored rather than opening a phantom
    turn.
    """

    def __init__(self) -> None:
        self._turns: dict[str, Turn] = {}
        self._tasks: dict[str, BackgroundTask] = {}
        self._turn_seq = 0
        self._running_turn: Optional[Turn] = None
        self._candidate_turn: Optional[Turn] = None
        self._typed_turn: Optional[Turn] = None
        self._tool_descriptions: dict[str, str] = {}
        self._tool_commands: dict[str, str] = {}
        self._last_enqueued_task: Optional[str] = None
        self._dequeued_cause: Optional[str] = None

    # ------------------------------------------------------------------
    # Client side
    # ------------------------------------------------------------------

    def submit(self, text: str) -> tuple[Turn, list[dict]]:
        """Accept a client prompt and give it the id its EOT will carry.

        The turn counts as outstanding from here, before anything is typed:
        a prompt waiting in the queue is work the client is still owed.
        Returns the turn together with the `ack` the client is answered with.
        """
        self._turn_seq += 1
        turn = Turn(
            id=f"t-{self._turn_seq}", kind=KIND_USER, state=STATE_QUEUED, text=text
        )
        self._turns[turn.id] = turn
        return turn, [{"type": "ack", "turn": turn.ref()}]

    def mark_typed(self, turn: Turn) -> None:
        """Record that `turn`'s prompt has been typed into the TUI.

        Call it once the keystrokes are away. Until the transcript shows an
        entry matching this text, the prompt is what `on_entry` binds against;
        only one may be outstanding at a time, since the proxy types one at a
        time.
        """
        if self._typed_turn is not None and self._typed_turn is not turn:
            log.log_text(
                "tracker",
                f"typed prompt {turn.id} while {self._typed_turn.id} is unmatched",
            )
        self._typed_turn = turn

    def fail_session(self, reason: str) -> list[dict]:
        """End everything unfinished after the inner `claude` died or errored.

        Closes the running turn and everything queued behind it with an
        `error` outcome, so no counter is stranded above zero and no client
        waits on an id that will never complete. Background tasks go too:
        they were children of the inner `claude`, so nothing survives to
        report their completion. Returns an `error` message followed by one
        `turn_end` per turn and one `task_end` per task it closed.
        """
        events: list[dict] = [{"type": "error", "error": reason}]
        if self._running_turn is not None:
            events += self._end_turn(self._running_turn, BASIS_ERROR, OUTCOME_ERROR)
        self._discard_candidate("session failed")
        for turn in list(self._turns.values()):
            if turn.state != STATE_ENDED:
                events += self._end_turn(turn, BASIS_ERROR, OUTCOME_ERROR)
        for task in self._tasks.values():
            if task.state != TASK_DONE:
                events.append(self._end_task(task, "failed"))
        self._typed_turn = None
        return events

    def end_running_turn(self, basis: str, outcome: str = OUTCOME_SUCCESS) -> list[dict]:
        """End whatever turn is running, on a `basis` no marker supplied.

        The entry point for the TUI-idle fallback in `fallback.py`, the second
        of the two end-of-turn tiers. Returns no events when nothing is
        running.
        """
        if self._running_turn is None:
            return []
        return self._end_turn(self._running_turn, basis, outcome)

    def end_typed_turn(self, basis: str, outcome: str = OUTCOME_SUCCESS) -> list[dict]:
        """End a prompt that was typed and never recognised in the transcript.

        The other half of `end_running_turn`, and the one that keeps "every
        turn ends" true. A typed turn is not the running turn — it is waiting
        for the transcript entry that would make it one — so the fallback tier
        cannot reach it through `end_running_turn`, and nothing else ever
        closes it. Without this, a prompt the TUI altered on its way in — a
        trailing backslash, eaten as a line continuation — leaves a `user` turn
        open for the life of the process: `outstanding.user` stuck at 1, and
        `is_ready_to_send` blocked on `typed_turn`, so no later prompt is typed
        either.

        When to call it is `Proxy.on_iteration`'s decision, and it turns on the
        prompt's age: a prompt typed a moment ago and not yet picked up looks
        identical from here, so only `FallbackEot.typed_is_lost` tells the two
        apart.
        """
        if self._typed_turn is None:
            return []
        return self._end_turn(self._typed_turn, basis, outcome)

    def cancel(self, turn_id: str) -> tuple[str, list[dict]]:
        """Act on a client's `cancel` by what state the turn is in.

        Returns the action taken and the events it produced. A queued turn is
        ended here and now (`CANCEL_WITHDRAWN`); the caller must also drop it
        from the submit queue, which this tracker does not hold. A running
        turn is marked (`CANCEL_INTERRUPT`) and the caller sends the `Esc`. A
        turn typed but not yet seen in the transcript is marked too and the
        interrupt waits for it to bind (`CANCEL_HELD`) — it is at most the
        typing latency away from running, and an `Esc` before then would land
        on an idle composer instead. Raises `CancelError` for a turn that
        cannot be cancelled: unknown, already ended, or unowned — the last
        because a restored prompt is recognised by the text the proxy typed,
        which an unowned turn does not have.
        """
        turn = self._turns.get(turn_id)
        if turn is None:
            raise CancelError(f"no turn {turn_id!r}")
        if turn.kind != KIND_USER:
            raise CancelError(f"{turn_id} is an unowned turn; only user turns can be cancelled")
        if turn.state == STATE_ENDED:
            raise CancelError(f"{turn_id} has already ended")
        if turn.cancel_requested:
            # Already asked. Say so rather than send a second `Esc`: on the
            # empty composer the inner leaves after an interrupt, a second
            # `Esc` is the first half of the gesture that opens the rewind
            # picker, a dialog the proxy has no rule for.
            return CANCEL_HELD, []
        turn.cancel_requested = True
        if turn is self._typed_turn:
            return CANCEL_HELD, []
        if turn.state == STATE_RUNNING:
            return CANCEL_INTERRUPT, []
        return CANCEL_WITHDRAWN, self._end_turn(turn, BASIS_WITHDRAWN, OUTCOME_CANCELLED)

    def end_restored_turn(self) -> list[dict]:
        """End the cancelled running turn whose prompt the inner handed back.

        The other ending a cancel can have, and the one the transcript never
        shows: `Esc` before the model has produced anything is treated by the
        inner as an un-send, the prompt goes back into the composer, and no
        entry is written. `Proxy` calls this when it sees that prompt on the
        composer row of an idle frame; nothing here reads a screen.
        """
        turn = self._running_turn
        if turn is None or not turn.cancel_requested:
            return []
        return self._end_turn(turn, BASIS_RESTORED, OUTCOME_CANCELLED)

    # ------------------------------------------------------------------
    # Transcript side
    # ------------------------------------------------------------------

    def on_entry(self, entry: dict) -> list[dict]:
        """Fold one transcript entry into the tables and report what it changed.

        Entries arrive in the order the inner `claude` wrote them; the caller
        passes each one exactly once. Types with no bearing on turns or tasks
        return an empty list.
        """
        kind = entry.get("type")
        if kind == "assistant":
            return self._on_assistant(entry)
        if kind == "user":
            return self._on_user(entry)
        if kind == "system":
            return self._on_system(entry)
        if kind == "queue-operation":
            return self._on_queue_operation(entry)
        return []

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def outstanding(self) -> dict:
        """The counts, computed from the tables at the moment of the call.

        The keys are disjoint and every kind of outstanding work has one, so a
        client asking "is anything still running" can test them all without
        knowing what they mean — including a key added after it was written.
        Background work is split by `kind`; a task whose launch was never seen
        counts under `background`, where an unrecognised shell command is the
        likeliest thing it is.

        A candidate turn is absent from the table until the model is seen
        working on it, so an entry that never becomes a turn — the cluster
        `/compact` writes after its boundary, for one — is never counted.
        """
        return {
            "user": sum(
                1
                for t in self._turns.values()
                if t.kind == KIND_USER and t.state != STATE_ENDED
            ),
            "unowned": sum(
                1
                for t in self._turns.values()
                if t.kind == KIND_UNOWNED and t.state != STATE_ENDED
            ),
            "background": self._count_tasks(TASK_SHELL, TASK_UNKNOWN),
            "monitor": self._count_tasks(TASK_MONITOR),
            "agent": self._count_tasks(TASK_AGENT),
        }

    def _count_tasks(self, *kinds: str) -> int:
        """How many unfinished tasks are of these kinds."""
        return sum(
            1
            for b in self._tasks.values()
            if b.state != TASK_DONE and b.kind in kinds
        )

    def snapshot(self) -> dict:
        """Everything unfinished, for the `state` reply to a client `status`."""
        return {
            "turns": [
                {
                    "id": t.id,
                    "kind": t.kind,
                    "state": t.state,
                    "caused_by": t.caused_by,
                }
                for t in self._turns.values()
                if t.state != STATE_ENDED
            ],
            "tasks": [
                {"id": b.id, "kind": b.kind, "summary": b.summary, "command": b.command}
                for b in self._tasks.values()
                if b.state != TASK_DONE
            ],
        }

    @property
    def running_turn(self) -> Optional[Turn]:
        return self._running_turn

    @property
    def typed_turn(self) -> Optional[Turn]:
        """The prompt typed into the TUI whose transcript entry has not arrived.

        None means the keyboard is free: nothing is waiting to be recognised,
        so the next prompt can go in. A caller that types past this would put
        two prompts in flight with one binding decision to tell them apart.
        """
        return self._typed_turn

    @property
    def turns(self) -> list[Turn]:
        """Every turn of the session, ended ones included, in id order."""
        return list(self._turns.values())

    @property
    def tasks(self) -> list[BackgroundTask]:
        """Every background task of the session, finished ones included."""
        return list(self._tasks.values())

    def turn(self, turn_id: str) -> Optional[Turn]:
        return self._turns.get(turn_id)

    def task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    # ------------------------------------------------------------------
    # Entry handlers
    # ------------------------------------------------------------------

    def _on_user(self, entry: dict) -> list[dict]:
        content = entry.get("message", {}).get("content")
        if _is_interruption(content):
            return self._on_interruption()
        events = self._absorb_notification(_flatten_text(content))
        if isinstance(content, str):
            if self._running_turn is None:
                events += self._open_turn(content)
            return events
        if not isinstance(content, list):
            return events
        # A tool_result means the model has been working, so it both confirms
        # a candidate turn and, if the transcript is somehow ahead of us,
        # opens one.
        if self._running_turn is None and self._candidate_turn is None:
            events += self._open_turn(None)
        events += self._confirm_candidate()
        turn = self._running_turn
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            events += self._note_background_launch(block)
            events += self._note_task_stop(block)
            events.append(
                {
                    "type": "tool_result",
                    "turn": turn.ref() if turn else None,
                    "tool_use_id": block.get("tool_use_id"),
                    "content": block.get("content"),
                    "is_error": bool(block.get("is_error")),
                }
            )
        return events

    def _on_interruption(self) -> list[dict]:
        """End the running turn on the inner's own interruption entry.

        Tier 1, like `turn_duration`: the entry is written by the inner when
        an `Esc` stops a turn that had produced something, and nothing else
        writes it. It ends the turn whether or not this proxy asked — the
        proxy is the only thing typing, so an unrequested one is worth a log
        line, not a different outcome. Opens nothing: the entry is the shape
        of a prompt (a `user` entry) but it is the end of a turn, not the
        start of one, and a candidate it confirmed would be a phantom. An
        older inner may follow it with a `turn_duration`, which then finds no
        turn running and is ignored like any late marker.
        """
        self._discard_candidate("interrupted before the model was seen working")
        turn = self._running_turn
        if turn is None:
            log.log_text("tracker", f"{BASIS_INTERRUPTED} with no turn running")
            return []
        if not turn.cancel_requested:
            log.log_text("tracker", f"{turn.id} interrupted, but not by this proxy")
        return self._end_turn(turn, BASIS_INTERRUPTED, OUTCOME_CANCELLED)

    def _on_assistant(self, entry: dict) -> list[dict]:
        events = self._confirm_candidate()
        if self._running_turn is None:
            events += self._open_turn(None)
            events += self._confirm_candidate()
        turn = self._running_turn
        message = entry.get("message", {})
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                events.append(
                    {"type": "text", "turn": turn.ref(), "text": block.get("text", "")}
                )
            elif block_type == "thinking":
                events.append(
                    {
                        "type": "thinking",
                        "turn": turn.ref(),
                        "text": block.get("thinking", ""),
                    }
                )
            elif block_type == "tool_use":
                tool_input = block.get("input") or {}
                description = tool_input.get("description")
                if isinstance(description, str):
                    self._tool_descriptions[block.get("id")] = description
                command = tool_input.get("command")
                if isinstance(command, str):
                    self._tool_commands[block.get("id")] = command
                events.append(
                    {
                        "type": "tool_use",
                        "turn": turn.ref(),
                        "tool": block.get("name"),
                        "input": tool_input,
                        "tool_use_id": block.get("id"),
                    }
                )
        usage = message.get("usage") or {}
        if turn is not None and isinstance(usage.get("output_tokens"), int):
            turn.output_tokens += usage["output_tokens"]
        return events

    def _on_system(self, entry: dict) -> list[dict]:
        basis = MARKER_BASIS.get(entry.get("subtype"))
        if basis is None:
            return []
        events = self._confirm_candidate()
        if self._running_turn is None:
            log.log_text("tracker", f"{basis} with no turn running")
            return events
        usage = {}
        if isinstance(entry.get("durationMs"), int):
            usage["duration_ms"] = entry["durationMs"]
        return events + self._end_turn(
            self._running_turn, basis, OUTCOME_SUCCESS, usage
        )

    def _on_queue_operation(self, entry: dict) -> list[dict]:
        # A completed task is announced twice — here first, then again in the
        # user entry the notification becomes. `_absorb_notification` is
        # idempotent, so whichever arrives first ends the task and the other
        # is a no-op; neither is required for the count to be right.
        operation = entry.get("operation")
        events = self._absorb_notification(entry.get("content"))
        note = _parse_notification(entry.get("content"))
        if note and operation == "enqueue":
            self._last_enqueued_task = note["task_id"]
        if operation == "dequeue":
            self._dequeued_cause = self._last_enqueued_task
        return events

    # ------------------------------------------------------------------
    # Turn transitions
    # ------------------------------------------------------------------

    def _open_turn(self, text: Optional[str]) -> list[dict]:
        """Decide what an entry arriving with no turn running means.

        `text` is the entry's content when it is a plain string, else None.
        A match against the typed prompt makes it that user turn, running
        from now. Anything else becomes a candidate unowned turn, which stays
        out of the table and off the wire until the model is seen working on
        it — a further unmatched entry folds into the same candidate rather
        than opening a second one.
        """
        if (
            text is not None
            and self._typed_turn is not None
            and _normalise(text) == _normalise(self._typed_turn.text or "")
        ):
            self._discard_candidate("displaced by a matching prompt")
            turn = self._typed_turn
            self._typed_turn = None
            turn.state = STATE_RUNNING
            self._running_turn = turn
            return [self._turn_start(turn)]

        if self._candidate_turn is not None:
            if self._candidate_turn.caused_by is None:
                self._candidate_turn.caused_by = _cause_of(text)
            return []

        self._turn_seq += 1
        self._candidate_turn = Turn(
            id=f"t-{self._turn_seq}",
            kind=KIND_UNOWNED,
            state=STATE_RUNNING,
            text=text,
            caused_by=_cause_of(text) or self._dequeued_cause,
        )
        if self._typed_turn is not None:
            log.log_text(
                "tracker",
                f"unmatched entry opened {self._candidate_turn.id} while "
                f"{self._typed_turn.id} is still unmatched",
            )
        return []

    def _confirm_candidate(self) -> list[dict]:
        """Promote the candidate turn to a real one, now the model has run.

        Registering it here rather than at `_open_turn` is what keeps the
        counters honest: until this point the candidate exists only as a
        guess, and a guess that never confirms leaves no trace.
        """
        if self._candidate_turn is None:
            return []
        turn = self._candidate_turn
        self._candidate_turn = None
        self._running_turn = turn
        self._turns[turn.id] = turn
        self._dequeued_cause = None
        return [self._turn_start(turn)]

    def _discard_candidate(self, reason: str) -> None:
        if self._candidate_turn is None:
            return
        log.log_text("tracker", f"dropped candidate {self._candidate_turn.id}: {reason}")
        self._candidate_turn = None

    def _turn_start(self, turn: Turn) -> dict:
        event = {"type": "turn_start", "turn": turn.ref()}
        if turn.caused_by:
            event["caused_by"] = turn.caused_by
        return event

    def _end_turn(
        self,
        turn: Turn,
        basis: str,
        outcome: str,
        usage: Optional[dict] = None,
    ) -> list[dict]:
        turn.state = STATE_ENDED
        turn.basis = basis
        turn.outcome = outcome
        if self._running_turn is turn:
            self._running_turn = None
        if self._typed_turn is turn:
            self._typed_turn = None
        full_usage = dict(usage or {})
        if turn.output_tokens:
            full_usage["output_tokens"] = turn.output_tokens
        return [
            {
                "type": "turn_end",
                "turn": turn.ref(),
                "basis": basis,
                "outcome": outcome,
                "usage": full_usage,
            }
        ]

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _note_background_launch(self, block: dict) -> list[dict]:
        """Register a task from the tool_result that announced it, if any.

        Every kind of background work is recognised by its own wording, and a
        wording the inner changes is a task that goes unseen until it reports
        completion — so this is the surface to check first when
        `outstanding.background` looks wrong. `_absorb_notification` is the
        backstop that keeps the count correct even then.
        """
        text = _flatten_text(block.get("content"))
        for kind, pattern in _LAUNCH_RES:
            match = pattern.search(text)
            if match is not None:
                break
        else:
            return []
        task_id = match.group(1)
        if task_id in self._tasks:
            return []
        tool_use_id = block.get("tool_use_id")
        task = BackgroundTask(
            id=task_id,
            kind=kind,
            tool_use_id=tool_use_id,
            summary=self._tool_descriptions.get(tool_use_id),
            command=self._tool_commands.get(tool_use_id),
        )
        self._tasks[task_id] = task
        return [self._task_start_event(task)]

    def _task_start_event(self, task: BackgroundTask) -> dict:
        """The `task_start` message announcing one task to the client."""
        return {
            "type": "task_start",
            "task": {"id": task.id, "kind": task.kind},
            "summary": task.summary,
            "command": task.command,
        }

    def _end_task(
        self, task: BackgroundTask, status: str, exit_code: Optional[int] = None
    ) -> dict:
        """Mark `task` done and return the `task_end` announcing it.

        The one place that closes a task, so every ending — a terminal
        `<task-notification>`, a session failure, a `TaskStop` confirmation —
        produces the same shape on the wire.

        `output_file` is a local path, not wire content — the tracker does no
        I/O (see module docstring), so `Proxy` reads it, caps it, and replaces
        it with `output` before the message reaches a client.
        """
        task.state = TASK_DONE
        task.status = status
        task.exit_code = exit_code
        return {
            "type": "task_end",
            "task": {"id": task.id, "kind": task.kind},
            "status": task.status,
            "exit_code": task.exit_code,
            "output_file": task.output_file,
        }

    def _note_task_stop(self, block: dict) -> list[dict]:
        """End a task the model stopped on purpose, confirmed by `TaskStop`'s own tool_result.

        `TaskStop` resolves in-band: its tool_result *is* the confirmation,
        and no `<task-notification>` follows it. Left to `_absorb_notification`
        alone, a stopped task's count never falls, stranding
        `outstanding.background` (or `.monitor` / `.agent`) above zero for the
        life of the process — the same failure Session 13 found for the four
        notification statuses, but from a fifth ending path that emits no
        notification at all.
        """
        if block.get("is_error"):
            return []
        match = _STOP_RE.search(_flatten_text(block.get("content")))
        if match is None:
            return []
        task = self._tasks.get(match.group(1))
        if task is None or task.state == TASK_DONE:
            return []
        return [self._end_task(task, "stopped")]

    def _absorb_notification(self, text: Optional[str]) -> list[dict]:
        """Fold one `<task-notification>` into the task table.

        Ends the task when the notification reports a terminal status, and
        registers a task nobody saw launch either way. Safe to call on any
        entry's text: entries carrying no notification, and repeats of one
        already handled, both return no events.

        A notification with no status is a monitor reporting an event, not
        ending. It still registers the monitor if the launch went unseen, so
        that a `caused_by` naming it resolves to a task the client was told
        about.
        """
        note = _parse_notification(text)
        if note is None:
            return []
        task_id = note["task_id"]
        if task_id is None:
            return []
        task = self._tasks.get(task_id)
        events: list[dict] = []
        if task is None:
            # The launch went unseen — the transcript was joined mid-session,
            # or the announcing tool_result was worded in some way this does
            # not know. Register it now so the count and any `caused_by` still
            # refer to a task the client has seen start.
            task = BackgroundTask(
                id=task_id,
                kind=TASK_UNKNOWN,
                tool_use_id=note["tool_use_id"],
                summary=note["summary"],
            )
            self._tasks[task_id] = task
            log.log_text("tracker", f"background task {task_id} first seen mid-life")
            events.append(self._task_start_event(task))
        if note["output_file"]:
            # Only the notification carries this, never the launch — so it is
            # picked up here regardless of whether `task` is new or already
            # known, and in time for `_end_task` to put it on `task_end`.
            task.output_file = note["output_file"]
        if note["status"] not in TERMINAL_STATUSES:
            return events
        if task.state == TASK_DONE:
            return events
        events.append(self._end_task(task, note["status"], note["exit_code"]))
        return events


# ----------------------------------------------------------------------
# Entry parsing helpers
# ----------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Reverse the TUI's own edits so a typed prompt matches its transcript entry.

    The text in the transcript is not byte-identical to what the proxy typed,
    and every difference here is something the TUI did to it:

    - **Re-wrapping.** Collapsed by comparing whitespace-normalised text.
    - **A trailing backslash.** The TUI reads it as a line continuation and
      eats it, so a prompt ending in `\\` reaches `claude` without one. Left
      in, the comparison fails and the prompt is never recognised as the one
      the proxy typed — see `end_typed_turn` for what that used to cost.
    - **A `pasted_content` wrapper.** From 2.1.277 the TUI records a paste
      inside `<pasted_content id="…">…</pasted_content id="…">` tags, and a
      paste is how every prompt goes in. The tags are removed; the text
      between them is the prompt.

    Both sides go through this, so stripping is symmetric: a backslash the TUI
    keeps is stripped from the transcript entry too, and the two still match.
    """
    text = _PASTED_CONTENT_RE.sub(" ", text)
    return " ".join(text.split()).rstrip("\\")


def _is_interruption(content: object) -> bool:
    """True for the `user` entry the inner writes when `Esc` stops a turn.

    Its content is a list holding one text block, never a plain string —
    which is what keeps it apart from a prompt of the same words, should a
    client ever send one.
    """
    if not isinstance(content, list) or not content:
        return False
    if not all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return False
    text = "".join(b.get("text", "") for b in content).strip()
    return text in INTERRUPTION_TEXTS


def _flatten_text(content: object) -> str:
    """Every string in a message's content, joined — a haystack for the scanners.

    Content is a plain string for prompts and notifications, and a list of
    blocks for tool results, whose own `content` may nest another list.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_flatten_text(item) for item in content)
    if isinstance(content, dict):
        return "\n".join(
            _flatten_text(content.get(key)) for key in ("text", "content")
        )
    return ""


def _parse_notification(text: Optional[str]) -> Optional[dict]:
    """The fields of a `<task-notification>` block, or None if there is none.

    The block is how the inner `claude` reports a background command
    finishing. It reaches the tracker twice for the same task — once as a
    `queue-operation`, once as the user entry it turns into — so callers must
    tolerate repeats.
    """
    if not isinstance(text, str) or "<task-notification>" not in text:
        return None
    tags = {name: value.strip() for name, value in _NOTIFICATION_TAG_RE.findall(text)}
    summary = tags.get("summary")
    exit_match = _EXIT_CODE_RE.search(summary or "")
    return {
        "task_id": tags.get("task-id"),
        "tool_use_id": tags.get("tool-use-id"),
        "status": tags.get("status"),
        "summary": summary,
        "exit_code": int(exit_match.group(1)) if exit_match else None,
        "output_file": tags.get("output-file"),
    }


def _cause_of(text: Optional[str]) -> Optional[str]:
    """The background-task id a turn-initiating entry names, if it names one."""
    note = _parse_notification(text)
    return note["task_id"] if note else None
