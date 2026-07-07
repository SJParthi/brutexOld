"""Integration tests for the 🎯 Options movers snapshot (DEC-MOVERS-OPTIONS-001).

Two layers, both against a REAL Parquet + DuckDB lake (mirrors
``test_dashboard_movers_futures.py``):

1. The lake-reading core (:func:`~brutex.dashboard.movers_options.run_options_snapshot`
   + ``_discover_options``, reusing the futures ``_read_future_session`` (whose
   ``value_paisa`` field is now the options turnover, DEC-MOVERS-OPTIONS-VALUE-DEDUP-001)
   / ``_prev_day_oi`` / ``_prev_day_close`` /
   ``_underlying_spot_ltp``) is exercised directly over a ``tmp_path`` lake
   seeded with :class:`~brutex.data.parquet_writer.ParquetWriter` — the OI / OI%
   / pct / value math, the spot pairing, the per-contract rows (NOT collapsed per
   underlying), the missing-prior-day null branch, the futures-only "no options"
   branch, and the daily-vs-intraday cutoff all reached deterministically.
2. The ``GET /api/v1/movers?asset=options`` route is proven end-to-end through
   FastAPI's :class:`TestClient` (the honest proof the production ``Catalog``
   lake feeds the options reader). Store at ``tmp_path/instruments.duckdb`` ⇒
   lake root ``tmp_path``.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_options import (
    _discover_options,
    compute_options_candidates,
    run_options_snapshot,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXCHANGE = "NSE"
_DAY = date(2024, 6, 13)
_CE = "NSE-NIFTY-27Jun24-24000-CE"
_PE = "NSE-NIFTY-27Jun24-24000-PE"
_FUT = "NSE-NIFTY-27Jun24-FUT"


# ── seeding helpers ───────────────────────────────────────────────────────


def _min_bar(
    *,
    hour: int,
    minute: int,
    close: str,
    volume: int,
    oi: int | None,
    day: date = _DAY,
    high: str | None = None,
    low: str | None = None,
) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` **IST** on *day* (IST = UTC + 05:30), with OI.

    *high*/*low* default to *close* (a flat zero-range bar) so existing callers are
    unchanged; pass them to exercise the HLC3 typical-price turnover.
    """
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=Decimal(high) if high is not None else c,
        low=Decimal(low) if low is not None else c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(*, day: date, close: str, oi: int | None) -> CandleBar:
    """A 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*, with OI."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


def _write(
    tmp_path: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=_EXCHANGE, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_options_lake(tmp_path: Path) -> None:
    """Two NIFTY option contracts (CE + PE) + the NIFTY INDEX spot.

    CE LTP 164 (+OI 1100) vs prior-day close 100 / prior-day OI 1000 → pct +64%,
    oi_chg +100, oi% +10%. PE LTP 80 (+OI 900) vs prior-day close 90 / prior-day
    OI 1200 → pct −11.11%, oi_chg −300, oi% −25%. Spot LTP 23622.90.
    """
    _write(
        tmp_path,
        segment="FNO",
        symbol=_CE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="150", volume=100, oi=1050),
            _min_bar(hour=9, minute=16, close="164", volume=400, oi=1100),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_CE,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_PE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="85", volume=50, oi=950),
            _min_bar(hour=9, minute=16, close="80", volume=150, oi=900),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_PE,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="90", oi=1200)],
    )
    # Underlying NIFTY spot under INDEX (LTP 23622.90 at 09:16).
    _write(
        tmp_path,
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="23600.00", volume=50, oi=None),
            _min_bar(hour=9, minute=16, close="23622.90", volume=50, oi=None),
        ],
    )


# ── _discover_options (FNO 1minute, -CE/-PE only) ─────────────────────────


class TestDiscoverOptions:
    def test_discovers_both_contracts_not_the_future(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # A future shares the FNO segment but ends -FUT → must be excluded.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=1, oi=5)],
        )
        found = {row[0] for row in _discover_options(tmp_path)}
        assert found == {_CE, _PE}

    def test_skips_unparseable_option_symbol(self, tmp_path: Path) -> None:
        # Ends -CE but the expiry token is malformed → parse None → dropped.
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Xxx25-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="10", volume=1, oi=5)],
        )
        assert _discover_options(tmp_path) == []


# ── run_options_snapshot (direct, every branch) ───────────────────────────


class TestRunOptionsSnapshot:
    def test_empty_lake_says_pull_options_first(self, tmp_path: Path) -> None:
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_unknown_interval_degrades_honestly(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="bogus", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_futures_only_lake_has_no_options(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=1, oi=5)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_happy_daily_snapshot(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            names={"NIFTY": "Nifty 50"},
        )
        assert payload["instrument_count"] == 2  # CE + PE, one row each
        assert payload["skipped_count"] == 0

        # highest_oi: CE (1100) before PE (900).
        highest = payload["highest_oi"]
        assert [r["name"] for r in highest] == [
            "Nifty 50 27-Jun-2024 24000 CE",
            "Nifty 50 27-Jun-2024 24000 PE",
        ]
        ce_row = highest[0]
        assert ce_row["spot"] == "₹23,622.90"
        assert ce_row["ltp"] == "₹164.00"
        assert Decimal(ce_row["pct_change"]) == Decimal("64")  # 164 vs 100
        assert ce_row["oi"] == 1100
        assert ce_row["oi_chg"] == 100  # 1100 - 1000
        assert Decimal(ce_row["oi_chg_pct"]) == Decimal("10")  # 1100 vs 1000
        assert ce_row["volume"] == 500
        # Moneyness vs spot 23622.90 < strike 24000 (DEC-FNO-MONEYNESS-001): CE OTM,
        # PE ITM. Single strike in the lake → no inferable grid → ATM±N is None.
        assert ce_row["moneyness"] == "OTM"
        assert ce_row["atm_offset"] is None
        pe_row = highest[1]
        assert pe_row["moneyness"] == "ITM"
        assert pe_row["atm_offset"] is None

        # CE oi% +10 → in oi_gainers; PE oi% -25 → in oi_losers.
        assert [r["name"] for r in payload["oi_gainers"]] == ["Nifty 50 27-Jun-2024 24000 CE"]
        assert [r["name"] for r in payload["oi_losers"]] == ["Nifty 50 27-Jun-2024 24000 PE"]

        # CE pct +64 → price_gainers; PE pct -11.11 → price_losers.
        assert [r["name"] for r in payload["price_gainers"]] == ["Nifty 50 27-Jun-2024 24000 CE"]
        assert [r["name"] for r in payload["price_losers"]] == ["Nifty 50 27-Jun-2024 24000 PE"]

        # value: CE 80600 > PE (85*50 + 80*150 = 4250 + 12000 = 16250).
        assert [r["name"] for r in payload["value"]] == [
            "Nifty 50 27-Jun-2024 24000 CE",
            "Nifty 50 27-Jun-2024 24000 PE",
        ]
        pe_signed = next(r for r in payload["oi_losers"])
        assert pe_signed["oi_chg"] == -300  # 900 - 1200

    def test_dynamic_strike_step_drives_atm_offset(self, tmp_path: Path) -> None:
        # Three CE strikes on the SAME (underlying, expiry) → the grid step is DERIVED
        # (min gap 50), so ATM±N resolves for EVERY F&O underlying without a hardcoded
        # NSE rule (DEC-FNO-MONEYNESS-001 / Golden Rule #1). Spot 23622.90.
        for strike in ("23600", "23650", "24000"):
            symbol = f"NSE-NIFTY-27Jun24-{strike}-CE"
            _write(
                tmp_path,
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                bars=[_min_bar(hour=9, minute=16, close="164", volume=400, oi=1100)],
            )
            _write(
                tmp_path,
                segment="FNO",
                symbol=symbol,
                interval="1day",
                bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
            )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="23622.90", volume=50, oi=None)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        by_name = {r["name"]: r for r in payload["highest_oi"]}
        # 23600 CE: |23622.90 - 23600| = 22.90 <= step/2 (25) → ATM, offset 0.
        assert by_name["NIFTY 27-Jun-2024 23600 CE"]["moneyness"] == "ATM"
        assert by_name["NIFTY 27-Jun-2024 23600 CE"]["atm_offset"] == 0
        # 24000 CE: spot below strike → OTM by 8 strikes; the shown cell is the combined
        # label "OTM +8" (round((24000-23622.90)/50) = 8), the raw offset stays in payload.
        assert by_name["NIFTY 27-Jun-2024 24000 CE"]["moneyness"] == "OTM +8"
        assert by_name["NIFTY 27-Jun-2024 24000 CE"]["moneyness_bucket"] == "OTM"
        assert by_name["NIFTY 27-Jun-2024 24000 CE"]["atm_offset"] == 8

    def test_no_underlying_spot_nulls_spot(self, tmp_path: Path) -> None:
        # CE present but NO underlying spot → spot null, still ranked.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="164", volume=100, oi=1100)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["highest_oi"][0]["spot"] is None

    def test_no_prior_day_nulls_pct_and_oi_pct(self, tmp_path: Path) -> None:
        # CE has a session bar but NO 1day prior partition → null %/OI%.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="164", volume=100, oi=1100)],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        row = payload["highest_oi"][0]
        assert row["pct_change"] is None
        assert row["oi_chg"] is None
        assert row["oi_chg_pct"] is None
        assert payload["price_gainers"] == []
        assert payload["oi_gainers"] == []

    def test_contract_with_no_session_bar_is_skipped(self, tmp_path: Path) -> None:
        # The contract exists but has NO bar on the snapshot day → skip.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="10", volume=1, oi=5, day=date(2024, 6, 10))],
        )
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=10
        )
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 1
        assert payload["highest_oi"] == []

    def test_intraday_cutoff_path(self, tmp_path: Path) -> None:
        _seed_options_lake(tmp_path)
        # 5-minute interval at 09:15 → only the 09:15 CE/PE bars (CE close 150).
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="5minute", date="2024-06-13", time="09:15", top_n=10
        )
        assert payload["instrument_count"] == 2
        ce_row = payload["highest_oi"][0]
        assert ce_row["ltp"] == "₹150.00"
        assert ce_row["oi"] == 1050


# ── run_options_snapshot WITHIN a cadence track (DEC-MOVERS-OPTIONS-TRACK-001) ──


_WK_CE = "NSE-NIFTY-20Jun24-24000-CE"  # June 20 → a weekly (June 27 is the June monthly)


def _seed_weekly_plus_monthly(tmp_path: Path) -> None:
    """The standard June-27 monthly CE/PE/spot PLUS a June-20 weekly CE (same month)."""
    _seed_options_lake(tmp_path)  # June 27 CE + PE + NIFTY spot
    _write(
        tmp_path,
        segment="FNO",
        symbol=_WK_CE,
        interval="1minute",
        bars=[_min_bar(hour=9, minute=16, close="120", volume=200, oi=700)],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_WK_CE,
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="110", oi=600)],
    )


class TestRunOptionsSnapshotTrack:
    def test_monthly_keeps_only_the_last_in_month_expiry(self, tmp_path: Path) -> None:
        _seed_weekly_plus_monthly(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            expiry_track="monthly",
        )
        # Only the June-27 monthly CE + PE → the June-20 weekly drops out.
        assert payload["instrument_count"] == 2
        names = {row["name"] for row in payload["highest_oi"]}
        assert names == {"NIFTY 27-Jun-2024 24000 CE", "NIFTY 27-Jun-2024 24000 PE"}
        assert "Monthly" in payload["note"]

    def test_weekly_keeps_only_the_non_terminal_expiry(self, tmp_path: Path) -> None:
        _seed_weekly_plus_monthly(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            expiry_track="weekly",
        )
        # Only the June-20 weekly CE → the June-27 monthly contracts drop out.
        assert payload["instrument_count"] == 1
        assert payload["highest_oi"][0]["name"] == "NIFTY 20-Jun-2024 24000 CE"
        assert "Weekly" in payload["note"]

    def test_unknown_track_normalises_to_all(self, tmp_path: Path) -> None:
        _seed_weekly_plus_monthly(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            expiry_track="bogus",
        )
        # Normalised to ``all`` → the default Near tier picks the nearest expiry
        # overall (the June-20 weekly), and the tier label carries NO cadence suffix
        # (that is what proves it normalised to ``all`` and not to ``weekly``).
        assert payload["instrument_count"] == 1
        assert payload["highest_oi"][0]["name"] == "NIFTY 20-Jun-2024 24000 CE"
        assert "Near expiry" in payload["note"]
        assert "Near Weekly" not in payload["note"]
        assert "Near Monthly" not in payload["note"]


# ── route wiring (FastAPI TestClient) ─────────────────────────────────────


def _idx(symbol: str) -> Instrument:
    """A minimal NSE index instrument (the store only needs a row to exist)."""
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
        [_idx("NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


class TestOptionsApi:
    def test_ranks_a_real_options_lake(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        assert body["instrument_count"] == 2
        assert body["skipped_count"] == 0
        ce_row = body["highest_oi"][0]
        assert ce_row["name"] == "NIFTY 27-Jun-2024 24000 CE"
        assert ce_row["ltp"] == "₹164.00"
        assert ce_row["oi"] == 1100
        assert Decimal(ce_row["oi_chg_pct"]) == Decimal("10")
        assert ce_row["oi_chg"] == 100

    def test_expiry_tier_filters_near_vs_next(self, store: InstrumentStore, tmp_path: Path) -> None:
        # June (near) CE/PE + a July (next) CE for the SAME underlying.
        _seed_options_lake(tmp_path)
        jul_ce = "NSE-NIFTY-25Jul24-24000-CE"
        _write(
            tmp_path,
            segment="FNO",
            symbol=jul_ce,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="200", volume=300, oi=2000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=jul_ce,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="180", oi=1800)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        # expiry=near (the front expiry as-of the date) → only the June 27 CE/PE.
        near = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "expiry": "near"},
        ).json()
        assert near["instrument_count"] == 2
        assert "Near expiry" in near["note"]
        assert all("25-Jul-2024" not in row["name"] for row in near["highest_oi"])

        # expiry=Next (mixed case → lower-cased) → only the July 25 CE.
        nxt = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "expiry": "Next"},
        ).json()
        assert nxt["instrument_count"] == 1
        assert nxt["highest_oi"][0]["name"] == "NIFTY 25-Jul-2024 24000 CE"
        assert "Next expiry" in nxt["note"]

    def test_track_monthly_filters_out_the_weekly(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # June-27 monthly CE/PE + a June-20 weekly CE for the SAME underlying/month.
        _seed_weekly_plus_monthly(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "asset": "options",
                "interval": "1day",
                "date": "2024-06-13",
                "track": "Monthly",  # mixed case → lower-cased
            },
        ).json()
        assert body["instrument_count"] == 2  # the weekly June-20 CE is excluded
        assert all("20-Jun-2024" not in row["name"] for row in body["highest_oi"])
        assert "Near Monthly expiry" in body["note"]

    def test_empty_options_lake_degrades(self, store: InstrumentStore) -> None:
        # Store wired but no options parquet → honest empty payload (never 500).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O options first" in body["note"]

    def test_futures_asset_still_works(self, store: InstrumentStore, tmp_path: Path) -> None:
        # The route change must NOT break the futures dispatch (no contracts → empty).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers", params={"asset": "futures"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O futures first" in body["note"]

    def test_malformed_date_is_400(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"asset": "options", "date": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_no_lake_root_options_empty(self) -> None:
        # A store whose `path` is not a filesystem Path → empty options payload.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["instrument_count"] == 0
        assert "pull the F&O options first" in body["note"]

    def test_min_volume_floor_drops_thin_contracts(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-MIN-VOLUME-001: the OPT-IN ?min_volume= floor filters the Top Volume
        # ranking. The seeded CE has volume 500, the PE 200 → a floor of 300 keeps only CE.
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "asset": "options",
                "interval": "1day",
                "date": "2024-06-13",
                "min_volume": 300,
            },
        ).json()
        assert body["min_volume"] == 300
        assert [r["volume"] for r in body["volume"]] == [500]
        assert "Min volume ≥ 300." in body["note"]

    def test_min_volume_default_zero_is_byte_identical(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # No ?min_volume= (route default 0) → both contracts rank, no floor note.
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13"},
        ).json()
        assert body["min_volume"] == 0
        assert len(body["volume"]) == 2
        assert "Min volume" not in body["note"]

    def test_negative_min_volume_is_422(self, store: InstrumentStore) -> None:
        # The route caps min_volume >= 0 — a negative value is rejected at validation.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"asset": "options", "min_volume": -1})
        assert resp.status_code == 422

    def test_calendar_tier_uses_registry_over_sparse_lake(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-CALENDAR-TIERS-001 end-to-end through the route: a sparse July lake
        # (07-Jul candles only) + a registry calendar with the full July weeklies → the
        # Far weekly tier resolves to the calendar's 21-Jul (honest no-data), NOT a wrong
        # serial expiry from the sparse lake.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from brutex.data.registry_store import RegistryStore

        for opt in ("CE", "PE"):
            sym = f"NSE-NIFTY-07Jul25-24000-{opt}"
            _write(
                tmp_path,
                segment="FNO",
                symbol=sym,
                interval="1minute",
                bars=[
                    _min_bar(
                        hour=9, minute=15, close="150", volume=100, oi=10, day=date(2025, 7, 7)
                    )
                ],
            )
        now = _dt(2025, 7, 1, tzinfo=_UTC)
        with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
            for exp in (date(2025, 7, 7), date(2025, 7, 14), date(2025, 7, 21), date(2025, 7, 28)):
                reg.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=now)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "asset": "options",
                "interval": "1day",
                "date": "2025-07-07",
                "expiry": "Far",
                "track": "Weekly",
            },
        ).json()
        # 21-Jul is the calendar Far weekly with no pulled candles → honest no-data note.
        assert body["instrument_count"] == 0
        assert "vendor calendar" in body["note"]


# ── compute_options_candidates discovery + resilience branches ────────────
# These exercise the per-file gate branches the broad dashboard suite covered
# but the narrowly-mapped tests did not: the _discover_options segment/interval
# skip and the compute_options_candidates corrupt-Parquet skip
# (DEC-MOVERS-CORRUPT-SKIP-001).


class TestComputeOptionsCandidatesBranches:
    def test_non_one_minute_fno_dataset_is_skipped_in_discovery(self, tmp_path: Path) -> None:
        # A real 1minute option is discovered; a sibling FNO dataset at a NON-1minute
        # interval (here 1day) is skipped at the segment/interval guard in
        # _discover_options, so only the 1minute option registers.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="120", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_PE,
            interval="1day",
            bars=[_day_bar(day=_DAY, close="60", oi=700)],
        )
        result = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            expiry_tier="near",
            expiry_track="all",
        )
        # Only the 1minute CE registers; the 1day PE is skipped in discovery.
        assert len(result.movers) == 1
        assert result.skipped == 0

    def test_corrupt_option_partition_is_skipped_not_raised(self, tmp_path: Path) -> None:
        # A single corrupt option Parquet is SKIPPED (logged + counted), never
        # 500-ing the options screen (DEC-MOVERS-CORRUPT-SKIP-001).
        from structlog.testing import capture_logs

        good = "NSE-NIFTY-27Jun24-24000-CE"
        bad = "NSE-NIFTY-27Jun24-25000-CE"
        _write(
            tmp_path,
            segment="FNO",
            symbol=good,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="120", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=bad,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="60", volume=80, oi=700)],
        )
        corrupt = list((tmp_path / "bars" / _EXCHANGE / "FNO" / bad / "1minute").rglob("*.parquet"))
        assert corrupt, "expected the 25000-CE 1minute partition to exist"
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")

        with capture_logs() as logs:
            result = compute_options_candidates(
                lake_root=tmp_path,
                interval="1day",
                date="2024-06-13",
                time=None,
                expiry_tier="near",
                expiry_track="all",
            )
        assert len(result.movers) == 1
        assert result.skipped == 1
        skips = [e for e in logs if e["event"] == "movers.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["source"] == "options"
        assert skips[0]["symbol"] == bad

    def test_exchange_filter_skips_other_exchange(self, tmp_path: Path) -> None:
        # An option whose exchange != the requested one hits the per-exchange
        # discovery ``continue`` in ``_discover_options`` (the branch the broad
        # exchange-cache suite covered but this stem file did not).
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-24000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="120", volume=100, oi=1000)],
        )
        writer.write(
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-27Jun24-80000-CE",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="200", volume=50, oi=500)],
        )
        nse = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            expiry_tier="near",
            expiry_track="all",
            exchange="NSE",
        )
        # The BSE option is filtered out by the per-exchange discovery ``continue``.
        assert {m.underlying for m in nse.movers} == {"NIFTY"}


# ── DEC-MOVERS-CALENDAR-TIERS-001: calendar-driven tiers over a sparse lake ──


class TestCalendarDrivenTiersEndToEnd:
    """End-to-end: a REPRESENTATIVE sparsely-pulled lake + the full vendor calendar.

    Proves the calendar (registry) drives the tier over a sparse lake and surfaces an
    honest no-data tier — a representative config that exercises the behaviour, NOT a
    reconstruction of the operator's exact reported lake (Golden Rule #1).
    """

    @staticmethod
    def _seed_sparse_july_lake(tmp_path: Path) -> None:
        # Only the 07-Jul + 04-Aug NIFTY options have candles on disk (21/28-Jul were never
        # pulled) — so the lake-only "Far weekly" would wrongly jump past them.
        for exp_tag, exp in (("07Jul25", date(2025, 7, 7)), ("04Aug25", date(2025, 8, 4))):
            for opt in ("CE", "PE"):
                sym = f"NSE-NIFTY-{exp_tag}-24000-{opt}"
                _write(
                    tmp_path,
                    segment="FNO",
                    symbol=sym,
                    interval="1minute",
                    bars=[_min_bar(hour=9, minute=15, close="150", volume=100, oi=10, day=exp)],
                )

    @staticmethod
    def _full_calendar() -> dict[str, set[date]]:
        return {
            "NIFTY": {
                date(2025, 7, 7),
                date(2025, 7, 14),
                date(2025, 7, 21),
                date(2025, 7, 28),
                date(2025, 8, 4),
            }
        }

    def test_far_weekly_resolves_to_calendar_21jul_with_honest_no_data(
        self, tmp_path: Path
    ) -> None:
        # As-of 2025-07-07: Weekly track = {07,14,21-Jul} (28-Jul is the monthly), so
        # Far = 21-Jul — a calendar expiry with NO pulled contracts → calendar_only_expiry.
        self._seed_sparse_july_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2025-07-07",
            time=None,
            expiry_tier="far",
            expiry_track="weekly",
            calendar_expiries=self._full_calendar(),
        )
        assert cands.movers == ()  # no 21-Jul candles pulled
        assert cands.calendar_only_expiry is True
        # Design test-plan item 25's calendar-only × PCR permutation: the
        # early-return honest-no-data shape carries NO pcr rows — never a
        # fabricated chain (DEC-MOVERS-OPTIONS-PCR-001 edge 15).
        assert cands.pcr == ()

    def test_near_weekly_resolves_to_07jul_with_real_data(self, tmp_path: Path) -> None:
        # Near weekly = 07-Jul, which HAS pulled candles → real movers, not calendar-only.
        self._seed_sparse_july_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2025-07-07",
            time=None,
            expiry_tier="near",
            expiry_track="weekly",
            calendar_expiries=self._full_calendar(),
        )
        assert {m.expiry for m in cands.movers} == {date(2025, 7, 7)}
        assert cands.calendar_only_expiry is False

    def test_without_calendar_far_weekly_is_empty_no_honest_flag(self, tmp_path: Path) -> None:
        # WITHOUT the calendar (registry-less fallback): the lake weeklies are {07-Jul,
        # 04-Aug} (each the sole expiry of its month → both "monthly"), so the weekly
        # track is empty and no Far weekly resolves — and the honest flag is NOT set
        # (there is no calendar-known tier expiry to surface).
        self._seed_sparse_july_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2025-07-07",
            time=None,
            expiry_tier="far",
            expiry_track="weekly",
            calendar_expiries=None,
        )
        assert cands.movers == ()
        assert cands.calendar_only_expiry is False

    def test_run_snapshot_threads_calendar_into_payload_note(self, tmp_path: Path) -> None:
        # The thin compose surfaces the honest no-data note end-to-end.
        self._seed_sparse_july_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2025-07-07",
            time=None,
            top_n=10,
            expiry_tier="far",
            expiry_track="weekly",
            calendar_expiries=self._full_calendar(),
        )
        assert "vendor calendar" in payload["note"]
        assert "NO option contract candles" in payload["note"]

    def test_registry_round_trip_drives_calendar_tiers(self, tmp_path: Path) -> None:
        # The FULL chain: persist the July calendar into a real RegistryStore, read it
        # back via load_registry_expiries, and prove the Far weekly tier is 21-Jul.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from brutex.dashboard.movers_options import load_registry_expiries
        from brutex.data.registry_store import RegistryStore

        self._seed_sparse_july_lake(tmp_path)
        registry_path = tmp_path / "registry.duckdb"
        now = _dt(2025, 7, 1, tzinfo=_UTC)
        with RegistryStore.open(registry_path) as reg:
            for exp in (date(2025, 7, 7), date(2025, 7, 14), date(2025, 7, 21), date(2025, 7, 28)):
                reg.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=now)
        calendar = load_registry_expiries(registry_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2025-07-07",
            time=None,
            expiry_tier="far",
            expiry_track="weekly",
            calendar_expiries=calendar,
        )
        # 21-Jul is the calendar Far weekly, but no candles → honest empty.
        assert cands.calendar_only_expiry is True


# ── stored Greeks on the options screen (DEC-GREEKS-ENGINE-PR3B-001) ───────


def _greeks_min_bar(
    *,
    hour: int,
    minute: int,
    close: str,
    volume: int,
    oi: int | None,
    iv: float | None,
    delta: float | None,
    day: date = _DAY,
) -> FnoGreeksBar:
    """An ENRICHED option 1-minute bar (stored Greeks overlay) at ``HH:MM`` IST."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return FnoGreeksBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
        iv=iv,
        delta=delta,
    )


