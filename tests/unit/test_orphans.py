"""`orphans.py`: the subreaper flag proven for real, and the reaper's rules on fakes.

The first test is the one everything else in Phase 4 rests on: that the
kernel really does reparent an orphaned grandchild to a process carrying
`PR_SET_CHILD_SUBREAPER`. It runs in a subprocess because the flag is
process-wide: set in the pytest process it would start collecting every
other test's orphans too.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from agentproxy import orphans
from agentproxy.orphans import (
    STATUS_ABANDONED,
    STATUS_EXITED,
    STATUS_KILLED,
    Orphan,
)
from tests.harness import ProcessTree


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="subreaper is a Linux prctl"
)


# ----------------------------------------------------------------------
# The mechanism, for real
# ----------------------------------------------------------------------

# A process that (optionally) becomes a subreaper, runs a shell that leaves a
# `sleep` behind, and reports what `OrphanReaper` then sees. Prints one JSON
# object so the test can assert on it.
_PROBE = textwrap.dedent(
    """
    import json, os, signal, subprocess, time
    from agentproxy import orphans

    problem = orphans.become_subreaper() if {flag!r} else "not requested"
    subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 &"], check=True,
                   stdout=subprocess.DEVNULL)
    # The shell has exited, but its forked child may still be a `sh` an
    # instant away from exec'ing `sleep`; give it that instant so the
    # command line we read is the sleep's.
    time.sleep(0.2)
    reaper = orphans.OrphanReaper()
    arrived, ended = reaper.poll()
    seen = [o.command for o in arrived]
    count_after_adopt = reaper.count()
    for o in arrived:
        os.kill(o.pid, signal.SIGTERM)
    reaped = []
    deadline = time.monotonic() + 5
    while reaper.count() and time.monotonic() < deadline:
        time.sleep(0.05)
        _, gone = reaper.poll()
        reaped += [(e.status, e.signal) for e in gone]
    print(json.dumps({{"problem": problem, "seen": seen,
                       "count_after_adopt": count_after_adopt,
                       "reaped": reaped, "count_at_end": reaper.count()}}))
    """
)


def _probe(flag: bool) -> dict:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(flag=flag)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@linux_only
def test_the_flag_adopts_a_grandchild_whose_parent_exited() -> None:
    """Criterion 7's mechanism: `sh -c 'sleep &'` exits; the sleep becomes ours."""
    report = _probe(flag=True)
    assert report["problem"] is None
    assert report["seen"] == ["sleep 30"]
    assert report["count_after_adopt"] == 1
    assert report["reaped"] == [[STATUS_KILLED, int(signal.SIGTERM)]]
    assert report["count_at_end"] == 0


@linux_only
def test_without_the_flag_the_grandchild_goes_elsewhere() -> None:
    """The control: the same shell, no flag, and the sleep is init's problem."""
    report = _probe(flag=False)
    assert report["seen"] == []
    assert report["count_after_adopt"] == 0


@linux_only
def test_a_direct_child_is_listed_by_both_proc_readers() -> None:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert proc.pid in orphans._list_children()
        assert proc.pid in orphans._scan_children(os.getpid())
        assert orphans._read_command(proc.pid) == "sleep 30"
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_wait_reports_a_pid_that_is_not_ours_as_gone() -> None:
    assert orphans._wait(1) == orphans._GONE


# ----------------------------------------------------------------------
# The rules, on fakes
# ----------------------------------------------------------------------


def test_a_new_child_is_adopted_with_its_command() -> None:
    tree = ProcessTree()
    tree.spawn(101, "python serve.py")
    arrived, ended = tree.reaper().poll()
    assert [o.to_wire() for o in arrived] == [{"pid": 101, "command": "python serve.py"}]
    assert ended == []


def test_a_long_command_line_is_cut_to_its_head() -> None:
    """The inner's shell-snapshot bash carries a 6 KB script in its argv; the
    program name is at the front, and that is what the wire keeps."""
    tree = ProcessTree()
    tree.spawn(101, "/bin/bash -c -l " + "x" * 10_000)
    arrived, _ = tree.reaper().poll()
    assert len(arrived[0].command) == orphans.COMMAND_MAX_CHARS
    assert arrived[0].command.startswith("/bin/bash -c -l ")


def test_the_inner_claude_is_a_child_but_never_an_orphan() -> None:
    tree = ProcessTree()
    tree.spawn(50, "claude")
    tree.spawn(101)
    reaper = tree.reaper(exclude=(50,))
    arrived, _ = reaper.poll()
    assert [o.pid for o in arrived] == [101]
    assert reaper.count() == 1


