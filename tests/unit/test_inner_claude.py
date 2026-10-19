"""How the inner `claude` gets spawned: forwarding, hardening, and its id."""

from __future__ import annotations

import os

import pytest

from agentproxy import inner_claude


# ----------------------------------------------------------------------
# Argument forwarding
# ----------------------------------------------------------------------


def test_print_mode_flags_are_dropped_in_both_shapes():
    """They would switch off the interactive TUI the whole design rests on."""
    argv = inner_claude.filter_passthrough(
        ["-p", "--input-format", "stream-json", "--output-format=stream-json",
         "--verbose", "--model", "opus"]
    )
    assert argv == ["--model", "opus"]


def test_everything_after_a_double_dash_is_forwarded_verbatim():
    argv = inner_claude.filter_passthrough(["--model", "opus", "--", "-p", "--verbose"])
    assert argv == ["--model", "opus", "--", "-p", "--verbose"]


# ----------------------------------------------------------------------
# Hardening
# ----------------------------------------------------------------------


def test_the_interactive_selector_is_always_denied():
    """`AskUserQuestion` waits on a keystroke no client can send, so the turn
    never ends and no end-of-turn ever fires."""
    argv = inner_claude.build_argv([])
    assert argv[:2] == ["claude", "--dangerously-skip-permissions"]
    assert argv[-2:] == ["--disallowedTools", "AskUserQuestion"]


def test_a_caller_s_own_disallow_list_is_extended_not_duplicated():
    argv = inner_claude.build_argv(["--disallowedTools", "WebFetch"])
    assert argv.count("--disallowedTools") == 1
    assert argv[-2:] == ["WebFetch", "AskUserQuestion"]


def test_the_equals_shape_is_folded_into_the_same_value_group():
    argv = inner_claude.build_argv(["--disallowedTools=WebFetch"])
    assert "--disallowedTools=WebFetch,AskUserQuestion" in argv


def test_a_caller_who_already_denied_it_is_left_alone():
    argv = inner_claude.build_argv(["--disallowedTools", "AskUserQuestion"])
    assert argv.count("AskUserQuestion") == 1


def test_the_denial_goes_before_a_trailing_double_dash(tmp_path):
    """Arguments after `--` are not parsed as options by the inner, so a
    denial appended past one would reach the model as prompt text and leave
    `AskUserQuestion` in its inventory — the deadlock the flag exists to
    prevent, silently un-prevented."""
    argv = inner_claude.build_argv(["--", "write me a poem"])
    assert argv.index("--disallowedTools") < argv.index("--")
    assert argv[-2:] == ["--", "write me a poem"]
    # The `--` also terminates the flag's variadic value list, so the
    # positional text is not swallowed as another tool name.
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion"


def test_a_positional_disallow_flag_is_not_mistaken_for_a_real_one():
    """After `--` it is prompt text, not a denial, so the real one is still
    needed in the option section."""
    argv = inner_claude.build_argv(["--", "--disallowedTools", "AskUserQuestion"])
    before = argv[: argv.index("--")]
    assert "--disallowedTools" in before
    assert "AskUserQuestion" in before


def test_the_permission_flag_is_not_added_twice():
    argv = inner_claude.build_argv(["--dangerously-skip-permissions"])
    assert argv.count("--dangerously-skip-permissions") == 1


def test_an_explicit_autocompact_window_prevents_the_optional_suggestion():
    argv = inner_claude.build_argv([])
    assert argv.count("--autocompact") == 1
    assert argv[argv.index("--autocompact") + 1] == "1m"


@pytest.mark.parametrize("override", [
    ["--autocompact", "400k"], ["--autocompact=400k"],
    ["--autocompact", "auto"], ["--autocompact=auto"],
])
def test_a_callers_autocompact_choice_is_preserved_without_a_default(override):
    argv = inner_claude.build_argv(override + ["--", "prompt"])
    options = argv[:argv.index("--")]
    assert [arg for arg in options if arg.startswith("--autocompact")] == [override[0]]
    index = options.index(override[0])
    assert options[index:index + len(override)] == override
    assert "1m" not in options


