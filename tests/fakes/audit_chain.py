"""Writer-equivalent hash-chained orchestrator-audit fixtures (COMMON, §11).

Hand-built chains — bit-identical bytes to what ``AuditWriter.append`` writes
(same ``AuditEntry.to_json_line()`` + trailing newline, hash-valid links) but
WITHOUT the per-append ``os.fsync``, so a 100k-line base is cheap to build.
Shared by the streaming verify_chain unit suite and the append O(1) bench
(DEC-O1-CLAIMS-HONESTY-001).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from brutex.orchestrator.audit import GENESIS_PREV_HASH, AuditEntry

__all__ = ["chained_entry", "write_clean_chain"]


def chained_entry(
    seq: int, prev_hash: str, *, payload: dict[str, object] | None = None
) -> AuditEntry:
    """Build one hash-valid entry chained onto *prev_hash*."""
    bare = AuditEntry(
        seq=seq,
        prev_hash=prev_hash,
        ts_utc=float(seq),
        actor="brutex",
        action="STEP",
        subject=f"s:{seq}",
        payload={"i": seq} if payload is None else payload,
    )
    return replace(bare, entry_hash=bare.compute_hash())


def write_clean_chain(path: Path, n: int, *, pad: str = "") -> list[AuditEntry]:
    """Write a valid *n*-entry chain to *path* (writer-equivalent bytes)."""
    prev = GENESIS_PREV_HASH
    entries: list[AuditEntry] = []
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            payload: dict[str, object] = {"i": i}
            if pad:
                payload["pad"] = pad
            entry = chained_entry(i, prev, payload=payload)
            f.write(entry.to_json_line() + "\n")
            prev = entry.entry_hash
            entries.append(entry)
    return entries
