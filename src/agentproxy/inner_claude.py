"""How the inner `claude` is spawned: its argv, its env, and its session id.

Ported from claudejson, where each rule here was paid for once already. Three
jobs:

- **Forward the caller's arguments** minus the print-mode flags, so a client
  can pass `--resume`, `--model` or anything else straight through.
- **Harden the spawn**, so the proxy never meets a screen it cannot answer:
  no permission prompts, and no `AskUserQuestion` selector, which blocks a
  turn on a keystroke no client can send and deadlocks the session.
- **Know the transcript path up front** by resolving a session id for every
  invocation, so the tailer opens a deterministic file and the proxy can name
  the session before it types anything ([requirement 12] / DESIGN Decision 6).

[requirement 12]: ../../../../proj_docs/agent-proxy/SPEC.md#functional-requirements
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from agentproxy import ids
from agentproxy.transcript_tailer import PROJECTS_ROOT, encode_cwd


# Print-mode flags agent-proxy speaks itself. Passing them to the inner would
# turn off the interactive TUI the whole design depends on (SPEC constraint 1).
_FLAGS_WITH_VALUE = {"--input-format", "--output-format"}
_FLAGS_BOOLEAN = {"-p", "--print", "--verbose"}

# The built-in tool the proxy can never satisfy: it renders a TUI selector and
# waits for a human keystroke, so the turn never ends and no end-of-turn ever
# fires. `--dangerously-skip-permissions` does not cover it — that governs
# permission checks, not tool inventory — so it has to be disallowed by name.
_INTERACTIVE_TOOL = "AskUserQuestion"
_DISALLOWED_FLAGS = ("--disallowedTools", "--disallowed-tools")


def build_env(base: dict[str, str]) -> dict[str, str]:
    """The environment the inner TUI is spawned into.

    Colour is off and the geometry is pinned to the pty's, so the frames the
    liveness reader parses are the ones `PtyDriver` sized.

    Session persistence is forced, and that one is load-bearing rather than
    cosmetic: a `claude` that inherits `CLAUDE_CODE_CHILD_SESSION` — which any
    process launched from inside a Claude Code session does — writes no
    transcript at all. Since the transcript is where every message and every
    turn boundary comes from, such an inner runs turns the proxy cannot see,
    binds no prompt, and ends no turn. Setting this is what makes the proxy
    work when its client is itself running under `claude`.
    """
    env = dict(base)
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = "120"
    env["LINES"] = "40"
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    env.setdefault("CI", "")
    return env


def build_argv(passthrough: Sequence[str]) -> list[str]:
    """Compose the inner `claude` command line from the caller's arguments."""
    forwarded = _deny_interactive_tool(filter_passthrough(passthrough))
    has_skip = any(
        arg == "--dangerously-skip-permissions"
        or arg.startswith("--dangerously-skip-permissions=")
        for arg in forwarded
    )
    head = ["claude"]
    if not has_skip:
        head.append("--dangerously-skip-permissions")
    return head + list(forwarded)


def filter_passthrough(argv: Sequence[str]) -> list[str]:
    """Drop the print-mode flags; forward everything else untouched.

    Handles both `--flag value` and `--flag=value`, and stops filtering after
    `--` so positional arguments survive verbatim.
    """
    out: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            out.extend(argv[index:])
            break
        head = arg.split("=", 1)[0] if arg.startswith("--") and "=" in arg else arg
        if head in _FLAGS_WITH_VALUE:
            index += 1 if "=" in arg else 2
            continue
        if head in _FLAGS_BOOLEAN:
            index += 1
            continue
        out.append(arg)
        index += 1
    return out


class SessionArgumentError(ValueError):
    """The caller's session flags cannot be resolved to one session."""


def most_recent_session(
    cwd: os.PathLike | str, projects_root: Path = PROJECTS_ROOT
) -> Optional[str]:
    """The session id `--continue` would land on, or None if there is none.

    Newest non-empty transcript in this directory's project dir, by mtime.
    Zero-byte files are skipped: a session that never got written is not one
    a client can be handed back.

    The proxy uses this to *choose* a session rather than to predict the
    inner's choice — the id is then passed as `--resume <id>`, so the two
    cannot disagree even if the inner's own selection rule differs.
    """
    project_dir = Path(projects_root) / encode_cwd(cwd)
    if not project_dir.is_dir():
        return None
    try:
        candidates = [
            p
            for p in project_dir.iterdir()
            if p.suffix == ".jsonl" and p.stat().st_size > 0
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).stem


