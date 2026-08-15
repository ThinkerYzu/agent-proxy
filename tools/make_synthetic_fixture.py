"""Build a synthetic transcript with the same entry shapes as a real session.

    python3 tools/make_synthetic_fixture.py            # writes tests/fixtures/

Writes `synthetic-session.jsonl` and its `synthetic-session.prompts.json`
sidecar, in exactly the form `tools/capture_fixture.py` produces from a real
`~/.claude/projects` transcript, so `agentproxy.replay` cannot tell them
apart.

**Why synthesise one.** A captured session carries whatever was being
discussed, which is fine on a laptop and not fine in a repository that may be
pushed. Everything the tracker reads is structure — entry types, content
block types, marker subtypes, the wording of two launch announcements, the
tags of a task notification — and none of it is the conversation. So the
conversation can be invented and the structure kept exact.

**What is faithful here.** Every field of every entry is copied from the
shapes observed in session 08ff12b4 (SPEC Appendix A): the envelope keys, the
usage block, the `queue-operation` pairs, the four-entry cluster `/compact`
writes after its boundary, and the noise types the tailer skips. The session
this builds is deliberately harder than the recorded one — three background
tasks across both wordings and both completion contexts, a task outstanding
across three turns, and a nonzero exit code — but every shape in it was seen
in a real transcript first.

Regenerate after editing, and check the replay still drains:

    python3 tools/make_synthetic_fixture.py
    python3 -m agentproxy.replay tests/fixtures/synthetic-session.jsonl \
        --quiet --assert-drained
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


SESSION_ID = "5f2c0a1e-0000-4000-8000-a6d3b1c40001"
VERSION = "2.1.232"
CWD = "/home/example/src/widget-sorter"
GIT_BRANCH = "main"
MODEL = "claude-opus-5"
TASK_DIR = f"/tmp/agent-proxy-fixture/{SESSION_ID}/tasks"

# One fixed instant, so regenerating the fixture changes nothing.
START = datetime(2026, 3, 9, 17, 4, 11, tzinfo=timezone.utc)
STEP = timedelta(seconds=3, milliseconds=140)


class Session:
    """Accumulates entries, handing out uuids, timestamps and parent links.

    The counters make the output byte-identical from run to run, which is
    what lets the fixture be reviewed as a diff.
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.prompts: list[dict] = []
        self._uuid = 0
        self._msg = 0
        self._clock = START
        self._parent: str | None = None
        self._prompt_id = self._new_uuid()

    # -- identifiers ---------------------------------------------------

    def _new_uuid(self) -> str:
        self._uuid += 1
        return f"7c1e0000-0000-4000-8000-{self._uuid:012d}"

    def _new_message_id(self) -> str:
        self._msg += 1
        return f"msg_01SYNTH{self._msg:08d}"

    def _tick(self) -> str:
        self._clock += STEP
        return self._clock.isoformat().replace("+00:00", "Z")

    # -- emission ------------------------------------------------------

    def add(self, entry: dict, *, chain: bool = True) -> dict:
        """Append an entry, threading `parentUuid` through the ones that chain.

        Bookkeeping types (`mode`, `ai-title`, the file-history pair) carry no
        uuid in a real transcript and so do not chain.
        """
        self.entries.append(entry)
        if chain:
            self._parent = entry["uuid"]
        return entry

    def conversation_envelope(self) -> dict:
        return {
            "userType": "external",
            "entrypoint": "cli",
            "cwd": CWD,
            "sessionId": SESSION_ID,
            "version": VERSION,
            "gitBranch": GIT_BRANCH,
        }

    # -- entry builders ------------------------------------------------

    def prompt(self, text: str) -> dict:
        """A prompt the proxy typed: a user entry, plus its sidecar record.

        The sidecar names the line index, because provenance is exactly what
        a transcript does not record — see `capture_fixture.py`.
        """
        self._prompt_id = self._new_uuid()
        self.prompts.append({"index": len(self.entries), "text": text})
        return self.add(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "promptId": self._prompt_id,
                "type": "user",
                "message": {"role": "user", "content": text},
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                "permissionMode": "bypassPermissions",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                **self.conversation_envelope(),
            }
        )

    def user_string(self, text: str, **extra) -> dict:
        """A user-shaped entry nobody typed — the compact cluster, a notification."""
        return self.add(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "promptId": self._prompt_id,
                "type": "user",
                "message": {"role": "user", "content": text},
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                "permissionMode": "bypassPermissions",
                **extra,
                **self.conversation_envelope(),
            }
        )

    def assistant(self, blocks: list[dict], output_tokens: int) -> dict:
        return self.add(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "message": {
                    "model": MODEL,
                    "id": self._new_message_id(),
                    "type": "message",
                    "role": "assistant",
                    "content": blocks,
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "stop_details": None,
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 1024,
                        "cache_read_input_tokens": 8192,
                        "output_tokens": output_tokens,
                        "output_tokens_details": {"thinking_tokens": 0},
                        "service_tier": "standard",
                        "speed": "standard",
                    },
                    "diagnostics": None,
                },
                "requestId": f"req_01SYNTH{self._msg:08d}",
                "type": "assistant",
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                "effort": "high",
                "session_id": SESSION_ID,
                **self.conversation_envelope(),
            }
        )

    def text(self, text: str, output_tokens: int = 96) -> dict:
        return self.assistant([{"type": "text", "text": text}], output_tokens)

    def thinking(self, text: str = "", output_tokens: int = 64) -> dict:
        return self.assistant(
            [{"type": "thinking", "thinking": text, "signature": "CAISzQMKhwEIEBgCKkB"}],
            output_tokens,
        )

    def tool_use(
        self,
        tool_use_id: str,
        name: str,
        tool_input: dict,
        output_tokens: int = 128,
    ) -> dict:
        return self.assistant(
            [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": tool_input,
                    "caller": {"type": "direct"},
                }
            ],
            output_tokens,
        )

    def tool_result(
        self,
        tool_use_id: str,
        content: object,
        tool_use_result: dict | None = None,
    ) -> dict:
        return self.add(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "promptId": self._prompt_id,
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": content,
                            "is_error": False,
                        }
                    ],
                },
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                "toolUseResult": tool_use_result
                or {
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
                "sourceToolAssistantUUID": self._parent,
                "session_id": SESSION_ID,
                **self.conversation_envelope(),
            }
        )

    def turn_duration(self, duration_ms: int) -> dict:
        return self.add(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "type": "system",
                "subtype": "turn_duration",
                "durationMs": duration_ms,
                "messageCount": len(self.entries),
                "timestamp": self._tick(),
                "uuid": self._new_uuid(),
                "isMeta": False,
                **self.conversation_envelope(),
            }
        )

    def compact_boundary(self, duration_ms: int) -> dict:
        tail = self._parent
        entry = self.add(
            {
                "parentUuid": None,
                "logicalParentUuid": tail,
                "isSidechain": False,
                "type": "system",
                "subtype": "compact_boundary",
                "content": "Conversation compacted",
                "level": "info",
                "compactMetadata": {
                    "trigger": "manual",
                    "preTokens": 198340,
                    "postTokens": 12105,
                    "cumulativeDroppedTokens": 186235,
                    "durationMs": duration_ms,
                    "preCompactDiscoveredTools": ["Bash", "Read"],
                    "preservedSegment": {
                        "headUuid": tail,
                        "anchorUuid": tail,
                        "tailUuid": tail,
                    },
                },
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                **self.conversation_envelope(),
            }
        )
        return entry

    def queue_operation(self, operation: str, content: str | None = None) -> dict:
        entry = {
            "type": "queue-operation",
            "operation": operation,
            "timestamp": self._tick(),
            "sessionId": SESSION_ID,
        }
        if content is not None:
            entry["content"] = content
        self.entries.append(entry)
        return entry

    def bookkeeping(self, *, title: str | None = None, last_prompt: str | None = None):
        """The types the tailer skips, interleaved as a real session has them.

        They are here so the fixture exercises `SKIP_TYPES` rather than
        assuming it: a filter that stops working shows up as a phantom turn.
        """
        self.entries.append({"type": "mode", "mode": "normal", "sessionId": SESSION_ID})
        self.entries.append(
            {
                "type": "permission-mode",
                "permissionMode": "bypassPermissions",
                "sessionId": SESSION_ID,
            }
        )
        if title is not None:
            self.entries.append(
                {"type": "ai-title", "aiTitle": title, "sessionId": SESSION_ID}
            )
        if last_prompt is not None:
            self.entries.append(
                {
                    "type": "last-prompt",
                    "lastPrompt": last_prompt,
                    "leafUuid": self._parent,
                    "sessionId": SESSION_ID,
                }
            )

    def attachment(self, kind: str = "file_history") -> None:
        self.entries.append(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "type": "attachment",
                "attachment": {"type": kind},
                "uuid": self._new_uuid(),
                "timestamp": self._tick(),
                **self.conversation_envelope(),
            }
        )

    def file_history_snapshot(self) -> None:
        self.entries.append(
            {
                "type": "file-history-snapshot",
                "messageId": self._parent,
                "snapshot": {
                    "messageId": self._parent,
                    "trackedFileBackups": {},
                    "timestamp": self._tick(),
                },
                "isSnapshotUpdate": False,
            }
        )


