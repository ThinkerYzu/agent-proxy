"""Tests for `agentproxy.log` (the AGENTPROXY_LOG debug dump, NF4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentproxy import log


@pytest.fixture(autouse=True)
def _isolate_log_state(monkeypatch):
    """Reset the cached file handle around each test so env-var toggles
    actually take effect.
    """
    log.reset_for_tests()
    monkeypatch.delenv("AGENTPROXY_LOG", raising=False)
    yield
    log.reset_for_tests()


def test_log_disabled_when_env_var_unset() -> None:
    assert log.is_enabled() is False
    # Calls are no-ops; no file written, no exception.
    log.log_bytes("pty.in", b"\x1b[?2026l")
    log.log_text("parser", "EOT")


def test_log_disabled_when_env_var_empty(monkeypatch) -> None:
    monkeypatch.setenv("AGENTPROXY_LOG", "")
    log.reset_for_tests()
    assert log.is_enabled() is False


def test_log_writes_to_file_when_env_var_set(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "dump.log"
    monkeypatch.setenv("AGENTPROXY_LOG", str(log_path))
    log.reset_for_tests()

    assert log.is_enabled() is True

    log.log_bytes("pty.in", b"hello\nworld")
    log.log_text("parser", "EOT")

    content = log_path.read_text()
    assert "# agentproxy log opened at" in content
    assert "pty.in" in content
    assert "parser" in content
    # bytes are repr'd, so the embedded newline survives as `\n` not a newline:
    assert r"b'hello\nworld'" in content
    # text is also repr'd:
    assert "'EOT'" in content


def test_log_lines_have_monotonic_timestamp_and_channel(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "dump.log"
    monkeypatch.setenv("AGENTPROXY_LOG", str(log_path))
    log.reset_for_tests()

    log.log_text("parser", "first")
    log.log_text("parser", "second")

    lines = [ln for ln in log_path.read_text().splitlines() if not ln.startswith("#")]
    assert len(lines) == 2
    for line in lines:
        # Format is "<right-padded ts_ms> <channel> <repr(payload)>";
        # str.split() with no arg collapses runs of whitespace.
        ts, channel, payload = line.split(maxsplit=2)
        assert ts.isdigit()
        assert channel == "parser"
        assert payload.startswith("'") and payload.endswith("'")


def test_log_empty_payload_is_skipped(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "dump.log"
    monkeypatch.setenv("AGENTPROXY_LOG", str(log_path))
    log.reset_for_tests()

    log.log_bytes("pty.in", b"")
    log.log_text("parser", "")

    body_lines = [ln for ln in log_path.read_text().splitlines() if not ln.startswith("#")]
    assert body_lines == []


def test_log_handle_survives_bad_path_silently(tmp_path: Path, monkeypatch) -> None:
    """A non-writable path must not crash the wrapper; it disables logging."""
    bad_path = tmp_path / "no" / "such" / "dir" / "dump.log"
    monkeypatch.setenv("AGENTPROXY_LOG", str(bad_path))
    log.reset_for_tests()

    assert log.is_enabled() is False
    # And the no-op path stays no-op:
    log.log_text("parser", "ignored")


def test_log_records_pty_round_trip(tmp_path: Path, monkeypatch) -> None:
    """Integration-ish: pty_driver write + read both log when enabled."""
    log_path = tmp_path / "dump.log"
    monkeypatch.setenv("AGENTPROXY_LOG", str(log_path))
    log.reset_for_tests()

    # Use a small subprocess that prints one line and exits. PtyDriver's
    # read/write paths are what we're exercising; what gets logged is the
    # `pty.out` from `write` and `pty.in` from the child's stdout.
    from agentproxy.pty_driver import PtyDriver
    import time

    driver = PtyDriver(["/bin/echo", "hello-from-pty"], env={})
    # Give the child a tick to emit and exit.
    deadline = time.monotonic() + 2.0
    saw = b""
    while time.monotonic() < deadline:
        chunk = driver.read(4096)
        if chunk:
            saw += chunk
        if not driver.is_alive() and not chunk:
            break
        time.sleep(0.02)
    driver.close()

    body = log_path.read_text()
    assert "pty.in" in body
    assert b"hello-from-pty" in saw
