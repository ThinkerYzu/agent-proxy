"""What the proxy reads off the screen, and what it refuses to read into it.

Frames here are built by hand rather than captured, for the same reason the
replay fixture is: the reader looks at four rows of structure and two dialog
markers, so those are all a test needs to supply.
"""

from __future__ import annotations

import pytest

from agentproxy.liveness import (
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
    seen: list[tuple[str, tuple[bytes, ...]]] = []
    liveness = Liveness(on_prompt=lambda p, k: seen.append((p, k)), clock=clock)
    # The live layout, not the historical guess: the selection glyph sits on
    # "No, exit", one row above "Yes, I trust this folder" — confirmed by
    # capturing a real trust dialog and observing that a bare Enter (which
    # used to assume "Yes" was pre-highlighted) left the session dead with no
    # further frame, while Down-then-Enter cleanly exited the inner.
    dialog = Screen(["❯ No, exit", "  Yes, I trust this folder"])
    liveness.on_commit(dialog)
    liveness.on_commit(dialog)
    assert [prompt for prompt, _ in seen] == [TRUST_PROMPT]
    assert not liveness.is_idle

    liveness.on_commit(panel())
    liveness.on_commit(dialog)
    assert [prompt for prompt, _ in seen] == [TRUST_PROMPT, TRUST_PROMPT]


def test_the_trust_prompt_answer_navigates_down_to_the_wanted_option(clock):
    """The keys are computed from the frame, not assumed — see the module docstring."""
    seen: list[tuple[str, tuple[bytes, ...]]] = []
    liveness = Liveness(on_prompt=lambda p, k: seen.append((p, k)), clock=clock)
    liveness.on_commit(Screen(["❯ No, exit", "  Yes, I trust this folder"]))
    [(_, keys)] = seen
    assert keys == (b"\x1b[B", b"\r")


def test_the_trust_prompt_answer_is_a_bare_enter_when_already_selected(clock):
    """A future release that restores "Yes" as the default needs no code change."""
    seen: list[tuple[str, tuple[bytes, ...]]] = []
    liveness = Liveness(on_prompt=lambda p, k: seen.append((p, k)), clock=clock)
    liveness.on_commit(Screen(["❯ Yes, I trust this folder", "  No, exit"]))
    [(_, keys)] = seen
    assert keys == (b"\r",)


def test_the_resume_selector_is_reported_too(clock):
    seen: list[tuple[str, tuple[bytes, ...]]] = []
    liveness = Liveness(on_prompt=lambda p, k: seen.append((p, k)), clock=clock)
    liveness.on_commit(
        Screen(["❯ 1. Resume from summary", "  2. Resume full session as-is"])
    )
    assert [prompt for prompt, _ in seen] == [RESUME_PROMPT]
    [(_, keys)] = seen
    assert keys == (b"\x1b[B", b"\r")


def test_a_dialog_whose_selection_glyph_cant_be_found_falls_back_to_enter(clock):
    """No selection row at all — a layout change deep enough to disorient this.

    Still answers something (a bare Enter) rather than leaving the dialog
    entirely untouched, on the same "a guess beats a permanent hang" logic
    the old fixed-answer table used throughout.
    """
    seen: list[tuple[str, tuple[bytes, ...]]] = []
    liveness = Liveness(on_prompt=lambda p, k: seen.append((p, k)), clock=clock)
    liveness.on_commit(Screen(["  Yes, I trust this folder", "  No, exit"]))
    [(_, keys)] = seen
    assert keys == (b"\r",)


# ----------------------------------------------------------------------
# The composer — what a cancelled prompt looks like when handed back
# ----------------------------------------------------------------------


def composer(*rows: str, footer: str = IDLE_FOOTER) -> Screen:
    """A panel whose composer spans `rows`: the ❯ row, then continuation rows."""
    return Screen(["some earlier output", HRULE, *rows, HRULE, footer])


def test_a_composer_holding_a_wrapped_prompt_still_reads_idle(clock):
    """The band used to be exactly four rows. A prompt the inner hands back
    after a cancel wraps onto more, and the footer is still where it says."""
    liveness = Liveness(clock=clock)
    liveness.on_commit(composer("❯ Write a long essay about the history of", "  the pty in Unix"))
    assert liveness.is_idle
    liveness.on_commit(
        composer("❯ Write a long essay about the history of", "  the pty in Unix", footer=ACTIVE_FOOTER)
    )
    assert not liveness.is_idle


def test_composer_holds_matches_the_first_line_of_the_typed_text(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(composer("❯ Write a 1500 word essay about the history of the pty in Unix."))
    assert liveness.composer_holds("Write a 1500 word essay about the history of the pty in Unix.")
    assert not liveness.composer_holds("Reply with exactly: AFTER")


def test_composer_holds_survives_the_composers_own_wrapping(clock):
    """The inner wraps at the panel width and indents the continuation; the
    typed text had neither, and whitespace is all that differs."""
    liveness = Liveness(clock=clock)
    liveness.on_commit(
        composer(
            "❯ Use the Bash tool, NOT in background mode, to run exactly: python3 -c",
            "  \"import time; time.sleep(40)\"   Then reply with exactly: DONE",
        )
    )
    assert liveness.composer_holds(
        "Use the Bash tool, NOT in background mode, to run exactly: python3 -c "
        '"import time; time.sleep(40)"   Then reply with exactly: DONE'
    )


def test_composer_holds_compares_only_the_first_line_of_a_multi_line_prompt(clock):
    liveness = Liveness(clock=clock)
    liveness.on_commit(composer("❯ first line of the prompt", "  second line"))
    assert liveness.composer_holds("first line of the prompt\nsecond line\nthird")


def test_the_placeholder_is_not_a_held_prompt(clock):
    """An empty composer shows a dimmed suggestion; it is compared against
    the cancelled prompt, so it can only match a prompt that *is* it."""
    liveness = Liveness(clock=clock)
    liveness.on_commit(panel())
    assert not liveness.composer_holds("fix the bug")
    assert not liveness.composer_holds("   ")


def test_composer_holds_is_false_before_any_panel_was_seen(clock):
    liveness = Liveness(clock=clock)
    assert not liveness.composer_holds("anything")
    liveness.on_commit(Screen(["a dialog with no panel"]))
    assert not liveness.composer_holds("anything")
