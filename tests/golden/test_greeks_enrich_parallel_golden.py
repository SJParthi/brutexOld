"""Sequential-vs-parallel byte-identity golden (DEC-GREEKS-PARALLEL-001).

Runs the SAME multi-underlying scenario through the REAL end-to-end
enrichment twice — ``workers=1`` (the unchanged sequential path) and
``workers=2`` (a REAL spawn ``ProcessPoolExecutor``) — over two INDEPENDENT
identical fixture lakes, and asserts the stored partitions are BYTE-EQUAL
(per-file sha256) and the reports EQUAL on every counter including
``spot_reads`` / ``spot_hits``.

Why raw byte equality is a sound oracle: the lake writer pins byte-for-byte
deterministic output for identical content (``parquet_writer.py`` docstring —
NFR-DET-001: bars sorted before render, fixed zstd level, ``use_pyarrow=False``,
fixed row-group size), and the parallel driver guarantees each partition's
merge SEQUENCE is identical to sequential (all writes to one symbol come from
ONE worker in the same sorted relative order; no other worker touches its
partitions). Both passes run in the SAME interpreter/venv (same polars/zstd
``created_by``), each over its OWN freshly-built lake — never pass 2 over pass
1's mutated lake.

The scenario deliberately includes the hard shapes: TWO underlyings (two work
units — the real pool genuinely fans out), a SAME-SYMBOL-TWO-WINDOWS target
pair via the public API (the symbol-affinity merge — write ordering must hold),
a no-spot minute (an honest NULL stamp) and an at-expiry bar (the boundary
stamp). GREEKS_FORMULA_VERSION stays 2 — the frozen sequential golden fixture
(``greeks_enrich_golden.json``) is NOT regenerated; this test compares the two
passes against EACH OTHER, never against a new frozen artifact.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars", reason="the lake writers need polars")

from brutex.data.greeks_enrich import GreeksEnrichTarget, enrich_greeks_for_targets
from brutex.options.risk_free import FixedRateSource

_RATE = FixedRateSource()
_BASE_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST, 10d to expiry
_NO_SPOT_TS = _BASE_TS + timedelta(minutes=10)
_AT_EXPIRY_TS = datetime(2024, 1, 25, 9, 59, tzinfo=UTC)  # 15:29 IST on expiry day
_WINDOW_A = (date(2024, 1, 1), date(2024, 1, 20))
_WINDOW_B = (date(2024, 1, 20), date(2024, 1, 26))
_NIFTY_CE = "NSE-NIFTY-25Jan24-21500-CE"
_NIFTY_PE = "NSE-NIFTY-25Jan24-21000-PE"
_BANK_CE = "NSE-BANKNIFTY-25Jan24-45500-CE"
_BANK_PE = "NSE-BANKNIFTY-25Jan24-45000-PE"


def _build_lake(lake: Path) -> list[GreeksEnrichTarget]:
    """Seed one deterministic two-underlying lake + the public-API target list."""
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    def bar(ts: datetime, close: str, *, oi: int | None) -> CandleBar:
        return CandleBar(
            timestamp=ts,
            open=Decimal(close),
            high=Decimal(close) + 2,
            low=Decimal(close) - 2,
            close=Decimal(close),
            volume=7,
            open_interest=oi,
        )

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    nifty_spot = [bar(_BASE_TS + timedelta(minutes=i), "21500", oi=None) for i in range(3)]
    nifty_spot.append(bar(_AT_EXPIRY_TS, "21500", oi=None))
    sink.append(
        exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=nifty_spot
    )
    bank_spot = [bar(_BASE_TS + timedelta(minutes=i), "45500", oi=None) for i in range(3)]
    sink.append(
        exchange="NSE", segment="INDEX", symbol="BANKNIFTY", interval="1minute", bars=bank_spot
    )
    ce = [
        bar(_BASE_TS + timedelta(minutes=i), c, oi=50)
        for i, c in enumerate(("200", "205.5", "198.25"))
    ]
    ce.append(bar(_NO_SPOT_TS, "199", oi=50))  # a minute with NO spot → honest NULL
    ce.append(bar(_AT_EXPIRY_TS, "1.05", oi=50))  # inside the T-floor → at_expiry
    sink.append(exchange="NSE", segment="FNO", symbol=_NIFTY_CE, interval="1minute", bars=ce)
    pe = [
        bar(_BASE_TS + timedelta(minutes=i), c, oi=25)
        for i, c in enumerate(("35.6", "36.15", "34.9"))
    ]
    sink.append(exchange="NSE", segment="FNO", symbol=_NIFTY_PE, interval="1minute", bars=pe)
    bce = [bar(_BASE_TS + timedelta(minutes=i), c, oi=10) for i, c in enumerate(("410", "415"))]
    sink.append(exchange="NSE", segment="FNO", symbol=_BANK_CE, interval="1minute", bars=bce)
    bpe = [bar(_BASE_TS + timedelta(minutes=i), c, oi=12) for i, c in enumerate(("95", "96.5"))]
    sink.append(exchange="NSE", segment="FNO", symbol=_BANK_PE, interval="1minute", bars=bpe)
    return [
        # SAME symbol under TWO windows — legal via the public API; the unit
        # builder must merge the whole NIFTY group into one worker.
        GreeksEnrichTarget(exchange="NSE", symbol=_NIFTY_CE, start=_WINDOW_A[0], end=_WINDOW_A[1]),
        GreeksEnrichTarget(exchange="NSE", symbol=_NIFTY_CE, start=_WINDOW_B[0], end=_WINDOW_B[1]),
        GreeksEnrichTarget(exchange="NSE", symbol=_NIFTY_PE, start=_WINDOW_A[0], end=_WINDOW_A[1]),
        GreeksEnrichTarget(exchange="NSE", symbol=_BANK_CE, start=_WINDOW_A[0], end=_WINDOW_A[1]),
        GreeksEnrichTarget(exchange="NSE", symbol=_BANK_PE, start=_WINDOW_A[0], end=_WINDOW_A[1]),
    ]


def _partition_hashes(lake: Path) -> dict[str, str]:
    return {
        str(p.relative_to(lake)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((lake / "bars").rglob("*.parquet"))
    }


def test_parallel_pass_is_byte_identical_to_sequential(tmp_path: Path) -> None:
    lake_seq = tmp_path / "lake_seq"
    lake_par = tmp_path / "lake_par"
    targets_seq = _build_lake(lake_seq)
    targets_par = _build_lake(lake_par)
    # The two fixture lakes are independently built yet byte-identical — the
    # dual-path comparison below can therefore attribute ANY divergence to the
    # enrichment pass itself.
    assert _partition_hashes(lake_seq) == _partition_hashes(lake_par)

    report_seq = enrich_greeks_for_targets(
        targets=targets_seq, lake_root=lake_seq, rate_source=_RATE, workers=1
    )
    # REAL spawn pool — two workers, two genuine child processes.
    report_par = enrich_greeks_for_targets(
        targets=targets_par, lake_root=lake_par, rate_source=_RATE, workers=2
    )

    assert report_par == report_seq
    assert report_seq.enriched == 5  # 4 symbols + the second window of the CE
    assert report_seq.futures_skipped == 0
    assert report_seq.errored == 0
    # Q3/A8 equality proof exercised: chains never split across units, so the
    # summed per-unit memo counters equal the sequential capacity-2 LRU's.
    assert report_par.spot_reads == report_seq.spot_reads
    assert report_par.spot_hits == report_seq.spot_hits

    hashes_seq = _partition_hashes(lake_seq)
    hashes_par = _partition_hashes(lake_par)
    assert set(hashes_seq) == set(hashes_par)  # identical relative file sets
    assert hashes_seq == hashes_par  # per-file sha256 byte-equality
