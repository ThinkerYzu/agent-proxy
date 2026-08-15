"""The `agent-proxy` entry point.

Live wrapping — spawning the inner `claude` on a pty, typing prompts, and
serving the protocol on stdio — is Phase 2 work. Until it lands, this
refuses rather than half-runs, and points at the one thing that does work:
replaying a recorded transcript through the tracker.
"""

from __future__ import annotations

import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    print(
        "agent-proxy: live wrapping is not wired up yet (Phase 2).\n"
        "To exercise the turn tracker against a recorded session:\n"
        "    python3 -m agentproxy.replay <transcript>.jsonl --assert-drained",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
