"""Shared real-writer twin-lake fixtures for the acceptance-check suites.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958). Everything here rides
the PRODUCTION writers via :mod:`brutex.acceptance.selftest` — never a
hand-written parquet shape (the #1631 fixture-only blindspot).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.selftest import _LEDGER_FIELDS, build_twin_lake

if TYPE_CHECKING:
    from brutex.models import Instrument

#: Pinned clock: 2026-07-10 11:30 IST — a regular NSE Friday mid-morning.
NOW = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)

#: The two twin sessions the pinned clock resolves (Wed + Thu, both regular).
DAY1 = date(2026, 7, 8)
DAY2 = date(2026, 7, 9)

#: Deterministic (PYTHONHASHSEED-independent) security ids for the twin master.
_TWIN_SECURITY_IDS = {"NIFTY": 26000, "BANKNIFTY": 26009}


def make_twin(root: Path, **kwargs: object) -> Path:
    """Build the standard twin lake under *root* and return it."""
    lake = root / "lake"
    build_twin_lake(lake, days=(DAY1, DAY2), **kwargs)  # type: ignore[arg-type]
    return lake


def _idx_instrument(symbol: str) -> Instrument:
    """A minimal NSE index instrument row (the master only needs the row to exist)."""
    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    return Instrument(
        exch_id=Exchange.NSE,
        security_id=_TWIN_SECURITY_IDS[symbol],
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def make_master_present_twin(
    root: Path, *, nifty_status: str = "SUCCESS", **kwargs: object
) -> Path:
    """A REALISTIC master-present lake — the shape every production / dashboard /
    pulled lake has (round-6 BLOCKER regression fixture).

    Rides the REAL writers throughout (never a hand-written parquet — the #1631
    fixture-only blindspot): the standard twin bars (1m + derived 5m/1day via
    the production resamplers), a REAL window ledger with settled rows for the
    present engine indices at ``1minute`` (both sessions), and a REAL
    ``instruments.duckdb`` master listing NIFTY + BANKNIFTY as index
    instruments. With the default ``nifty_status='SUCCESS'`` the lake is
    HEALTHY: the ``sanity.completeness`` sub reads each engine index's TRUE spot
    verdict (settled → ``full``/``settled_partial_range``) and PASSes — never
    the fabricated ``none``→FAIL the pre-fix plain ``summarize_spot_datasets``
    produced on every master-present lake. Pass ``nifty_status='FAILED'`` for
    the true-positive tooth: NIFTY's spot verdict then reads ``failed`` and the
    sub FAILs (a genuinely incomplete engine index is still caught). The
    ``.manifest/`` discovery cache is materialised lazily by the first
    ``list_candle_datasets`` read on the summarize path (the production shape).
    """
    from brutex.data.instrument_store import InstrumentStore
    from brutex.data.window_ledger import WindowLedger, WindowLedgerRecord
    from brutex.data.window_ledger_store import save_window_ledger

    lake = make_twin(root, **kwargs)
    statuses = {"NIFTY": nifty_status, "BANKNIFTY": "SUCCESS"}
    records = [
        WindowLedgerRecord(
            market="groww",
            security_id=symbol,
            interval="1minute",
            window_start=day.isoformat(),
            window_end=(day + timedelta(days=1)).isoformat(),
            status=statuses[symbol],
            bars=31 if statuses[symbol] == "SUCCESS" else 0,
            attempts=1,
            last_error=None if statuses[symbol] == "SUCCESS" else "poisoned",
            first_seen_at=f"{day.isoformat()}T18:00:00+00:00",
            last_attempt_at=f"{day.isoformat()}T18:00:00+00:00",
            empty_streak=0,
            in_pull_fetches=1,
            bars_net_new=0,
            empty_retries=0,
            transient_retries=0,
        )
        for symbol in ("NIFTY", "BANKNIFTY")
        for day in (DAY1, DAY2)
    ]
    ledger = WindowLedger.from_rows([tuple(getattr(r, f) for f in _LEDGER_FIELDS) for r in records])
    save_window_ledger(ledger, lake)

    with InstrumentStore.open(lake / "instruments.duckdb") as store:
        store.upsert_many(
            [_idx_instrument("NIFTY"), _idx_instrument("BANKNIFTY")],
            snapshot_date=DAY1,
            run_id="acceptance-twin",
            csv_source_sha256="0" * 64,
        )
    return lake


def make_ctx(lake: Path, **kwargs: object) -> AcceptanceContext:
    """A pinned-clock context over *lake* (tests override fields as needed)."""
    defaults: dict[str, object] = {
        "lake_root": lake,
        "seed": "2026-07-10",
        "sample_n": 50,
        "months": 6,
        "now_utc": lambda: NOW,
    }
    defaults.update(kwargs)
    return AcceptanceContext(**defaults)  # type: ignore[arg-type]
