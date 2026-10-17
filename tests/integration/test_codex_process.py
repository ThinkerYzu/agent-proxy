"""`agent-proxy --agent codex` as a process, against the fake app-server over stdio.

What `tests/requirements/test_codex.py` cannot reach: argv parsing, the
startup order, the environment the app-server is spawned into, the loop over
real pipes, and the exit codes. `AGENTPROXY_CODEX_BIN` points the proxy at a
wrapper that runs `tests/fake_app_server.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parent.parent / "fake_app_server.py"


@pytest.fixture
def codex_bin(tmp_path) -> Path:
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def run_proxy(codex_bin, tmp_path, args=(), lines=(), env_extra=None, timeout=20):
    """Run one proxy to completion; return (exit status, messages)."""
    env = {**os.environ, "AGENTPROXY_CODEX_BIN": str(codex_bin), **(env_extra or {})}
    env.pop("FAKE_CODEX_ACCOUNT", None)
    if env_extra and "FAKE_CODEX_ACCOUNT" in env_extra:
        env["FAKE_CODEX_ACCOUNT"] = env_extra["FAKE_CODEX_ACCOUNT"]
    stdin = "".join(json.dumps(line) + "\n" for line in lines)
    done = subprocess.run(
        [sys.executable, "-m", "agentproxy", "--agent", "codex", *args],
        input=stdin.encode(),
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=timeout,
    )
    messages = [json.loads(line) for line in done.stdout.decode().splitlines()]
    return done.returncode, messages


def test_a_prompt_runs_end_to_end_and_the_stream_drains(codex_bin, tmp_path):
    status, messages = run_proxy(
        codex_bin, tmp_path, lines=[{"type": "submit", "text": "reply ok", "ref": "a"}]
    )
    assert status == 0
    assert [m["type"] for m in messages] == ["ready", "ack", "turn_start", "text", "turn_end"]
    assert messages[0]["agent"] == "codex"
    assert messages[-1]["basis"] == "codex:completed"
    assert not any(messages[-1]["outstanding"].values())


def test_an_api_key_in_the_environment_never_reaches_codex(codex_bin, tmp_path):
    """The fake reports an API-key login if it can see a key, as real codex
    would bill one; the proxy starting normally means it saw none."""
    status, messages = run_proxy(
        codex_bin,
        tmp_path,
        lines=[{"type": "status"}],
        env_extra={"OPENAI_API_KEY": "sk-test", "CODEX_API_KEY": "sk-test"},
    )
    assert status == 0
    assert messages[0]["type"] == "ready"


@pytest.mark.parametrize("account", ["apiKey", "none"])
def test_an_account_that_is_not_chatgpt_is_refused_before_ready(codex_bin, tmp_path, account):
    status, messages = run_proxy(codex_bin, tmp_path, env_extra={"FAKE_CODEX_ACCOUNT": account})
    assert status == 2
    (only,) = messages
    assert only["type"] == "error"
    assert "not ChatGPT" in only["error"]


def test_resuming_a_thread_codex_does_not_know_is_refused(codex_bin, tmp_path):
    """Requirement 15(b): a claude session id handed to codex is an error at
    startup, not an empty conversation."""
    status, messages = run_proxy(
        codex_bin, tmp_path, args=["--resume", "4f0d2a61-claude-session"]
    )
    assert status == 2
    assert messages[0]["type"] == "error"
    assert "not found" in messages[0]["error"]


def test_continue_resumes_the_newest_thread(codex_bin, tmp_path):
    status, messages = run_proxy(codex_bin, tmp_path, args=["--continue"])
    assert status == 0
    assert messages[0]["session_id"] == "th-old"


def test_a_claude_only_option_is_refused_by_name(codex_bin, tmp_path):
    status, messages = run_proxy(codex_bin, tmp_path, args=["--allowedTools", "Bash"])
    assert status == 2
    assert messages == [
        {
            "seq": 1,
            "type": "error",
            "outstanding": messages[0]["outstanding"],
            "error": "--allowedTools has no codex equivalent",
        }
    ]


def test_a_missing_codex_binary_is_reported(tmp_path):
    status, messages = run_proxy(tmp_path / "no-such-codex", tmp_path)
    assert status == 2
    assert "not found" in messages[0]["error"]
