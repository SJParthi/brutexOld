"""Dhan instrument refresh — download + select + persist the universe.

The glue the cli/dashboard call to keep the ``instruments`` catalog current:
download the detailed scrip master, select the brute-force universe
(:func:`~brutex.adapters.dhan.instrument_loader.load_universe`), and
**full-snapshot upsert** it into the vendor-neutral
:class:`~brutex.data.instrument_store.InstrumentStore` keyed by
``(vendor_symbol=SECURITY_ID, snapshot_date)``, then prune to the newest
snapshot (lean lake — DEC-INSTRUMENT-UNIVERSE-FAST-001).

``http_get`` and the ``store`` are injected, so this is fully unit-testable
without a network or a real DuckDB. ``snapshot_date`` + ``run_id`` are
supplied by the caller (deterministic, no hidden clock).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instrument_loader import HttpGet, load_universe_report
from brutex.models import Instrument


class InstrumentSink(Protocol):
    """The slice of :class:`InstrumentStore` the refresh uses."""

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        replace_snapshot: bool,
    ) -> object:
        """Upsert *instruments* for this snapshot; return the store's result."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def prune_to_latest_snapshot(self) -> int:
        """Collapse the instrument table to its newest snapshot; return rows pruned."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of an :func:`refresh_instruments` run."""

    parsed: int  # in-scope (NSE/BSE) rows seen
    persisted: int  # universe rows upserted (index + F&O-stock SPOT)
    legs_dropped: int  # in-scope rows not persisted (derivative legs + non-F&O equity)
    filtered: int  # out-of-scope rows skipped (MCX, etc.)
    snapshot_date: date
    csv_sha256: str
    pruned: int


def refresh_instruments(
    store: InstrumentSink,
    http_get: HttpGet,
    *,
    snapshot_date: date,
    run_id: str,
    url: str = INSTRUMENT_MASTER_DETAILED_URL,
) -> RefreshResult:
    """Download the scrip master, select the universe, and persist it.

    Full-snapshot replace (the master IS the day's complete universe) + prune
    to the latest snapshot, so the catalog stays lean and idempotent on
    re-pull.
    """
    text = http_get(url)
    csv_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    report = load_universe_report(lambda _url: text, url=url)
    store.upsert_many(
        report.universe,
        snapshot_date=snapshot_date,
        run_id=run_id,
        csv_source_sha256=csv_sha256,
        replace_snapshot=True,
    )
    pruned = store.prune_to_latest_snapshot()
    return RefreshResult(
        parsed=report.parsed,
        persisted=report.persisted,
        legs_dropped=report.legs_dropped,
        filtered=report.filtered,
        snapshot_date=snapshot_date,
        csv_sha256=csv_sha256,
        pruned=pruned,
    )


__all__ = [
    "InstrumentSink",
    "RefreshResult",
    "refresh_instruments",
]
