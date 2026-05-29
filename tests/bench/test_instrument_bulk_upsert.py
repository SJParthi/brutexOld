"""Speed guards for :meth:`InstrumentStore.upsert_many` (DEC-INSTRUMENT-BULK-UPSERT-001).

The instrument-master upsert MUST use DuckDB's vectorised columnar path (a
registered Arrow table driving an ``INSERT … SELECT`` plus an ``EXISTS``
semi-join for the replace-in-place), NOT a per-row ``DELETE``/``INSERT`` loop
and NOT a giant ``IN (?, ?, … ×N)`` list.

History (measured on the dev envelope, 100k synthetic rows):

* per-row ``DELETE``+``INSERT`` loop — minutes (~150 rows/s)
* ``executemany`` (still a per-row loop internally) — ~22 s (no better)
* ``IN``-list ``DELETE`` (O(N²)) — ~10 s on the DELETE alone
* **Arrow ``INSERT … SELECT`` + ``EXISTS`` semi-join — ~1.9 s fresh**

## Two-tier gate (matches STANDARDS row 14 / DEC-P0-020)

* **Ratio gate (always on, here):** upserting 10× the rows must NOT take
  anywhere near 10²× the time. The Arrow path is O(N) (~10× for 10× rows);
  the rejected ``IN``-list path was O(N²) (~100× for 10× rows). A generous
  ratio ceiling cleanly separates the two and is **runner-independent**, so
  it never flakes on shared CI (unlike an absolute wall-clock assertion).
* **Absolute gate (``BRUTEX_M4PRO_ABS=1`` only):** a hard wall-clock ceiling
  for the full ~100k master, dormant on shared GHA (per DEC-P0-020 absolute
  gates run only on the self-hosted runner to avoid chronic flake).
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
    reason=f"{_ENV_M4PRO_ABS}=1 not set — absolute upsert-speed gate runs only "
    "on the M4 Pro self-hosted runner (shared GHA uses the ratio gate to avoid "
    "wall-clock flake, per DEC-P0-020).",
)

#: Rows for the absolute gate — ~2/3 of the real ~150k Groww master.
_N_ABS: Final[int] = 100_000

#: Wall-clock ceiling for the absolute gate (measured ~1.9 s; 30 s headroom).
_FRESH_CEILING_SECONDS: Final[float] = 30.0

#: Small/large sizes for the always-on ratio gate (10× scale step).
_N_SMALL: Final[int] = 2_000
_N_LARGE: Final[int] = 20_000

#: Ratio ceiling: O(N) Arrow path scales ~10× for a 10× row step; the rejected
#: O(N²) IN-list path scales ~100×. 35× sits well between the two and absorbs
#: shared-runner jitter + fixed per-call overhead that inflates small-N timing.
_RATIO_CEILING: Final[float] = 35.0


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


def _time_fresh_upsert(tmp_path: Path, n: int) -> tuple[float, int]:
    """Time a fresh upsert of *n* rows into a clean store; return (seconds, count)."""
    instruments = [_mk(i) for i in range(n)]
    with InstrumentStore.open(tmp_path / f"bulk_{n}.duckdb") as store:
        start = time.perf_counter()
        result = store.upsert_many(
            instruments,
            snapshot_date=date(2026, 5, 29),
            run_id="bench",
            csv_source_sha256="0" * 64,
        )
        elapsed = time.perf_counter() - start
        assert result.rows_written == n
        assert result.rows_replaced == 0
        return elapsed, store.count(in_scope_only=False)


@pytest.mark.bench
def test_bulk_upsert_scales_sub_quadratically(tmp_path: Path) -> None:
    """A 10× row increase must not blow up the time ~100× (O(N²) regression guard).

    Runner-independent ratio gate (always on). The Arrow ``INSERT … SELECT``
    + ``EXISTS`` semi-join is O(N) (~10×); a regression to the ``IN``-list
    ``DELETE`` is O(N²) (~100×) and trips this ceiling.
    """
    t_small, n_small = _time_fresh_upsert(tmp_path, _N_SMALL)
    t_large, n_large = _time_fresh_upsert(tmp_path, _N_LARGE)

    # Dedup proven at both scales: exactly N unique rows, no duplicates.
    assert n_small == _N_SMALL
    assert n_large == _N_LARGE

    ratio = t_large / max(t_small, 1e-6)
    assert ratio < _RATIO_CEILING, (
        f"upsert time ratio {ratio:.1f}× for a {_N_LARGE // _N_SMALL}× row step "
        f"(t_{_N_SMALL}={t_small:.3f}s, t_{_N_LARGE}={t_large:.3f}s) exceeds "
        f"{_RATIO_CEILING}× — likely a regression to a per-row or IN-list "
        f"(O(N²)) path; DEC-INSTRUMENT-BULK-UPSERT-001 requires the Arrow "
        f"INSERT…SELECT + EXISTS semi-join."
    )


@skip_unless_m4pro
@pytest.mark.bench
def test_fresh_bulk_upsert_under_ceiling(tmp_path: Path) -> None:
    """A fresh 100k-row upsert completes well under the absolute speed ceiling.

    Absolute wall-clock gate — active only under ``BRUTEX_M4PRO_ABS=1``
    (self-hosted runner) per DEC-P0-020; the ratio gate above runs everywhere.
    """
    elapsed, count = _time_fresh_upsert(tmp_path, _N_ABS)
    assert count == _N_ABS
    assert elapsed < _FRESH_CEILING_SECONDS, (
        f"fresh bulk upsert of {_N_ABS} rows took {elapsed:.2f}s "
        f"(ceiling {_FRESH_CEILING_SECONDS}s) — likely a regression to a "
        f"per-row or IN-list path; DEC-INSTRUMENT-BULK-UPSERT-001 requires "
        f"the Arrow INSERT…SELECT + EXISTS semi-join."
    )
