"""Efficient, safe log-file tail reader for the dashboard ``/logs`` viewer.

The dashboard needs to show the operator the last N lines of the rotating
log file written by :mod:`brutex.observability.logging`. This module is the
single COMMON source of that behaviour (CLAUDE.md Golden Rule #11): the
FastAPI route in ``brutex.dashboard.app`` is a thin wrapper over
:func:`tail_lines` / :func:`tail_records`, and the audit / debugging tooling
can reuse the same reader.

Design contract:

1. **Safe on a missing file.** A path that does not exist (or is not a
   regular file) yields ``[]`` rather than raising — the viewer should show
   an empty panel before the first log line is written.
2. **Bounded memory.** :func:`tail_lines` seeks from the end of the file and
   reads fixed-size blocks backwards until it has *max_lines* newlines (or
   reaches the start), so a multi-gigabyte rotating log never gets slurped
   into memory. Only the requested tail is materialised.
3. **Clamped request size.** ``max_lines`` is clamped to ``[1, 2000]`` so a
   hostile or buggy query string (``?lines=0`` / ``?lines=999999999`` /
   negative) can never trigger an unbounded read.
4. **Byte-tolerant.** The file is decoded with ``errors="replace"`` so a
   partially-written final line or a non-UTF-8 byte never crashes the
   reader — the viewer shows a replacement character instead.

The reader is pure (filesystem in, ``list`` out) and is therefore 100%
line + branch unit-testable offline, which is where the real logic lives;
the app.py route + HTML/JS are coverage-excluded glue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from brutex.data.guarded_read import open_regular_binary

#: Lower bound for a tail request — at least one line is always returned
#: when the file is non-empty.
_MIN_LINES: Final[int] = 1

#: Upper bound for a tail request. Caps memory + response size regardless
#: of what the caller (or a hostile query string) asks for.
_MAX_LINES: Final[int] = 2000

#: Block size for the backwards seek-and-read. 64 KiB balances syscall
#: count against over-reading on a short tail request.
_BLOCK_SIZE: Final[int] = 64 * 1024


def clamp_max_lines(max_lines: int) -> int:
    """Clamp a requested line count into the safe ``[1, 2000]`` range.

    Parameters
    ----------
    max_lines
        The caller-requested number of lines. May be any int (including
        zero or negative from a malformed query string).

    Returns
    -------
    int
        ``max_lines`` clamped to ``[_MIN_LINES, _MAX_LINES]``.
    """
    if max_lines < _MIN_LINES:
        return _MIN_LINES
    if max_lines > _MAX_LINES:
        return _MAX_LINES
    return max_lines


def tail_lines(path: Path, *, max_lines: int) -> list[str]:
    """Return the last *max_lines* lines of *path* efficiently and safely.

    Seeks from the end of the file and reads fixed-size blocks backwards
    until enough newlines have been seen, so the whole file is never read
    into memory. A missing / non-regular file yields ``[]``.

    Parameters
    ----------
    path
        The log file to tail.
    max_lines
        Desired number of trailing lines. Clamped to ``[1, 2000]`` via
        :func:`clamp_max_lines` before use.

    Returns
    -------
    list[str]
        Up to *max_lines* trailing lines, oldest-first, with trailing
        newlines stripped. Empty if the file is missing or empty.
    """
    limit = clamp_max_lines(max_lines)
    if not path.is_file():
        return []

    raw = _read_tail_bytes(path, limit)
    if not raw:
        return []

    text = raw.decode("utf-8", errors="replace")
    # ``splitlines()`` drops a trailing newline without producing a
    # spurious empty final element (unlike ``split("\n")``).
    lines = text.splitlines()
    return lines[-limit:]


def _read_tail_bytes(path: Path, limit: int) -> bytes:
    """Read trailing bytes covering at least *limit* lines from *path*.

    Reads ``_BLOCK_SIZE`` chunks backwards from EOF, accumulating until the
    buffer holds more than *limit* newlines or the file start is reached.

    Parameters
    ----------
    path
        The log file (already known to be a regular file).
    limit
        The (already clamped) number of lines wanted.

    Returns
    -------
    bytes
        A suffix of the file's bytes guaranteed to contain at least *limit*
        complete lines (or the whole file if it is shorter).
    """
    with open_regular_binary(path) as handle:
        handle.seek(0, 2)  # SEEK_END
        file_size = handle.tell()
        if file_size == 0:
            return b""

        buffer = b""
        newline_count = 0
        pos = file_size
        while pos > 0 and newline_count <= limit:
            read_size = min(_BLOCK_SIZE, pos)
            pos -= read_size
            handle.seek(pos)
            block = handle.read(read_size)
            buffer = block + buffer
            newline_count += block.count(b"\n")
        return buffer


def tail_records(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    """Return the tail of *path* as parsed JSON-log records for the viewer.

    Each trailing line from :func:`tail_lines` is parsed as a JSON object
    (the shape emitted by :mod:`brutex.observability.logging`'s
    ``JSONRenderer``). Lines that are not valid JSON objects — e.g. a
    console-renderer line, a third-party banner, or a half-written final
    line — are wrapped as ``{"event": <raw line>}`` so the viewer can still
    display them verbatim instead of dropping them.

    Parameters
    ----------
    path
        The log file to tail.
    max_lines
        Desired number of trailing lines. Clamped to ``[1, 2000]``.

    Returns
    -------
    list[dict[str, Any]]
        One dict per trailing line, oldest-first. Parsed JSON objects keep
        their keys (``timestamp`` / ``level`` / ``event`` / ...); unparseable
        lines become ``{"event": <line>}``.
    """
    records: list[dict[str, Any]] = []
    for line in tail_lines(path, max_lines=max_lines):
        records.append(_parse_record(line))
    return records


def _parse_record(line: str) -> dict[str, Any]:
    """Parse one log line into a dict, falling back to a raw-event wrapper.

    Parameters
    ----------
    line
        A single log line (newline already stripped).

    Returns
    -------
    dict[str, Any]
        The decoded JSON object if *line* is a JSON object, otherwise
        ``{"event": line}``.
    """
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError):
        return {"event": line}
    if isinstance(parsed, dict):
        return parsed
    return {"event": line}


__all__ = [
    "clamp_max_lines",
    "tail_lines",
    "tail_records",
]