# ----------------------------------------------------------------------
# The wordings that carry meaning
# ----------------------------------------------------------------------


def launched_in_background(task_id: str) -> str:
    """What `run_in_background: true` puts in the tool_result."""
    return (
        f"Command running in background with ID: {task_id}. Output is being "
        f"written to: {TASK_DIR}/{task_id}.output."
    )


def moved_to_background(task_id: str, timeout_s: int = 120) -> str:
    """What a *foreground* command that outlives its timeout puts there.

    The second launch shape, and the reason the tool_result rather than the
    tool_use is the launch signal: this task has no `run_in_background` flag
    anywhere, and its id appears nowhere else.
    """
    return (
        f"Command did not complete within its {timeout_s}s timeout and was "
        f"moved to the background (ID: {task_id}). Output is being written "
        f"to: {TASK_DIR}/{task_id}.output."
    )


def notification(task_id: str, tool_use_id: str, summary: str, exit_code: int) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<output-file>{TASK_DIR}/{task_id}.output</output-file>\n"
        "<status>completed</status>\n"
        f'<summary>Background command "{summary}" completed '
        f"(exit code {exit_code})</summary>\n"
        "</task-notification>"
    )


COMPACT_SUMMARY = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The summary below covers the earlier portion of the "
    "conversation.\n\nThe user asked about widget-sorter's comparator and the "
    "benchmark harness; both were read and explained."
)