@pytest.mark.parametrize("positional", [
    ["prompt"], ["--autocompact", "400k"], ["--autocompact=auto"],
])
def test_autocompact_default_precedes_double_dash_and_ignores_positional_flags(positional):
    argv = inner_claude.build_argv(["--disallowedTools", "WebFetch", "--", *positional])
    cut = argv.index("--")
    assert argv[cut:] == ["--", *positional]
    options = argv[:cut]
    assert options.count("--autocompact") == 1
    assert options[options.index("--autocompact") + 1] == "1m"
    assert options.index("--autocompact") < options.index("--disallowedTools")


# ----------------------------------------------------------------------
# Session id and where the transcript is read from
# ----------------------------------------------------------------------


def test_a_vanilla_spawn_mints_its_own_id_so_the_path_is_known():
    session_id, tail_mode, argv = inner_claude.resolve_session(["--model", "opus"])
    assert tail_mode == "from_start"
    assert argv == ["--session-id", session_id, "--model", "opus"]


def test_the_session_flags_go_in_front_of_a_trailing_double_dash(tmp_path):
    """Anything after `--` is a positional argument to the inner, so a session
    flag appended there would be read as prompt text and the session would not
    be the one the client was told about."""
    session_id, _, argv = inner_claude.resolve_session(
        ["--", "write me a poem"], cwd="/work/empty", projects_root=tmp_path
    )
    assert argv == ["--session-id", session_id, "--", "write me a poem"]
    assert argv.index("--session-id") < argv.index("--")


def test_a_named_session_is_read_from_the_start():
    assert inner_claude.resolve_session(["--session-id", "abc"])[:2] == (
        "abc",
        "from_start",
    )


def test_a_resumed_session_is_read_from_the_end():
    """Its file already holds prior turns; replaying them would emit history
    as this session's messages."""
    for argv in (["--resume", "abc"], ["--resume=abc"], ["-r", "abc"]):
        assert inner_claude.resolve_session(argv)[:2] == ("abc", "from_end")


def test_a_resumed_session_is_named_once_however_the_caller_spelled_it():
    """The caller's flag is replaced, not supplemented — `--resume=abc` and a
    second `--resume abc` reaching the inner together would be ambiguous."""
    _, _, argv = inner_claude.resolve_session(["--resume=abc", "--model", "opus"])
    assert argv.count("--resume") == 1
    assert "--resume=abc" not in argv
    assert argv == ["--resume", "abc", "--model", "opus"]


def _project_dir(tmp_path, cwd):
    """Make the transcript directory `cwd`'s sessions would be written to."""
    from agentproxy.transcript_tailer import encode_cwd

    made = tmp_path / encode_cwd(cwd)
    made.mkdir(parents=True)
    return made


def _write_session(project_dir, session_id, mtime):
    path = project_dir / f"{session_id}.jsonl"
    path.write_text('{"type":"user"}\n')
    os.utime(path, (mtime, mtime))
    return path


# ----------------------------------------------------------------------
# Requirement 12 — every invocation resolves to a session id
# ----------------------------------------------------------------------


def test_continue_resolves_to_the_newest_session_itself(tmp_path):
    """`--continue` names no session, so the proxy picks one and says which.

    Rewriting to `--resume <id>` is what stops the proxy and the inner
    disagreeing about which file this session is.
    """
    cwd = "/work/thing"
    project_dir = _project_dir(tmp_path, cwd)
    _write_session(project_dir, "older", mtime=1000)
    _write_session(project_dir, "newest", mtime=2000)

    for flag in (["--continue"], ["-c"], ["--resume"], ["-r"]):
        session_id, tail_mode, argv = inner_claude.resolve_session(
            flag, cwd=cwd, projects_root=tmp_path
        )
        assert (session_id, tail_mode) == ("newest", "from_end")
        assert argv == ["--resume", "newest"]
        assert "--continue" not in argv and "-c" not in argv


