"""Tier 2: when a turn no marker closed has gone on long enough to close.

Every test drives a fake clock, so the thresholds are exercised exactly rather
than waited out.
"""

from __future__ import annotations

import pytest

from agentproxy.fallback import (
    BASIS_IDLE,
    BASIS_QUIET,
    BASIS_STALLED,
    FallbackEot,
)


IDLE_AFTER = 8.0
QUIET_AFTER = 60.0
STALL_AFTER = 1800.0


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


@pytest.fixture
def fallback(clock) -> FallbackEot:
    eot = FallbackEot(
        idle_after_s=IDLE_AFTER,
        quiet_after_s=QUIET_AFTER,
        stall_after_s=STALL_AFTER,
        clock=clock,
    )
    eot.note_turn_start()
    return eot


def busy(fallback: FallbackEot, clock: Clock) -> float:
    """Put the session in the state a working turn is in, and return `last_pty`."""
    fallback.note_frame(is_idle=False)
    return clock.now


# ----------------------------------------------------------------------
# The idle tier
# ----------------------------------------------------------------------


def test_a_working_turn_is_never_ended(fallback, clock):
    last_pty = busy(fallback, clock)
    clock.advance(3600)
    fallback.note_transcript()
    last_pty = clock.now
    assert fallback.basis(last_pty) is None


def test_an_idle_footer_over_quiet_transcript_ends_the_turn(fallback, clock):
    fallback.note_frame(is_idle=True)
    clock.advance(IDLE_AFTER)
    assert fallback.basis(clock.now) == BASIS_IDLE


def test_a_flicker_shorter_than_the_grace_period_ends_nothing(fallback, clock):
    """The mid-turn footer flicker: idle for a moment between two tool calls.

    Calling that the end of the turn is the premature `result` that desynced
    an earlier client, which is why the grace period has to outlast it.
    """
    fallback.note_frame(is_idle=True)
    clock.advance(IDLE_AFTER - 1)
    assert fallback.basis(clock.now) is None
    fallback.note_frame(is_idle=False)
    clock.advance(IDLE_AFTER)
    assert fallback.basis(clock.now) is None


def test_transcript_activity_alone_holds_the_turn_open(fallback, clock):
    """An idle-reading footer proves nothing while entries are still arriving."""
    fallback.note_frame(is_idle=True)
    clock.advance(IDLE_AFTER - 1)
    fallback.note_transcript()
    clock.advance(2)
    assert fallback.basis(clock.now) is None


def test_a_new_turn_does_not_inherit_the_quiet_before_it(fallback, clock):
    """Otherwise a turn opening after an idle stretch ends on the tick it began."""
    fallback.note_frame(is_idle=True)
    clock.advance(STALL_AFTER)
    fallback.note_turn_start()
    assert fallback.basis(clock.now) is None


# ----------------------------------------------------------------------
# The tiers below it
# ----------------------------------------------------------------------


def test_a_silent_pty_ends_the_turn_whatever_the_footer_says(fallback, clock):
    """The stale-footer case: a `--resume` repaint leaves the reading active.

    The inner is sitting idle at the prompt, but the last frame it painted
    said otherwise and no frame has come since. A running turn paints its
    spinner about once a second, so silence this long is ground truth.
    """
    last_pty = busy(fallback, clock)
    clock.advance(QUIET_AFTER)
    assert fallback.basis(last_pty) == BASIS_QUIET


def test_a_busy_pty_holds_off_the_quiet_tier(fallback, clock):
    busy(fallback, clock)
    clock.advance(QUIET_AFTER)
    assert fallback.basis(clock.now) is None


def test_the_ceiling_ends_a_turn_nothing_else_would(fallback, clock):
    """Last resort. It can fire on a live turn, so it is set far out — but it
    is what makes "no turn hangs without an end-of-turn" unconditional."""
    busy(fallback, clock)
    clock.advance(STALL_AFTER)
    assert fallback.basis(clock.now) == BASIS_STALLED


def test_the_tiers_are_reported_apart(fallback, clock):
    """`basis` is how a blind spot becomes visible instead of just costly."""
    assert len({BASIS_IDLE, BASIS_QUIET, BASIS_STALLED}) == 3
    assert all(
        basis.startswith("fallback:")
        for basis in (BASIS_IDLE, BASIS_QUIET, BASIS_STALLED)
    )
