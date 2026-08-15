"""Debug logging gated on the `AGENTPROXY_LOG` env var.

When `AGENTPROXY_LOG` is unset, every public function in this module is a
no-op — no file IO, no formatting, no time call. The cost on the hot path
is one attribute load + an `is None` branch, which is what makes NF2's
sub-50 ms latency budget unaffected by leaving the env var off.

When `AGENTPROXY_LOG` is set to a file path, that file is opened in
append mode at first call and re-used for the process lifetime. Each call
writes one line:

    <monotonic-ms> <channel> <payload-repr>

Channels follow the dotted convention `<source>.<direction>`:

    pty.in     raw bytes the inner claude sent us (each os.read chunk)
    pty.out    raw bytes we wrote to the inner pty (paste / submit / signal)
    stdin.in   caller-supplied user message lines (one per readline)
    stdout.out the protocol lines we wrote to our stdout
    parser     block-extractor state transitions

Binary payloads (`pty.*`) use `repr(bytes)` so escape codes survive in
human-readable form; text payloads (`stdin.in`, `stdout.out`, `parser`) use
the string as-is (newlines escaped via `repr`).
"""

from __future__ import annotations

import os
import time
from typing import IO, Optional


_handle: Optional[IO[str]] = None
_inited = False
_start_monotonic: float = 0.0


def _maybe_init() -> Optional[IO[str]]:
    global _handle, _inited, _start_monotonic
    if _inited:
        return _handle
    _inited = True
    path = os.environ.get("AGENTPROXY_LOG")
    if not path:
        return None
    try:
        _handle = open(path, "a", buffering=1, encoding="utf-8")  # line-buffered
    except OSError:
        # Don't take the process down because the log path is bad — just
        # silently disable. The user's debugging session can still proceed.
        _handle = None
    else:
        _start_monotonic = time.monotonic()
        _handle.write(f"# agentproxy log opened at {time.time():.3f}\n")
    return _handle


def is_enabled() -> bool:
    return _maybe_init() is not None


def log_bytes(channel: str, data: bytes) -> None:
    """Record a raw byte payload on `channel` (`pty.in` / `pty.out`)."""
    handle = _maybe_init()
    if handle is None or not data:
        return
    ts_ms = int((time.monotonic() - _start_monotonic) * 1000)
    handle.write(f"{ts_ms:>10} {channel} {data!r}\n")


def log_text(channel: str, text: str) -> None:
    """Record a text payload (caller JSON, protocol envelope, parser note).

    `text` is wrapped in `repr` so embedded newlines stay on one log line —
    critical when greppingthe log for a specific event.
    """
    handle = _maybe_init()
    if handle is None or not text:
        return
    ts_ms = int((time.monotonic() - _start_monotonic) * 1000)
    handle.write(f"{ts_ms:>10} {channel} {text!r}\n")


def reset_for_tests() -> None:
    """Drop the cached handle and re-read `AGENTPROXY_LOG` on the next call.

    Tests that toggle the env var must call this between toggles — module
    state intentionally caches the handle for the process lifetime in
    production, but tests need to opt out of that cache.
    """
    global _handle, _inited, _start_monotonic
    if _handle is not None:
        try:
            _handle.close()
        except OSError:
            pass
    _handle = None
    _inited = False
    _start_monotonic = 0.0
