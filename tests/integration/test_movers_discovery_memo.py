"""Tests for the ``/movers`` O(1) discovery memo (DEC-MOVERS-DISCOVERY-MEMO-001).

The route memoises ``list_candle_datasets`` ONCE per write-generation (the proven
``/db`` #896 pattern) and threads the cached list into spot / futures / options
compute via the optional ``datasets=`` param + the COMMON
:func:`~brutex.dashboard.movers_snapshot.resolve_candle_datasets` seam — so a tab /
section / exchange / expiry switch within one generation REUSES a single ``bars/``
walk instead of re-walking the whole lake inside each compute.

Covers: the resolve helper's both branches, each compute using an injected dataset
list (NOT re-scanning), and the end-to-end route proof that the lake is walked ONCE
across a spot→futures→options tab sweep at one generation, then again after a
generation bump.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import brutex.dashboard.app as app_module
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.movers_futures import compute_futures_candidates
from brutex.dashboard.movers_options import compute_options_candidates
from brutex.dashboard.movers_snapshot import compute_spot_candidates, resolve_candle_datasets
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _min_bar(*, close: str, oi: int | None = None) -> CandleBar:
    """A flat 1-minute bar at 09:15 IST (03:45 UTC) on ``_DAY``."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=oi,
    )


def _day_bar(*, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=None,
    )


def _write(
    tmp_path: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


# ── resolve_candle_datasets: the COMMON seam ───────────────────────────────


class TestResolveCandleDatasets:
    def test_returns_injected_list_without_scanning(self, tmp_path: Path) -> None:
        # A non-None list is returned as-is — the lake is NEVER walked (even though
        # it holds a partition the scan would otherwise return).
        _write(
            tmp_path, segment="INDEX", symbol="NIFTY", interval="1day", bars=[_day_bar(close="1")]
        )
        injected: list[dict[str, str]] = [
            {"exchange": "NSE", "segment": "INDEX", "symbol": "X", "interval": "1day"}
        ]
        assert resolve_candle_datasets(tmp_path, injected) is injected

    def test_scans_lake_when_none(self, tmp_path: Path) -> None:
        _write(
            tmp_path, segment="INDEX", symbol="NIFTY", interval="1day", bars=[_day_bar(close="1")]
        )
        got = resolve_candle_datasets(tmp_path, None)
        assert any(d["symbol"] == "NIFTY" and d["segment"] == "INDEX" for d in got)


# ── each compute USES the injected datasets (does not re-scan) ─────────────


class TestComputeUsesInjectedDatasets:
    def test_spot_uses_injected_empty_list_not_lake(self, tmp_path: Path) -> None:
        # The lake HAS NIFTY, but an injected EMPTY datasets list yields NO candidates
        # — proving discovery used the injected list, never the lake.
        _write(
            tmp_path, segment="INDEX", symbol="NIFTY", interval="1day", bars=[_day_bar(close="100")]
        )
        cands = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, datasets=[]
        )
        assert cands.snapshots == ()

    def test_futures_uses_injected_empty_list_not_lake(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(close="100", oi=5)],
        )
        cands = compute_futures_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=[]
        )
        assert cands.movers == ()

    def test_options_uses_injected_empty_list_not_lake(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-22000-CE",
            interval="1minute",
            bars=[_min_bar(close="100", oi=5)],
        )
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=[]
        )
        assert cands.movers == ()


# ── route: ONE lake walk across a spot→futures→options sweep ───────────────


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


class TestRouteDiscoveryMemo:
    def test_lake_walked_once_across_tabs_then_again_after_pull(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(
            tmp_path, segment="INDEX", symbol="NIFTY", interval="1day", bars=[_day_bar(close="100")]
        )
        # Spy on the route's discovery walk — count how many times the whole bars/
        # tree is listed.
        calls = {"n": 0}
        real = app_module.list_candle_datasets

        def _counting(lake_root: Path) -> list[dict[str, str]]:
            calls["n"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "list_candle_datasets", _counting)

        summary_cache = SummaryCache(ttl_floor_s=0.0)
        client = TestClient(
            app_module.build_app(
                app_module.DashboardState(),
                instrument_store=store,
                summary_cache=summary_cache,
                movers_cache=MoversSnapshotCache(),
            )
        )
        params = {"interval": "1day", "date": "2024-06-13", "top_n": 5}
        # A full tab sweep at ONE generation: spot → futures → options.
        for asset in ("spot", "futures", "options"):
            assert (
                client.get("/api/v1/movers", params={**params, "asset": asset}).status_code == 200
            )
        # The whole bars/ tree was walked exactly ONCE for the three tabs (memoised).
        assert calls["n"] == 1

        # A write-generation bump (a pull) invalidates the memo → exactly one fresh walk.
        summary_cache.mark_dirty()
        assert client.get("/api/v1/movers", params={**params, "asset": "spot"}).status_code == 200
        assert calls["n"] == 2


# ── warm memo byte-identity (DEC-MOVERS-WARM-UNBLOCK-001 fix 1) ────────────


class TestWarmMemoByteIdentity:
    """The warmed (memoised-``datasets``) computation equals the unmemoized one.

    The post-pull warmer now threads ONE pre-discovered ``list_candle_datasets``
    walk into every ``compute_*_candidates`` job via the EXISTING ``datasets=``
    seam (DEC-MOVERS-DISCOVERY-MEMO-001). Same function, same inputs — the
    candidate output must be EQUAL to the ``datasets=None`` self-scan, so the
    memo can never change what the warm caches (Golden Rule #7 idempotence).
    """

    def test_spot_memoized_equals_self_scan(self, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import list_candle_datasets

        _write(
            tmp_path, segment="INDEX", symbol="NIFTY", interval="1day", bars=[_day_bar(close="100")]
        )
        _write(
            tmp_path, segment="CASH", symbol="RELIANCE", interval="1day", bars=[_day_bar(close="7")]
        )
        walked = list_candle_datasets(tmp_path)
        memoized = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, datasets=walked
        )
        self_scan = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, datasets=None
        )
        assert memoized == self_scan
        assert memoized.snapshots != ()  # a REAL candidate set, not vacuous equality

    def test_futures_memoized_equals_self_scan(self, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import list_candle_datasets

        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(close="100", oi=5)],
        )
        walked = list_candle_datasets(tmp_path)
        memoized = compute_futures_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=walked
        )
        self_scan = compute_futures_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=None
        )
        assert memoized == self_scan
        assert memoized.movers != ()

    def test_options_memoized_equals_self_scan(self, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import list_candle_datasets

        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-22000-CE",
            interval="1minute",
            bars=[_min_bar(close="100", oi=5)],
        )
        walked = list_candle_datasets(tmp_path)
        memoized = compute_options_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=walked
        )
        self_scan = compute_options_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15", datasets=None
        )
        assert memoized == self_scan
        assert memoized.movers != ()
