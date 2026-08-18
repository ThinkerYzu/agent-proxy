"""Tier 2 of end-of-turn: closing a turn no marker closed.

Tier 1 — a recognised transcript marker — lives in `turn_tracker`. This is the
other tier, and it is an everyday path rather than an error handler: the marker set is open (`compact_boundary` was found only by
surveying 412 transcripts), so a turn shape nobody has catalogued yet must
still end. A late end-of-turn is a recoverable client bug; a missing one hangs
the UI forever.

Three conditions can close a turn here, in falling order of confidence, and
each reports its own `basis` so a client — and whoever is reading the logs —
can tell which one fired:

| `basis`            | Condition                                    | The case it covers |
|--------------------|----------------------------------------------|--------------------|
| `fallback:idle`    | footer idle **and** transcript quiet         | a turn whose marker we do not recognise |
| `fallback:quiet`   | pty silent **and** transcript quiet          | the footer reading is stale |
| `fallback:stalled` | transcript quiet, nothing else considered    | last resort — the ceiling |

`fallback:quiet` exists because the footer reading is not always trustworthy:
a `--resume` repaint paints an active footer and then stops emitting frames,
so the reading sits at "active" while the inner is in fact idle at the prompt.
claudejson hit this and moved its own timing gates onto pty silence for the
same reason. A running turn paints its spinner about once a second, so pty
silence for a minute means the TUI is not working, whatever the footer says.

`fallback:stalled` is the ceiling that makes "every turn ends" unconditional. It
can fire on a turn that is genuinely alive — a single foreground command
running longer than the threshold writes nothing to the transcript and keeps
the spinner painting — so it is set far out, and its whole job is to make
"hangs forever" impossible. Anything but a near-zero rate of it is a bug.

A rate above roughly zero on *any* of the three is the signal that `claude`
grew a turn shape worth teaching to `MARKER_BASIS`.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional


BASIS_IDLE = "fallback:idle"
BASIS_QUIET = "fallback:quiet"
BASIS_STALLED = "fallback:stalled"

# Footer idle and transcript quiet for this long ends the turn. It has to
# outlast the mid-turn footer flicker — the footer reads idle for a moment
# between an assistant text block and the next tool call, and calling that the
# end of the turn is the premature `result` that desynced an earlier client.
# Both
# clocks must be quiet, and the flicker resets the transcript one, so this is
# already conservative at a few seconds; 8 buys margin for a slow frame.
IDLE_AFTER_S = float(os.environ.get("AGENTPROXY_FALLBACK_IDLE_S", "8"))
# No pty bytes and no transcript entries for this long. A working turn paints
# its spinner about once a second, so this cannot fire on one.
QUIET_AFTER_S = float(os.environ.get("AGENTPROXY_FALLBACK_QUIET_S", "60"))
# The ceiling: transcript quiet this long ends the turn on nothing else.
STALL_AFTER_S = float(os.environ.get("AGENTPROXY_FALLBACK_STALL_S", "1800"))


class FallbackEot:
    """The clocks tier 2 reads, and the verdict it draws from them.

    The caller feeds it three signals — a frame's idle reading, a transcript
    entry arriving, and a turn starting — then asks `basis()` on every loop
    tick while a turn is running. It holds no reference to the turn table and
    decides nothing about which turn ends: that stays with `TurnTracker`.
    """

    def __init__(
        self,
        idle_after_s: float = IDLE_AFTER_S,
        quiet_after_s: float = QUIET_AFTER_S,
        stall_after_s: float = STALL_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_after_s = idle_after_s
        self._quiet_after_s = quiet_after_s
        self._stall_after_s = stall_after_s
        self._clock = clock
        self._idle_since: Optional[float] = None
        self._last_transcript = clock()

    def note_turn_start(self) -> None:
        """Restart every clock, because a new turn owes us nothing yet.

        Without this a turn opening after a long idle stretch would inherit
        that stretch and end on the tick it started.
        """
        self._idle_since = None
        self._last_transcript = self._clock()

    def note_transcript(self) -> None:
        """A transcript entry arrived — the turn is demonstrably still working."""
        self._last_transcript = self._clock()

    def note_frame(self, is_idle: bool) -> None:
        """Record a frame's footer reading, keeping the idle clock running."""
        if not is_idle:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = self._clock()

    def basis(self, last_pty: float) -> Optional[str]:
        """How the running turn should end, or None to keep waiting.

        `last_pty` is the monotonic timestamp of the last read from the pty.
        It belongs to the io loop rather than here — that is the one place
        the bytes are counted — so it is passed in at each check.
        """
        now = self._clock()
        quiet_for = now - self._last_transcript
        if quiet_for < self._idle_after_s:
            return None
        if self._idle_since is not None and now - self._idle_since >= self._idle_after_s:
            return BASIS_IDLE
        if quiet_for >= self._quiet_after_s and now - last_pty >= self._quiet_after_s:
            return BASIS_QUIET
        if quiet_for >= self._stall_after_s:
            return BASIS_STALLED
        return None
