"""O(1)-repeat + zero-extra-I/O bench for the /movers PCR panel (DEC-MOVERS-OPTIONS-PCR-001).

Three pins (the second added by the round-1 hostile review — the timing arm
alone benched the cache PRIMITIVE with the PCR object as inert cargo and could
not fail for a PCR regression):

1. **Cache-layer flatness (Golden Rule #5):** the PCR rows ride INSIDE the
   cached :class:`~brutex.dashboard.movers_options.OptionsCandidates` under the
   existing ``MoversSnapshotCache`` — a warmed repeat hit is a pure dict
   handoff whose latency is INDEPENDENT of the tier chain size (10-leg vs
   200-leg chains, ratio under ``ratio_ceiling(5.0)``). HONEST: this arm times
   the content-agnostic cache primitive holding a real PCR payload; the
   end-to-end "fold never re-runs" property is pinned by arm 2, not here.
2. **Fold-once END-TO-END:** through the REAL route (``build_app`` +
   ``TestClient``), a monkeypatch spy on ``compute_pcr_rows`` proves the fold
   runs EXACTLY ONCE across N repeat polls at one generation — the teeth that
   trip if fold work ever migrates into the per-request
   ``build_options_payload`` path (which the route-level
   ``compute_options_candidates`` spy in the integration suite cannot see).
3. **Zero new I/O on the cold scan:** the candidate scan performs exactly ONE
   read per tier contract through EACH of the four movers readers
   (``_read_future_session`` / ``resolve_prev_close`` / ``resolve_prev_oi`` /
   ``_underlying_spot_ltp``) — the PCR fold adds NO Parquet read through ANY
   of them (all four spied; a PCR edit adding a read through any reader trips).

Honest envelope: the FIRST scan per (coordinate, generation) still pays the
real O(contracts × bars) read, exactly as before PCR — this bench pins only
the repeat-hit flatness + the fold-once + zero-read properties, NOT an
O(1)-total claim; a hypothetical direct ``pl.read_parquet`` bypassing all four
readers is out of this net (structurally excluded instead: ``brutex/data/pcr.py``
and ``brutex/dashboard/movers_pcr.py`` import no reader).
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
from tests.fakes.movers_spies import MOVERS_READER_NAMES, spy_all_movers_readers

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


class TestPcrFoldOnceEndToEnd:
    def test_fold_runs_once_across_repeat_polls_at_one_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-1 hostile-review teeth (design §9 test 31's repeat-poll intent,
        # pinned end-to-end): drive the PRODUCTION path — real lake, real store,
        # real route — and prove compute_pcr_rows runs EXACTLY ONCE across five
        # repeat polls at one generation. A regression that re-folded per
        # request (e.g. moving fold work into build_options_payload) passes the
        # cache-primitive timing arm AND the integration compute-spy, but trips
        # HERE.
        pytest.importorskip("fastapi")
        from brutex.dashboard import movers_options as mo_module
        from brutex.dashboard.app import DashboardState, build_app
        from brutex.data.instrument_store import InstrumentStore
        from fastapi.testclient import TestClient

        writer = ParquetWriter(lake_root=tmp_path)
        for strike in (24000, 24100):
            for i, side in enumerate(("CE", "PE")):
                writer.write(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-NIFTY-27Jun24-{strike}-{side}",
                    interval="1minute",
                    bars=[_min_bar(minute=0, oi=1000 + strike + i)],
                )
        folds = {"n": 0}
        real_rows = mo_module.compute_pcr_rows

        def _counting(*args: Any, **kwargs: Any) -> Any:
            folds["n"] += 1
            return real_rows(*args, **kwargs)

        monkeypatch.setattr(mo_module, "compute_pcr_rows", _counting)
        with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
            client = TestClient(build_app(DashboardState(), instrument_store=store))
            params = {"asset": "options", "interval": "1day", "date": _DAY.isoformat()}
            bodies = [client.get("/api/v1/movers", params=params).json() for _ in range(5)]
        assert folds["n"] == 1, "the PCR fold must run once per (coordinate, generation)"
        assert bodies[0]["pcr"] != []
        assert all(body["pcr"] == bodies[0]["pcr"] for body in bodies)


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
    def test_every_reader_runs_once_per_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed a 6-leg chain (3 strikes × CE/PE) and count reads through the
        # real scan across ALL FOUR reader seams: exactly one per contract per
        # reader — the PCR fold adds ZERO through any of them.
        from brutex.dashboard import movers_options as mo_module

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
        # The underlying spot must resolve on the EXACT-key probe so the
        # one-read-per-contract law stays crisp — a spot-less lake now
        # additionally pays ONE memoised drift-recovery walk per underlying
        # (the DEC-MOVERS-FUTURES-HYPHEN-PARSE-001 W3 fold-in), pinned in
        # its own suites, not here.
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(minute=0, oi=1000)],
        )
        counts = spy_all_movers_readers(mo_module, monkeypatch)
        candidates = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert counts == dict.fromkeys(MOVERS_READER_NAMES, len(symbols)), (
            "each movers reader must run exactly once per contract — PCR adds none"
        )
        assert len(candidates.pcr) == 1
        assert candidates.pcr[0].pcr.coverage.legs_total == len(symbols)
