"""The whole proxy against a real `claude`. Opt in with `pytest -m live`.

Everything else in the suite reasons about entry shapes; this is the one place
the shapes are whatever the installed `claude` actually writes. That is the
point of it — the marker set, the launch wordings and the TUI frames all move
between releases, and only a live run says whether they still parse.

It spends real quota and takes a minute or two, so it is doubly opt-in — the
`live` marker *and* an environment variable, following claudejson's
convention, because a marker alone still runs on a bare `pytest`:

    AGENTPROXY_RUN_LIVE=1 pytest -m live

Worth running when `claude` changes version, and before trusting a change to
the pty or transcript path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.live

# One prompt that starts work outliving its turn, one that queues behind it.
# Both are written in a single stdin write, which is how a client submits
# twice in a row — and the case that stranded the second prompt for 100 s
# before the io loop stopped reading stdin through a buffered stream.
START_BACKGROUND = (
    "Use the Bash tool with run_in_background set to true to start exactly "
    "this command: sleep 12; echo finished. Do not wait for it to finish. "
    "Then reply with exactly: STARTED"
)
SECOND_PROMPT = "Reply with exactly: SECOND"

SESSION_BUDGET_S = 180.0


class Reader:
    """Collects the proxy's messages off a thread, so nothing sits unread.

    A `select` loop over `proc.stdout` would repeat the bug this project just
    fixed one layer down: several messages written together land in Python's
    buffer, where `select` cannot see them, and the last of them is never
    read. A thread blocked on `readline` has no such blind spot.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.messages: list[dict] = []
        self._thread = threading.Thread(target=self._pump, args=(proc,), daemon=True)
        self._thread.start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.strip()
            if line:
                self.messages.append(json.loads(line))

    def wait_for(self, until, budget: float) -> list[dict]:
        """Wait until `until(messages)` holds, or the budget runs out."""
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if self.messages and until(list(self.messages)):
                break
            time.sleep(0.1)
        return list(self.messages)


def summarise(messages: list[dict]) -> str:
    """The stream in one line per message, for reading a failure."""
    return "\n".join(
        f"{m['seq']:>3} {m['type']:<11} {m.get('turn', {}).get('id', ''):<4} "
        f"{m.get('turn', {}).get('kind', ''):<8} {m.get('basis', '')}"
        for m in messages
    )


def drained(messages: list[dict]) -> bool:
    """True once a turn has ended with nothing at all outstanding."""
    return messages[-1]["type"] == "turn_end" and not any(
        messages[-1]["outstanding"].values()
    )


@pytest.fixture(autouse=True)
def opt_in():
    """Spending quota has to be asked for, not merely not-excluded."""
    if os.environ.get("AGENTPROXY_RUN_LIVE") != "1":
        pytest.skip(
            "needs a live `claude` round-trip; opt in with "
            '`AGENTPROXY_RUN_LIVE=1 pytest -m live`'
        )


