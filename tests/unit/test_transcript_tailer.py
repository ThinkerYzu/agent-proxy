"""TranscriptTailer — file discovery + incremental JSONL reading."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest

from agentproxy.transcript_tailer import TranscriptTailer, encode_cwd


def test_encode_cwd_known_paths():
    assert encode_cwd("/tmp") == "-tmp"
    assert encode_cwd("/home/me/proj_docs/foo.bar") == "-home-me-proj-docs-foo-bar"
    # underscores become dashes too
    assert encode_cwd("/a/proj_docs/x") == "-a-proj-docs-x"
    # numerics + version-style dots
    assert encode_cwd("/captures/2.1.146") == "-captures-2-1-146"


def test_discovers_new_file_against_baseline(tmp_path):
    """A `.jsonl` that didn't exist when `for_cwd` ran is the wrapper's file."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "myproj"
    project_dir = projects_root / encode_cwd(cwd)
    project_dir.mkdir(parents=True)

    # Pre-existing file from a prior session — must be ignored.
    (project_dir / "old.jsonl").write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "old"}}) + "\n")

    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)
    # No new file yet — read_new returns nothing.
    assert tailer.read_new() == []
    assert tailer.path is None

    # Inner claude creates its new file.
    new_file = project_dir / "new-sid.jsonl"
    new_file.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    entries = tailer.read_new()
    assert len(entries) == 1
    assert entries[0]["type"] == "user"
    assert tailer.path == new_file


def test_read_new_resumes_at_byte_offset(tmp_path):
    """Subsequent reads pick up only the entries written since the last read."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)

    f = pd / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "first"}}) + "\n")
    assert len(tailer.read_new()) == 1

    # Second call sees nothing new yet.
    assert tailer.read_new() == []

    # Append two more entries.
    with f.open("a") as fp:
        fp.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}) + "\n")
        fp.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    entries = tailer.read_new()
    assert [e.get("type") for e in entries] == ["assistant", "system"]


def test_partial_trailing_line_is_buffered(tmp_path):
    """A line written without trailing \\n stays in the buffer until completed."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)

    f = pd / "s.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "complete"}}) + "\n")
    f.open("a").write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hal')
    entries = tailer.read_new()
    # Only the complete entry shows up; the half-written one is buffered.
    assert len(entries) == 1
    assert entries[0]["type"] == "user"

    # Finish the half-written line.
    f.open("a").write('f-written"}]}}\n')
    entries = tailer.read_new()
    assert len(entries) == 1
    assert entries[0]["type"] == "assistant"
    assert entries[0]["message"]["content"][0]["text"] == "half-written"


def test_skip_listed_types_are_filtered(tmp_path):
    """`permission-mode`, `file-history-snapshot`, `attachment`, `ai-title`,
    `last-prompt` don't appear in the iterator output."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)

    f = pd / "s.jsonl"
    lines = [
        {"type": "permission-mode"},
        {"type": "file-history-snapshot"},
        {"type": "user", "message": {"role": "user", "content": "real"}},
        {"type": "attachment", "uuid": "x"},
        {"type": "ai-title"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "y"}]}},
        {"type": "last-prompt"},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    entries = tailer.read_new()
    assert [e["type"] for e in entries] == ["user", "assistant"]


def test_no_project_dir_returns_empty(tmp_path):
    """If the project dir doesn't exist (cwd never seen by claude), tailer
    quietly returns empty until the dir + file appear."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "never-touched"
    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)
    assert tailer.read_new() == []
    assert tailer.path is None


def test_for_known_path_reads_from_start_when_file_not_yet_created(tmp_path):
    """Vanilla `--session-id` case: file doesn't exist at spawn; once the
    inner creates it, all entries from offset 0 are returned."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    sid = "12345678-0000-0000-0000-000000000000"

    tailer = TranscriptTailer.for_known_path(
        cwd, sid, projects_root=projects_root
    )
    # File doesn't exist yet — read_new is a quiet no-op.
    assert tailer.read_new() == []
    assert tailer.path == pd / f"{sid}.jsonl"

    # Inner claude creates the file with two entries.
    f = pd / f"{sid}.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "a"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]}}) + "\n"
    )
    entries = tailer.read_new()
    assert [e["type"] for e in entries] == ["user", "assistant"]


def test_for_known_path_start_at_end_skips_historical(tmp_path):
    """`--resume <uuid>` case: file already populated from a prior session;
    `start_at_end=True` skips those entries, only new appends come through."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    sid = "12345678-0000-0000-0000-000000000000"

    f = pd / f"{sid}.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "old1"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "old2"}]}}) + "\n"
    )

    tailer = TranscriptTailer.for_known_path(
        cwd, sid, start_at_end=True, projects_root=projects_root
    )
    # Historical entries are skipped.
    assert tailer.read_new() == []

    # The next user turn appends a new entry.
    with f.open("a") as fp:
        fp.write(json.dumps({"type": "user", "message": {"role": "user", "content": "new"}}) + "\n")
    entries = tailer.read_new()
    assert len(entries) == 1
    assert entries[0]["message"]["content"] == "new"


