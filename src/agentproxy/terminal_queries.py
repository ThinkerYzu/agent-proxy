"""Scan inner-claude pty output for terminal capability queries and
synthesize the responses a real terminal would emit.

Claude Code v2.1.162 probes the controlling terminal on startup with

    \\x1b[>0q     (XTVERSION query)
    \\x1b[c       (DA1 — Primary Device Attributes query)
    \\x1b[?2026$p (DECRQM — Request Mode for private mode 2026)

and only enables DEC Synchronized Output (private mode 2026) once it
sees positive responses to all three. agentproxy's pyte-backed PTY
does not reply to *any* of them, so v2.1.162 stays in unbracketed-
redraw mode — and `ScreenModel._on_commit` (which fires on the mode-
2026 ESU) never runs. Without commits, `BlockExtractor.is_idle` stays
False and the io_loop's `pending_messages` queue is never drained:
the very first stdin message hangs forever. See HANDOFF / triage of
/tmp/agentproxy.log for the captured failure.

The DECRQM-2026 probe is the load-bearing one: it's the
kitty-documented protocol for sync-output detection. We answer
"recognized, currently reset" (state 2), which tells claude
"yes this terminal speaks mode 2026 — go ahead and enable it." A
"not recognized" response (state 0) — which is what silent terminals
get treated as — makes claude give up on sync output entirely.

We mimic a small VT220-ish terminal: just enough to convince claude
that "yes, sync output is fine." Responses are written back through
the same `pty_driver.write` path the inner claude reads from, so they
show up in the `pty.out` log channel.
"""

from __future__ import annotations

import re


# DA1 — Primary Device Attributes.  Claude sends a bare ``\x1b[c``;
# many emulators allow the explicit-zero form ``\x1b[0c`` too. Both
# mean the same thing.
DA1_QUERY_RE = re.compile(rb"\x1b\[0?c")

# XTVERSION query — ``\x1b[>0q`` (and the rarer parameter-less
# ``\x1b[>q``).  The leading ``>`` is the CSI "private parameter"
# prefix; final byte ``q`` distinguishes it from cursor-style queries.
XTVERSION_QUERY_RE = re.compile(rb"\x1b\[>0?q")

# DECRQM — DEC Request Mode for a private mode.  Shape:
# ``\x1b[?<digits>$p``.  Intermediate ``$`` + final ``p`` is the
# DECRQM-private form (DECRQM-public uses ``\x1b[<digits>$p``, no
# ``?`` — we don't see that and don't answer it).  The mode number
# is captured so the reply can echo it back.
DECRQM_PRIVATE_QUERY_RE = re.compile(rb"\x1b\[\?(\d+)\$p")

# DA1 response — advertise VT220 (62) + 132-column (1) + selective
# erase (6) + ANSI color (22).  That's the minimum modern terminals
# advertise and matches what xterm sends by default; claude is happy
# with this on stock terminals so it should be happy here.
DA1_RESPONSE = b"\x1b[?62;1;6;22c"

# XTVERSION response — DCS string framed by ``\x1bP>|`` and ``\x1b\\``
# (ESC + backslash = ST, String Terminator).  Form
# mirrors xterm's reply (``xterm(385)``).  Identifying as agentproxy
# is honest and gives any downstream tooling a hint without changing
# claude's behavior.
XTVERSION_RESPONSE = b"\x1bP>|agentproxy(0.1)\x1b\\"

# DECRQM private-mode state values:
#   0 = mode not recognized
#   1 = set
#   2 = reset
#   3 = permanently set
#   4 = permanently reset
# "Reset" (2) is the right answer for claude's mode-2026 probe: it tells
# claude "yes, this terminal knows what mode 2026 is, and currently it's
# off — go ahead and enable it via ``\x1b[?2026h``." Returning 0 (which
# is what unrecognized-mode behavior looks like) tells claude the
# terminal can't do sync output, and it falls back to unbracketed
# redraws — which is the broken path we're fixing.
_DECRQM_STATE_RESET = 2


def _decrqm_reply(mode_bytes: bytes) -> bytes:
    """Build the DECRQM reply for ``\\x1b[?<mode>;<state>$y``.

    Echoes the mode number verbatim so we don't have to reason about
    canonical formats (leading zeros etc.) — whatever claude asked
    about is what it gets back.
    """
    return b"\x1b[?" + mode_bytes + b";" + str(_DECRQM_STATE_RESET).encode() + b"$y"


def scan(data: bytes) -> list[bytes]:
    """Return canned responses for each terminal query found in ``data``.

    Responses are returned in the order their queries appear so the
    inner claude sees them in a sensible sequence.  A query that
    appears more than once produces multiple responses (we don't
    de-dup — claude is the one re-asking).

    Splits across read boundaries are *not* handled here: the buffer
    grain is small (claude sends all relevant queries in one or two
    adjacent writes) and re-tries on the inner side haven't been
    observed.
    """
    if not data:
        return []

    hits: list[tuple[int, bytes]] = []
    for match in DA1_QUERY_RE.finditer(data):
        hits.append((match.start(), DA1_RESPONSE))
    for match in XTVERSION_QUERY_RE.finditer(data):
        hits.append((match.start(), XTVERSION_RESPONSE))
    for match in DECRQM_PRIVATE_QUERY_RE.finditer(data):
        hits.append((match.start(), _decrqm_reply(match.group(1))))

    hits.sort(key=lambda h: h[0])
    return [resp for _, resp in hits]