def test_an_empty_transcript_is_not_offered_as_a_session(tmp_path):
    """A session that never got written is not one a client can be handed."""
    cwd = "/work/thing"
    project_dir = _project_dir(tmp_path, cwd)
    _write_session(project_dir, "real", mtime=1000)
    (project_dir / "empty.jsonl").write_text("")
    os.utime(project_dir / "empty.jsonl", (9999, 9999))

    session_id, _, _ = inner_claude.resolve_session(
        ["--continue"], cwd=cwd, projects_root=tmp_path
    )
    assert session_id == "real"


def test_continue_with_nothing_to_continue_starts_a_named_new_session(tmp_path):
    """What the inner does there too — it starts a conversation rather than
    failing — and a stated new id is more use to a client than a refusal."""
    session_id, tail_mode, argv = inner_claude.resolve_session(
        ["--continue"], cwd="/work/empty", projects_root=tmp_path
    )
    assert tail_mode == "from_start"
    assert argv == ["--session-id", session_id]
    assert "--continue" not in argv


def test_a_fork_reports_the_new_session_not_the_one_it_forked_from(tmp_path):
    """The id a client records must be the one a later `--resume` can name."""
    cwd = "/work/thing"
    _write_session(_project_dir(tmp_path, cwd), "original", mtime=1000)

    session_id, tail_mode, argv = inner_claude.resolve_session(
        ["--resume", "original", "--fork-session"], cwd=cwd, projects_root=tmp_path
    )
    assert tail_mode == "fork"
    assert session_id != "original"
    assert argv == [
        "--resume",
        "original",
        "--fork-session",
        "--session-id",
        session_id,
    ]


def test_a_fork_can_be_told_which_id_to_use(tmp_path):
    cwd = "/work/thing"
    _write_session(_project_dir(tmp_path, cwd), "original", mtime=1000)
    session_id, _, argv = inner_claude.resolve_session(
        ["--continue", "--fork-session", "--session-id", "chosen"],
        cwd=cwd,
        projects_root=tmp_path,
    )
    assert session_id == "chosen"
    assert argv == ["--resume", "original", "--fork-session", "--session-id", "chosen"]


def test_fork_with_nothing_to_fork_from_is_just_a_new_session(tmp_path):
    session_id, tail_mode, argv = inner_claude.resolve_session(
        ["--fork-session"], cwd="/work/empty", projects_root=tmp_path
    )
    assert tail_mode == "from_start"
    assert argv == ["--session-id", session_id]


def test_naming_a_session_while_resuming_another_is_refused(tmp_path):
    """The inner rejects this combination outright; catching it here turns a
    spawn that dies into a message that says why."""
    with pytest.raises(inner_claude.SessionArgumentError):
        inner_claude.resolve_session(
            ["--resume", "abc", "--session-id", "def"],
            cwd="/work/thing",
            projects_root=tmp_path,
        )


def test_every_invocation_resolves_to_some_session_id(tmp_path):
    """Requirement 12's whole content: there is no invocation left that the
    proxy cannot name a session for."""
    cwd = "/work/thing"
    _write_session(_project_dir(tmp_path, cwd), "existing", mtime=1000)
    invocations = (
        [],
        ["--model", "opus"],
        ["--session-id", "named"],
        ["--resume", "existing"],
        ["--continue"],
        ["--resume"],
        ["-c", "--fork-session"],
        ["--continue", "--model", "opus"],
    )
    for argv in invocations:
        session_id, tail_mode, _ = inner_claude.resolve_session(
            argv, cwd=cwd, projects_root=tmp_path
        )
        assert session_id, f"{argv} resolved to no session id"
        assert tail_mode in ("from_start", "from_end", "fork")


def test_the_environment_pins_geometry_and_turns_colour_off():
    env = inner_claude.build_env({"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"
    assert env["NO_COLOR"] == "1"
    assert (env["COLUMNS"], env["LINES"]) == ("120", "40")


def test_transcript_saving_is_forced_on():
    """An inner that writes no transcript runs turns the proxy cannot see.

    `CLAUDE_CODE_CHILD_SESSION` is set in every process a Claude Code session
    launches, and it switches transcript saving off — so a client running
    under `claude` would otherwise spawn a blind proxy.
    """
    env = inner_claude.build_env({"CLAUDE_CODE_CHILD_SESSION": "1"})
    assert env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1"
