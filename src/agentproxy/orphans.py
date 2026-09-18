"""Processes the inner `claude` lost track of: adopted, reaped, counted.

A background process can outlive the shell the model launched it from: a
`command &` after its shell exits, a daemon that double-forked, everything
left behind when the inner `claude` itself dies. The inner's own task
tracking never ends such a process, so no `<task-notification>` will, and
the transcript is silent about it from then on.

The proxy sees them through the kernel instead. `become_subreaper` marks the
proxy as the process its orphaned descendants are reparented to (in place of
init), so from then on "the orphans" are exactly "the proxy's children other
than the inner `claude`", a list the kernel keeps and `OrphanReaper` reads
from `/proc`. Reading it is also the price: an adopted child that exits is
the proxy's zombie until it is `waitpid`ed, so the reaper reaps.

Nothing here is a table the proxy could get wrong. The count is a re-read of
the process tree on every tick, in processes rather than tasks.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from agentproxy import log


# linux/prctl.h. Linux 3.4+; the flag survives exec and is not inherited by a
# forked child, so setting it here makes the proxy the only subreaper below
# whoever spawned it.
PR_SET_CHILD_SUBREAPER = 36

# `process.command` is for telling processes apart, not for reading scripts:
# the inner's own shell-snapshot bash carries a 6 KB script in its argv, and
# the wire gets its head (where the program name is), cut to this many
# characters. Same figure as `task_end.output`'s tail.
COMMAND_MAX_CHARS = 256

# How `orphan_end.status` names an ending.
STATUS_EXITED = "exited"        # exited on its own; `exit_code` is set
STATUS_KILLED = "killed"        # ended by a signal; `signal` is set
STATUS_ABANDONED = "abandoned"  # still alive when the proxy stopped watching

# A wait status that says "the pid is not ours to wait on any more": the
# child was reaped by something else, or was never a child. Real wait
# statuses are non-negative, so this cannot collide with one.
_GONE = -1


def become_subreaper() -> Optional[str]:
    """Make this process the reaper for every orphan below it.

    Returns None on success, or a short reason the flag could not be set.
    The caller logs it and runs without an orphan count rather than failing,
    since nothing else about the proxy depends on it. Call it before spawning
    the inner `claude`: reparenting goes to the nearest living subreaper at
    the moment a parent dies, so the flag must already be on then.
    """
    if not sys.platform.startswith("linux"):
        return f"not linux ({sys.platform})"
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
    except (OSError, AttributeError) as missing:
        return f"prctl unavailable: {missing}"
    if prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        return f"prctl failed: {os.strerror(ctypes.get_errno())}"
    return None


@dataclass
class Orphan:
    pid: int
    command: Optional[str]  # `/proc/<pid>/cmdline`, or None if unreadable

    def to_wire(self) -> dict:
        return {"pid": self.pid, "command": self.command}


@dataclass
class OrphanExit:
    process: Orphan
    status: str
    exit_code: Optional[int] = None
    signal: Optional[int] = None


class OrphanReaper:
    """The proxy's adopted children: who is alive, who just arrived, who just left.

    `poll()` is the whole interface for a running session; call it once per
    loop tick. `count()` and `alive()` read the same state for the counts and
    the `state` reply. `shutdown()` closes the books when the proxy exits.

    `exclude` is the inner `claude`'s pid: a direct child too, but not an
    orphan, and one whose exit status `PtyDriver` waits on itself. Every
    `waitpid` here names a pid for that reason: a `waitpid(-1)` would
    consume the inner's status before `ptyprocess` ever saw it.

    The process readers and `kill` are injectable so the rules can be tested
    with a scripted set of pids and no processes at all.
    """

    def __init__(
        self,
        exclude: Iterable[int] = (),
        *,
        list_children: Optional[Callable[[], list[int]]] = None,
        read_command: Optional[Callable[[int], Optional[str]]] = None,
        wait: Optional[Callable[[int], Optional[int]]] = None,
        kill: Optional[Callable[[int, int], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._exclude = set(exclude)
        self._list_children = list_children or _list_children
        self._read_command = read_command or _read_command
        self._wait = wait or _wait
        self._kill = kill or os.kill
        self._sleep = sleep
        self._alive: dict[int, Orphan] = {}

    # -- reading -------------------------------------------------------

    def count(self) -> int:
        return len(self._alive)

    def alive(self) -> list[Orphan]:
        return [self._alive[pid] for pid in sorted(self._alive)]

    # -- the tick --------------------------------------------------------

    def poll(self) -> tuple[list[Orphan], list[OrphanExit]]:
        """Adopt new children, reap exited ones; return (arrived, ended).

        Adopt before reaping, so a process that arrived and died between two
        ticks (a zombie child by now, still in the kernel's list) reports
        both its arrival and its ending from the one call rather than being
        missed. A zombie stays listed until reaped, which is what makes this
        safe: nothing that ever became the proxy's child can slip past unseen.
        """
        arrived: list[Orphan] = []
        for pid in sorted(set(self._list_children()) - self._exclude):
            if pid not in self._alive:
                command = self._read_command(pid)
                if command is not None:
                    command = command[:COMMAND_MAX_CHARS]
                orphan = Orphan(pid, command)
                self._alive[pid] = orphan
                arrived.append(orphan)
                log.log_text("orphans", f"adopted {pid}: {orphan.command!r}")
        ended: list[OrphanExit] = []
        for pid in sorted(self._alive):
            status = self._wait(pid)
            if status is None:
                continue
            ended.append(self._ended(self._alive.pop(pid), status))
        return arrived, ended

    # -- shutdown ------------------------------------------------------

    def shutdown(self, kill: bool, grace: float = 2.0) -> list[OrphanExit]:
        """Close the books on every adopted process; return one ending each.

        With `kill` false the survivors are left running and reported as
        `abandoned`: the proxy is exiting and will not see them end. With
        `kill` true they get SIGTERM, `grace` seconds, then SIGKILL, and are
        reported with their real ending; anything the kernel still has not
        let go of by then is reported `abandoned` rather than waited on
        forever.
        """
        if kill and self._alive:
            self._signal_all(signal.SIGTERM)
            ended = self._reap_until(grace)
            if self._alive:
                self._signal_all(signal.SIGKILL)
                ended += self._reap_until(grace)
        else:
            ended = []
        for pid in sorted(self._alive):
            ended.append(OrphanExit(self._alive.pop(pid), STATUS_ABANDONED))
        return ended

    def _signal_all(self, sig: int) -> None:
        for pid in list(self._alive):
            try:
                self._kill(pid, sig)
            except ProcessLookupError:
                pass

    def _reap_until(self, grace: float) -> list[OrphanExit]:
        deadline = time.monotonic() + grace
        ended: list[OrphanExit] = []
        while self._alive:
            for pid in sorted(self._alive):
                status = self._wait(pid)
                if status is not None:
                    ended.append(self._ended(self._alive.pop(pid), status))
            if not self._alive or time.monotonic() >= deadline:
                break
            self._sleep(0.05)
        return ended

    @staticmethod
    def _ended(orphan: Orphan, status: int) -> OrphanExit:
        """Turn a raw wait status into the ending `orphan_end` reports."""
        if status == _GONE:
            log.log_text("orphans", f"{orphan.pid} gone before it could be reaped")
            return OrphanExit(orphan, STATUS_EXITED)
        if os.WIFSIGNALED(status):
            log.log_text("orphans", f"{orphan.pid} killed by signal {os.WTERMSIG(status)}")
            return OrphanExit(orphan, STATUS_KILLED, signal=os.WTERMSIG(status))
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        log.log_text("orphans", f"{orphan.pid} exited {code}")
        return OrphanExit(orphan, STATUS_EXITED, exit_code=code)


# ----------------------------------------------------------------------
# The real process readers
# ----------------------------------------------------------------------


def _list_children(pid: Optional[int] = None) -> list[int]:
    """Direct children of `pid` (default: this process), from `/proc`.

    `/proc/<pid>/task/<tid>/children` is one small read per thread and needs
    `CONFIG_PROC_CHILDREN`, which mainstream kernels enable. Where it is
    missing, fall back to scanning every `/proc/<n>/stat` for our ppid.
    Slower, but the same answer.
    """
    pid = os.getpid() if pid is None else pid
    children: set[int] = set()
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return _scan_children(pid)
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children") as handle:
                children.update(int(field) for field in handle.read().split())
        except FileNotFoundError:
            return _scan_children(pid)
        except OSError:
            continue
    return sorted(children)


def _scan_children(parent: int) -> list[int]:
    children: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as handle:
                stat = handle.read()
        except OSError:
            continue
        # The command name is in parentheses and may itself contain spaces
        # or parentheses; the fields we want follow the *last* ')'.
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 1 and int(fields[1]) == parent:
            children.append(int(name))
    return sorted(children)


def _read_command(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    return " ".join(parts) or None


def _wait(pid: int) -> Optional[int]:
    """Reap `pid` if it has exited: None while it runs, its wait status once done.

    `_GONE` when the kernel no longer has a child by that pid for us (reaped
    elsewhere, or never ours), so the caller drops it instead of retrying.
    """
    try:
        reaped, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return _GONE
    return status if reaped == pid else None
