"""Replay a recorded transcript through `TurnTracker`, with no live `claude`.

    python3 -m agentproxy.replay tests/fixtures/session-08ff12b4.jsonl --assert-drained

The transcript alone does not say which entries were prompts the proxy typed
— that is provenance, and a recording has none. A sidecar file supplies it:
`<transcript>.prompts.json`, a list of `{"index", "text"}` naming the line
each prompt was typed before. `tools/capture_fixture.py` writes both files
together.

`--assert-drained` is the invariant worth having in CI: replaying a whole
session must leave every counter at zero. A turn that never ends, or a
background task whose completion is missed, shows up as a non-zero exit here.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Optional

from agentproxy.protocol import Emitter
from agentproxy.transcript_tailer import SKIP_TYPES
from agentproxy.turn_tracker import KIND_USER, STATE_ENDED, TurnTracker


def load_prompts(path: Path) -> dict[int, str]:
    """Read the prompt sidecar as a line-index → prompt-text map.

    Returns an empty map when the file is absent, which replays the session
    as if the proxy had typed nothing — every turn then comes out unowned.
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    return {int(item["index"]): item["text"] for item in data["prompts"]}


def replay(
    transcript: Path,
    prompts: dict[int, str],
    out: Optional[io.TextIOBase] = None,
) -> TurnTracker:
    """Feed a recorded session to a fresh tracker and return it.

    Prompts are submitted and typed immediately before the line they were
    recorded against, so the tracker faces the same binding decision it would
    have faced live. Messages go to `out`; pass an `io.StringIO` to keep them
    out of the way.
    """
    tracker = TurnTracker()
    emitter = Emitter(tracker, out=out)
    with transcript.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            text = prompts.get(index)
            if text is not None:
                turn, events = tracker.submit(text)
                tracker.mark_typed(turn)
                emitter.emit_all(events)
            entry = json.loads(line)
            if entry.get("type") in SKIP_TYPES:
                continue
            emitter.emit_all(tracker.on_entry(entry))
    return tracker


def summarise(tracker: TurnTracker) -> dict:
    """Turn and task totals, for a human or an assertion to check."""
    turns = tracker.turns
    bases: dict[str, int] = {}
    for turn in turns:
        bases[turn.basis or "unfinished"] = bases.get(turn.basis or "unfinished", 0) + 1
    return {
        "turns": len(turns),
        "user_turns": sum(1 for t in turns if t.kind == KIND_USER),
        "unowned_turns": sum(1 for t in turns if t.kind != KIND_USER),
        "unfinished_turns": sum(1 for t in turns if t.state != STATE_ENDED),
        "bases": bases,
        "tasks": len(tracker.tasks),
        "outstanding": tracker.outstanding(),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcript", type=Path)
    parser.add_argument(
        "--prompts",
        type=Path,
        help="prompt sidecar (default: <transcript>.prompts.json)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the message stream"
    )
    parser.add_argument(
        "--assert-drained",
        action="store_true",
        help="exit non-zero unless every counter reached zero",
    )
    args = parser.parse_args(argv)

    prompts_path = args.prompts or args.transcript.with_suffix(".prompts.json")
    tracker = replay(
        args.transcript,
        load_prompts(prompts_path),
        out=io.StringIO() if args.quiet else sys.stdout,
    )
    summary = summarise(tracker)
    print(json.dumps(summary, indent=2), file=sys.stderr)
    if args.assert_drained and any(summary["outstanding"].values()):
        print("not drained: " + json.dumps(summary["outstanding"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
