"""The turn table, the background-task table, and the rules that move them.

Everything the protocol says about turns is decided here. The tracker takes
parsed transcript entries and client submissions, and returns the messages
those imply; it does no I/O and knows nothing about stdout, which is what
makes it testable by replaying a recorded session.

Three rules from DESIGN.md carry the design:

- **Provenance, not content** (Decision 1). The proxy is the only thing that
  types client prompts into the TUI, so a turn is `user` when its initiating
  entry matches the prompt we typed and `unowned` otherwise. No rule here
  inspects an entry to decide whether it "looks like" a prompt.
- **Two-tier end-of-turn** (Decision 2). Tier 1 — a recognised transcript
  marker — lives here. Tier 2, the TUI-idle fallback, arrives with the live
  pty and calls `end_running_turn` from the outside.
- **Derived counters** (Decision 3). `outstanding()` counts the tables; no
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

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"

BASIS_ERROR = "error"

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
_BACKGROUND_LAUNCH_RE = re.compile(r"background[^.]*?ID: ([A-Za-z0-9_-]+)")

_NOTIFICATION_TAG_RE = re.compile(
    r"<(task-id|tool-use-id|status|summary)>(.*?)</\1>", re.DOTALL
)
_EXIT_CODE_RE = re.compile(r"exit code (-?\d+)")


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

    def ref(self) -> dict:
        """The `turn` field every message belonging to this turn carries."""
        return {"id": self.id, "kind": self.kind}


@dataclass
class BackgroundTask:
    """A `run_in_background` command, tracked apart from any turn.

    Completing while a turn runs folds the notification into that turn and
    starts nothing; completing while idle starts an unowned turn. The count
    must fall in both cases, which is why tasks have their own table and
    their own lifecycle (DESIGN.md Decision 4).
    """

    id: str
    tool_use_id: Optional[str] = None
    summary: Optional[str] = None
    state: str = TASK_RUNNING
    exit_code: Optional[int] = None


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
        """End every unfinished turn after the inner `claude` died or errored.

        Closes the running turn and everything queued behind it with an
        `error` outcome, so no counter is stranded above zero and no client
        waits on an id that will never complete. Returns an `error` message
        followed by one `turn_end` per turn it closed.
        """
        events: list[dict] = [{"type": "error", "error": reason}]
        if self._running_turn is not None:
            events += self._end_turn(self._running_turn, BASIS_ERROR, OUTCOME_ERROR)
        self._discard_candidate("session failed")
        for turn in list(self._turns.values()):
            if turn.state != STATE_ENDED:
                events += self._end_turn(turn, BASIS_ERROR, OUTCOME_ERROR)
        self._typed_turn = None
        return events

    def end_running_turn(self, basis: str, outcome: str = OUTCOME_SUCCESS) -> list[dict]:
        """End whatever turn is running, on a `basis` no marker supplied.

        The entry point for the TUI-idle fallback (DESIGN.md Decision 2,
        tier 2). Returns no events when nothing is running.
        """
        if self._running_turn is None:
            return []
        return self._end_turn(self._running_turn, basis, outcome)

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
        """The three counts, computed from the tables at the moment of the call.

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
            "background": sum(
                1 for b in self._tasks.values() if b.state != TASK_DONE
            ),
        }

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
                {"id": b.id, "summary": b.summary}
                for b in self._tasks.values()
                if b.state != TASK_DONE
            ],
        }

    @property
    def running_turn(self) -> Optional[Turn]:
        return self._running_turn

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
        """Register a task from the tool_result that announced it, if any."""
        text = _flatten_text(block.get("content"))
        match = _BACKGROUND_LAUNCH_RE.search(text)
        if match is None:
            return []
        task_id = match.group(1)
        if task_id in self._tasks:
            return []
        tool_use_id = block.get("tool_use_id")
        task = BackgroundTask(
            id=task_id,
            tool_use_id=tool_use_id,
            summary=self._tool_descriptions.get(tool_use_id),
        )
        self._tasks[task_id] = task
        return [
            {"type": "task_start", "task": {"id": task.id}, "summary": task.summary}
        ]

    def _absorb_notification(self, text: Optional[str]) -> list[dict]:
        """End the background task a completion notification names.

        Safe to call on any entry's text: entries carrying no notification,
        and repeats of one already handled, both return no events.
        """
        note = _parse_notification(text)
        if note is None or note["status"] != "completed":
            return []
        task_id = note["task_id"]
        task = self._tasks.get(task_id)
        events: list[dict] = []
        if task is None:
            # The launch went unseen — the transcript was joined mid-session,
            # or the announcing tool_result was worded in some third way.
            # Register it now so the completion still has a matching start.
            task = BackgroundTask(
                id=task_id,
                tool_use_id=note["tool_use_id"],
                summary=note["summary"],
            )
            self._tasks[task_id] = task
            log.log_text("tracker", f"background task {task_id} first seen at completion")
            events.append(
                {"type": "task_start", "task": {"id": task.id}, "summary": task.summary}
            )
        if task.state == TASK_DONE:
            return events
        task.state = TASK_DONE
        task.exit_code = note["exit_code"]
        events.append(
            {"type": "task_end", "task": {"id": task.id}, "exit_code": task.exit_code}
        )
        return events


# ----------------------------------------------------------------------
# Entry parsing helpers
# ----------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Collapse whitespace so a typed prompt matches its transcript entry.

    The TUI re-wraps what it is given, so the text in the transcript is not
    byte-identical to what the proxy typed.
    """
    return " ".join(text.split())


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
    }


def _cause_of(text: Optional[str]) -> Optional[str]:
    """The background-task id a turn-initiating entry names, if it names one."""
    note = _parse_notification(text)
    return note["task_id"] if note else None