def test_a_child_is_adopted_once_and_counted_while_it_lives() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    arrived, ended = reaper.poll()
    assert (arrived, ended) == ([], [])
    assert reaper.count() == 1
    assert [o.pid for o in reaper.alive()] == [101]


def test_an_exit_is_reaped_with_its_code_and_leaves_the_count() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    tree.exits(101, code=3)
    _, ended = reaper.poll()
    assert len(ended) == 1
    assert (ended[0].status, ended[0].exit_code, ended[0].signal) == (STATUS_EXITED, 3, None)
    assert reaper.count() == 0


def test_a_signal_death_is_reported_as_killed_with_the_signal() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    tree.dies_from(101, int(signal.SIGKILL))
    _, ended = reaper.poll()
    assert (ended[0].status, ended[0].exit_code, ended[0].signal) == (
        STATUS_KILLED,
        None,
        int(signal.SIGKILL),
    )


def test_a_child_that_arrived_and_died_between_ticks_reports_both_at_once() -> None:
    """A zombie is still listed, so one poll adopts it and reaps it."""
    tree = ProcessTree()
    tree.spawn(101, command=None)  # cmdline of a zombie reads empty
    tree.exits(101, code=0)
    arrived, ended = tree.reaper().poll()
    assert [o.pid for o in arrived] == [101]
    assert arrived[0].command is None
    assert [e.process.pid for e in ended] == [101]


def test_a_pid_the_kernel_no_longer_owns_to_us_is_dropped_as_exited() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    tree.statuses[101] = orphans._GONE
    _, ended = reaper.poll()
    assert (ended[0].status, ended[0].exit_code) == (STATUS_EXITED, None)
    assert reaper.count() == 0


def test_shutdown_abandons_by_default_and_signals_nothing() -> None:
    tree = ProcessTree()
    tree.spawn(101, "nohup server")
    tree.spawn(102, "sleep 900")
    reaper = tree.reaper()
    reaper.poll()
    ended = reaper.shutdown(kill=False)
    assert [(e.process.pid, e.status) for e in ended] == [
        (101, STATUS_ABANDONED),
        (102, STATUS_ABANDONED),
    ]
    assert tree.signals == []
    assert reaper.count() == 0


def test_shutdown_with_kill_terminates_and_reports_the_real_ending() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    ended = reaper.shutdown(kill=True, grace=0.1)
    assert tree.signals == [(101, int(signal.SIGTERM))]
    assert (ended[0].status, ended[0].signal) == (STATUS_KILLED, int(signal.SIGTERM))
    assert reaper.count() == 0


def test_shutdown_with_kill_escalates_to_sigkill_when_sigterm_is_ignored() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    # Ignore SIGTERM: the fake keeps the process alive until SIGKILL.
    ignored = []

    def stubborn_kill(pid: int, sig: int) -> None:
        ignored.append((pid, sig))
        if sig == signal.SIGKILL:
            tree.statuses[pid] = int(signal.SIGKILL)

    reaper._kill = stubborn_kill
    ended = reaper.shutdown(kill=True, grace=0.0)
    assert ignored == [(101, int(signal.SIGTERM)), (101, int(signal.SIGKILL))]
    assert (ended[0].status, ended[0].signal) == (STATUS_KILLED, int(signal.SIGKILL))


def test_shutdown_reports_what_would_not_die_as_abandoned() -> None:
    tree = ProcessTree()
    tree.spawn(101)
    reaper = tree.reaper()
    reaper.poll()
    reaper._kill = lambda pid, sig: None  # nothing ever dies
    ended = reaper.shutdown(kill=True, grace=0.0)
    assert [(e.process.pid, e.status) for e in ended] == [(101, STATUS_ABANDONED)]


def test_shutdown_with_nothing_adopted_reports_nothing() -> None:
    assert ProcessTree().reaper().shutdown(kill=True) == []


def test_an_orphan_on_the_wire_is_pid_and_command() -> None:
    assert Orphan(7, None).to_wire() == {"pid": 7, "command": None}


def test_become_subreaper_refuses_off_linux(monkeypatch) -> None:
    monkeypatch.setattr(orphans.sys, "platform", "darwin")
    assert orphans.become_subreaper() == "not linux (darwin)"


@linux_only
def test_become_subreaper_succeeds_in_a_fresh_process() -> None:
    # In a subprocess, so the pytest process itself never carries the flag.
    out = subprocess.run(
        [sys.executable, "-c", "from agentproxy import orphans; print(orphans.become_subreaper())"],
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
    ).stdout.strip()
    assert out == "None"