class TestOptionsGreeksEndToEnd:
    """The stored per-bar Greeks overlay (Phase-2b, DEC-GREEKS-ENGINE-PR2B-001)
    reaches the options movers rows as-of the snapshot (DEC-GREEKS-ENGINE-PR3B-001):
    sourced from the SAME session read as ltp/volume/oi (never recomputed at
    render), schema-tolerant — an enriched CE shows its last kept bar's iv/delta
    while a legacy pre-Greeks PE in the SAME lake honestly reads None."""

    def _seed_mixed_lake(self, tmp_path: Path) -> None:
        """The standard CE/PE lake, but the CE's 1-minute partition is ENRICHED."""
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[
                _greeks_min_bar(
                    hour=9, minute=15, close="150", volume=100, oi=1050, iv=0.17, delta=0.58
                ),
                _greeks_min_bar(
                    hour=9, minute=16, close="164", volume=400, oi=1100, iv=0.1812, delta=0.62
                ),
            ],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="100", oi=1000)],
        )
        # The PE partition stays LEGACY (plain CandleBar, 7 columns — pre-Greeks).
        _write(
            tmp_path,
            segment="FNO",
            symbol=_PE,
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15, close="85", volume=50, oi=950),
                _min_bar(hour=9, minute=16, close="80", volume=150, oi=900),
            ],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_PE,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="90", oi=1200)],
        )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15, close="23600.00", volume=50, oi=None),
                _min_bar(hour=9, minute=16, close="23622.90", volume=50, oi=None),
            ],
        )

    def test_candidates_carry_stored_greeks_and_legacy_reads_none(self, tmp_path: Path) -> None:
        self._seed_mixed_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None
        )
        by_symbol = {m.symbol: m for m in cands.movers}
        ce, pe = by_symbol[_CE], by_symbol[_PE]
        # The LAST kept session bar's STORED values, full precision — not the first's.
        assert ce.iv == 0.1812
        assert ce.delta == 0.62
        # A legacy pre-Greeks partition honestly reads None — never fabricated.
        assert pe.iv is None
        assert pe.delta is None

    def test_intraday_cutoff_snapshots_that_bars_greeks(self, tmp_path: Path) -> None:
        """The 09:15 cutoff shows the AS-OF bar's Greeks (0.17/0.58), the same
        last-kept-bar rule the LTP/OI snapshot uses."""
        self._seed_mixed_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1minute", date="2024-06-13", time="09:15"
        )
        ce = next(m for m in cands.movers if m.symbol == _CE)
        assert ce.ltp == Decimal("150.00")
        assert ce.iv == 0.17
        assert ce.delta == 0.58

    def test_options_api_rows_carry_iv_and_delta(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """End-to-end through the real route: the payload rows carry full-precision
        float iv/delta for the enriched CE and honest nulls for the legacy PE."""
        self._seed_mixed_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        rows = {row["name"]: row for row in body["highest_oi"]}
        ce_row = rows["NIFTY 27-Jun-2024 24000 CE"]
        pe_row = rows["NIFTY 27-Jun-2024 24000 PE"]
        assert ce_row["iv"] == 0.1812
        assert ce_row["delta"] == 0.62
        assert pe_row["iv"] is None
        assert pe_row["delta"] is None

    def test_movers_template_offers_iv_and_delta_columns(self) -> None:
        """The options table config carries the IV + Delta columns (num4 = 4dp
        DISPLAY-only formatting; the payload floats stay full precision)."""
        from brutex.dashboard.app import _render_movers_html

        html = _render_movers_html()
        assert '["IV","iv","num4"]' in html
        assert '["Delta","delta","num4"]' in html
        assert 'type === "num4"' in html  # the 4dp display formatter exists


# ── the AUTHORITATIVE registry strike-grid step (DEC-MONEYNESS-REGISTRY-STEP-001) ──


class TestRegistryStrikeStep:
    """The options screen prefers the FULL vendor strike chain for the ATM±N grid.

    A sparsely-pulled expiry (only alternate strikes have candles) makes the
    lake-observed min gap OVERESTIMATE the true base step; the registry chain —
    loaded via the graceful ``load_registry_strike_chains`` fallback pattern —
    corrects it, and the fallback is honestly labelled low-confidence.
    """

    _CE_24100 = "NSE-NIFTY-27Jun24-24100-CE"

    def _seed_sparse_grid_lake(self, tmp_path: Path) -> None:
        """CE strikes 24000 + 24100 ONLY (observed gap 100; the true grid is 50)."""
        _seed_options_lake(tmp_path)  # 24000 CE/PE + the NIFTY spot (LTP 23622.90)
        _write(
            tmp_path,
            segment="FNO",
            symbol=self._CE_24100,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="120", volume=100, oi=500)],
        )

    def _chains(self) -> dict[tuple[str, date], tuple[Decimal, ...]]:
        return {
            ("NIFTY", date(2024, 6, 27)): (
                Decimal("24000"),
                Decimal("24050"),
                Decimal("24100"),
            )
        }

    def test_without_chains_the_sparse_step_overestimates_with_honest_label(
        self, tmp_path: Path
    ) -> None:
        # REGRESSION (the pre-fix depth, now honestly labelled): spot 23622.90,
        # strike 24000, observed step 100 → (377.10 / 100) rounds to +4.
        self._seed_sparse_grid_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None
        )
        ce = next(m for m in cands.movers if m.symbol == _CE)
        assert ce.moneyness_label == "OTM +4"
        assert ce.strike_step_confidence is not None
        assert ce.strike_step_confidence.value == "observed"

    def test_registry_chains_correct_the_step(self, tmp_path: Path) -> None:
        # The FULL chain restores the 50 grid → (377.10 / 50) rounds to +8, and the
        # row is high-confidence. Fails before the fix (no chain consumption).
        self._seed_sparse_grid_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            registry_chain_for=lambda u, e: self._chains().get((u, e)),
        )
        ce = next(m for m in cands.movers if m.symbol == _CE)
        assert ce.moneyness_label == "OTM +8"
        assert ce.strike_step_confidence is not None
        assert ce.strike_step_confidence.value == "registry"

    def test_underlying_absent_from_chains_degrades_to_observed(self, tmp_path: Path) -> None:
        # A registry↔lake naming mismatch (or a never-enumerated underlying) must
        # never break the screen — the observed ladder + low-confidence label win.
        self._seed_sparse_grid_lake(tmp_path)
        cands = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            registry_chain_for=lambda u, e: {
                ("OTHER", date(2024, 6, 27)): (Decimal("1"), Decimal("2"))
            }.get((u, e)),
        )
        ce = next(m for m in cands.movers if m.symbol == _CE)
        assert ce.moneyness_label == "OTM +4"
        assert ce.strike_step_confidence is not None
        assert ce.strike_step_confidence.value == "observed"

    def test_run_snapshot_threads_the_chains(self, tmp_path: Path) -> None:
        payload = run_options_snapshot(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            registry_chain_for=lambda u, e: self._chains().get((u, e)),
        )
        # An options-less lake still degrades honestly with the chains wired.
        assert payload["instrument_count"] == 0

    def test_route_serves_the_registry_confidence(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # End-to-end: the registry sits beside the instrument store
        # (registry_path_for) and its chain reaches the movers rows.
        from brutex.data.registry_store import RegistryStore

        self._seed_sparse_grid_lake(tmp_path)
        now = datetime(2024, 6, 1, tzinfo=UTC)
        with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
            for strike in (24000.0, 24050.0, 24100.0):
                reg.upsert_contract(
                    "NSE",
                    "NIFTY",
                    date(2024, 6, 27),
                    f"NSE-NIFTY-27Jun24-{int(strike)}-CE",
                    side="CE",
                    strike=strike,
                    run_id="r1",
                    fetched_at=now,
                )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        ce_row = next(r for r in body["highest_oi"] if r["symbol"] == _CE)
        assert ce_row["moneyness"] == "OTM +8"
        assert ce_row["strike_step_confidence"] == "registry"

    def test_route_without_registry_is_honestly_observed(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        self._seed_sparse_grid_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        ce_row = next(r for r in body["highest_oi"] if r["symbol"] == _CE)
        assert ce_row["moneyness"] == "OTM +4"
        assert ce_row["strike_step_confidence"] == "observed"

    def test_route_memoises_the_chains_per_generation(
        self,
        store: InstrumentStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two polls within ONE write-generation share a single per-key registry chain
        # read (the dedicated generation-stamped MoversSnapshotCache in build_app,
        # keyed per (exchange, underlying, expiry) — the LAZY bounded loader).
        import brutex.dashboard.registry_strikes as rs_module

        self._seed_sparse_grid_lake(tmp_path)
        calls: list[tuple[str, str]] = []
        real_loader = rs_module.load_registry_strike_chain

        def _counting_loader(*args: object, **kwargs: object) -> object:
            calls.append((str(kwargs["underlying"]), str(kwargs["expiry"])))
            return real_loader(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(rs_module, "load_registry_strike_chain", _counting_loader)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        params = {"asset": "options", "interval": "1day", "date": "2024-06-13"}
        first = client.get("/api/v1/movers", params=params).json()
        second = client.get("/api/v1/movers", params=params).json()
        assert first["instrument_count"] == second["instrument_count"] == 3
        assert calls == [("NIFTY", "2024-06-27")]  # ONE narrowed read; 2nd poll O(1) hit
        # A contract-LESS exchange coordinate has no tier-target keys → ZERO extra
        # reads (the bounded-read win: no whole-registry load per exchange key).
        client.get("/api/v1/movers", params={**params, "exchange": "BSE"}).json()
        assert len(calls) == 1
        # An exchange coordinate WITH contracts keys separately → exactly one more.
        client.get("/api/v1/movers", params={**params, "exchange": "NSE"}).json()
        assert calls == [("NIFTY", "2024-06-27")] * 2

    def test_route_reads_only_tier_target_chains_never_whole_registry(
        self,
        store: InstrumentStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # BOUNDED-READ pin (Golden Rule #5, DEC-FNO-HONESTY-PACK-001): the registry
        # holds EXTRA chains (another underlying + a far historical expiry) that the
        # snapshot's tier does NOT target — the lazy loader must request ONLY the
        # tier-target (underlying, expiry) key, never materialise the whole table.
        import brutex.dashboard.registry_strikes as rs_module
        from brutex.data.registry_store import RegistryStore

        self._seed_sparse_grid_lake(tmp_path)
        now = datetime(2024, 6, 1, tzinfo=UTC)
        with RegistryStore.open(tmp_path / "registry.duckdb") as reg:
            for strike in (24000.0, 24050.0, 24100.0):
                reg.upsert_contract(
                    "NSE",
                    "NIFTY",
                    date(2024, 6, 27),
                    f"NSE-NIFTY-27Jun24-{int(strike)}-CE",
                    side="CE",
                    strike=strike,
                    run_id="r1",
                    fetched_at=now,
                )
            # EXTRA chains the tier never targets — must never be read.
            reg.upsert_contract(
                "NSE",
                "BANKNIFTY",
                date(2024, 6, 26),
                "NSE-BANKNIFTY-26Jun24-52000-CE",
                side="CE",
                strike=52000.0,
                run_id="r1",
                fetched_at=now,
            )
            reg.upsert_contract(
                "NSE",
                "NIFTY",
                date(2023, 1, 25),
                "NSE-NIFTY-25Jan23-18000-CE",
                side="CE",
                strike=18000.0,
                run_id="r1",
                fetched_at=now,
            )
        requested: list[tuple[str, str]] = []
        real_loader = rs_module.load_registry_strike_chain

        def _spy(*args: object, **kwargs: object) -> object:
            requested.append((str(kwargs["underlying"]), str(kwargs["expiry"])))
            return real_loader(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(rs_module, "load_registry_strike_chain", _spy)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 5},
        ).json()
        ce_row = next(r for r in body["highest_oi"] if r["symbol"] == _CE)
        assert ce_row["strike_step_confidence"] == "registry"  # the narrowed read worked
        assert requested == [("NIFTY", "2024-06-27")]  # ONLY the tier-target key

    def test_movers_template_renders_the_low_confidence_marker(self) -> None:
        # The frontend honesty marker (a trailing " ~" keyed off the row field) is
        # present in the served template — display-only, string-pinned.
        import brutex.dashboard as dashboard_pkg

        template = (Path(dashboard_pkg.__file__).parent / "templates" / "movers.html").read_text(
            encoding="utf-8"
        )
        assert 'strike_step_confidence === "observed"' in template


# ── the per-underlying PCR panel (DEC-MOVERS-OPTIONS-PCR-001) ──────────────


def _seed_pcr_lake(tmp_path: Path) -> None:
    """One NIFTY CE + PE pair with distinct multi-minute OI/volume trajectories.

    CE: 09:16 (vol 100, oi 1000) → 13:45 (vol 50, oi 1100) → 14:00 (vol 25, oi 1200).
    PE: 09:16 (vol 200, oi 900) → 13:45 (vol 60, oi 990) → 14:00 (vol 30, oi 1080).
    As-of 13:45 (INCLUSIVE): CE vol 150 / oi 1100 · PE vol 260 / oi 990.
    Whole day: CE vol 175 / oi 1200 · PE vol 290 / oi 1080.
    """
    _write(
        tmp_path,
        segment="FNO",
        symbol=_CE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=16, close="100", volume=100, oi=1000),
            _min_bar(hour=13, minute=45, close="110", volume=50, oi=1100),
            _min_bar(hour=14, minute=0, close="120", volume=25, oi=1200),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_PE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=16, close="80", volume=200, oi=900),
            _min_bar(hour=13, minute=45, close="75", volume=60, oi=990),
            _min_bar(hour=14, minute=0, close="70", volume=30, oi=1080),
        ],
    )


class TestOptionsPcr:
    """End-to-end PCR over a REAL Parquet lake through the real route."""

    def _get(self, store: InstrumentStore, params: dict[str, str]) -> dict[str, Any]:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        response = client.get("/api/v1/movers", params={"asset": "options", **params})
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    def test_intraday_as_of_cutoff_hand_summed(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The INCLUSIVE 13:45 as-of bar counts (edge 21's intraday twin): OI is
        # each leg's LAST open_interest ≤ cutoff, volume the session sum ≤ cutoff
        # — hand-summed independently and Fraction-cross-checked.
        _seed_pcr_lake(tmp_path)
        body = self._get(
            store,
            {"interval": "1minute", "date": "2024-06-13", "time": "13:45"},
        )
        assert len(body["pcr"]) == 1
        row = body["pcr"][0]
        assert row["underlying"] == "NIFTY"
        assert row["display_underlying"] == "NIFTY"
        assert row["expiry"] == "2024-06-27"
        assert (row["put_oi"], row["call_oi"]) == (990, 1100)
        assert (row["put_volume"], row["call_volume"]) == (260, 150)
        # Independent recompute (fractions — never the production Decimal path).
        assert Fraction(row["oi_pcr"]) == Fraction(990, 1100)
        assert abs(Fraction(row["vol_pcr"]) - Fraction(260, 150)) < Fraction(1, 10**26)
        assert row["oi_pcr"] == "0.9"
        assert (row["oi_status"], row["vol_status"]) == ("ok", "ok")
        assert (row["legs_read"], row["legs_total"]) == (2, 2)
        assert row["coverage"] == "full"
        assert row["coverage_note"] is None
        # Volume is non-null by the session reader's contract — pinned 0 on the
        # wire (the field exists for the primitive's other callers).
        assert row["volume_missing_legs"] == 0

    def test_daily_interval_folds_the_whole_day(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Daily / blank time ⇒ cutoff None ⇒ whole-day semantics — identical to
        # the movers rows (edge 21): last OI of day + whole-day volume.
        _seed_pcr_lake(tmp_path)
        body = self._get(store, {"interval": "1day", "date": "2024-06-13"})
        row = body["pcr"][0]
        assert (row["put_oi"], row["call_oi"]) == (1080, 1200)
        assert (row["put_volume"], row["call_volume"]) == (290, 175)
        assert row["oi_pcr"] == "0.9"

    def test_futures_leg_never_pollutes_the_sums(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A FUT partition on the SAME expiry with a huge OI must not move ANY
        # PCR number (edge 10 — parse_option_symbol drops -FUT at discovery).
        _seed_pcr_lake(tmp_path)
        baseline = self._get(store, {"interval": "1day", "date": "2024-06-13"})["pcr"]
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="23650", volume=10**6, oi=10**9)],
        )
        with_fut = self._get(store, {"interval": "1day", "date": "2024-06-13"})["pcr"]
        assert with_fut == baseline

    def test_corrupt_leg_reads_partial_with_honest_note(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # One corrupt PE Parquet → the row is PRESENT with n = m − 1, coverage
        # "partial", a non-null server-composed note, skipped bumped, no 500.
        _seed_pcr_lake(tmp_path)
        corrupt = list((tmp_path / "bars" / _EXCHANGE / "FNO" / _PE / "1minute").rglob("*.parquet"))
        assert corrupt
        for path in corrupt:
            path.write_bytes(b"not a parquet file \r\n garbage")
        body = self._get(store, {"interval": "1day", "date": "2024-06-13"})
        assert body["skipped_count"] == 1
        row = body["pcr"][0]
        assert (row["legs_read"], row["legs_total"]) == (1, 2)
        assert row["coverage"] == "partial"
        assert row["coverage_note"] == "1/2 legs read"
        # Only the CE leg summed; the put side honestly reads 0 with a sentinel.
        assert (row["put_oi"], row["call_oi"]) == (0, 1200)
        assert row["oi_pcr"] == "0"

    def test_null_oi_leg_is_excluded_and_counted_volume_still_real(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A legacy null-OI PE cell (edge 4 end-to-end): excluded from the OI
        # sums + counted, while its volume STILL feeds the volume PCR.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="100", volume=100, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_PE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="80", volume=50, oi=None)],
        )
        row = self._get(store, {"interval": "1day", "date": "2024-06-13"})["pcr"][0]
        assert row["oi_missing_legs"] == 1
        # The put OI cell was missing → the put sum is honestly 0 over a REAL
        # 1000 call denominator: the ratio computes to exactly 0 (status ok) —
        # a missing numerator cell is exclusion, not a zero-denominator case.
        assert row["oi_pcr"] == "0"
        assert row["oi_status"] == "ok"
        assert (row["put_oi"], row["call_oi"]) == (0, 1000)
        assert Fraction(row["vol_pcr"]) == Fraction(50, 100)
        assert row["coverage"] == "partial"
        assert row["coverage_note"] == "1 leg(s) missing OI"

    def test_all_call_chain_reads_an_exact_zero_ratio(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # All-CE chain: the CALL denominator is REAL (1000), the put numerator 0
        # → both ratios are an exact "0" with status "ok" (the fold law clause
        # 4 — only a zero DENOMINATOR yields the sentinel; the design sketch's
        # "both null" wording for all-CE contradicted its own §1.1 law and the
        # law wins). The raw sums keep "zero puts, real calls" visible.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="100", volume=100, oi=1000)],
        )
        row = self._get(store, {"interval": "1day", "date": "2024-06-13"})["pcr"][0]
        assert (row["oi_pcr"], row["vol_pcr"]) == ("0", "0")
        assert (row["oi_status"], row["vol_status"]) == ("ok", "ok")
        assert (row["put_oi"], row["call_oi"]) == (0, 1000)
        assert (row["put_volume"], row["call_volume"]) == (0, 100)

    def test_negative_oi_lake_corruption_is_an_honest_500_not_a_400(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Round-1 hostile-review fold: the fold's fail-loud negative-metric trip
        # (design edge 5) must surface as a SERVER-side 500 with a
        # corruption-specific detail — never ride the route's malformed-input
        # 400 mapping. Reachable ONLY out-of-band (the ingest boundary clamps
        # `max(0, …)` and CandleBar rejects negatives), so the corruption is
        # seeded by rewriting the raw Parquet cell directly.
        import polars as pl

        _seed_pcr_lake(tmp_path)
        files = list((tmp_path / "bars" / _EXCHANGE / "FNO" / _PE / "1minute").rglob("*.parquet"))
        assert files
        for path in files:
            frame = pl.read_parquet(path)
            frame.with_columns(pl.col("open_interest") * -1).write_parquet(path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        response = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13"},
        )
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail.startswith("lake corruption:")
        assert "negative metric" in detail

    def test_all_put_chain_reads_real_put_sums(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_PE,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=16, close="80", volume=50, oi=700)],
        )
        row = self._get(store, {"interval": "1day", "date": "2024-06-13"})["pcr"][0]
        assert row["oi_pcr"] is None  # 700/0 → sentinel, puts visible in the sums
        assert (row["put_oi"], row["call_oi"]) == (700, 0)
        assert row["oi_status"] == "zero_denominator"
        # Design §9 test 23 pins BOTH ratios null for the all-PE chain: the
        # volume side hits the SAME zero call-side denominator (50/0), so the
        # wire carries the sentinel on both variants (round-4 review fold).
        assert row["vol_pcr"] is None
        assert row["vol_status"] == "zero_denominator"
        assert (row["put_volume"], row["call_volume"]) == (50, 0)

    def test_pre_open_as_of_emits_honest_empty_rows(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A 09:00 as-of (pre-open) leaves every leg bar-less (edges 8-9): the
        # chain is still REPORTED — one row, legs_read 0, coverage "empty".
        _seed_pcr_lake(tmp_path)
        body = self._get(store, {"interval": "1minute", "date": "2024-06-13", "time": "09:00"})
        assert len(body["pcr"]) == 1
        row = body["pcr"][0]
        assert (row["legs_read"], row["legs_total"]) == (0, 2)
        assert row["coverage"] == "empty"
        assert row["coverage_note"] == "0/2 legs read"
        assert row["oi_pcr"] is None
        assert row["vol_pcr"] is None

    def test_empty_lake_and_no_lake_root_serve_empty_pcr(self, store: InstrumentStore) -> None:
        # Edge 15/16: the honest degrades all carry "pcr": [] — never a 500.
        body = self._get(store, {"interval": "1day"})
        assert body["pcr"] == []
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        no_root = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert no_root["pcr"] == []

    def test_unknown_interval_serves_empty_pcr(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_pcr_lake(tmp_path)
        candidates = compute_options_candidates(
            lake_root=tmp_path, interval="99weird", date="2024-06-13", time=None
        )
        assert candidates.pcr == ()

    def test_gated_payload_carries_no_pcr_key(self, store: InstrumentStore) -> None:
        # Edge 17: the gated shell is byte-unchanged — zero lake reads, no key.
        from brutex.dashboard.prebuild_gate import PrebuildGate

        gate = PrebuildGate()
        client = TestClient(build_app(DashboardState(), instrument_store=store, prebuild_gate=gate))
        gate.begin()
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert body["gated"] is True
        assert "pcr" not in body

    def test_repeat_poll_is_a_cache_hit_and_a_pull_recomputes(
        self,
        store: InstrumentStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Edge 19 / Golden Rule #5: PCR rides INSIDE the cached OptionsCandidates
        # — a repeat poll at one generation computes ONCE (O(1) LRU handoff, the
        # warm-parity guarantee), and a pull (mark_dirty) recomputes.
        import brutex.dashboard.app as app_module
        from brutex.dashboard.summary_cache import SummaryCache

        _seed_pcr_lake(tmp_path)
        calls = {"n": 0}
        real_compute = app_module.compute_options_candidates

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real_compute(*args, **kwargs)

        monkeypatch.setattr(app_module, "compute_options_candidates", _counting)
        summary_cache = SummaryCache()
        client = TestClient(
            build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
        )
        params = {"asset": "options", "interval": "1day", "date": "2024-06-13"}
        first = client.get("/api/v1/movers", params=params).json()
        second = client.get("/api/v1/movers", params=params).json()
        assert calls["n"] == 1  # the second poll is a pure cache hit
        assert first["pcr"] == second["pcr"] != []
        summary_cache.mark_dirty()  # a pull landed → generation bump
        third = client.get("/api/v1/movers", params=params).json()
        assert calls["n"] == 2  # recomputed, never stale
        assert third["pcr"] == first["pcr"]

    def test_warm_grid_coordinate_poll_is_a_cache_hit_carrying_pcr(
        self,
        store: InstrumentStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Design test #28 (warm parity, round-1 hostile-review fold): prime the
        # INJECTED movers cache exactly as the app's `_make_opt_job` warm job
        # does — an `options_warm_grid()` coordinate keyed by `candidate_key` +
        # `compute_options_candidates` at `latest_movers_date` and the live
        # generation — then poll the route at that coordinate: a PURE cache hit
        # (compute never re-invoked) whose payload carries the warmed PCR rows.
        import brutex.dashboard.app as app_module
        from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key
        from brutex.dashboard.movers_snapshot import DEFAULT_INTERVAL
        from brutex.dashboard.movers_warm import latest_movers_date, options_warm_grid
        from brutex.dashboard.summary_cache import SummaryCache

        _seed_pcr_lake(tmp_path)
        # A spot 1day partition so latest_movers_date resolves the seeded day —
        # the SAME date the default (no-date) route poll resolves.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(day=_DAY, close="23600", oi=None)],
        )
        warm_date = latest_movers_date(tmp_path)
        assert warm_date is not None and warm_date == _DAY.isoformat()
        # The default-poll coordinate MUST be on the warm grid (all-exchange ×
        # near × all) — the parity this test pins.
        exchange: str | None = None
        tier, track = "near", "all"
        assert {"exchange": exchange, "expiry_tier": tier, "expiry_track": track} in (
            options_warm_grid()
        )
        movers_cache = MoversSnapshotCache()
        summary_cache = SummaryCache()
        warm_date_str: str = warm_date
        movers_cache.get_or_compute(
            candidate_key(
                asset="options",
                exchange=exchange,
                interval=DEFAULT_INTERVAL,
                date=warm_date_str,
                time=None,
                expiry_tier=tier,
                expiry_track=track,
            ),
            generation=summary_cache.generation,
            compute=lambda: compute_options_candidates(
                lake_root=tmp_path,
                interval=DEFAULT_INTERVAL,
                date=warm_date_str,
                time=None,
                expiry_tier=tier,
                expiry_track=track,
                exchange=exchange,
            ),
        )

        def _never(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a warmed coordinate poll must be a pure cache hit")

        monkeypatch.setattr(app_module, "compute_options_candidates", _never)
        client = TestClient(
            build_app(
                DashboardState(),
                instrument_store=store,
                movers_cache=movers_cache,
                summary_cache=summary_cache,
            )
        )
        body = client.get("/api/v1/movers", params={"asset": "options"}).json()
        assert len(body["pcr"]) == 1
        assert body["pcr"][0]["underlying"] == "NIFTY"
        assert body["pcr"][0]["expiry"] == "2024-06-27"

    def test_rename_carries_parsed_key_and_display_label(self, tmp_path: Path) -> None:
        # Edge 14 end-to-end: a display rename never re-keys the PCR group — the
        # row carries the PARSED underlying + the renamed display label, and the
        # movers rows show the SAME renamed label (panel/table agreement).
        _seed_pcr_lake(tmp_path)
        candidates = compute_options_candidates(
            lake_root=tmp_path,
            interval="1day",
            date="2024-06-13",
            time=None,
            names={"NIFTY": "NIFTY 50"},
        )
        assert len(candidates.pcr) == 1
        row = candidates.pcr[0]
        assert row.underlying == "NIFTY"
        assert row.display_underlying == "NIFTY 50"
        assert all(m.underlying == "NIFTY 50" for m in candidates.movers)

    def test_run_options_snapshot_threads_pcr_to_the_payload(self, tmp_path: Path) -> None:
        # Compose parity: the thin run_options_snapshot compose carries the SAME
        # rows the candidate scan produced.
        _seed_pcr_lake(tmp_path)
        payload = run_options_snapshot(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, top_n=5
        )
        assert len(payload["pcr"]) == 1
        assert payload["pcr"][0]["underlying"] == "NIFTY"

    def test_pcr_reuses_the_scan_reads_zero_extra_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Zero-new-I/O pin across ALL FOUR per-contract reader seams (round-1
        # hostile-review widen — a single-reader spy would miss a PCR edit that
        # added a read through _prev_day_close / _prev_day_oi /
        # _underlying_spot_ltp): the candidate scan performs EXACTLY one read
        # per tier contract through EACH reader — the PCR fold adds none.
        import brutex.dashboard.movers_options as mo_module

        from tests.fakes.movers_spies import spy_all_movers_readers

        _seed_pcr_lake(tmp_path)
        counts = spy_all_movers_readers(mo_module, monkeypatch)
        candidates = compute_options_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None
        )
        # One read per contract (CE + PE) per reader — none for PCR.
        assert counts == {
            "_read_future_session": 2,
            "_prev_day_close": 2,
            "_prev_day_oi": 2,
            "_underlying_spot_ltp": 2,
        }
        assert len(candidates.pcr) == 1
