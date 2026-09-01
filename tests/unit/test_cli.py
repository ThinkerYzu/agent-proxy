"""Tests for `agentproxy.cli`'s small file-reading helper.

`_read_task_output` is the one piece of the command/output feature that does
real file I/O — everything else (`TurnTracker` parsing the notification,
`Proxy` truncating to 256 chars) is exercised with a stub in
`tests/unit/test_turn_tracker.py` and `tests/requirements/test_functional.py`.
This is what backs that stub in production.
"""

from __future__ import annotations

from pathlib import Path

from agentproxy.cli import _read_task_output


def test_reads_a_small_files_whole_content(tmp_path: Path) -> None:
    path = tmp_path / "bg1.output"
    path.write_text("build finished\n")
    assert _read_task_output(str(path)) == "build finished\n"


def test_reads_only_the_tail_of_a_large_file(tmp_path: Path) -> None:
    """The read is bounded, so a multi-megabyte log costs one seek, not a load."""
    path = tmp_path / "bg1.output"
    path.write_bytes(b"a" * 20000 + b"TAIL")
    content = _read_task_output(str(path))
    assert content is not None
    assert content.endswith("TAIL")
    assert len(content) <= 8192


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert _read_task_output(str(tmp_path / "never-written.output")) is None


def test_unreadable_bytes_are_replaced_not_raised(tmp_path: Path) -> None:
    """A task's own output is not guaranteed to be valid UTF-8."""
    path = tmp_path / "bg1.output"
    path.write_bytes(b"before \xff\xfe after")
    content = _read_task_output(str(path))
    assert content is not None
    assert "before" in content and "after" in content
