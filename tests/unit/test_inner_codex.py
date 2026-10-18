"""How a codex session is opened: arguments mapped or refused, never dropped."""

from __future__ import annotations

import pytest

from agentproxy.inner_codex import (
    CodexArgumentError,
    build_env,
    list_params,
    plan,
    with_latest,
)

CWD = "/home/u/proj"


def test_no_arguments_start_a_new_thread_with_full_access_that_never_asks_for_approval():
    p = plan([], CWD)
    assert p.server_argv == ["codex", "app-server"]
    assert p.open_method == "thread/start"
    assert p.open_params == {"cwd": CWD, "approvalPolicy": "never", "sandbox": "danger-full-access"}
    assert not p.latest


def test_the_options_codex_has_become_thread_parameters():
    p = plan(
        ["--model", "gpt-6.1-sol", "-s", "workspace-write", "--append-system-prompt", "Be brief."],
        CWD,
    )
    assert p.open_params == {
        "cwd": CWD,
        "approvalPolicy": "never",
        "model": "gpt-6.1-sol",
        "sandbox": "workspace-write",
        "developerInstructions": "Be brief.",
    }


def test_inline_values_work_too():
    assert plan(["--model=o9"], CWD).open_params["model"] == "o9"


def test_a_config_override_goes_to_the_app_server_itself():
    p = plan(["-c", 'model_reasoning_effort="low"'], CWD)
    assert p.server_argv == ["codex", "app-server", "-c", 'model_reasoning_effort="low"']


def test_skipping_permissions_is_accepted_because_it_is_already_so():
    params = plan(["--dangerously-skip-permissions"], CWD).open_params
    assert (params["approvalPolicy"], params["sandbox"]) == ("never", "danger-full-access")


def test_a_resumed_thread_gets_full_access_too():
    assert plan(["--resume", "01a1-th"], CWD).open_params["sandbox"] == "danger-full-access"


def test_resume_with_an_id_resumes_that_thread():
    p = plan(["--resume", "01a1-th"], CWD)
    assert p.open_method == "thread/resume"
    assert p.open_params["threadId"] == "01a1-th"


def test_resume_and_fork_forks_it():
    p = plan(["--resume", "01a1-th", "--fork-session"], CWD)
    assert p.open_method == "thread/fork"
    assert p.open_params["threadId"] == "01a1-th"


@pytest.mark.parametrize("argv", [["--continue"], ["--resume"], ["--resume", "--model", "m"]])
def test_continue_or_a_bare_resume_look_up_the_newest_thread(argv):
    assert plan(argv, CWD).latest


def test_the_newest_thread_is_resumed_or_forked():
    found = {"data": [{"id": "newest"}, {"id": "older"}]}
    resumed = with_latest(plan(["--continue"], CWD), found)
    assert (resumed.open_method, resumed.open_params["threadId"]) == ("thread/resume", "newest")
    forked = with_latest(plan(["--continue", "--fork-session"], CWD), found)
    assert forked.open_method == "thread/fork"


def test_with_no_thread_in_the_directory_a_new_one_starts():
    p = with_latest(plan(["--continue", "--model", "m"], CWD), {"data": []})
    assert p.open_method == "thread/start"
    assert p.open_params == {"cwd": CWD, "approvalPolicy": "never", "sandbox": "danger-full-access", "model": "m"}


def test_the_lookup_asks_for_this_directory_newest_first():
    assert list_params(CWD) == {
        "cwd": CWD,
        "sortKey": "recency_at",
        "sortDirection": "desc",
        "limit": 1,
    }


@pytest.mark.parametrize(
    "argv, words",
    [
        (["--session-id", "4f0d"], "--session-id"),
        (["--allowedTools", "Bash"], "--allowedTools"),
        (["--disallowedTools", "Bash"], "--disallowedTools"),
        (["--mcp-config", "{}"], "--mcp-config"),
        (["--verbose"], "'--verbose'"),
        (["hello"], "'hello'"),
        (["--model"], "needs a value"),
        (["-s", "yolo"], "must be one of"),
        (["--fork-session"], "needs --resume"),
    ],
)
def test_what_codex_cannot_honour_is_refused_by_name(argv, words):
    with pytest.raises(CodexArgumentError, match=words.replace("-", r"\-")):
        plan(argv, CWD)


def test_an_api_key_never_reaches_the_app_server():
    env = build_env({"PATH": "/bin", "OPENAI_API_KEY": "sk-x", "CODEX_API_KEY": "sk-y", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}