COMPACT_CAVEAT = (
    "<local-command-caveat>Caveat: The messages below were generated by the "
    "user while running local commands. DO NOT respond to these messages or "
    "otherwise consider them in your response unless the user explicitly asks "
    "you to.</local-command-caveat>"
)

COMPACT_COMMAND = (
    "<command-name>/compact</command-name>\n"
    "            <command-message>compact</command-message>\n"
    "            <command-args></command-args>"
)

COMPACT_STDOUT = "<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------


def build() -> Session:
    """Write the session, one turn at a time.

    Nine turns: eight prompted, one woken by a background task finishing while
    the agent was idle. Three background tasks cover both launch wordings and
    both completion contexts; one of them stays outstanding across three
    consecutive turns, which is the case the counters exist for.
    """
    s = Session()
    s.bookkeeping()
    s.file_history_snapshot()

    # -- turn 1: an ordinary turn, one foreground tool ------------------
    s.prompt("what does sort_widgets do?")
    s.bookkeeping(title="widget-sorter walkthrough", last_prompt="what does sort_widgets do?")
    s.text("I'll read the sorter and tell you.")
    s.tool_use(
        "toolu_01SYNTHREAD0001",
        "Read",
        {"file_path": f"{CWD}/sorter/core.py"},
    )
    s.tool_result(
        "toolu_01SYNTHREAD0001",
        "def sort_widgets(widgets, key=None):\n    return sorted(widgets, key=key or size_of)\n",
    )
    s.attachment()
    s.text(
        "`sort_widgets` is a thin wrapper over `sorted`, defaulting the key to "
        "`size_of`. Nothing else in the module touches ordering."
    )
    s.turn_duration(18422)

    # -- turn 2: launches a task that finishes inside the same turn -----
    s.prompt("run the benchmark in the background while we keep reading")
    s.bookkeeping(last_prompt="run the benchmark in the background while we keep reading")
    s.thinking()
    s.tool_use(
        "toolu_01SYNTHBASH0001",
        "Bash",
        {
            "command": "python3 -m sorter.bench --runs 200",
            "description": "Run the widget-sorter benchmark",
            "run_in_background": True,
        },
    )
    s.tool_result("toolu_01SYNTHBASH0001", launched_in_background("bsynthbench"))
    s.text("Benchmark is running. While it works, here is the comparator.")
    s.tool_use("toolu_01SYNTHREAD0002", "Read", {"file_path": f"{CWD}/sorter/compare.py"})
    s.tool_result(
        "toolu_01SYNTHREAD0002",
        [
            {"type": "text", "text": "def size_of(widget):\n    return widget.width * widget.height\n"},
        ],
    )
    # The task finishes mid-turn: enqueued, then removed from the queue and
    # folded into the running turn. No turn is started, and no user entry
    # carries the notification — the queue pair is the only signal.
    s.queue_operation(
        "enqueue",
        notification(
            "bsynthbench",
            "toolu_01SYNTHBASH0001",
            "Run the widget-sorter benchmark",
            0,
        ),
    )
    s.queue_operation(
        "remove",
        notification(
            "bsynthbench",
            "toolu_01SYNTHBASH0001",
            "Run the widget-sorter benchmark",
            0,
        ),
    )
    s.text("The benchmark finished mid-answer: 200 runs, median 4.1 ms.")
    s.turn_duration(41077)

    # -- turn 3: launches the task that outlives three turns ------------
    s.prompt("start the corpus import in the background too")
    s.bookkeeping(last_prompt="start the corpus import in the background too")
    s.tool_use(
        "toolu_01SYNTHBASH0002",
        "Bash",
        {
            "command": "python3 -m sorter.import_corpus ./corpus",
            "description": "Import the widget corpus",
            "run_in_background": True,
        },
    )
    s.tool_result("toolu_01SYNTHBASH0002", launched_in_background("bsynthimport"))
    s.text("Import started. It will take a while; ask me anything meanwhile.")
    s.turn_duration(9310)

    # -- turns 4 and 5: ordinary turns with a task still outstanding ----
    s.prompt("how is the comparator tested?")
    s.bookkeeping(last_prompt="how is the comparator tested?")
    s.tool_use("toolu_01SYNTHGREP0001", "Grep", {"pattern": "size_of", "path": "tests"})
    s.tool_result("toolu_01SYNTHGREP0001", "tests/test_compare.py:11:def test_size_of_orders_by_area():")
    s.attachment()
    s.text("One test, `test_size_of_orders_by_area`, covering the default key only.")
    s.turn_duration(12880)

    s.prompt("add a case for equal areas")
    s.bookkeeping(last_prompt="add a case for equal areas")
    s.thinking()
    s.tool_use(
        "toolu_01SYNTHEDIT0001",
        "Edit",
        {
            "file_path": f"{CWD}/tests/test_compare.py",
            "old_string": "def test_size_of_orders_by_area():",
            "new_string": "def test_equal_areas_keep_input_order():\n    pass\n\n\ndef test_size_of_orders_by_area():",
        },
    )
    s.tool_result("toolu_01SYNTHEDIT0001", "The file tests/test_compare.py has been updated.")
    s.file_history_snapshot()
    s.text("Added `test_equal_areas_keep_input_order`, asserting the sort is stable.")
    s.turn_duration(26514)

    # -- the unowned turn: the import finishes while nothing is running --
    # Enqueued and then *dequeued* — the asymmetry that makes this one a turn
    # rather than a fold. The notification arrives twice, once in the queue
    # operation and once as the user entry it becomes.
    import_note = notification(
        "bsynthimport", "toolu_01SYNTHBASH0002", "Import the widget corpus", 0
    )
    s.queue_operation("enqueue", import_note)
    s.queue_operation("dequeue")
    s.user_string(
        import_note,
        origin={"kind": "task-notification"},
        promptSource="system",
    )
    s.bookkeeping()
    s.thinking()
    s.text("The corpus import finished with exit code 0 — 12,400 widgets loaded.")
    s.turn_duration(7902)

    # -- turn 6: /compact, and the four entries it writes afterwards ----
    s.prompt("/compact")
    s.bookkeeping(last_prompt="/compact")
    s.compact_boundary(121004)
    s.user_string(COMPACT_SUMMARY, isCompactSummary=True)
    s.user_string(COMPACT_CAVEAT, isMeta=True)
    s.user_string(COMPACT_COMMAND)
    s.user_string(COMPACT_STDOUT)
    s.attachment()
    s.bookkeeping()

    # -- turn 7: a foreground command promoted to the background --------
    s.prompt("find where the corpus path is configured")
    s.bookkeeping(last_prompt="find where the corpus path is configured")
    s.tool_use(
        "toolu_01SYNTHBASH0003",
        "Bash",
        {
            "command": "grep -rn 'corpus_path' /",
            "description": "Locate the corpus path setting",
        },
    )
    s.tool_result(
        "toolu_01SYNTHBASH0003",
        moved_to_background("bsynthgrep"),
        tool_use_result={
            "stdout": "",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "backgroundTaskId": "bsynthgrep",
            "timedOutAfterMs": 120000,
        },
    )
    s.text("That grep was slow enough to be moved to the background; I'll check it shortly.")
    s.turn_duration(133918)

    # -- turn 8: the promoted task completes mid-turn, with exit code 1 --
    s.prompt("meanwhile, what is the default corpus path?")
    s.bookkeeping(last_prompt="meanwhile, what is the default corpus path?")
    s.tool_use("toolu_01SYNTHREAD0003", "Read", {"file_path": f"{CWD}/sorter/config.py"})
    s.tool_result("toolu_01SYNTHREAD0003", 'CORPUS_PATH = "./corpus"\n')
    grep_note = notification(
        "bsynthgrep", "toolu_01SYNTHBASH0003", "Locate the corpus path setting", 1
    )
    s.queue_operation("enqueue", grep_note)
    s.queue_operation("remove", grep_note)
    s.text(
        "`CORPUS_PATH` defaults to `./corpus`. The background grep exited 1 — "
        "permission denied on parts of `/` — so it adds nothing."
    )
    s.turn_duration(21460)

    return s


def write(session: Session, target: Path) -> dict:
    target.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in session.entries)
        + "\n",
        encoding="utf-8",
    )
    sidecar = target.with_suffix(".prompts.json")
    sidecar.write_text(
        json.dumps(
            {"source": "synthetic", "prompts": session.prompts}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "entries": len(session.entries),
        "prompts": len(session.prompts),
        "fixture": str(target),
        "sidecar": str(sidecar),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "synthetic-session.jsonl",
    )
    args = parser.parse_args(argv)
    print(json.dumps(write(build(), args.target), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