def test_for_known_path_start_at_end_with_no_file_is_offset_zero(tmp_path):
    """`start_at_end=True` but file doesn't exist — should still read from 0
    when the file eventually appears (defensive against a stale `--resume`
    pointing at an id whose file was deleted)."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    sid = "12345678-0000-0000-0000-000000000000"

    tailer = TranscriptTailer.for_known_path(
        cwd, sid, start_at_end=True, projects_root=projects_root
    )
    assert tailer.read_new() == []

    f = pd / f"{sid}.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "first"}}) + "\n"
    )
    entries = tailer.read_new()
    assert len(entries) == 1
    assert entries[0]["message"]["content"] == "first"


def test_malformed_jsonl_line_is_skipped(tmp_path):
    """A non-JSON line (corrupted transcript or future format) doesn't kill
    the iterator; surrounding valid lines still come through."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    pd = projects_root / encode_cwd(cwd)
    pd.mkdir(parents=True)
    tailer = TranscriptTailer.for_cwd(cwd, projects_root=projects_root)

    f = pd / "s.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "ok"}}) + "\n"
        + "not json {{{\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "after"}]}}) + "\n"
    )
    entries = tailer.read_new()
    assert [e["type"] for e in entries] == ["user", "assistant"]


# ----------------------------------------------------------------------
# A forked session's backfill
# ----------------------------------------------------------------------


def _entry(kind, text, timestamp):
    return json.dumps(
        {
            "type": kind,
            "timestamp": timestamp,
            "message": {"role": kind, "content": text},
        }
    )


def test_a_forks_copied_history_is_not_read_as_this_sessions_messages(tmp_path):
    """Forking backfills the new file with the original's exchanges, rewritten
    under the new session id. They keep the *original's* timestamps, which is
    the only thing separating them from what this session goes on to say —
    without the cutoff the proxy would replay a past conversation as news.
    """
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    project_dir = projects_root / encode_cwd(cwd)
    project_dir.mkdir(parents=True)

    started_at = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    tailer = TranscriptTailer.for_known_path(
        cwd, "forked", skip_before=started_at, projects_root=projects_root
    )

    (project_dir / "forked.jsonl").write_text(
        "\n".join(
            [
                # Backfill: yesterday's conversation, copied in at startup.
                _entry("user", "old question", "2026-08-14T09:00:00.000Z"),
                _entry("assistant", "old answer", "2026-08-14T09:00:02.000Z"),
                # Bookkeeping the new session writes with no timestamp.
                json.dumps({"type": "mode", "mode": "default"}),
                # This session's own work.
                _entry("user", "new question", "2026-08-15T12:00:05.000Z"),
                _entry("assistant", "new answer", "2026-08-15T12:00:07.000Z"),
            ]
        )
        + "\n"
    )

    entries = tailer.read_new()
    said = [e.get("message", {}).get("content") for e in entries]
    assert said == [None, "new question", "new answer"]
    assert entries[0]["type"] == "mode"


def test_without_a_cutoff_every_entry_is_read(tmp_path):
    """The filter is opt-in: a new or resumed session has no backfill to drop,
    and must not lose an entry to a clock comparison it never asked for."""
    projects_root = tmp_path / "projects"
    cwd = tmp_path / "proj"
    project_dir = projects_root / encode_cwd(cwd)
    project_dir.mkdir(parents=True)
    tailer = TranscriptTailer.for_known_path(cwd, "plain", projects_root=projects_root)

    (project_dir / "plain.jsonl").write_text(
        _entry("user", "ancient", "2001-01-01T00:00:00.000Z") + "\n"
    )
    assert len(tailer.read_new()) == 1
