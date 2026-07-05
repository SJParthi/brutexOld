"""Golden byte-identity: sequential vs THREADED prebuild (DEC-HARDWARE-ENVELOPE-001).

The correctness net for the parallel prebuild (Golden Rule #7): resample the SAME
seeded 1-minute lake into the derived timeframes + ``1day`` via BOTH paths — the
sequential :func:`run_pipeline_prebuild` and the threaded
:func:`run_pipeline_prebuild_parallel` (``workers=3``, a real
:class:`~concurrent.futures.ThreadPoolExecutor`) — and assert the two lakes are
**byte-for-byte identical** (same partition file set, same Parquet bytes).
Different symbols write different partitions under a per-partition ``flock``, so
the thread fan-out cannot diverge; the fixed ``daily_before`` removes the only
clock-dependent input (M1). A divergence here fails the merge.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.fno_cascade import build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    PrebuildTarget,
    run_pipeline_prebuild,
    run_pipeline_prebuild_parallel,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import materialize_lake_timeframes
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_FIXED_CUTOFF = date(2021, 1, 6)  # strictly after the seeded sessions → stable 1day.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)  # 09:15 IST


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


def _seed_symbol(lake_root: Path, symbol: str, *, base: float, n: int = 240) -> None:
    """Write *n* deterministic in-session 1-minute bars for *symbol*."""
    bars = [
        CandleBar(
            timestamp=_SESSION_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + 10 + i)),
            low=Decimal(str(base - 5 + i)),
            close=Decimal(str(base + 2 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=symbol, interval="1minute", bars=bars)


_SYMBOLS = {"NIFTY": 100.0, "BANKNIFTY": 400.0, "SENSEX": 700.0}


def _seed_lake(root: Path) -> None:
    for name, base in _SYMBOLS.items():
        _seed_symbol(root, name, base=base)


def _derived_parquets(root: Path) -> dict[str, bytes]:
    """Every DERIVED (non-1minute) Parquet under ``bars/``, keyed by relative path."""
    out: dict[str, bytes] = {}
    bars_root = root / "bars"
    for pq in sorted(bars_root.rglob("*.parquet")):
        rel = pq.relative_to(bars_root).as_posix()
        if "/1minute/" in f"/{rel}":
            continue  # the vendor base is copied verbatim, not derived — skip.
        out[rel] = pq.read_bytes()
    return out


def test_parallel_lake_is_byte_identical_to_sequential(tmp_path: Path) -> None:
    seq_root = tmp_path / "seq"
    par_root = tmp_path / "par"
    _seed_lake(seq_root)
    _seed_lake(par_root)

    instruments = [_spot(n) for n in _SYMBOLS]
    empty_cascade = build_fno_cascade([])

    def _seq_materialize(target: PrebuildTarget) -> object:
        return materialize_lake_timeframes(
            lake_root=seq_root,
            exchange=target.exchange,
            segment=target.segment,
            symbol=target.symbol,
            kind=target.kind,
            include_daily=True,
            daily_before=_FIXED_CUTOFF,
            incremental=False,
            dense_symbol=target.dense,
        )

    seq_outcome = run_pipeline_prebuild(
        instruments=instruments, cascade=empty_cascade, materialize=_seq_materialize
    )
    par_outcome = run_pipeline_prebuild_parallel(
        instruments=instruments,
        cascade=empty_cascade,
        lake_root=par_root,
        workers=3,
        include_daily=True,
        daily_before=_FIXED_CUTOFF,
        incremental=False,
    )

    # Both built the 3 spot symbols with identical totals.
    assert seq_outcome.symbols_built == par_outcome.symbols_built == 3
    assert seq_outcome.total_bars == par_outcome.total_bars > 0
    assert seq_outcome.ok and par_outcome.ok

    seq_files = _derived_parquets(seq_root)
    par_files = _derived_parquets(par_root)

    # Same derived-partition file SET (paths), and BYTE-IDENTICAL contents.
    assert seq_files.keys() == par_files.keys()
    assert seq_files, "expected derived partitions to have been written"
    mismatches = [rel for rel in seq_files if seq_files[rel] != par_files[rel]]
    assert mismatches == [], f"byte-divergent derived partitions: {mismatches}"


def test_kill_switch_workers_one_matches_sequential(tmp_path: Path) -> None:
    """``workers=1`` (the kill switch) is byte-identical too — the in-process path."""
    seq_root = tmp_path / "seq"
    par_root = tmp_path / "par"
    _seed_lake(seq_root)
    _seed_lake(par_root)
    instruments = [_spot(n) for n in _SYMBOLS]
    empty_cascade = build_fno_cascade([])

    run_pipeline_prebuild(
        instruments=instruments,
        cascade=empty_cascade,
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
    run_pipeline_prebuild_parallel(
        instruments=instruments,
        cascade=empty_cascade,
        lake_root=par_root,
        workers=1,
        include_daily=True,
        daily_before=_FIXED_CUTOFF,
        incremental=False,
    )
    assert _derived_parquets(seq_root) == _derived_parquets(par_root)
