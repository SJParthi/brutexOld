"""Bench: threaded prebuild scales (or at least is not slower) (DEC-HARDWARE-ENVELOPE-001).

HONEST envelope (Golden Rule #10): this measures the SEQUENTIAL vs THREADED
(``workers=N``) prebuild over a seeded multi-symbol lake and asserts the threaded
run is NOT materially slower — a regression guard, NOT a "2h → 20 min" claim
(the real speedup is bounded by the external-SSD write bandwidth + the
per-symbol GIL-light Python overhead, and only materialises on a COLD contract
build; an incremental re-run is near-zero work either way). On a shared CI box
with ≤ 2 usable cores the threaded run can be at parity — hence the tolerant
ceiling; a genuine cross-run improvement is reported, never required.

M4-Pro gate (DEC-PREBUILD-BENCH-M4PRO-GATE-001, mirroring DEC-P0-020 /
DEC-HARDENING-LEFTOVERS-001 #1149): this is an A/B WALL-CLOCK comparison —
exactly the bench class every sibling in this job env-gates behind
``BRUTEX_M4PRO_ABS=1`` — and it FLAKED on shared GHA within a day of landing
(run 28772957825: ``1.827 s <= 1.329 s * 1.05`` failed while every other gate
passed; reproduced locally on an idle 4-core box at ~1-in-4 — GIL noise flips
the cross-arm median, the #1149 write-consumer failure mode verbatim). The
timing assertion now runs only on the M4 Pro self-hosted runner; the threaded
prebuild's CORRECTNESS (byte-identical lake output vs sequential) stays
always-on in ``tests/unit/test_prebuild_parallel.py``.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.config.hardware_envelope import cpu_pool_workers
from brutex.dashboard.fno_cascade import build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    run_pipeline_prebuild,
    run_pipeline_prebuild_parallel,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import materialize_lake_timeframes
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_FIXED_CUTOFF = date(2021, 1, 8)
_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)

#: The SAME opt-in env the sibling A/B wall-clock benches key on (DEC-P0-020;
#: e.g. tests/bench/test_write_consumer_overlap_bench.py) — set only on the
#: M4 Pro self-hosted runner, never on shared GHA.
_ENV_M4PRO_ABS = "BRUTEX_M4PRO_ABS"

skip_unless_m4pro = pytest.mark.skipif(
    os.environ.get(_ENV_M4PRO_ABS, "").strip() != "1",
    reason=(
        f"{_ENV_M4PRO_ABS}=1 not set — the sequential-vs-threaded prebuild "
        "A/B wall-clock gate runs only on the M4 Pro self-hosted runner "
        "(skipping on shared GHA to avoid chronic flake per DEC-P0-020 "
        "honest-envelope; measured flake: run 28772957825 + ~1-in-4 on an "
        "idle 4-core box — DEC-PREBUILD-BENCH-M4PRO-GATE-001). The threaded "
        "prebuild's byte-identical-output correctness stays always-on in "
        "tests/unit/test_prebuild_parallel.py."
    ),
)


def _spot(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(name)) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _seed(root: Path, symbol: str, base: float, n: int = 375) -> None:
    bars = [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 9 + i)),
            low=Decimal(str(base - 4 + i)),
            close=Decimal(str(base + 1 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]
    ParquetBarSink(writer=ParquetWriter(lake_root=root)).append(
        exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars
    )


def _instruments(k: int) -> list[Instrument]:
    return [_spot(f"SYM{i:03d}") for i in range(k)]


def _seed_lake(root: Path, instruments: list[Instrument]) -> None:
    for i, inst in enumerate(instruments):
        _seed(root, inst.symbol_name, base=100.0 + 50 * i)


@pytest.mark.bench
@skip_unless_m4pro
def test_threaded_prebuild_not_slower(tmp_path: Path) -> None:
    instruments = _instruments(12)
    empty = build_fno_cascade([])
    workers = max(2, cpu_pool_workers({}))

    seq_root = tmp_path / "seq"
    par_root = tmp_path / "par"
    _seed_lake(seq_root, instruments)
    _seed_lake(par_root, instruments)

    t0 = time.perf_counter()
    run_pipeline_prebuild(
        instruments=instruments,
        cascade=empty,
        materialize=lambda t: materialize_lake_timeframes(
            lake_root=seq_root,
            exchange=t.exchange,
            segment=t.segment,
            symbol=t.symbol,
            kind=t.kind,
            include_daily=True,
            daily_before=_FIXED_CUTOFF,
            incremental=False,
            dense_symbol=t.dense,
        ),
    )
    seq_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    run_pipeline_prebuild_parallel(
        instruments=instruments,
        cascade=empty,
        lake_root=par_root,
        workers=workers,
        include_daily=True,
        daily_before=_FIXED_CUTOFF,
        incremental=False,
    )
    par_s = time.perf_counter() - t0

    speedup = seq_s / par_s if par_s > 0 else 1.0
    print(
        f"\nprebuild bench: seq={seq_s:.3f}s par={par_s:.3f}s "
        f"workers={workers} speedup={speedup:.2f}x"
    )
    # Regression guard (m-5): the threaded run must be AT WORST at parity with
    # sequential on this 12-symbol fixture. 1.05 tolerates shared-CI timing
    # noise around parity (a genuinely slower pool — lock contention, GIL-bound
    # work — reads well above it; the measured sandbox ratio is 0.72, i.e.
    # 1.39x FASTER). Deliberately NOT a >1x speedup assertion: a ≤2-core CI box
    # may only reach parity, and asserting a speedup there is flaky-by-design.
    assert par_s <= seq_s * 1.05
