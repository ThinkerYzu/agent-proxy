"""`agent-proxy --agent codex` against the installed `codex`. Opt in with `pytest -m live`.

The codex counterpart of `test_live_session.py`: the one place the message
shapes are whatever the installed `codex app-server` actually sends, rather
than `tests/fake_app_server.py`'s copies of them. The app-server protocol is
marked experimental, so this is what notices it moving.

Doubly opt-in, like the claude tier, and also skipped when `codex` is not on
the path. It spends plan quota (a few short turns) and needs `codex` signed
in with ChatGPT; with any other login the proxy refuses to start, and the
first test fails saying so.

    AGENTPROXY_RUN_LIVE=1 pytest -m live tests/live/test_live_codex.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.live.test_live_session import Reader, drained, summarise


pytestmark = pytest.mark.live

SESSION_BUDGET_S = 120.0

FIRST_PROMPT = (
    "Run the shell command `sleep 4` in the foreground, then reply with "
    "exactly: FIRST"
)
SECOND_PROMPT = "Reply with exactly: SECOND. Do not run any tools."
# SPEC finding 10: the interrupt ends the turn, and the command runs on.
LONG_COMMAND_PROMPT = (
    "Run the shell command `sleep 20` in the foreground, then reply with "
    "exactly: SLEPT"
)
REMEMBER_PROMPT = "Remember the word PELICAN. Reply with exactly: NOTED"
RECALL_PROMPT = "What word did I ask you to remember? Reply with just that word."


@pytest.fixture(autouse=True)
def opt_in():
    if os.environ.get("AGENTPROXY_RUN_LIVE") != "1":
        pytest.skip(
            "needs a live `codex` round-trip; opt in with "
            "`AGENTPROXY_RUN_LIVE=1 pytest -m live`"
        )
    if shutil.which(os.environ.get("AGENTPROXY_CODEX_BIN", "codex")) is None:
        pytest.skip("no `codex` on the path")


def spawn(cwd, *args) -> subprocess.Popen:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(name, None)
    return subprocess.Popen(
        [sys.executable, "-m", "agentproxy", "--agent", "codex", *args],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )


@pytest.fixture
def proxies(tmp_path):
    started: list[subprocess.Popen] = []

    def start(*args) -> subprocess.Popen:
        proc = spawn(tmp_path, *args)
        started.append(proc)
        return proc

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def submit(proc, text, ref=None) -> None:
    command = {"type": "submit", "text": text}
    if ref is not None:
        command["ref"] = ref
    proc.stdin.write(json.dumps(command) + "\n")
    proc.stdin.flush()


def ready_of(reader: Reader) -> dict:
    messages = reader.wait_for(lambda ms: True, 30)
    assert messages, "the proxy said nothing"
    first = messages[0]
    assert first["type"] == "ready", f"codex refused to start: {first}"
    assert first["agent"] == "codex"
    return first


def test_a_prompt_queued_behind_a_running_turn_gets_its_own_turn(proxies):
    """SPEC C2 against real codex: the second prompt is held, not folded."""
    proc = proxies()
    reader = Reader(proc)
    ready_of(reader)
    submit(proc, FIRST_PROMPT, ref="a")
    submit(proc, SECOND_PROMPT, ref="b")

    def both_ended(messages):
        return sum(1 for m in messages if m["type"] == "turn_end") >= 2 and drained(messages)

    messages = reader.wait_for(both_ended, SESSION_BUDGET_S)
    print(summarise(messages))
    acks = {m["ref"]: m["turn"]["id"] for m in messages if m["type"] == "ack"}
    ends = {m["turn"]["id"]: m for m in messages if m["type"] == "turn_end"}
    assert set(ends) >= set(acks.values())
    for ref in ("a", "b"):
        assert ends[acks[ref]]["basis"] == "codex:completed"
    texts = {m["turn"]["id"]: m["text"] for m in messages if m["type"] == "text"}
    assert "SECOND" in texts.get(acks["b"], "")


def test_a_cancelled_turn_s_command_is_counted_until_it_ends(proxies):
    """SPEC C3 against real codex: `codex:interrupted`, then a `background`
    task for the still-running `sleep`, ended by codex's late report."""
    proc = proxies()
    reader = Reader(proc)
    ready_of(reader)
    submit(proc, LONG_COMMAND_PROMPT, ref="a")

    def command_started(messages):
        return any(m["type"] == "tool_use" and m["tool"] == "commandExecution" for m in messages)

    messages = reader.wait_for(command_started, SESSION_BUDGET_S)
    assert command_started(messages), summarise(messages)
    turn = next(m["turn"]["id"] for m in messages if m["type"] == "ack")
    proc.stdin.write(json.dumps({"type": "cancel", "turn": turn}) + "\n")
    proc.stdin.flush()

    def task_ended(messages):
        return any(m["type"] == "task_end" for m in messages) and not any(
            messages[-1]["outstanding"].values()
        )

    messages = reader.wait_for(task_ended, SESSION_BUDGET_S)
    print(summarise(messages))
    (end,) = [m for m in messages if m["type"] == "turn_end"]
    assert (end["basis"], end["outcome"]) == ("codex:interrupted", "cancelled")
    (task_start,) = [m for m in messages if m["type"] == "task_start"]
    assert task_start["task"]["kind"] == "shell"
    assert end["outstanding"]["background"] == 1
    (task_end,) = [m for m in messages if m["type"] == "task_end"]
    assert task_end["task"] == task_start["task"]
    assert task_end["exit_code"] == 0


def test_a_resumed_thread_keeps_its_context(proxies):
    first = proxies()
    reader = Reader(first)
    thread_id = ready_of(reader)["session_id"]
    submit(first, REMEMBER_PROMPT)
    reader.wait_for(drained, SESSION_BUDGET_S)
    first.stdin.close()
    first.wait(timeout=30)

    second = proxies("--resume", thread_id)
    reader = Reader(second)
    assert ready_of(reader)["session_id"] == thread_id
    submit(second, RECALL_PROMPT)
    messages = reader.wait_for(drained, SESSION_BUDGET_S)
    print(summarise(messages))
    answer = " ".join(m["text"] for m in messages if m["type"] == "text")
    assert "PELICAN" in answer.upper()
