"""Speed guard for :meth:`InstrumentStore.upsert_many` (DEC-INSTRUMENT-BULK-UPSERT-001).

The instrument-master upsert MUST use DuckDB's vectorised columnar path (a
registered Arrow table driving an ``INSERT … SELECT`` plus an ``EXISTS``
semi-join for the replace-in-place), NOT a per-row ``DELETE``/``INSERT`` loop
and NOT a giant ``IN (?, ?, … ×N)`` list.

History (measured on the dev envelope, 100k synthetic rows):

* per-row ``DELETE``+``INSERT`` loop — minutes (~150 rows/s)
* ``executemany`` (still a per-row loop internally) — ~22 s (no better)
* ``IN``-list ``DELETE`` (O(N²)) — ~10 s on the DELETE alone
* **Arrow ``INSERT … SELECT`` + ``EXISTS`` semi-join — ~1.9 s fresh**

## Why this whole file is env-gated (BRUTEX_M4PRO_ABS=1)

Every gate here is a **wall-clock** measurement of a bulk *write*, and the
small-N timing is dominated by *fixed* setup overhead (store open, PK-index
build, Arrow registration), which is highly variable on a shared 2-core CI
runner. Neither an absolute ceiling NOR a small-vs-large ratio is stable
there — a ratio test in particular explodes when the tiny, overhead-bound
small-N case happens to time fast. Per STANDARDS row 14 / DEC-P0-020 (the
same reason ``test_throughput_absolute.py`` env-gates its class), absolute
wall-clock gates run ONLY on the M4 Pro self-hosted runner; on shared GHA
this file is fully dormant. Bulk-upsert *correctness* (dedup, idempotent
``rows_replaced``, transaction) is covered separately and always-on by the
27 integration tests in ``tests/integration/test_instrument_store.py``.
"""

from __future__ import annotations

import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

#: Env flag mirroring tests/bench/test_throughput_absolute.py — absolute
#: wall-clock gates run ONLY on the M4 Pro self-hosted runner.
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

skip_unless_m4pro = pytest.mark.skipif(
    os.environ.get(_ENV_M4PRO_ABS, "").strip() != "1",
    reason=f"{_ENV_M4PRO_ABS}=1 not set — wall-clock upsert-speed gate runs only "
    "on the M4 Pro self-hosted runner; shared GHA is too noisy for a stable "
    "bulk-write timing (per DEC-P0-020). Correctness is covered always-on by "
    "tests/integration/test_instrument_store.py.",
)

#: Rows for the absolute gate — ~2/3 of the real ~150k Groww master.
_N_ABS: Final[int] = 100_000

#: Wall-clock ceiling for the absolute gate (measured ~1.9 s; 30 s headroom).
_FRESH_CEILING_SECONDS: Final[float] = 30.0


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


@skip_unless_m4pro
@pytest.mark.bench
def test_fresh_bulk_upsert_under_ceiling(tmp_path: Path) -> None:
    """A fresh 100k-row upsert completes well under the absolute speed ceiling.

    Active only under ``BRUTEX_M4PRO_ABS=1`` (self-hosted runner). A
    regression to a per-row or ``IN``-list (O(N²)) path takes minutes and
    trips the ceiling; the Arrow ``INSERT … SELECT`` + ``EXISTS`` semi-join
    completes in ~1.9 s. Also asserts dedup (count == N).
    """
    instruments = [_mk(i) for i in range(_N_ABS)]
    with InstrumentStore.open(tmp_path / "bulk.duckdb") as store:
        start = time.perf_counter()
        result = store.upsert_many(
            instruments,
            snapshot_date=date(2026, 5, 29),
            run_id="bench",
            csv_source_sha256="0" * 64,
        )
        elapsed = time.perf_counter() - start

    assert result.rows_written == _N_ABS
    assert result.rows_replaced == 0
    assert store.count(in_scope_only=False) == _N_ABS
    assert elapsed < _FRESH_CEILING_SECONDS, (
        f"fresh bulk upsert of {_N_ABS} rows took {elapsed:.2f}s "
        f"(ceiling {_FRESH_CEILING_SECONDS}s) — likely a regression to a "
        f"per-row or IN-list path; DEC-INSTRUMENT-BULK-UPSERT-001 requires "
        f"the Arrow INSERT…SELECT + EXISTS semi-join."
    )
