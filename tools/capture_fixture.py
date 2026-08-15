"""Cut a test fixture out of a real `~/.claude/projects` transcript.

    python3 tools/capture_fixture.py <session>.jsonl /tmp/session-under-test.jsonl

Writes two files: the fixture itself, with every long string truncated so a
2.6 MB session becomes something a repository can carry, and a
`.prompts.json` sidecar naming which lines were prompts a client typed.
`agentproxy.replay` reads both.

What it produces is for checking the tracker against a real session on the
machine that recorded it — a truncated transcript is still a transcript of
somebody's work, so it does not belong in the repository. The committed
fixture is built instead by `make_synthetic_fixture.py`, out of the same entry
shapes and none of the content.

The sidecar exists because provenance cannot be recovered from a recording:
the transcript shows what was said, never who typed it. This tool guesses,
using exactly the content rules the proxy itself refuses to rely on — which
is fine here and not there. A recording is a fixed artefact that can be
eyeballed once; a live session is not.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


MAX_STRING = 120

# Entry text that looks like a prompt but never is one. Two populations, both
# from Appendix A of the spec: the cluster `/compact` writes after it finishes,
# and the notification a completed background task turns into.
_NOT_A_PROMPT_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "This session is being continued from a previous conversation",
)


def shrink(value: object) -> object:
    """Copy a decoded entry with its long strings cut down to `MAX_STRING`.

    Task notifications survive whole: the tracker reads the task id and status
    out of them, and they run past the limit.
    """
    if isinstance(value, str):
        if len(value) <= MAX_STRING or "<task-notification>" in value:
            return value
        return value[:MAX_STRING] + "…[truncated]"
    if isinstance(value, list):
        return [shrink(item) for item in value]
    if isinstance(value, dict):
        return {key: shrink(item) for key, item in value.items()}
    return value


def is_prompt(entry: dict) -> bool:
    """Whether this entry looks like text a human typed into the TUI."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    if entry.get("isCompactSummary"):
        return False
    content = entry.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    return not content.lstrip().startswith(_NOT_A_PROMPT_PREFIXES)


def capture(source: Path, target: Path) -> dict:
    prompts = []
    lines = []
    for index, line in enumerate(source.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        # Take the prompt text from the shrunk entry, not the original: a
        # prompt long enough to be truncated must reach the tracker in the
        # same form on both paths, or it will not bind to its own turn.
        entry = shrink(json.loads(line))
        if is_prompt(entry):
            prompts.append({"index": index, "text": entry["message"]["content"]})
        lines.append(json.dumps(entry, ensure_ascii=False))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sidecar = target.with_suffix(".prompts.json")
    sidecar.write_text(
        json.dumps({"source": source.name, "prompts": prompts}, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "entries": len(lines),
        "prompts": len(prompts),
        "fixture": str(target),
        "sidecar": str(sidecar),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(capture(args.source, args.target), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
