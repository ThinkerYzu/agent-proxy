"""What the proxy reads off the screen, and what it refuses to read into it.

Frames here are built by hand rather than captured, for the same reason the
replay fixture is: the reader looks at four rows of structure and two dialog
markers, so those are all a test needs to supply.
"""

from __future__ import annotations

import pytest

from agentproxy.liveness import (
    PROMPT_ANSWERS,
    RESUME_PROMPT,
    TRUST_PROMPT,
    Liveness,
)


HRULE = "─" * 80
IDLE_FOOTER = "  ? for shortcuts"
ACTIVE_FOOTER = "  esc to interrupt"


class Screen:
    """The one attribute `Liveness.on_commit` reads off a pyte screen."""

    def __init__(self, rows: list[str]) -> None:
        self.display = rows + [""] * (40 - len(rows))


def panel(footer: str = IDLE_FOOTER, *content: str) -> Screen:
    """A frame with the input panel the inner shows whenever it wants input."""
    return Screen([*content, HRULE, "❯ Try \"fix the bug\"", HRULE, footer])


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


# ----------------------------------------------------------------------
# Idle and active
# ----------------------------------------------------------------------


def test_a_panel_without_the_interrupt_hint_is_idle(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel())
    assert liveness.is_idle


def test_the_interrupt_hint_means_a_turn_is_running(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel(ACTIVE_FOOTER))
    assert not liveness.is_idle


def test_idle_survives_a_footer_hint_nobody_has_seen_before(clock):
    """Idle is the absence of the active hint, never the presence of a known one.

    claudejson matched a specific idle hint and missed end-of-turn whenever a
    long turn settled to a footer worded some other way.
    """
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel("  ⏵⏵ bypass permissions on · 12% context left"))
    assert liveness.is_idle


def test_a_frame_with_no_input_panel_is_never_idle(clock):
    """A dialog, a startup frame, a repaint — typing into any loses the keys."""
    liveness = Liveness(clock=clock)
    liveness.on_commit(Screen(["Welcome to Claude Code", "", "Loading…"]))
    assert not liveness.is_idle


# ----------------------------------------------------------------------
# Settling
# ----------------------------------------------------------------------


def test_nothing_is_settled_before_the_first_idle_frame(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel(ACTIVE_FOOTER))
    assert not liveness.has_settled


def test_a_second_frame_settles_the_inner(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel())
    assert not liveness.has_settled
    liveness.on_commit(panel())
    assert liveness.has_settled


def test_waiting_long_enough_settles_it_with_no_second_frame(clock):
    """The common case: no follow-up frame ever arrives, so time decides."""
    liveness = Liveness(settle_quiescence_s=0.5, clock=clock)
    liveness.on_commit(panel())
    assert not liveness.has_settled
    clock.advance(0.5)
    assert liveness.has_settled


# ----------------------------------------------------------------------
# The dialogs that swallow keystrokes
# ----------------------------------------------------------------------


def test_the_trust_prompt_is_reported_once_per_appearance(clock):
    seen: list[str] = []
    liveness = Liveness(on_prompt=seen.append, clock=clock)
    dialog = Screen(["Do you trust the files in this folder?", "❯ 1. Yes, I trust this folder"])
    liveness.on_commit(dialog)
    liveness.on_commit(dialog)
    assert seen == [TRUST_PROMPT]
    assert not liveness.is_idle

    liveness.on_commit(panel())
    liveness.on_commit(dialog)
    assert seen == [TRUST_PROMPT, TRUST_PROMPT]


def test_the_resume_selector_is_reported_too(clock):
    seen: list[str] = []
    liveness = Liveness(on_prompt=seen.append, clock=clock)
    liveness.on_commit(
        Screen(["❯ 1. Resume from summary", "  2. Resume full session as-is"])
    )
    assert seen == [RESUME_PROMPT]


def test_each_dialog_has_an_answer_to_type():
    """A recognised dialog with no answer would hang exactly as an unknown one."""
    assert set(PROMPT_ANSWERS) == {TRUST_PROMPT, RESUME_PROMPT}
    assert all(keys for keys in PROMPT_ANSWERS.values())