@pytest.fixture
def session(tmp_path):
    """A proxy over a real inner `claude`, in a directory of its own."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentproxy.cli"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def test_a_queued_prompt_a_background_task_and_an_unowned_turn_all_drain(session):
    """SPEC success criterion 2, against the `claude` that is installed.

    Also criterion 5: three end-of-turn messages reach the client, and each
    names the turn it belongs to, so a client waiting on its own prompt can
    ignore the two that are not its.
    """
    reader = Reader(session)
    session.stdin.write(
        json.dumps({"type": "submit", "text": START_BACKGROUND}) + "\n"
        + json.dumps({"type": "submit", "text": SECOND_PROMPT}) + "\n"
    )
    session.stdin.flush()

    def three_turns_ended(messages: list[dict]) -> bool:
        return sum(1 for m in messages if m["type"] == "turn_end") >= 3

    messages = reader.wait_for(three_turns_ended, SESSION_BUDGET_S)
    print(summarise(messages))
    kinds = [m["type"] for m in messages]
    assert kinds[0] == "ready"

    # Both prompts are acknowledged before either turn begins: the second is
    # accepted and counted while the first is still running.
    acks = [m for m in messages if m["type"] == "ack"]
    assert len(acks) == 2
    assert acks[1]["outstanding"]["user"] == 2
    assert kinds.index("ack") < kinds.index("turn_start")

    # The background task outlives the turn that launched it.
    launched = [m for m in messages if m["type"] == "task_start"][0]["task"]["id"]
    ends = [m for m in messages if m["type"] == "turn_end"]
    assert ends[0]["outstanding"]["background"] == 1

    # Its completion wakes a turn nobody prompted, and the turn names the
    # task that woke it.
    finished = [m for m in messages if m["type"] == "task_end"]
    assert [m["task"]["id"] for m in finished] == [launched]
    unowned = [
        m
        for m in messages
        if m["type"] == "turn_start" and m["turn"]["kind"] == "unowned"
    ]
    assert len(unowned) == 1
    assert unowned[0]["caused_by"] == launched

    # Each end-of-turn names its own turn, and every counter is back to zero.
    assert len({m["turn"]["id"] for m in ends}) == 3
    assert [m["turn"]["kind"] for m in ends].count("unowned") == 1
    assert all(e["basis"].startswith("marker:") for e in ends)
    assert drained(messages)

    # The stream is well formed all the way through.
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert all(
        set(m["outstanding"]) == {"user", "unowned", "background"} for m in messages
    )

    session.stdin.close()
    assert session.wait(timeout=60) == 0


def test_a_status_query_is_answered_while_a_turn_runs(session):
    """Requirement 8: authoritative state on demand, owing nothing to what the
    client saw before."""
    reader = Reader(session)
    session.stdin.write(
        json.dumps({"type": "submit", "text": "Reply with exactly: PONG"}) + "\n"
    )
    session.stdin.flush()
    reader.wait_for(lambda ms: any(m["type"] == "turn_start" for m in ms), 90.0)

    session.stdin.write(json.dumps({"type": "status"}) + "\n")
    session.stdin.flush()
    messages = reader.wait_for(lambda ms: any(m["type"] == "state" for m in ms), 30.0)
    state = [m for m in messages if m["type"] == "state"][-1]
    assert [turn["kind"] for turn in state["turns"]] == ["user"]

    reader.wait_for(drained, 90.0)
    session.stdin.close()
    assert session.wait(timeout=60) == 0


# ----------------------------------------------------------------------
# Requirement 12 — the session id, round-tripped through a real `claude`
# ----------------------------------------------------------------------


def run_session(cwd, passthrough, prompts, budget=SESSION_BUDGET_S):
    """One whole proxied session: submit each prompt, wait for it to drain.

    Returns the messages. Prompts go in one write, as a client sends them.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentproxy.cli", *passthrough],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        reader = Reader(proc)
        proc.stdin.write(
            "".join(json.dumps({"type": "submit", "text": p}) + "\n" for p in prompts)
        )
        proc.stdin.flush()

        def all_answered(messages):
            return sum(1 for m in messages if m["type"] == "turn_end") >= len(prompts)

        messages = reader.wait_for(all_answered, budget)
        proc.stdin.close()
        proc.wait(timeout=60)
        return messages
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def said(messages):
    """Everything the agent actually said, lowercased, as one string."""
    return " ".join(m["text"] for m in messages if m["type"] == "text").lower()


def test_the_session_id_a_client_is_given_is_one_it_can_resume(tmp_path):
    """The whole point of naming the session up front: record the id, come
    back later.

    Three sessions against a real `claude` — start one and remember its id,
    resume it by that id, then let `--continue` find it with no id at all.
    Each has to carry the earlier conversation forward, which is the proof
    that the id names the session the inner actually ran.
    """
    first = run_session(
        tmp_path, [], ["Remember this codeword: ZUCCHINI. Reply with exactly: STORED"]
    )
    assert first[0]["type"] == "ready"
    session_id = first[0]["session_id"]
    assert session_id, "the first message must name the session"
    print(f"session 1: {session_id}\n" + summarise(first))

    # Resumed by the id the client was handed.
    second = run_session(
        tmp_path, ["--resume", session_id], ["What was the codeword? Reply with just the word."]
    )
    assert second[0]["session_id"] == session_id
    assert "zucchini" in said(second), summarise(second)

    # And `--continue`, which names no session, resolves to the same one and
    # says so — before this it could only answer `null`.
    third = run_session(
        tmp_path, ["--continue"], ["Say the codeword once more, just the word."]
    )
    assert third[0]["session_id"] == session_id
    assert "zucchini" in said(third), summarise(third)


def test_a_forked_session_gets_a_new_id_and_replays_no_history(tmp_path):
    """A fork's transcript is backfilled with a copy of the original's
    exchanges, under the new session id and with the original's timestamps.
    The client must be told the *new* id, and must not be shown the copy as
    though this session had just said it.
    """
    original = run_session(
        tmp_path, [], ["Remember this codeword: AUBERGINE. Reply with exactly: STORED"]
    )
    original_id = original[0]["session_id"]

    forked = run_session(
        tmp_path,
        ["--resume", original_id, "--fork-session"],
        ["What was the codeword? Reply with just the word."],
    )
    print(f"original {original_id}, fork {forked[0]['session_id']}\n" + summarise(forked))

    # A new session, and the id is the fork's — what a later --resume needs.
    assert forked[0]["session_id"] not in (None, original_id)

    # It inherited the conversation...
    assert "aubergine" in said(forked), summarise(forked)

    # ...but the copied history is not re-emitted: exactly one turn ran here,
    # and nothing from the original session's exchange came back as news.
    assert len([m for m in forked if m["type"] == "turn_end"]) == 1
    assert "stored" not in said(forked), summarise(forked)
