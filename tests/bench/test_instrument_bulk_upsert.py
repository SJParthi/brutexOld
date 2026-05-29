"""Bulk-upsert speed guard for :meth:`InstrumentStore.upsert_many`.

Pins DEC-INSTRUMENT-BULK-UPSERT-001: the instrument master upsert MUST
use DuckDB's vectorised columnar path (a registered Arrow table driving
an ``INSERT … SELECT`` plus an ``EXISTS`` semi-join for the
replace-in-place), NOT a per-row ``DELETE``/``INSERT`` loop and NOT a
giant ``IN (?, ?, … ×N)`` list.

History (measured on the dev envelope, 100k synthetic rows):

* per-row ``DELETE``+``INSERT`` loop — minutes (~150 rows/s)
* ``executemany`` (still a per-row loop internally) — ~22 s (no better)
* ``IN``-list ``DELETE`` (O(N²)) — ~10 s on the DELETE alone
* **Arrow ``INSERT … SELECT`` + ``EXISTS`` semi-join — ~1.9 s fresh**

The guard asserts a *generous* ceiling (well above the measured ~1.9 s,
below the multi-minute regressions) so it catches an accidental return
to a per-row / ``IN``-list path on a shared CI runner without flaking on
load. A true regression (linear/quadratic) blows straight past it.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

#: Rows in the speed probe — ~2/3 of the real ~150k Groww master, enough
#: that a per-row or IN-list regression is unmissable.
_N: int = 100_000

#: Wall-clock ceiling for a fresh bulk insert of :data:`_N` rows. Measured
#: ~1.9 s; 30 s leaves generous shared-runner headroom while still failing
#: hard on the ~minutes-long per-row / IN-list regressions.
_FRESH_CEILING_SECONDS: float = 30.0


def _mk(i: int) -> Instrument:
    """Construct a deterministic in-scope equity row keyed on *i*."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=i,
        trading_symbol=f"SYM{i}",
        vendor_symbol=f"NSE-SYM{i}",
        name=f"Name {i}",
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


@pytest.mark.bench
def test_fresh_bulk_upsert_under_ceiling(tmp_path: Path) -> None:
    """A fresh 100k-row upsert completes well under the speed ceiling."""
    instruments = [_mk(i) for i in range(_N)]
    with InstrumentStore.open(tmp_path / "bulk.duckdb") as store:
        start = time.perf_counter()
        result = store.upsert_many(
            instruments,
            snapshot_date=date(2026, 5, 29),
            run_id="bench",
            csv_source_sha256="0" * 64,
        )
        elapsed = time.perf_counter() - start

        assert result.rows_written == _N
        assert result.rows_replaced == 0
        # Dedup proven: exactly _N unique rows, no duplicates.
        assert store.count(in_scope_only=False) == _N
        assert elapsed < _FRESH_CEILING_SECONDS, (
            f"fresh bulk upsert of {_N} rows took {elapsed:.2f}s "
            f"(ceiling {_FRESH_CEILING_SECONDS}s) — likely a regression to a "
            f"per-row or IN-list path; DEC-INSTRUMENT-BULK-UPSERT-001 requires "
            f"the Arrow INSERT…SELECT + EXISTS semi-join."
        )
