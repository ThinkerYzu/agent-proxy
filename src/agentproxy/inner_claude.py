"""How the inner `claude` is spawned: its argv, its env, and its session id.

Ported from claudejson, where each rule here was paid for once already. Three
jobs:

- **Forward the caller's arguments** minus the print-mode flags, so a client
  can pass `--resume`, `--model` or anything else straight through.
- **Harden the spawn**, so the proxy never meets a screen it cannot answer:
  no permission prompts, and no `AskUserQuestion` selector, which blocks a
  turn on a keystroke no client can send and deadlocks the session.
- **Know the transcript path up front** by minting a session id when the
  caller did not name one, so the tailer opens a deterministic file instead
  of diffing the project directory for a new one.
"""

from __future__ import annotations

import re
from typing import Sequence

from agentproxy import ids


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


def resolve_session(passthrough: Sequence[str]) -> tuple[str | None, str, list[str]]:
    """Decide which transcript the tailer should open, and from where.

    Returns `(session_id, tail_mode, argv)`. `tail_mode` is one of:

    - `from_start` — a new session, or one the caller named; read the whole file
    - `from_end` — resuming an existing session; skip what it already holds,
      or every historical entry replays as this session's messages
    - `discover` — the id cannot be known up front (`--continue`, a bare
      `--resume` that opens the picker, `--fork-session`), so fall back to
      diffing the project directory for the file the inner creates

    On a vanilla invocation the id is minted here and `--session-id` appended,
    which is what makes the common case deterministic rather than a race
    against directory scanning.
    """
    explicit_session: str | None = None
    resume_value: str | None = None
    has_resume = False
    has_continue = False
    has_fork = False

    index = 0
    while index < len(passthrough):
        arg = passthrough[index]
        if arg == "--session-id":
            if index + 1 < len(passthrough):
                explicit_session = passthrough[index + 1]
            index += 2
            continue
        if arg.startswith("--session-id="):
            explicit_session = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in ("--resume", "-r"):
            has_resume = True
            if index + 1 < len(passthrough) and not passthrough[index + 1].startswith("-"):
                resume_value = passthrough[index + 1]
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("--resume="):
            has_resume = True
            resume_value = arg.split("=", 1)[1]
            index += 1
            continue
        if arg in ("--continue", "-c"):
            has_continue = True
        if arg == "--fork-session":
            has_fork = True
        index += 1

    if has_fork or has_continue or (has_resume and resume_value is None):
        return None, "discover", list(passthrough)
    if resume_value:
        return resume_value, "from_end", list(passthrough)
    if explicit_session:
        return explicit_session, "from_start", list(passthrough)
    minted = ids.make_session_id()
    return minted, "from_start", list(passthrough) + ["--session-id", minted]


def _deny_interactive_tool(forwarded: list[str]) -> list[str]:
    """Make sure `AskUserQuestion` is denied, whatever the caller passed.

    `--disallowedTools` is variadic and its repeat behaviour is unspecified,
    so a second copy of the flag is never emitted: the tool is folded into the
    caller's own value group when there is one.
    """
    out = list(forwarded)
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
