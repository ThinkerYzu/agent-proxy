"""How the inner `claude` gets spawned: forwarding, hardening, and its id."""

from __future__ import annotations

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


def test_the_permission_flag_is_not_added_twice():
    argv = inner_claude.build_argv(["--dangerously-skip-permissions"])
    assert argv.count("--dangerously-skip-permissions") == 1


# ----------------------------------------------------------------------
# Session id and where the transcript is read from
# ----------------------------------------------------------------------


def test_a_vanilla_spawn_mints_its_own_id_so_the_path_is_known():
    session_id, tail_mode, argv = inner_claude.resolve_session(["--model", "opus"])
    assert tail_mode == "from_start"
    assert argv[-2:] == ["--session-id", session_id]


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


def test_an_unpredictable_id_falls_back_to_watching_the_directory():
    for argv in (["--continue"], ["-c"], ["--resume"], ["--resume", "--fork-session"]):
        session_id, tail_mode, _ = inner_claude.resolve_session(argv)
        assert (session_id, tail_mode) == (None, "discover")


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
