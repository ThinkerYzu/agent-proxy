"""How a codex session is opened: the app-server's argv and env, and the thread to open.

The codex counterpart of `inner_claude.py`, with one rule that differs on
purpose: an argument is mapped or refused, never forwarded blind and never
dropped. The app-server takes no session flags on its command line, so every
argument a caller passes has to become a request parameter, and an argument
dropped silently would give the host a different agent from the one it
configured (DESIGN Decision 10).

Two more jobs, both before `ready`:

- **Keep billing on the plan.** An API key in the app-server's environment
  bills per token whatever the interface (SPEC constraint 1), so both names
  codex reads one from are removed.
- **Name the thread.** `--resume`, `--continue` and `--fork-session` become
  `thread/resume`, `thread/list` then `thread/resume`, and `thread/fork`.
  A chosen id (`--session-id`) is refused: codex mints its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


# The environment variables codex takes an API key from. Either one moves
# the session onto per-token billing.
API_KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

# Sent on every thread the proxy opens. Nothing can answer an approval
# prompt, so none may be asked; one that arrives anyway is refused by the
# proxy, never left waiting.
APPROVAL_POLICY = "never"

_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# Claude options with no per-session codex equivalent. Refused by name, so a
# host learns which of its settings codex cannot honour.
_CLAUDE_ONLY = frozenset(
    {
        "--allowedTools",
        "--allowed-tools",
        "--disallowedTools",
        "--disallowed-tools",
        "--mcp-config",
    }
)


class CodexArgumentError(ValueError):
    """Arguments a codex session cannot be opened with; the message says which."""


@dataclass
class CodexPlan:
    """What to spawn and which thread to open, decided before anything runs.

    `open_method` is `thread/start`, `thread/resume` or `thread/fork`, with
    `open_params` as its parameters. With `latest` set, the thread id is
    not known yet: the newest thread in the directory is looked up first
    (`thread/list`), and the plan resumes it, or starts a new thread when
    there is none.
    """

    server_argv: list[str]
    open_method: str
    open_params: dict
    latest: bool = False
    fork: bool = False
    thread_options: dict = field(default_factory=dict)


def build_env(base: dict[str, str]) -> dict[str, str]:
    """The app-server's environment: the caller's, without an API key."""
    return {k: v for k, v in base.items() if k not in API_KEY_VARS}


def plan(argv: Sequence[str], cwd: str, binary: str = "codex") -> CodexPlan:
    """Turn the caller's arguments into a `CodexPlan`, or raise `CodexArgumentError`.

    Accepted: `--model`/`-m`, `-c key=value` (to the app-server),
    `--sandbox`/`-s`, `--append-system-prompt`, `--dangerously-skip-permissions`
    (what `approvalPolicy: never` already says), and the session flags
    `--resume [id]`, `--continue` and `--fork-session`. Anything else is
    refused, naming the argument. `-c` is codex's config override here, not
    claude's short form of `--continue`. `binary` is the codex executable.
    """
    server_argv = [binary, "app-server"]
    options: dict = {"cwd": cwd, "approvalPolicy": APPROVAL_POLICY}
    resume: Optional[str] = None
    resume_latest = False
    fork = False

    args = list(argv)
    i = 0

    def value(flag: str) -> str:
        nonlocal i
        if i + 1 >= len(args):
            raise CodexArgumentError(f"{flag} needs a value")
        i += 1
        return args[i]

    while i < len(args):
        arg = args[i]
        flag, eq, inline = arg.partition("=")
        if flag in ("--model", "-m"):
            options["model"] = inline if eq else value(flag)
        elif flag == "-c":
            server_argv += ["-c", inline if eq else value(flag)]
        elif flag in ("--sandbox", "-s"):
            mode = inline if eq else value(flag)
            if mode not in _SANDBOX_MODES:
                raise CodexArgumentError(
                    f"{flag} must be one of {', '.join(_SANDBOX_MODES)}, not {mode!r}"
                )
            options["sandbox"] = mode
        elif flag == "--append-system-prompt":
            options["developerInstructions"] = inline if eq else value(flag)
        elif flag == "--dangerously-skip-permissions":
            pass
        elif flag in ("--resume", "-r"):
            if eq:
                resume = inline
            elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                resume = value(flag)
            else:
                resume_latest = True
        elif flag == "--continue":
            resume_latest = True
        elif flag == "--fork-session":
            fork = True
        elif flag == "--session-id":
            raise CodexArgumentError("--session-id is not supported with --agent codex: codex mints its own thread ids")
        elif flag in _CLAUDE_ONLY:
            raise CodexArgumentError(f"{flag} has no codex equivalent")
        else:
            raise CodexArgumentError(f"{arg!r} is not an argument --agent codex understands")
        i += 1

    if fork and resume is None and not resume_latest:
        raise CodexArgumentError("--fork-session needs --resume or --continue")
    if resume is not None:
        method = "thread/fork" if fork else "thread/resume"
        return CodexPlan(server_argv, method, {**options, "threadId": resume}, thread_options=options)
    return CodexPlan(
        server_argv,
        "thread/start",
        options,
        latest=resume_latest,
        fork=fork,
        thread_options=options,
    )


def with_latest(plan_: CodexPlan, list_result: dict) -> CodexPlan:
    """Resolve a `latest` plan from `thread/list`'s answer.

    The newest thread is resumed (or forked); with none, the plan stays a
    new `thread/start`, which is what `claude --continue` does in a
    directory with no sessions.
    """
    threads = list_result.get("data") or []
    newest = threads[0].get("id") if threads and isinstance(threads[0], dict) else None
    if not isinstance(newest, str):
        return CodexPlan(plan_.server_argv, "thread/start", dict(plan_.thread_options))
    method = "thread/fork" if plan_.fork else "thread/resume"
    return CodexPlan(
        plan_.server_argv, method, {**plan_.thread_options, "threadId": newest}
    )


def list_params(cwd: str) -> dict:
    """`thread/list`'s parameters for "the newest thread in this directory"."""
    return {"cwd": cwd, "sortKey": "recency_at", "sortDirection": "desc", "limit": 1}
