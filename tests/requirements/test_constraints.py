"""The constraints and the resolved decisions, where they are checkable.

Requirements say what the proxy must do; these say what it must not become.
Most are one-line properties that nothing else would notice breaking — a
dependency creeping in, a cancel that ends a turn nobody saw stop, a content
rule sneaking into turn classification.

| Item | Test |
|---|---|
| Constraint 5 / NFR 2 — no new dependencies, Python 3.11+ | `test_constraint_5_*`, `test_nfr_2_*` |
| Constraint 7 — stdio, ordered, no request/response | `test_constraint_7_*` |
| R3 — an unowned turn is one the proxy did not prompt | `test_r3_*` |
| R11 — cancellation is a request the proxy makes and an ending it observes | `test_r11_*` |
| NFR 3 — counters are self-correcting | `test_nfr_3_*` |

"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

from tests.harness import (
    Session,
    assistant_text,
    assistant_thinking,
    assistant_tool_use,
    interruption,
    background_launch,
    marker,
    notification,
    queue_operation,
    user_text,
)


REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "agentproxy"
ALLOWED_DEPENDENCIES = {"pyte", "ptyprocess"}


@pytest.fixture
def session() -> Session:
    return Session()


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# Constraint 5 and non-functional requirement 2 — what may be installed
# ----------------------------------------------------------------------


def test_constraint_5_the_declared_dependencies_are_the_two_inherited_ones(pyproject):
    """`pyte` and `ptyprocess`, both already required by claudejson.

    The proxy runs wherever claudejson runs today, on the same machine and in
    the same environment, so anything else added here is a new thing to
    install on a machine that is already working.
    """
    declared = {
        requirement.split(">")[0].split("=")[0].strip()
        for requirement in pyproject["project"]["dependencies"]
    }
    assert declared == ALLOWED_DEPENDENCIES


def test_constraint_5_the_source_imports_nothing_beyond_them():
    """The declaration is only half of it — this reads the imports themselves.

    A module reaching for `requests` or `pydantic` would work on the machine
    that added it and fail on the one that installs from `pyproject.toml`.
    """
    imported: set[str] = set()
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = imported - sys.stdlib_module_names - {"agentproxy"}
    assert third_party <= ALLOWED_DEPENDENCIES, sorted(third_party)


def test_nfr_2_python_311_is_the_floor(pyproject):
    """Both clients run on it today; the proxy must not need anything newer."""
    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert sys.version_info >= (3, 11)


# ----------------------------------------------------------------------
# Constraint 7 — stdio is ordered and single-client
# ----------------------------------------------------------------------


def test_constraint_7_a_command_is_answered_on_the_stream_not_by_a_return_value(
    session,
):
    """Everything the proxy says is a message, in the order it wrote them.

    `on_stdin_line` hands the io loop a payload to type and nothing else; a
    client learns what happened by reading, which is why the answer to a
    command can arrive several lines after other traffic and why `ref` exists.
    """
    assert session.proxy.on_stdin_line('{"type": "status"}') is None
    assert session.proxy.on_stdin_line('{"type": "nonsense"}') is None
    payload = session.proxy.on_stdin_line('{"type": "submit", "text": "hello"}')
    assert payload == {
        "type": "user",
        "message": {"role": "user", "content": "hello"},
        "turn": "t-1",
    }

    assert [m["type"] for m in session.messages()[-3:]] == ["state", "error", "ack"]
    assert [m["seq"] for m in session.messages()] == list(
        range(1, len(session.messages()) + 1)
    )


# ----------------------------------------------------------------------
# R3 — provenance decides the kind, never content
# ----------------------------------------------------------------------


def test_r3_the_same_text_is_a_user_turn_or_an_unowned_one_by_who_typed_it(session):
    """Two turns, the same initiating text, different kinds.

    The transcript gives an unowned turn's initiating entry the same type, the
    same unset `isMeta` and the same shape as a human prompt, so no content
    rule can separate them. What separates them is that the proxy typed one
    and not the other — a rule that needs no list of sources and survives
    `claude` growing new ways to wake itself up.
    """
    text = "check whether the build is still green"
    session.submit(text)
    session.run()
    session.footer_idle = False
    session.writes(user_text(text), assistant_text("green"), marker())
    session.footer_idle = True
    session.run()

    session.footer_idle = False
    session.writes(user_text(text), assistant_text("still green"), marker())
    session.footer_idle = True
    session.run()

    starts = session.of_type("turn_start")
    assert [m["turn"]["kind"] for m in starts] == ["user", "unowned"]
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_r3_a_prompt_shaped_entry_that_is_not_a_prompt_opens_nothing(session):
    """The three populations of user-shaped entries that are not turns.

    A `<task-notification>`, the cluster `/compact` writes after its boundary,
    and a skill injection all arrive typed `user` with plain text content. The
    first two arrive with nothing running and open a turn only if the model is
    then seen working; the third arrives mid-turn, where nothing can open a
    turn at all.
    """
    turn_id = session.submit("/compact")
    session.run()
    session.footer_idle = False
    session.writes(user_text("/compact"), marker("compact_boundary"))
    session.footer_idle = True
    session.run()
    assert [m["turn"]["id"] for m in session.of_type("turn_end")] == [turn_id]

    session.writes(
        user_text("This session is being continued from a previous conversation…"),
        user_text("<command-name>/compact</command-name>"),
        user_text("<local-command-stdout>Compacted</local-command-stdout>"),
    )
    session.run()
    assert len(session.of_type("turn_start")) == 1
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


# ----------------------------------------------------------------------
# R11 — cancellation is a request the proxy makes and an ending it observes
# ----------------------------------------------------------------------


def test_r11_a_running_turn_is_not_ended_on_the_clients_say_so(session):
    """The cancel sends one `Esc`; the turn ends when the inner is seen to
    stop, not when the client asked. Until then it is still running, still
    counted, and still the turn its messages belong to."""
    turn_id = session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(user_text("start the build"), assistant_thinking())
    session.run()
    session.cancel(turn_id)
    assert session.keys == [(b"\x1b",)]
    assert session.tracker.outstanding()["user"] == 1
    assert session.of_type("turn_end") == []

    # The inner declines the tool it was about to run and writes its
    # interruption entry: that is the ending, and it says so.
    session.writes(interruption())
    session.run()
    end = session.of_type("turn_end")[-1]
    assert end["turn"]["id"] == turn_id
    assert end["basis"] == "marker:interrupted"
    assert end["outcome"] == "cancelled"
    assert session.tracker.outstanding()["user"] == 0


def test_r11_an_esc_the_inner_ignored_ends_the_turn_however_it_really_ended(session):
    """A cancel whose `Esc` did not take does not pretend: the turn runs to
    its marker and reports `success`, visibly, rather than a cancellation
    nobody observed."""
    turn_id = session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(user_text("start the build"), assistant_thinking())
    session.run()
    session.cancel(turn_id)
    session.writes(assistant_text("done anyway"), marker())
    session.footer_idle = True
    session.run()
    end = session.of_type("turn_end")[-1]
    assert (end["basis"], end["outcome"]) == ("marker:turn_duration", "success")


@pytest.mark.parametrize("command", ["interrupt", "withdraw", "abort"])
def test_r11_cancel_is_the_one_command_that_takes_a_prompt_back(session, command):
    """Other spellings are still errors, and change nothing."""
    turn_id = session.submit("start the build")
    session.send({"type": command, "turn": turn_id})
    assert session.messages()[-1]["type"] == "error"
    assert session.tracker.outstanding()["user"] == 1


# ----------------------------------------------------------------------
# NFR 3 — a counter that cannot be stranded
# ----------------------------------------------------------------------


def test_nfr_3_a_repeated_signal_decrements_once(session):
    """The same completion arrives twice by design, and may arrive again.

    A completed background command is announced as a `queue-operation` and
    again as the user entry it becomes, both carrying the same
    `<task-notification>`. Counting either occurrence would double-count; the
    count is derived from the task table instead, so a repeat is a no-op.
    """
    session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("start the build"),
        assistant_tool_use("toolu_1", "build"),
        background_launch("toolu_1", "bg1"),
    )
    session.run()
    for _ in range(3):
        session.writes(queue_operation("enqueue", notification("bg1", "toolu_1")))
        session.writes(user_text(notification("bg1", "toolu_1")))
        session.run()

    assert len(session.of_type("task_start")) == 1
    assert len(session.of_type("task_end")) == 1
    session.writes(marker())
    session.footer_idle = True
    session.run()
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}


def test_nfr_3_every_counter_can_reach_zero_from_a_session_at_its_busiest(session):
    """All three counts up, then a failure — and nothing is left above zero.

    "Self-correcting" has to hold from any state the inner session can reach,
    not just from the happy path, because the states that stranded the latch
    and the ordinal were the unusual ones.
    """
    session.submit("start the build")
    session.run()
    session.footer_idle = False
    session.writes(
        user_text("start the build"),
        assistant_tool_use("toolu_1", "build"),
        background_launch("toolu_1", "bg1"),
        assistant_tool_use("toolu_2", "bench"),
        background_launch("toolu_2", "bg2"),
    )
    session.run()
    session.submit("and then this")
    session.submit("and this")
    assert session.tracker.outstanding() == {"user": 3, "unowned": 0, "background": 2, "monitor": 0, "agent": 0}

    session.proxy.fail("inner claude exited")
    assert session.tracker.outstanding() == {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0}
    assert session.messages()[-1]["outstanding"] == {
        "user": 0,
        "unowned": 0,
        "background": 0,
        "monitor": 0,
        "agent": 0,
    }
