"""Tail the inner `claude`'s on-disk session transcript.

Why this exists: the TUI parser sees the compact rendered form of every
tool call (e.g. `Called bzdash (ctrl+o to expand)`) and never the underlying
tool name, structured args, or full result. The inner `claude` writes the
full ground truth as JSONL to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
as the session progresses. Tailing that file gives the wrapper the real
tool_use / tool_result / text content with proper IDs already assigned.

Discovery: at wrapper-spawn time we snapshot the existing `*.jsonl` files
under the encoded-cwd project dir. The inner creates a new file when it
starts the session; the diff against the snapshot is our file. Robust
against pre-existing files from prior sessions in the same directory.

Reading: byte-offset based, with a one-line partial-write buffer. Each
`read_new()` call returns the new fully-formed JSONL entries since the
last call; the trailing partial line stays in the buffer until completed.

Skipped entry types (wrapper bookkeeping / TUI-only artefacts, no
bearing on turn accounting): `permission-mode`, `file-history-snapshot`,
`attachment`, `ai-title`, `last-prompt`.

Encoded-cwd rule: claude maps the inner's cwd to a directory name by
replacing every char that isn't `[a-zA-Z0-9-]` with `-`. So
`/home/me/proj_docs/foo.bar` becomes `-home-me-proj-docs-foo-bar`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Transcript entry types the proxy has no use for — the tailer
# silently drops them. (Listed explicitly rather than allow-listed so a
# future addition surfaces as an unknown type that can be inspected.)
SKIP_TYPES = frozenset({
    "permission-mode",
    "file-history-snapshot",
    "attachment",
    "ai-title",
    "last-prompt",
})


def encode_cwd(cwd: os.PathLike | str) -> str:
    """Translate an absolute cwd path to claude's project-dir name.

    Empirical rule (verified against 16 existing project dirs under
    `~/.claude/projects/`): replace every char outside `[a-zA-Z0-9-]` with
    `-`. A leading `/` becomes a leading `-`.
    """
    return re.sub(r"[^a-zA-Z0-9-]", "-", str(cwd))


@dataclass
class TranscriptTailer:
    """Discovers and tails one inner-`claude` JSONL transcript file.

    Lifecycle:
        t = TranscriptTailer.for_cwd(cwd)        # snapshot existing files
        # ... wrapper spawns inner claude ...
        for entry in t.read_new():                # discovers + reads
            handle(entry)
    """

    project_dir: Path
    _baseline: set[Path] = field(default_factory=set)
    _path: Optional[Path] = None
    _offset: int = 0
    _partial: str = ""

    @classmethod
    def for_cwd(cls, cwd: os.PathLike | str, projects_root: Path = PROJECTS_ROOT) -> "TranscriptTailer":
        project_dir = projects_root / encode_cwd(cwd)
        t = cls(project_dir=project_dir)
        t._baseline = t._scan()
        return t

    @classmethod
    def for_known_path(
        cls,
        cwd: os.PathLike | str,
        session_id: str,
        *,
        start_at_end: bool = False,
        projects_root: Path = PROJECTS_ROOT,
    ) -> "TranscriptTailer":
        """Deterministic-path constructor used when we know the session id
        up-front (caller passed `--session-id <uuid>` to the inner spawn,
        or the wrapper is resuming via `--resume <uuid>`).

        Bypasses the snapshot-diff: `_path` is set immediately to
        `<projects_root>/<encoded-cwd>/<session_id>.jsonl`. When the inner
        hasn't created the file yet, `read_new()` quietly returns `[]` until
        it does. When `start_at_end=True` and the file already exists (resume
        case), the read cursor is seeded at end-of-file so historical entries
        from prior turns are not re-emitted as fresh envelopes.
        """
        project_dir = projects_root / encode_cwd(cwd)
        path = project_dir / f"{session_id}.jsonl"
        t = cls(project_dir=project_dir)
        t._path = path
        if start_at_end and path.is_file():
            t._offset = path.stat().st_size
        return t

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def _scan(self) -> set[Path]:
        if not self.project_dir.is_dir():
            return set()
        try:
            return {p for p in self.project_dir.iterdir() if p.suffix == ".jsonl"}
        except OSError:
            return set()

    def _detect(self) -> Optional[Path]:
        """Find the new file claude created since `for_cwd` snapshotted."""
        if self._path is not None:
            return self._path
        current = self._scan()
        new_files = current - self._baseline
        if not new_files:
            return None
        # If multiple appeared (rare race with parallel spawns), pick the
        # most recently modified one.
        self._path = max(new_files, key=lambda p: p.stat().st_mtime)
        return self._path

    def read_new(self) -> list[dict]:
        """Return any complete new JSONL entries since the last call.

        On first call, discovers the transcript file (returns `[]` if claude
        hasn't created it yet — caller retries on the next loop tick).
        Partial trailing lines stay in `_partial` until the rest arrives.
        Skip-listed entry types are filtered out here so callers only see
        actionable entries.
        """
        path = self._detect()
        if path is None:
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except FileNotFoundError:
            return []
        if not chunk:
            return []
        buf = self._partial + chunk
        # Split on \n; the last segment is incomplete iff `buf` doesn't
        # end with \n. (split keeps the final empty string when it does.)
        lines = buf.split("\n")
        self._partial = lines.pop()
        out: list[dict] = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError:
                # Malformed line — skip silently; the transcript writer
                # should never produce one, but defensive against future
                # format quirks.
                continue
            if entry.get("type") in SKIP_TYPES:
                continue
            out.append(entry)
        return out
