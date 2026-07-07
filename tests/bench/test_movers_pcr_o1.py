"""O(1)-repeat + zero-extra-I/O bench for the /movers PCR panel (DEC-MOVERS-OPTIONS-PCR-001).

Two pins:

1. **O(1) repeat (Golden Rule #5):** the PCR rows ride INSIDE the cached
   :class:`~brutex.dashboard.movers_options.OptionsCandidates` under the
   existing ``MoversSnapshotCache`` — a warmed repeat poll is a pure dict
   handoff whose latency is INDEPENDENT of the tier chain size (10-leg vs
   200-leg chains, ratio under ``ratio_ceiling(5.0)``, tightened to the strict
   1.1× M4-Pro tier). The fold NEVER re-runs on a hit.
2. **Zero new I/O on the cold scan:** the candidate scan performs exactly ONE
   session read per tier contract — the PCR fold adds NO Parquet read (spied
   reader call count == contract count).

Honest envelope: the FIRST scan per (coordinate, generation) still pays the
real O(contracts × bars) read, exactly as before PCR — this bench pins only
the repeat-hit flatness + the fold's zero-read property, NOT an O(1)-total
claim.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key
from brutex.dashboard.movers_options import OptionsCandidates, compute_options_candidates
from brutex.dashboard.movers_pcr import PcrLeg, compute_pcr_rows
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.bench._o1_tiers import ratio_ceiling

_DAY = date(2024, 6, 13)
_EXPIRY = date(2024, 6, 27)


def _candidates_with_pcr(n_legs: int) -> OptionsCandidates:
    """An in-memory OptionsCandidates whose PCR folded *n_legs* legs."""
    legs = [
        PcrLeg(
            underlying="NIFTY",
            opt_type="PE" if i % 2 else "CE",
            oi=100 + i,
            volume=10 + i,
        )
        for i in range(n_legs)
    ]
    return OptionsCandidates(
        movers=(),
        skipped=0,
        date=_DAY.isoformat(),
        expiry_tier="near",
        expiry_track="all",
        pcr=compute_pcr_rows(
            legs,
            legs_total={"NIFTY": n_legs},
            target_expiry={"NIFTY": _EXPIRY},
        ),
    )


def _primed(n_legs: int) -> tuple[MoversSnapshotCache, str]:
    cache = MoversSnapshotCache()
    key = candidate_key(
        asset="options",
        exchange="NSE",
        interval="1day",
        date=_DAY.isoformat(),
        time=None,
        expiry_tier="near",
        expiry_track="all",
    )
    payload = _candidates_with_pcr(n_legs)
    cache.get_or_compute(key, generation=0, compute=lambda: payload)
    return cache, key


def _must_not_recompute() -> OptionsCandidates:
    raise AssertionError("a warmed repeat poll must NEVER re-run the scan/fold")


class TestPcrRepeatHitIsO1:
    def test_repeat_hit_ratio_flat_in_chain_depth(self) -> None:
        cache_small, key_small = _primed(10)
        cache_big, key_big = _primed(200)

        def _time(cache: MoversSnapshotCache, key: str, iterations: int) -> float:
            start = time.perf_counter()
            for _ in range(iterations):
                cache.get_or_compute(key, generation=0, compute=_must_not_recompute)
            return time.perf_counter() - start

        iterations = 20_000
        _time(cache_small, key_small, 2_000)  # warm up
        _time(cache_big, key_big, 2_000)
        small = _time(cache_small, key_small, iterations)
        big = _time(cache_big, key_big, iterations)
        ratio = big / small if small > 0 else 1.0
        assert ratio < ratio_ceiling(5.0), f"PCR repeat-hit latency ratio {ratio:.2f} scaled"


def _min_bar(*, minute: int, oi: int) -> CandleBar:
    ts = datetime(_DAY.year, _DAY.month, _DAY.day, 3, 46 + minute, tzinfo=UTC)  # 09:16+ IST
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=10,
        open_interest=oi,
    )


class TestPcrColdScanAddsNoRead:
    def test_reader_runs_once_per_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed a 6-leg chain (3 strikes × CE/PE) and count session reads through
        # the real scan: exactly one per contract — the PCR fold adds ZERO.
        import brutex.dashboard.movers_options as mo_module

        writer = ParquetWriter(lake_root=tmp_path)
        symbols = [
            f"NSE-NIFTY-27Jun24-{strike}-{side}"
            for strike in (24000, 24100, 24200)
            for side in ("CE", "PE")
        ]
        for i, symbol in enumerate(symbols):
            writer.write(
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                bars=[_min_bar(minute=0, oi=1000 + i)],
            )
        calls = {"n": 0}
        real_read = mo_module._read_future_session  # pyright: ignore[reportPrivateUsage]

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real_read(*args, **kwargs)

        monkeypatch.setattr(mo_module, "_read_future_session", _counting)
        candidates = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert calls["n"] == len(symbols)  # one read per contract, none for PCR
        assert len(candidates.pcr) == 1
        assert candidates.pcr[0].pcr.coverage.legs_total == len(symbols)
