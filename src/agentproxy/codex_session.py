"""What a `codex app-server` thread's notifications mean, in this proxy's protocol.

Codex states its own turn boundaries, so nothing here infers one: the turn
id comes back on `turn/start`'s response, `turn/completed` ends a turn with a
status, and items say what happened in between. `CodexSession` maps those
onto `TurnTracker`'s structured side and returns the protocol events they
imply. It does no I/O, the way `TurnTracker` does none, so a recorded
sequence of notifications is a complete test.

Three codex behaviours shape the mapping (SPEC finding 10, DESIGN Decision 10):

- A `turn/start` sent mid-turn is folded into the running turn. The proxy
  never sends one then, but an unowned turn can start between a
  `turn/start` and its response; the response then names that turn, and the
  user turn is bound to it as well, so both end on its `turn/completed`.
- Codex keeps no task list. Its subagents are reported, and become `agent`
  tasks; a detached shell command is not, and is left to the orphan count.
- An interrupt does not stop the command a turn was running. A command still
  open when its turn ends becomes a `shell` task, ended by the late
  `item/completed` codex sends when it finishes.

Notifications about any other thread, a subagent's own turns and items
among them, are not this session's turns and produce nothing.
"""

from __future__ import annotations

from typing import Optional

from agentproxy import log
from agentproxy.turn_tracker import (
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    STATE_ENDED,
    TASK_AGENT,
    TASK_SHELL,
    Turn,
    TurnTracker,
)


BASIS_COMPLETED = "codex:completed"
BASIS_INTERRUPTED = "codex:interrupted"
BASIS_FAILED = "codex:failed"
BASIS_ERROR = "error"

# Items that are the conversation rather than the agent acting: reported as
# `text` and `thinking`, or (the prompt itself) not at all. Every other item
# type is reported as a tool call named by its type, so a type codex adds
# later still reaches the client rather than vanishing.
_PROMPT_ITEMS = frozenset({"userMessage", "hookPrompt"})
_TEXT_ITEM = "agentMessage"
_THINKING_ITEM = "reasoning"
_SUBAGENT_ITEM = "subAgentActivity"
_COMMAND_ITEM = "commandExecution"

# Fields of an item that describe its outcome, not what was asked for, so
# they belong on the `tool_result` rather than in the `tool_use`'s input.
_RESULT_FIELDS = frozenset(
    {"id", "type", "status", "aggregatedOutput", "exitCode", "durationMs", "result", "error"}
)
_FAILED_STATUSES = frozenset({"failed", "declined"})

# How a subagent's activity ends its task. `interacted` is the parent talking
# to a running subagent and ends nothing.
_SUBAGENT_ENDINGS = {"completed": "completed", "interrupted": "stopped"}


