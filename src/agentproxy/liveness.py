"""Is the inner TUI working, or waiting for input?

This is the whole of what agent-proxy reads off the screen. Every piece of
message *content* comes from the transcript, so
nothing here parses a tool call, a result, or a line of prose — it answers one
question, plus the two modal dialogs that must be dismissed before the inner
`claude` will accept a keystroke at all.

claudejson's `block_extractor` is where the frame-reading rules were worked
out; this module keeps its idle detection and its two dialog markers and drops
everything else, which is most of it.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, List, Optional, Tuple

from agentproxy import log


# The dialogs that swallow every keystroke until answered. Each is reported
# once per appearance, and the caller answers it — the reply is pty bytes, and
# this module does no I/O.
TRUST_PROMPT = "trust_prompt"
RESUME_PROMPT = "resume_prompt"

_TRUST_MARKER = "I trust this folder"
_RESUME_MARKER = "Resume full session as-is"

# Last-resort answer when a dialog's marker text is on screen but the
# navigation to it can't be computed (`_keys_for_dialog` returns None) — a
# layout `_INPUT_MARKER` doesn't find a selection glyph on. A bare Enter is
# the same blind bet the old fixed-answer table made; logged loudly since a
# wrong guess here is a silent hang or a silent exit, not an error.
_BLIND_ANSWER = (b"\r",)

# How long after the first idle frame to wait for a second frame before calling
# the inner settled. It initialises its keyboard handler lazily on first input;
# when a large `.claude/settings.local.json` is loaded from a parent directory
# that init lags ~10 s behind the idle footer, and a prompt typed inside that
# window loses its submit CR permanently. A follow-up frame commit is the
# reliable signal that the handler is up; this timeout is the fallback for the
# common case where no second frame ever arrives.
SETTLE_QUIESCENCE_S = float(os.environ.get("AGENTPROXY_SETTLE_QUIESCENCE_S", "0.5"))

_HRULE_RE = re.compile(r"^─{60,}$")
_INPUT_MARKER = "❯"

# Idle is the *absence* of the active-turn hint, not the presence of an idle
# one. The idle footer's text varies with context ("? for shortcuts", a bypass-
# permissions banner, a context-left warning), but "esc to interrupt" is shown
# for an in-flight interruptible turn and nothing else. claudejson missed
# end-of-turn on long turns until it stopped matching a specific idle hint.
_ACTIVE_FOOTER_RE = re.compile(r"esc to interrupt")


PromptCallback = Callable[[str, Tuple[bytes, ...]], None]


class Liveness:
    """Tracks the idle/active reading of the inner TUI across frame commits.

    Feed it each committed frame with `on_commit`; read `is_idle` to gate
    typing and to drive the fallback end-of-turn tier. `on_prompt`, if given,
    fires once per appearance of a modal dialog with `TRUST_PROMPT` or
    `RESUME_PROMPT` and the keys that answer it — computed from the frame
    itself (which row carries the wanted option, which row the inner has
    currently selected) rather than a fixed guess, so a `claude` release that
    reorders a dialog's options doesn't silently answer the wrong one.
    """

    def __init__(
        self,
        on_prompt: Optional[PromptCallback] = None,
        settle_quiescence_s: float = SETTLE_QUIESCENCE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_prompt = on_prompt
        self._settle_quiescence_s = settle_quiescence_s
        self._clock = clock
        self._is_idle = False
        self._showing = {TRUST_PROMPT: False, RESUME_PROMPT: False}
        self._first_idle_at: Optional[float] = None
        self._frames_since_first_idle = 0
        self._has_settled = False
        self._unknown_frames = 0

    @property
    def is_idle(self) -> bool:
        """True when the input panel is present and not showing an in-flight turn.

        A dialog has no input panel at all, so it never reads idle — which is
        what keeps a prompt from being typed into a trust prompt.
        """
        return self._is_idle

    @property
    def has_settled(self) -> bool:
        """True once the inner is past its post-spawn keyboard-handler race.

        Fires on whichever comes first after the first idle frame: another
        frame commit, or `settle_quiescence_s` of wall time. See
        SETTLE_QUIESCENCE_S for what goes wrong without it.
        """
        if self._has_settled:
            return True
        if self._first_idle_at is None:
            return False
        if self._frames_since_first_idle >= 1:
            self._has_settled = True
        elif self._clock() - self._first_idle_at >= self._settle_quiescence_s:
            self._has_settled = True
        return self._has_settled

    def on_commit(self, screen) -> None:
        """Read one settled frame. Safe to call on the same frame repeatedly.

        The caller polls it every loop tick as well as on each mode-2026
        commit, because the inner renders its first dialog before enabling
        synchronised output — so a trust prompt can appear on a frame that
        never commits.
        """
        rows = [row.rstrip() for row in screen.display]
        self._check_prompt(rows, _TRUST_MARKER, TRUST_PROMPT)
        self._check_prompt(rows, _RESUME_MARKER, RESUME_PROMPT)

        band = self._find_input_panel(rows)
        if band is None:
            # No panel: a dialog, a startup frame, or a repaint in progress.
            # Never idle — typing into any of those loses the keystrokes.
            self._note_unknown_frame(rows)
            is_idle = False
        else:
            is_idle = not bool(_ACTIVE_FOOTER_RE.search(rows[band + 3]))

        if is_idle != self._is_idle:
            log.log_text("liveness", "idle" if is_idle else "active")
        self._is_idle = is_idle

        if self._first_idle_at is None:
            if is_idle:
                self._first_idle_at = self._clock()
        else:
            self._frames_since_first_idle += 1

    def _check_prompt(self, rows: List[str], marker: str, prompt: str) -> None:
        showing = any(marker in row for row in rows)
        if showing and not self._showing[prompt]:
            log.log_text("liveness", f"{prompt} showing")
            if self._on_prompt is not None:
                self._on_prompt(prompt, self._keys_for_dialog(rows, marker))
        self._showing[prompt] = showing

    @staticmethod
    def _keys_for_dialog(rows: List[str], marker: str) -> Tuple[bytes, ...]:
        """The arrow-then-Enter sequence that selects the row carrying `marker`.

        Reads the frame rather than assuming an option's position, because a
        dialog's default has already changed under this module once — see
        `on_prompt`'s docstring. `marker`'s own row is the wanted option;
        whichever row starts with the selection glyph is where the inner's
        cursor already sits. One `Down`/`Up` per row of difference — every
        dialog seen so far lays its options on consecutive rows with nothing
        between them, so a row of difference is an option of difference.

        Falls back to `_BLIND_ANSWER` (and logs the rows, same as an
        unreadable frame) when either row can't be found — a layout change
        deep enough that `marker` itself still matched but nothing here can
        orient against it.
        """
        target_row = next((i for i, row in enumerate(rows) if marker in row), None)
        selected_row = next(
            (i for i, row in enumerate(rows) if row.strip().startswith(_INPUT_MARKER)),
            None,
        )
        if target_row is None or selected_row is None:
            log.log_text(
                "liveness",
                f"dialog {marker!r}: could not locate target/selection row "
                f"(target={target_row}, selected={selected_row}); guessing {_BLIND_ANSWER!r}",
            )
            for index, row in enumerate(rows):
                if row.strip():
                    log.log_text("liveness", f"  row[{index:02d}]: {row!r}")
            return _BLIND_ANSWER
        delta = target_row - selected_row
        if delta == 0:
            return (b"\r",)
        step = b"\x1b[B" if delta > 0 else b"\x1b[A"
        return (step,) * abs(delta) + (b"\r",)

    def _note_unknown_frame(self, rows: List[str]) -> None:
        """Log the first few unreadable frames, so a version bump is diagnosable.

        A dialog whose wording changed reads exactly like this — no panel, no
        marker — and the only way to learn its new wording is to have the rows
        in the log. Capped so a slow startup does not flood it.
        """
        if any(self._showing.values()):
            return
        if self._unknown_frames >= 3:
            return
        self._unknown_frames += 1
        log.log_text("liveness", f"unreadable frame #{self._unknown_frames}")
        for index, row in enumerate(rows):
            if row.strip():
                log.log_text("liveness", f"  row[{index:02d}]: {row!r}")

    @staticmethod
    def _find_input_panel(rows: List[str]) -> Optional[int]:
        """Index of the four-row [hrule, ❯, hrule, footer] band, if it is there."""
        for row in range(len(rows) - 4, -1, -1):
            if (
                _HRULE_RE.match(rows[row])
                and rows[row + 1].startswith(_INPUT_MARKER)
                and _HRULE_RE.match(rows[row + 2])
                and rows[row + 3].strip()
            ):
                return row
        return None
