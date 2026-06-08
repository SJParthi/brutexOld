"""Audit-log → Parquet replay harness (invariant **P-20**).

Per ``docs/TRACK1_VERIFICATION_PYRAMID.md`` rows G2 (Reproducibility) +
G7 (Audit chain), this module is the cross-layer correctness anchor:

> ``replay(audit_log) → Parquet`` byte-equals the original Parquet.

The L3 dedup oracle in :mod:`brutex.orchestrator.replay`
(``assert_dedup_consistent``, DEC-L3-REPLAY-ORACLE-001) validates the
**dedup-subject** projection of the audit log. This module extends that
to the full **Parquet-output** projection: the audit log carries enough
to reconstruct every persisted bar, so re-running the writer from the
audit log alone reproduces byte-identical partition files.

## How it works

An ingest run that wants to be replayable calls :func:`write_and_audit`
for each partition write. That helper:

1. Writes the bars to their canonical partition via the deterministic
   :class:`~brutex.data.parquet_writer.ParquetWriter`.
2. Appends ONE hash-chained audit entry (action
   :data:`PARQUET_WRITE_ACTION`) whose payload carries the partition
   coordinates, the exact bars (lossless ``Decimal``/timestamp
   encoding), and the resulting ``raw_hash``.

:func:`replay_parquet_writes` then reads the audit log, reconstructs the
bars from each ``PARQUET_WRITE`` payload, and re-writes them through the
SAME writer into a fresh lake root. Because the writer is deterministic
(zstd level 3, statistics disabled, fixed schema + row-group size,
timestamp-sorted) the replayed bytes equal the original bytes;
:func:`assert_replay_byte_equals_original` proves it and also re-checks
each replayed ``raw_hash`` against the one recorded at ingest time.

## Why the audit log is the sole replay input

The audit log is the tamper-evident record (hash-chained per
:mod:`brutex.orchestrator.audit`). Replaying from it — rather than from
the live lake — proves the lake can be rebuilt bit-for-bit from the
audit trail after a crash, an accidental delete, or a cold-storage
restore. That is exactly the reproducibility guarantee CLAUDE.md Golden
Rule #4 + #7 demand.

## Decision: DEC-P20-REPLAY-HARNESS-001 (``docs/MIGRATION.md`` entry #580).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.partition import partition_for_timestamp
from brutex.orchestrator.audit import AuditError, read_entries

if TYPE_CHECKING:  # pragma: no cover — import-only for static typing
    from collections.abc import Sequence

    from brutex.data.models import CandleBar
    from brutex.orchestrator.audit import AuditWriter

#: Audit ``action`` stamped on every replayable partition write. The
#: replay reads only entries carrying this action, so an audit log may
#: freely interleave other actions (RUN_START, dedup "add", …) without
#: confusing the Parquet projection.
PARQUET_WRITE_ACTION: Final[str] = "PARQUET_WRITE"


# ────────────────────────────────────────────────────────────────────────
# Lossless bar (de)serialisation for the audit payload
# ────────────────────────────────────────────────────────────────────────


def _bar_to_payload(bar: CandleBar) -> dict[str, Any]:
    """Encode a :class:`CandleBar` to a JSON-safe, lossless dict.

    Prices are serialised as their exact ``Decimal`` string (no float
    round-trip) and the timestamp as an ISO-8601 string (preserves the
    UTC offset + microseconds), so reconstruction reproduces an
    identical bar — the prerequisite for byte-identical Parquet.
    """
    return {
        "ts": bar.timestamp.isoformat(),
        "o": str(bar.open),
        "h": str(bar.high),
        "l": str(bar.low),
        "c": str(bar.close),
        "v": bar.volume,
        "oi": bar.open_interest,
    }


def _bar_from_payload(data: dict[str, Any]) -> CandleBar:
    """Inverse of :func:`_bar_to_payload` — reconstruct an exact bar."""
    # Imported lazily so this module stays importable without the data
    # extras when only the encoding helpers are inspected.
    from brutex.data.models import CandleBar

    oi = data["oi"]
    return CandleBar(
        timestamp=datetime.fromisoformat(str(data["ts"])),
        open=Decimal(str(data["o"])),
        high=Decimal(str(data["h"])),
        low=Decimal(str(data["l"])),
        close=Decimal(str(data["c"])),
        volume=int(data["v"]),
        open_interest=None if oi is None else int(oi),
    )


def partition_subject(
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    year: int,
    month: int,
) -> str:
    """Canonical audit ``subject`` string for one partition write.

    Mirrors the on-disk partition layout so the subject doubles as a
    dedup key the L3 oracle (:func:`brutex.orchestrator.replay`) can
    project + compare against the live set.
    """
    return f"{exchange}/{segment}/{symbol}/{interval}/{year:04d}/{month:02d}"


# ────────────────────────────────────────────────────────────────────────
# Ingest-side helper: write Parquet + append the replayable audit entry
# ────────────────────────────────────────────────────────────────────────


def write_and_audit(
    *,
    writer: ParquetWriter,
    audit: AuditWriter,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    bars: Sequence[CandleBar],
) -> WriteResult:
    """Write *bars* to their partition AND append one replayable audit entry.

    All *bars* must belong to a single ``(year, month)`` partition (the
    :class:`~brutex.data.parquet_writer.ParquetWriter` contract). The
    audit entry's payload carries the partition coordinates + the exact
    bars + the resulting ``raw_hash`` so :func:`replay_parquet_writes`
    can reconstruct the file from the audit log alone.

    Returns the underlying :class:`WriteResult`.
    """
    bar_list = list(bars)
    result = writer.write(
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        bars=bar_list,
    )
    year, month = partition_for_timestamp(result.first_ts)
    audit.append(
        action=PARQUET_WRITE_ACTION,
        subject=partition_subject(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=year,
            month=month,
        ),
        payload={
            "exchange": exchange,
            "segment": segment,
            "symbol": symbol,
            "interval": interval,
            "year": year,
            "month": month,
            "raw_hash": result.raw_hash,
            "row_count": result.row_count,
            "bars": [_bar_to_payload(b) for b in bar_list],
        },
    )
    return result


# ────────────────────────────────────────────────────────────────────────
# Replay
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayedWrite:
    """One partition reconstructed + re-written during replay.

    Attributes
    ----------
    subject
        The audit subject (partition key) this write came from.
    result
        The :class:`WriteResult` produced by re-writing into the replay
        lake root.
    expected_raw_hash
        The ``raw_hash`` recorded in the audit payload at ingest time.
        :attr:`hash_matches` compares it against ``result.raw_hash``.
    """

    subject: str
    result: WriteResult
    expected_raw_hash: str

    @property
    def hash_matches(self) -> bool:
        """True iff the replayed file hashes to the recorded ingest hash."""
        return self.result.raw_hash == self.expected_raw_hash


def replay_parquet_writes(
    *,
    audit_path: Path,
    lake_root: Path,
    action_filter: str = PARQUET_WRITE_ACTION,
) -> list[ReplayedWrite]:
    """Reconstruct every Parquet partition from the audit log.

    Reads *audit_path*, reconstructs the bars from each
    ``PARQUET_WRITE`` payload (in audit order), and re-writes them
    through a fresh :class:`ParquetWriter` rooted at *lake_root*. The
    merge-on-write writer is order-independent + idempotent, so replaying
    the entries in their recorded order reproduces the original lake.

    Parameters
    ----------
    audit_path
        Path to the hash-chained audit JSONL.
    lake_root
        Destination lake root for the replayed files. Use a fresh
        directory so the byte-comparison is against the original lake.
    action_filter
        Audit action to replay. Defaults to
        :data:`PARQUET_WRITE_ACTION`.

    Returns
    -------
    list[ReplayedWrite]
        One entry per replayed partition write, in audit order.
    """
    entries = read_entries(audit_path)
    writer = ParquetWriter(lake_root=lake_root)
    replayed: list[ReplayedWrite] = []
    for entry in entries:
        if entry.action != action_filter:
            continue
        payload = entry.payload
        bars = [_bar_from_payload(b) for b in payload["bars"]]
        result = writer.write(
            exchange=str(payload["exchange"]),
            segment=str(payload["segment"]),
            symbol=str(payload["symbol"]),
            interval=str(payload["interval"]),
            bars=bars,
        )
        replayed.append(
            ReplayedWrite(
                subject=entry.subject,
                result=result,
                expected_raw_hash=str(payload["raw_hash"]),
            )
        )
    return replayed


def _lake_parquet_files(lake_root: Path) -> dict[str, bytes]:
    """Map every ``*.parquet`` file under *lake_root* to its bytes.

    The key is the path RELATIVE to *lake_root* (POSIX form) so two
    lakes are comparable regardless of their absolute roots.
    """
    out: dict[str, bytes] = {}
    for path in sorted(lake_root.rglob("*.parquet")):
        rel = path.relative_to(lake_root).as_posix()
        out[rel] = path.read_bytes()
    return out


def assert_replay_byte_equals_original(
    *,
    audit_path: Path,
    original_lake_root: Path,
    replay_lake_root: Path,
    action_filter: str = PARQUET_WRITE_ACTION,
) -> list[ReplayedWrite]:
    """Replay the audit log + assert the result byte-equals the original lake.

    This is the executable form of invariant **P-20**. It:

    1. Replays *audit_path* into *replay_lake_root*.
    2. Asserts every replayed ``raw_hash`` matches the hash recorded at
       ingest time.
    3. Asserts the set of partition files + their exact bytes are
       identical between *original_lake_root* and *replay_lake_root*.

    Returns the :class:`ReplayedWrite` list on success.

    Raises
    ------
    AuditError
        If a replayed file does not byte-equal its original counterpart,
        a recorded hash does not match, or the two lakes hold a
        different set of partition files.
    """
    replayed = replay_parquet_writes(
        audit_path=audit_path,
        lake_root=replay_lake_root,
        action_filter=action_filter,
    )

    hash_mismatches = [rw.subject for rw in replayed if not rw.hash_matches]
    if hash_mismatches:
        raise AuditError(
            "P-20 replay: replayed raw_hash differs from the audited ingest "
            f"hash for partitions {sorted(hash_mismatches)}."
        )

    original = _lake_parquet_files(original_lake_root)
    replay = _lake_parquet_files(replay_lake_root)

    if set(original) != set(replay):
        only_original = sorted(set(original) - set(replay))
        only_replay = sorted(set(replay) - set(original))
        raise AuditError(
            "P-20 replay: partition file SET differs. "
            f"only_in_original={only_original}; only_in_replay={only_replay}."
        )

    byte_mismatches = [rel for rel in original if original[rel] != replay[rel]]
    if byte_mismatches:
        raise AuditError(
            f"P-20 replay: byte content differs for partitions {sorted(byte_mismatches)} "
            "— Parquet output is NOT reproducible from the audit log."
        )

    return replayed


def replay_summary_json(replayed: list[ReplayedWrite]) -> str:
    """Render a compact, sorted JSON summary of a replay (audit/dashboard use).

    Sorted by subject so the output is deterministic — usable as a
    golden fixture or an audit-log line.
    """
    return json.dumps(
        [
            {
                "subject": rw.subject,
                "raw_hash": rw.result.raw_hash,
                "row_count": rw.result.row_count,
                "hash_matches": rw.hash_matches,
            }
            for rw in sorted(replayed, key=lambda r: r.subject)
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "PARQUET_WRITE_ACTION",
    "ReplayedWrite",
    "assert_replay_byte_equals_original",
    "partition_subject",
    "replay_parquet_writes",
    "replay_summary_json",
    "write_and_audit",
]