def resolve_session(
    passthrough: Sequence[str],
    *,
    cwd: os.PathLike | str | None = None,
    projects_root: Path = PROJECTS_ROOT,
) -> tuple[str, str, list[str]]:
    """Decide which session the inner will run, and how to read its transcript.

    Returns `(session_id, tail_mode, argv)`. The session id is never None —
    that is [requirement 12], and the reason this function rewrites the
    caller's session flags instead of forwarding them: every invocation is
    turned into one that names its session explicitly.

    `tail_mode` is one of:

    - `from_start` — a new session; read its file from the top
    - `from_end` — resuming an existing session; seek past what it already
      holds, or every historical entry replays as this session's messages
    - `fork` — a forked session, whose file the inner backfills with a copy of
      the original's history. The copy carries the *original's* timestamps, so
      the tailer drops entries stamped before the proxy started rather than
      seeking, which it cannot do to a file that does not exist yet

    Raises `SessionArgumentError` for `--session-id` alongside `--resume` or
    `--continue` without `--fork-session` — the inner rejects that combination
    outright, and failing here turns a spawn that dies into a message that
    says why.
    """
    flags = _parse_session_flags(passthrough)
    argv = flags.rest

    if flags.explicit_id and (flags.resume_id or flags.wants_recent) and not flags.fork:
        raise SessionArgumentError(
            "--session-id can only be combined with --resume or --continue "
            "when --fork-session is also given"
        )

    base_id = flags.resume_id
    if base_id is None and flags.wants_recent:
        base_id = most_recent_session(cwd if cwd is not None else os.getcwd(), projects_root)

    # The flags go in front of the caller's arguments, never after them: a
    # passthrough may end in `--`, and anything following that is a positional
    # argument to the inner rather than an option — an appended `--session-id`
    # would be read as prompt text and the session would not be the one we
    # named.
    #
    # Forking mints a second session out of the first, so the id the client
    # gets back is the *new* one — it is what a later `--resume` must name.
    if flags.fork and base_id is not None:
        forked = flags.explicit_id or ids.make_session_id()
        return forked, "fork", [
            "--resume",
            base_id,
            "--fork-session",
            "--session-id",
            forked,
        ] + argv

    if base_id is not None:
        return base_id, "from_end", ["--resume", base_id] + argv

    # Nothing to resume — either none was asked for, or `--continue` was asked
    # for in a directory with no sessions yet. The inner does the same thing
    # there (it starts a new conversation rather than failing), and a stated
    # new id is more use to a client than a refusal.
    fresh = flags.explicit_id or ids.make_session_id()
    return fresh, "from_start", ["--session-id", fresh] + argv


@dataclass
class _SessionFlags:
    """The caller's session-control flags, lifted out of the passthrough."""

    explicit_id: Optional[str] = None
    resume_id: Optional[str] = None
    wants_recent: bool = False  # `--continue`, or `--resume` with no id
    fork: bool = False
    rest: list[str] = field(default_factory=list)


def _parse_session_flags(argv: Sequence[str]) -> _SessionFlags:
    """Split the session-control flags off the rest of the passthrough.

    They are removed rather than read in place, because `resolve_session`
    re-emits a canonical set in their stead; leaving the originals would let
    the caller's `--continue` reach the inner alongside the `--resume` that
    replaced it.
    """
    flags = _SessionFlags()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            flags.rest.extend(argv[index:])
            break
        if arg == "--session-id":
            if index + 1 < len(argv):
                flags.explicit_id = argv[index + 1]
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("--session-id="):
            flags.explicit_id = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in ("--resume", "-r"):
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                flags.resume_id = argv[index + 1]
                index += 2
                continue
            flags.wants_recent = True
            index += 1
            continue
        if arg.startswith("--resume="):
            flags.resume_id = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in ("--continue", "-c"):
            flags.wants_recent = True
            index += 1
            continue
        if arg == "--fork-session":
            flags.fork = True
            index += 1
            continue
        flags.rest.append(arg)
        index += 1
    return flags


def _deny_interactive_tool(forwarded: list[str]) -> list[str]:
    """Make sure `AskUserQuestion` is denied, whatever the caller passed.

    `--disallowedTools` is variadic and its repeat behaviour is unspecified,
    so a second copy of the flag is never emitted: the tool is folded into the
    caller's own value group when there is one.

    Everything from a `--` onwards is held back and re-appended afterwards, so
    the denial always lands in the option section. Two facts about the inner
    make that placement the only correct one, and they pull in opposite
    directions (both verified against 2.1.233):

    - arguments after `--` are **not parsed as options**, so a denial appended
      to the very end of an argv containing one is passed to the model as
      prompt text and the tool stays in its inventory
    - the flag is **greedy**, swallowing every following token that does not
      start with `-` as another tool name, so it cannot simply be moved to the
      front either — it would eat the caller's positional arguments

    Inserting it immediately before the `--` satisfies both: it is parsed, and
    the `--` terminates its value list.
    """
    cut = forwarded.index("--") if "--" in forwarded else len(forwarded)
    out, positionals = list(forwarded[:cut]), list(forwarded[cut:])
    return _fold_into_options(out) + positionals


def _fold_into_options(out: list[str]) -> list[str]:
    """Add or extend the denial within an argv's option section."""
    for index, arg in enumerate(out):
        for flag in _DISALLOWED_FLAGS:
            if arg.startswith(flag + "="):
                if _names_tool(arg.split("=", 1)[1]):
                    return out
                out[index] = f"{arg},{_INTERACTIVE_TOOL}"
                return out
        if arg in _DISALLOWED_FLAGS:
            end = index + 1
            while end < len(out) and not out[end].startswith("-"):
                if _names_tool(out[end]):
                    return out
                end += 1
            out.insert(end, _INTERACTIVE_TOOL)
            return out
    out.extend([_DISALLOWED_FLAGS[0], _INTERACTIVE_TOOL])
    return out


def _names_tool(value: str) -> bool:
    """True when a comma- or space-separated flag value already lists the tool."""
    return _INTERACTIVE_TOOL in re.split(r"[,\s]+", value.strip())
