"""Append-only JSONL tick log — the durable, full-fidelity capture sink.

Every Groww feed callback persists ONE line: the monotonically-increasing
sequence number, the UTC receive timestamp, and the **raw snapshot verbatim**.
Storing the raw snapshot (not a parsed value) is deliberate — the exact live
leaf shape is a Phase-0 VERIFY-ON-RUN item, so any later parsing decision
(:mod:`brutex.live.tick`) can be re-applied offline to the same log without ever
re-capturing (idempotence, Golden Rule #7; "capture EVERYTHING" operator decree).

The log is **resume-safe**: re-opening an existing file truncates any torn
partial last line (a crash mid-``flush``) and continues the sequence from the
count of COMPLETE records, so a crash + restart mid-session never duplicates or
drops a sequence number. Each line is flushed immediately for crash durability,
and :meth:`TickStore.read` tolerates a torn tail (but never hides real mid-file
corruption).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

import structlog

_LOG = structlog.get_logger("brutex.live.store")


@dataclass(frozen=True, slots=True)
class TickRecord:
    """One persisted feed-callback observation.

    Attributes
    ----------
    seq
        Zero-based, gap-free sequence number within the log.
    recv_ts
        UTC tz-aware wall-clock time the callback was recorded (µs resolution).
    snapshot
        The raw feed snapshot mapping exactly as the SDK returned it — the
        vendor's own ``tsInMillis`` lives in here, UNTAMPERED.
    recv_ns
        OUR receive time in **nanoseconds** since the epoch (``time.time_ns()``)
        — the high-resolution cadence oracle. Defaults to ``recv_ts`` in ns for
        records read from a pre-``recv_ns`` log.
    """

    seq: int
    recv_ts: datetime
    snapshot: Mapping[str, Any]
    recv_ns: int = 0


class TickSink(Protocol):
    """The append-only sink contract :class:`~brutex.live.capture.TickCapture` needs.

    Both :class:`TickStore` (a single file) and
    :class:`~brutex.live.rolling.RollingTickStore` (a rotating segment set) satisfy
    it structurally, so the capture loop is agnostic to which sink it drives
    (Golden Rule #11 COMMON — one interface, two implementations).
    """

    @property
    def next_seq(self) -> int:
        """The sequence number the next :meth:`append` will assign."""

    def append(
        self, snapshot: Mapping[str, Any], *, recv_ts: datetime, recv_ns: int | None = None
    ) -> TickRecord:
        """Persist one snapshot, returning the :class:`TickRecord` written."""


def _json_default(obj: object) -> str:
    """Stringify any non-JSON-native leaf (defensive — keeps capture loss-free)."""
    return str(obj)


def _repair_and_count(path: Path) -> int:
    """Truncate any torn trailing partial line, then count complete records.

    A crash / ``SIGKILL`` / disk-full mid-``flush`` leaves a partial final line
    with NO terminating newline — bytes that can never be a committed record
    (Golden Rule #7: a partial write was never a completed append). Appending
    after them would concatenate the next record onto the garbage, so on resume
    we first **truncate back to the last complete newline** (single-writer
    capture is the only writer — the cross-process lock is a Phase-1 item), then
    count ONLY newline-terminated, non-blank lines as the resume cursor. This
    makes ``next_seq`` exact even after a torn tail, so a restart never
    duplicates or drops a sequence number.
    """
    data = path.read_bytes()
    if not data:
        return 0
    if not data.endswith(b"\n"):
        keep = data.rfind(b"\n") + 1  # 0 when there is no newline at all
        with path.open("r+b") as handle:
            handle.truncate(keep)
        _LOG.warning(
            "tick_store_torn_tail_truncated", path=str(path), dropped_bytes=len(data) - keep
        )
        data = data[:keep]
    return sum(1 for line in data.split(b"\n") if line.strip())


class TickStore:
    """Append-only JSONL writer/reader for raw feed snapshots."""

    def __init__(self, path: Path, *, start_seq: int, handle: IO[str] | None) -> None:
        """Construct directly; prefer :meth:`open` which wires the file handle."""
        self._path = path
        self._seq = start_seq
        self._handle = handle

    @classmethod
    def open(cls, path: str | Path, *, resume: bool = True) -> TickStore:
        """Open *path* for appending, resuming the sequence if it exists.

        Parameters
        ----------
        path
            Destination JSONL file (parent dirs are created).
        resume
            When ``True`` (default) and the file exists, continue numbering
            after its existing lines; when ``False`` the next sequence is ``0``
            but existing lines are still appended after (never truncated).
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        start = _repair_and_count(target) if (resume and target.exists()) else 0
        handle = target.open("a", encoding="utf-8")
        return cls(target, start_seq=start, handle=handle)

    @property
    def next_seq(self) -> int:
        """The sequence number the next :meth:`append` will assign."""
        return self._seq

    def append(
        self, snapshot: Mapping[str, Any], *, recv_ts: datetime, recv_ns: int | None = None
    ) -> TickRecord:
        """Persist one snapshot, returning the :class:`TickRecord` written.

        *recv_ns* is OUR nanosecond receive time (``time.time_ns()``); when
        omitted it is derived from *recv_ts* (µs). The vendor timestamp is NOT a
        parameter — it rides inside *snapshot* untampered.

        Raises
        ------
        ValueError
            If *recv_ts* is naive (timezone-aware UTC is mandatory).
        RuntimeError
            If the store has been closed.
        """
        if recv_ts.tzinfo is None:
            raise ValueError(f"recv_ts must be timezone-aware, got naive {recv_ts!r}")
        if self._handle is None:
            raise RuntimeError("TickStore is closed; reopen before appending")
        ns = recv_ns if recv_ns is not None else int(recv_ts.timestamp() * 1_000_000_000)
        record = TickRecord(seq=self._seq, recv_ts=recv_ts, snapshot=dict(snapshot), recv_ns=ns)
        line = json.dumps(
            {
                "seq": record.seq,
                "recv_ts": recv_ts.astimezone(UTC).isoformat(),
                "recv_ns": record.recv_ns,
                "snapshot": record.snapshot,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_json_default,
        )
        self._handle.write(line + "\n")
        self._handle.flush()
        self._seq += 1
        return record

    def close(self) -> None:
        """Close the underlying file handle (idempotent)."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> TickStore:
        """Enter a ``with`` block, returning self."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close on ``with``-block exit."""
        self.close()

    @staticmethod
    def read(path: str | Path) -> list[TickRecord]:
        """Read every persisted record back from *path* (in capture order).

        Returns ``[]`` for a missing or empty file.
        """
        target = Path(path)
        if not target.exists():
            return []
        records: list[TickRecord] = []
        with target.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        last = len(lines) - 1
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # A torn tail (a crash mid-flush) is the ONLY tolerable parse
                # failure: the last physical line with no terminating newline.
                # Anything else is real mid-file corruption — never hide it.
                if index == last and not line.endswith("\n"):
                    _LOG.warning("tick_store_torn_tail_skipped", path=str(target))
                    continue
                raise
            records.append(
                TickRecord(
                    seq=int(payload["seq"]),
                    recv_ts=datetime.fromisoformat(payload["recv_ts"]),
                    snapshot=payload["snapshot"],
                    recv_ns=int(payload.get("recv_ns", 0)),
                )
            )
        return records


__all__ = ["TickRecord", "TickSink", "TickStore"]