class CodexSession:
    """One codex thread, seen as turns and tasks.

    The caller drives it from two sides: `on_turn_started_response` when its
    `turn/start` is answered, and `on_notification` for everything the
    server says. Both return protocol events in emission order. `failure`
    becomes a reason string when the thread reports it can no longer run,
    which the caller turns into a session failure.
    """

    def __init__(self, tracker: TurnTracker, thread_id: str) -> None:
        self._tracker = tracker
        self.thread_id = thread_id
        self.failure: Optional[str] = None
        # Codex turn id to the proxy turns bound to it: one, or two when a
        # user turn was folded into an unowned one.
        self._turns: dict[str, list[str]] = {}
        # A `turn/started` that arrived while a `turn/start` was still
        # unanswered; the response decides whose it was.
        self._early_start: Optional[str] = None
        # Command items started and not yet completed, by item id, with the
        # codex turn they belong to. What is left here when that turn ends
        # is still running.
        self._open_commands: dict[str, tuple[str, dict]] = {}
        self._output_tokens: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Client side
    # ------------------------------------------------------------------

    def codex_turn_of(self, turn_id: str) -> Optional[str]:
        """The codex turn a proxy turn is bound to, for `turn/interrupt`."""
        for codex_id, turn_ids in self._turns.items():
            if turn_id in turn_ids:
                return codex_id
        return None

    def on_turn_started_response(
        self, result: Optional[dict], error: Optional[dict]
    ) -> list[dict]:
        """Bind the typed turn to the codex turn its `turn/start` started.

        An error answer ends the typed turn: codex refused the prompt, so
        nothing will ever run for it. A turn id already bound is the folded
        case in the module docstring.
        """
        typed = self._tracker.typed_turn
        early, self._early_start = self._early_start, None
        codex_id = ((result or {}).get("turn") or {}).get("id")
        if early is not None and early != codex_id:
            # Not seen in any probe: a turn started that the response did
            # not claim. Logged rather than opened, since the typed turn is
            # about to take the one running slot.
            log.log_text("codex", f"turn/started {early} was never claimed by a response")
        if typed is None:
            log.log_text("codex", "turn/start answered with no typed turn")
            return []
        if error is not None:
            reason = str(error.get("message", error))
            log.log_text("codex", f"{typed.id}: turn/start refused: {reason}")
            return self._tracker.end_turn(typed.id, BASIS_ERROR, OUTCOME_ERROR, error=reason)
        if not isinstance(codex_id, str):
            log.log_text("codex", f"{typed.id}: turn/start answered with no turn id")
            return self._tracker.end_turn(
                typed.id, BASIS_ERROR, OUTCOME_ERROR, error="turn/start returned no turn id"
            )
        if codex_id in self._turns:
            log.log_text(
                "codex",
                f"{typed.id} folded into codex turn {codex_id}, already "
                f"{self._turns[codex_id]}; both end together",
            )
            self._turns[codex_id].append(typed.id)
            return []
        self._turns[codex_id] = [typed.id]
        return self._tracker.bind_typed()

    # ------------------------------------------------------------------
    # Server side
    # ------------------------------------------------------------------

    def on_notification(self, method: str, params: dict) -> list[dict]:
        """Fold one server notification into the tables; return what it changed."""
        if params.get("threadId") != self.thread_id:
            return []
        if method == "turn/started":
            return self._on_turn_started(params.get("turn") or {})
        if method == "turn/completed":
            return self._on_turn_completed(params.get("turn") or {})
        if method == "item/started":
            return self._on_item_started(params.get("item") or {}, params.get("turnId"))
        if method == "item/completed":
            return self._on_item_completed(params.get("item") or {}, params.get("turnId"))
        if method == "thread/tokenUsage/updated":
            self._note_tokens(params)
        elif method == "thread/status/changed":
            if (params.get("status") or {}).get("type") == "systemError":
                self.failure = "codex reported a system error on the thread"
        elif method == "error":
            error = params.get("error") or {}
            log.log_text(
                "codex",
                f"error (will retry: {params.get('willRetry')}): {error.get('message')}",
            )
        return []

    def _on_turn_started(self, turn: dict) -> list[dict]:
        """A turn began; open it as unowned unless it is one the proxy asked for.

        Ours are bound by `turn/start`'s response, which can arrive on either
        side of this notification, so one arriving while a response is still
        owed waits for the response to claim it.
        """
        codex_id = turn.get("id")
        if not isinstance(codex_id, str) or codex_id in self._turns:
            return []
        if self._tracker.typed_turn is not None:
            self._early_start = codex_id
            return []
        opened, events = self._tracker.open_unowned()
        if opened is not None:
            self._turns[codex_id] = [opened.id]
        return events

    def _on_turn_completed(self, turn: dict) -> list[dict]:
        """End every proxy turn bound to this codex turn, on codex's own status.

        Commands still open become tasks first, so the `turn_end` that
        follows already counts them.
        """
        codex_id = turn.get("id")
        turn_ids = self._turns.get(codex_id) if isinstance(codex_id, str) else None
        if not turn_ids:
            log.log_text("codex", f"turn/completed for unknown codex turn {codex_id}")
            return []
        events = self._promote_open_commands(codex_id)
        status = turn.get("status")
        error = turn.get("error") or {}
        usage = {}
        if isinstance(turn.get("durationMs"), int):
            usage["duration_ms"] = turn["durationMs"]
        for turn_id in turn_ids:
            proxy_turn = self._tracker.turn(turn_id)
            if proxy_turn is None or proxy_turn.state == STATE_ENDED:
                continue
            proxy_turn.output_tokens = self._output_tokens.get(codex_id, 0)
            if status == "completed":
                events += self._tracker.end_turn(turn_id, BASIS_COMPLETED, OUTCOME_SUCCESS, usage)
            elif status == "interrupted":
                outcome = OUTCOME_CANCELLED if proxy_turn.cancel_requested else OUTCOME_ERROR
                events += self._tracker.end_turn(turn_id, BASIS_INTERRUPTED, outcome, usage)
            else:
                events += self._tracker.end_turn(
                    turn_id,
                    BASIS_FAILED,
                    OUTCOME_ERROR,
                    usage,
                    error=error.get("message") or f"codex turn ended {status!r}",
                )
        return events

    def _promote_open_commands(self, codex_id: str) -> list[dict]:
        """Turn the commands a just-ended turn left running into `shell` tasks."""
        events: list[dict] = []
        for item_id, (owner, item) in list(self._open_commands.items()):
            if owner != codex_id:
                continue
            log.log_text("codex", f"command {item_id} outlived codex turn {codex_id}")
            events += self._tracker.start_task(
                item_id, TASK_SHELL, command=item.get("command")
            )
        return events

    def _on_item_started(self, item: dict, codex_id: object) -> list[dict]:
        item_type = item.get("type")
        if item_type in _PROMPT_ITEMS or item_type in (_TEXT_ITEM, _THINKING_ITEM, _SUBAGENT_ITEM):
            return []
        turn = self._live_turn(codex_id)
        if turn is None:
            return []
        if item_type == _COMMAND_ITEM and isinstance(item.get("id"), str):
            self._open_commands[item["id"]] = (codex_id, item)
        return [
            {
                "type": "tool_use",
                "turn": turn.ref(),
                "tool": item_type,
                "input": {k: v for k, v in item.items() if k not in _RESULT_FIELDS},
                "tool_use_id": item.get("id"),
            }
        ]

    def _on_item_completed(self, item: dict, codex_id: object) -> list[dict]:
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type == _SUBAGENT_ITEM:
            return self._on_subagent(item)
        if item_type in _PROMPT_ITEMS:
            return []
        if item_type == _COMMAND_ITEM:
            self._open_commands.pop(item_id, None)
            task = self._tracker.task(item_id) if isinstance(item_id, str) else None
            if task is not None:
                return self._tracker.end_task(
                    item_id,
                    "failed" if item.get("status") in _FAILED_STATUSES else "completed",
                    item.get("exitCode"),
                    output=item.get("aggregatedOutput"),
                )
        turn = self._live_turn(codex_id)
        if turn is None:
            return []
        if item_type == _TEXT_ITEM:
            return [{"type": "text", "turn": turn.ref(), "text": item.get("text") or ""}]
        if item_type == _THINKING_ITEM:
            text = "\n".join(
                part for part in (item.get("summary") or []) + (item.get("content") or [])
                if isinstance(part, str)
            )
            return [{"type": "thinking", "turn": turn.ref(), "text": text}] if text else []
        exit_code = item.get("exitCode")
        content = item.get("aggregatedOutput")
        if content is None:
            content = {k: v for k, v in item.items() if k not in ("id", "type")}
        return [
            {
                "type": "tool_result",
                "turn": turn.ref(),
                "tool_use_id": item_id,
                "content": content,
                "is_error": item.get("status") in _FAILED_STATUSES
                or (isinstance(exit_code, int) and exit_code != 0),
            }
        ]

    def _on_subagent(self, item: dict) -> list[dict]:
        """A subagent started or finished; it is an `agent` task, keyed by its thread."""
        agent_thread = item.get("agentThreadId")
        if not isinstance(agent_thread, str):
            return []
        kind = item.get("kind")
        if kind == "started":
            return self._tracker.start_task(agent_thread, TASK_AGENT, summary=item.get("agentPath"))
        if kind in _SUBAGENT_ENDINGS:
            return self._tracker.end_task(agent_thread, _SUBAGENT_ENDINGS[kind])
        return []

    def _note_tokens(self, params: dict) -> None:
        """Add one model call's output tokens to its turn's total."""
        codex_id = params.get("turnId")
        last = (params.get("tokenUsage") or {}).get("last") or {}
        if isinstance(codex_id, str) and isinstance(last.get("outputTokens"), int):
            self._output_tokens[codex_id] = self._output_tokens.get(codex_id, 0) + last["outputTokens"]

    def _live_turn(self, codex_id: object) -> Optional[Turn]:
        """The first still-open proxy turn bound to a codex turn, if any.

        Content for a turn that has already ended (or was never bound) is
        dropped: there is no turn left for a client to attribute it to.
        """
        for turn_id in self._turns.get(codex_id, []) if isinstance(codex_id, str) else []:
            turn = self._tracker.turn(turn_id)
            if turn is not None and turn.state != STATE_ENDED:
                return turn
        log.log_text("codex", f"content for codex turn {codex_id}, which has no open turn")
        return None
