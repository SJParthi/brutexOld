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
from pathlib import Path

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

    def test_default_intraday_echoes_latest_time_from_spot(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-MOVERS-ASOF-LATEST-DEFAULT-001: the additive `latest_time` echo rides the
        # Options payload too, resolved from the SPOT 1-minute (INDEX NIFTY → 09:16);
        # `time` stays null (whole-day cut, identical latest close across every TF).
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "3minute", "date": "2024-06-13"},
        ).json()
        assert body["latest_time"] == "09:16"
        assert body["time"] is None

    def test_daily_options_latest_time_is_none(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_options_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": "2024-06-13"},
        ).json()
        assert body["latest_time"] is None

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


# ── prev-close/OI calendar-contiguity goldens (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) ──

from brutex.data.moneyness import Moneyness  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

#: The FROZEN additive-key vocabulary this decision may ever add to a row.
_CONTIGUITY_KEYS = frozenset(
    {
        "prev_close_session",
        "prev_close_contiguous",
        "oi_baseline_session",
        "oi_baseline_contiguous",
    }
)

#: The pre-fix options row key set (the byte-identity vocabulary).
_OPTION_ROW_KEYS = frozenset(
    {
        "name",
        "symbol",
        "exchange",
        "segment",
        "spot",
        "ltp",
        "pct_change",
        "oi",
        "oi_chg",
        "oi_chg_pct",
        "volume",
        "moneyness",
        "moneyness_bucket",
        "atm_offset",
        "strike_step_confidence",
        "iv",
        "delta",
    }
)

_G_DAY = date(2025, 9, 29)  # Mon — the repro snapshot day
_G_CE = "NSE-NIFTY-30Sep25-24000-CE"  # expiry 2025-09-30 ≥ snapshot → near tier


def _seed_contiguity_option(tmp_path: Path, *, prior_day: date) -> None:
    """One CE contract: session bars on Mon 2025-09-29 + ONE prior 1-day bar on
    *prior_day* (close 100, OI 1000). NO underlying spot (an honest None spot —
    keeps the golden fully deterministic)."""
    _write(
        tmp_path,
        segment="FNO",
        symbol=_G_CE,
        interval="1minute",
        bars=[
            _min_bar(hour=9, minute=15, close="150", volume=100, oi=1050, day=_G_DAY),
            _min_bar(hour=9, minute=16, close="164", volume=400, oi=1100, day=_G_DAY),
        ],
    )
    _write(
        tmp_path,
        segment="FNO",
        symbol=_G_CE,
        interval="1day",
        bars=[_day_bar(day=prior_day, close="100", oi=1000)],
    )


class TestOptionsContiguityGoldens:
    def test_contiguous_row_is_byte_identical_no_warn(self, tmp_path: Path) -> None:
        # Prior 1-day on Fri 2025-09-26 = the immediately-preceding session of
        # Mon 2025-09-29 (weekend spanned) → NO additive key, no WARN.
        _seed_contiguity_option(tmp_path, prior_day=date(2025, 9, 26))
        with capture_logs() as logs:
            payload = run_options_snapshot(
                lake_root=tmp_path, interval="1day", date=_G_DAY.isoformat(), time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # FULL-row byte identity: the exact pre-fix wire, computed independently
        # (the moneyness triple is the honest no-spot NA — DEC-FNO-MONEYNESS-001).
        assert row == {
            "name": "NIFTY 30-Sep-2025 24000 CE",
            "symbol": _G_CE,
            "exchange": "NSE",
            "segment": "FNO",
            "spot": None,
            "ltp": "₹164.00",
            "pct_change": str((Decimal("164.00") - Decimal("100.00")) / Decimal("100.00") * 100),
            "oi": 1100,
            "oi_chg": 100,
            "oi_chg_pct": str((Decimal(1100) - Decimal(1000)) / Decimal(1000) * 100),
            "volume": 500,
            "moneyness": "?",
            "moneyness_bucket": Moneyness.UNKNOWN.value,
            "atm_offset": None,
            "strike_step_confidence": None,
            "iv": None,
            "delta": None,
        }
        assert set(row) == _OPTION_ROW_KEYS
        assert set(row) & _CONTIGUITY_KEYS == set()
        assert [e for e in logs if e["event"].startswith("movers.prev_baseline")] == []

    def test_noncontiguous_close_and_oi_both_stamped_two_warns(self, tmp_path: Path) -> None:
        # The lake-hole arm: the ONLY prior 1-day bar sits on Thu 2025-09-25
        # while Fri 2025-09-26 is a hole — BOTH baselines flagged, one WARN per
        # kind, served values UNCHANGED vs the contiguous golden.
        _seed_contiguity_option(tmp_path, prior_day=date(2025, 9, 25))
        with capture_logs() as logs:
            payload = run_options_snapshot(
                lake_root=tmp_path, interval="1day", date=_G_DAY.isoformat(), time=None, top_n=5
            )
        (row,) = payload["price_gainers"]
        # VALUE-EQUALITY: identical served numbers — the fix stamps, never edits.
        assert row["ltp"] == "₹164.00"
        assert row["pct_change"] == str(
            (Decimal("164.00") - Decimal("100.00")) / Decimal("100.00") * 100
        )
        assert row["oi_chg"] == 100
        # The four additive keys, both baselines independently False.
        assert row["prev_close_session"] == "2025-09-25"
        assert row["prev_close_contiguous"] is False
        assert row["oi_baseline_session"] == "2025-09-25"
        assert row["oi_baseline_contiguous"] is False
        assert set(row) - _OPTION_ROW_KEYS == _CONTIGUITY_KEYS
        warns = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert sorted((e["kind"], e["symbol"]) for e in warns) == [
            ("oi", _G_CE),
            ("prev_close", _G_CE),
        ]
