"""Dhan instrument refresh — download + select + persist the universe.

The glue the cli/dashboard call to keep the ``instruments`` catalog current:
download the detailed scrip master, select the brute-force universe
(:func:`~brutex.adapters.dhan.instrument_loader.load_universe`), and
**full-snapshot upsert** it into the vendor-neutral
:class:`~brutex.data.instrument_store.InstrumentStore` keyed by
``(security_id, exch_id, snapshot_date)`` (ADR-0003), then prune to the
newest snapshot (lean lake — DEC-INSTRUMENT-UNIVERSE-FAST-001).

``http_get`` and the ``store`` are injected, so this is fully unit-testable
without a network or a real DuckDB. ``snapshot_date`` + ``run_id`` are
supplied by the caller (deterministic, no hidden clock).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from brutex import __version__
from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instrument_loader import HttpGet, load_universe_report
from brutex.data.instrument_store import SCHEMA_VERSION, InstrumentRun
from brutex.models import Instrument
from brutex.universe import is_reference_only


class InstrumentSink(Protocol):
    """The slice of :class:`InstrumentStore` the refresh uses."""

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        reference_only_of: object = None,
        replace_snapshot: bool,
    ) -> object:
        """Upsert *instruments* for this snapshot; return the store's result."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def prune_to_latest_snapshot(self) -> int:
        """Collapse the instrument table to its newest snapshot; return rows pruned."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def record_run(self, run: InstrumentRun) -> None:
        """Record one :class:`InstrumentRun` audit row for this load."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _utcnow() -> datetime:
    """Default wall clock for the run's ``fetched_at`` (injectable for tests)."""
    return datetime.now(UTC)


def _code_commit_sha() -> str:
    """Best-available code identity for the audit row (DEC-INSTRUMENT-RUN-AUDIT-001).

    The operator's ``make run`` can stamp ``BRUTEX_COMMIT_SHA`` (a real git sha);
    absent that we fall back to the package ``__version__``. No subprocess
    (bandit-clean); ``instrument_runs.code_commit_sha`` has no length constraint,
    so either form is accepted.
    """
    return os.environ.get("BRUTEX_COMMIT_SHA") or __version__


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
    reference_security_ids: frozenset[int] = frozenset(),
    extra_index_symbols: frozenset[str] = frozenset(),
    clock: Callable[[], datetime] = _utcnow,
) -> RefreshResult:
    """Download the scrip master, select the universe, and persist it.

    Full-snapshot replace (the master IS the day's complete universe) + prune
    to the latest snapshot, so the catalog stays lean and idempotent on
    re-pull. The load is recorded in the ``instrument_runs`` audit table
    (DEC-INSTRUMENT-RUN-AUDIT-001). NOTE: a malformed CSV raises in
    ``load_universe_report`` BEFORE the record, so ``instrument_runs`` captures
    SUCCESSFUL loads only — the parser fails closed (no failed-attempt rows).
    ``clock`` is injectable so ``fetched_at`` is deterministic in tests.

    ``reference_security_ids`` (DEC-NIFTY-TOTAL-MARKET-PERSIST-001, Stage 2b
    PR-4c): NSE-equity ``SECURITY_ID``s (the resolved NIFTY-Total-Market
    constituents, typically from
    :func:`brutex.universe_constituents.resolve_reference_security_ids`) to ALSO
    admit + persist as REFERENCE_ONLY. They are admitted in the selection AND
    tagged ``reference_only=TRUE`` at upsert (alongside India VIX, which the
    default :func:`brutex.universe.is_reference_only` already tags). Defaults
    empty → byte-identical to the F&O-only refresh. The brute-force ENGINE is
    unaffected (DEC-ENGINE-ISOLATION-INVARIANT-001).

    ``extra_index_symbols`` (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001): ``SYMBOL_NAME``
    tokens to ALSO admit as INDEX rows (the runtime-discovered NIFTY Total Market
    token from :func:`brutex.adapters.dhan.ntm.prepare_ntm_reference_plan`). The
    NTM index's own ``SECURITY_ID`` is included in ``reference_security_ids`` so
    the index row is tagged ``reference_only=TRUE`` too. Default empty →
    byte-identical. NTM is an ``IDX`` outside the hardcoded brute-force 3, so the
    engine never sweeps it (DEC-ENGINE-ISOLATION-INVARIANT-001).
    """
    text = http_get(url)
    csv_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    report = load_universe_report(
        lambda _url: text,
        url=url,
        reference_security_ids=reference_security_ids,
        extra_index_symbols=extra_index_symbols,
    )

    def _is_reference(instrument: Instrument) -> bool:
        # Tag the admitted constituents (by id) AND India VIX (the default rule).
        return instrument.security_id in reference_security_ids or is_reference_only(instrument)

    store.upsert_many(
        report.universe,
        snapshot_date=snapshot_date,
        run_id=run_id,
        csv_source_sha256=csv_sha256,
        reference_only_of=_is_reference if reference_security_ids else None,
        replace_snapshot=True,
    )
    pruned = store.prune_to_latest_snapshot()
    store.record_run(
        InstrumentRun(
            run_id=run_id,
            snapshot_date=snapshot_date,
            fetched_at=clock(),
            csv_source_sha256=csv_sha256,
            total_raw_rows=report.total_raw_rows,
            integrity_failures=report.integrity_failures,
            in_scope_count=report.in_scope_count,
            out_of_scope_count=report.out_of_scope_count,
            index_count=report.index_count,
            futures_count=report.futures_count,
            options_count=report.options_count,
            bse_count=report.bse_count,
            nse_count=report.nse_count,
            code_commit_sha=_code_commit_sha(),
            schema_version=SCHEMA_VERSION,
        )
    )
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
