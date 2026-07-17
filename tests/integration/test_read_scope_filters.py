"""DEC-UNIVERSE-INDEX-ONLY-001 stage-2a read-surface scope filters (design §3 R1/R3/R4).

The §6 battery for the LAKE-WALK discovery family:

* **R1 movers** — planted stock partitions are excluded from spot/futures/
  options candidate discovery under the shipped ``index4`` default (counted,
  the emit-on-nonzero note names the DEC), served again under ``full`` (the
  widen path), byte-identical on an index-only lake, and a DRIFTED index dir
  (``NSE-NIFTY``) is recovered-then-admitted (the Rev-2 ordering pin). The
  candidate cache keys fold ``scope_revision()`` — the §6 primed-cache widen
  arm proves a REGISTRATION widening re-keys a primed route cache with NO
  generation bump.
* **R3 preopen** — the report dataset resolver screens scoped-out symbols
  (route-threaded; the ``scope=None`` direct call stays byte-identical).
* **R4 greeks** — stored stock contracts are gated out of enrich/backfill
  target discovery (counted ``scope_excluded``; stamps stay INERT).

Fails-before law: every ``scope=None`` arm below IS the merge-base behavior
(the §2.4 read-default seam), so each index4 arm asserting exclusion fails
on the pre-#1934 tree by construction.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.config.feed_scope import (
    FEED_SCOPE_ENV,
    note_feed_registered,
    resolve_read_scope,
    scope_revision,
)
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_futures import compute_futures_candidates
from brutex.dashboard.movers_options import compute_options_candidates
from brutex.dashboard.movers_snapshot import build_snapshot_payload, compute_spot_candidates
from brutex.dashboard.preopen_report import resolve_report_datasets
from brutex.data.greeks_backfill import scan_stale_greeks_targets
from brutex.data.greeks_enrich_pass import discover_option_windows, scoped_option_windows
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.scope_filter import scope_excluded_echo, scope_note_suffix
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.options.risk_free import FixedRateSource
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)
_SCOPE_NOTE = "stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."


def _day_bar(*, close: str, volume: int, day: date = _DAY) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _min_bar(*, close: str, volume: int, oi: int | None = None) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _write(
    tmp_path: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    bars: list[CandleBar],
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_spot(tmp_path: Path) -> None:
    """NIFTY (admitted) + RELIANCE (stock — scope-excluded under index4)."""
    _write(
        tmp_path,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(close="22000", volume=500)],
    )
    _write(
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1day",
        bars=[_day_bar(close="2900", volume=800)],
    )


def _seed_fno(tmp_path: Path) -> None:
    """One admitted NIFTY FUT/CE + one stock RELIANCE FUT/CE (1-minute)."""
    for symbol, close, oi in (
        ("NSE-NIFTY-27Jun24-FUT", "22100", 900),
        ("NSE-RELIANCE-27Jun24-FUT", "2950", 700),
    ):
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=[_min_bar(close=close, volume=100, oi=oi)],
        )
    for symbol, close in (
        ("NSE-NIFTY-27Jun24-22000-CE", "150"),
        ("NSE-RELIANCE-27Jun24-2900-CE", "55"),
    ):
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=[_min_bar(close=close, volume=60, oi=500)],
        )


def _inst(symbol: str, *, kind: InstrumentType) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
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
        [_inst("NIFTY", kind=InstrumentType.IDX), _inst("RELIANCE", kind=InstrumentType.EQ)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _client(store: InstrumentStore) -> Any:
    return build_app(DashboardState(), instrument_store=store)


_SPOT_PARAMS = {"asset": "spot", "interval": "1day", "date": _DAY.isoformat(), "top_n": 5}


# ── the COMMON note/echo helpers (design §3 R1 wording law) ───────────────


class TestScopeNoteHelpers:
    def test_note_suffix_zero_is_empty(self) -> None:
        assert scope_note_suffix(0) == ""
        assert scope_note_suffix(-1) == ""

    def test_note_suffix_wording_names_count_and_dec(self) -> None:
        assert scope_note_suffix(3) == (
            " 3 stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."
        )

    def test_echo_is_emit_on_nonzero(self) -> None:
        assert scope_excluded_echo(0) == {}
        assert scope_excluded_echo(2) == {"scope_excluded": 2}


# ── R1: compute-level discovery filters ───────────────────────────────────


class TestMoversComputeScope:
    def test_spot_stock_excluded_and_counted_under_index4(self, tmp_path: Path) -> None:
        _seed_spot(tmp_path)
        scoped = compute_spot_candidates(
            lake_root=tmp_path,
            interval="1day",
            date=_DAY.isoformat(),
            time=None,
            scope=resolve_read_scope(),
        )
        assert [s.instrument.symbol for s in scoped.snapshots] == ["NIFTY"]
        assert scoped.scope_excluded == 1

    def test_spot_scope_none_serves_stock_unchanged(self, tmp_path: Path) -> None:
        # The merge-base arm (§2.4 default law) — the fails-before oracle.
        _seed_spot(tmp_path)
        legacy = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time=None
        )
        assert sorted(s.instrument.symbol for s in legacy.snapshots) == ["NIFTY", "RELIANCE"]
        assert legacy.scope_excluded == 0

    def test_spot_full_scope_serves_stock_with_zero_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The widen path: an unbounded scope is pass-through (count stays 0).
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        _seed_spot(tmp_path)
        wide = compute_spot_candidates(
            lake_root=tmp_path,
            interval="1day",
            date=_DAY.isoformat(),
            time=None,
            scope=resolve_read_scope(),
        )
        assert sorted(s.instrument.symbol for s in wide.snapshots) == ["NIFTY", "RELIANCE"]
        assert wide.scope_excluded == 0

    def test_spot_index_only_lake_is_byte_identical(self, tmp_path: Path) -> None:
        # Emit-on-nonzero law: zero exclusions ⇒ the scoped candidate set
        # equals the unscoped one field-for-field.
        _write(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(close="22000", volume=500)],
        )
        kwargs: dict[str, Any] = {
            "lake_root": tmp_path,
            "interval": "1day",
            "date": _DAY.isoformat(),
            "time": None,
        }
        assert compute_spot_candidates(**kwargs, scope=resolve_read_scope()) == (
            compute_spot_candidates(**kwargs)
        )

    def test_spot_drifted_index_dir_recovered_then_admitted(self, tmp_path: Path) -> None:
        # The Rev-2 ordering pin: a legacy drifted "NSE-NIFTY" INDEX dir is
        # recovered THEN admitted — never silently dropped by raw-key matching.
        _write(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1day",
            bars=[_day_bar(close="22000", volume=500)],
        )
        scoped = compute_spot_candidates(
            lake_root=tmp_path,
            interval="1day",
            date=_DAY.isoformat(),
            time=None,
            scope=resolve_read_scope(),
        )
        assert [s.instrument.symbol for s in scoped.snapshots] == ["NSE-NIFTY"]
        assert scoped.scope_excluded == 0

    def test_futures_stock_contract_excluded_and_counted(self, tmp_path: Path) -> None:
        _seed_fno(tmp_path)
        scoped = compute_futures_candidates(
            lake_root=tmp_path,
            interval="1minute",
            date=_DAY.isoformat(),
            time=None,
            scope=resolve_read_scope(),
        )
        assert [m.underlying for m in scoped.movers] == ["NIFTY"]
        assert scoped.scope_excluded == 1
        legacy = compute_futures_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time=None
        )
        assert sorted(m.underlying for m in legacy.movers) == ["NIFTY", "RELIANCE"]
        assert legacy.scope_excluded == 0

    def test_options_stock_contract_excluded_and_counted(self, tmp_path: Path) -> None:
        _seed_fno(tmp_path)
        scoped = compute_options_candidates(
            lake_root=tmp_path,
            interval="1minute",
            date=_DAY.isoformat(),
            time=None,
            scope=resolve_read_scope(),
        )
        assert {m.underlying for m in scoped.movers} == {"NIFTY"}
        assert scoped.scope_excluded == 1
        legacy = compute_options_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time=None
        )
        assert {m.underlying for m in legacy.movers} == {"NIFTY", "RELIANCE"}
        assert legacy.scope_excluded == 0

    def test_payload_note_and_echo_are_emit_on_nonzero(self) -> None:
        zero = build_snapshot_payload(
            (), interval="1day", date=_DAY.isoformat(), time=None, top_n=5, skipped=0
        )
        assert "scope_excluded" not in zero
        assert "excluded by scope" not in zero["note"]
        nonzero = build_snapshot_payload(
            (),
            interval="1day",
            date=_DAY.isoformat(),
            time=None,
            top_n=5,
            skipped=0,
            scope_excluded=2,
        )
        assert nonzero["scope_excluded"] == 2
        assert f" 2 {_SCOPE_NOTE}" in nonzero["note"]


# ── R1: the route (Law-1 shipped-default serve arm + widen + key fold) ────


class TestMoversRouteScope:
    def test_index4_default_excludes_stock_with_note(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        body = client.get("/api/v1/movers", params=_SPOT_PARAMS).json()
        assert {row["symbol"] for row in body["volume"]} == {"NIFTY"}
        assert body["scope_excluded"] == 1
        assert f" 1 {_SCOPE_NOTE}" in body["note"]

    def test_full_env_serves_stock_again(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The widen path (§6#6): BRUTEX_FEED_SCOPE=full re-serves the stock rows.
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        body = client.get("/api/v1/movers", params=_SPOT_PARAMS).json()
        assert {row["symbol"] for row in body["volume"]} == {"NIFTY", "RELIANCE"}
        assert "scope_excluded" not in body
        assert "excluded by scope" not in body["note"]

    def test_registration_widening_rekeys_a_primed_cache(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The §6 primed-cache widen arm (design §3 R1 Rev 4): a registration
        # widening changes the read union with NO env flip and NO generation
        # bump — the scope_revision() key fold must re-key the primed entry,
        # never serve the pre-widening narrow payload + exclusion note.
        monkeypatch.setenv(FEED_SCOPE_ENV, "groww=index4,dhan=full")
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        narrow = client.get("/api/v1/movers", params=_SPOT_PARAMS).json()
        assert {row["symbol"] for row in narrow["volume"]} == {"NIFTY"}
        assert narrow["scope_excluded"] == 1
        before = scope_revision()
        note_feed_registered("dhan")  # the second feed ACTIVATES with a wide scope
        assert scope_revision() != before
        wide = client.get("/api/v1/movers", params=_SPOT_PARAMS).json()
        assert {row["symbol"] for row in wide["volume"]} == {"NIFTY", "RELIANCE"}
        assert "scope_excluded" not in wide


# ── R3: preopen symbol discovery ──────────────────────────────────────────


class TestPreopenScope:
    def test_resolver_screens_stock_under_index4(self, tmp_path: Path) -> None:
        _seed_spot(tmp_path)
        scoped = resolve_report_datasets(tmp_path, basis="day", scope=resolve_read_scope())
        assert scoped == [("NSE", "INDEX", "NIFTY")]
        # The §2.4 default arm (merge-base behavior — the fails-before oracle).
        legacy = resolve_report_datasets(tmp_path, basis="day")
        assert legacy == [("NSE", "INDEX", "NIFTY"), ("NSE", "CASH", "RELIANCE")]

    def test_resolver_index_only_lake_byte_identical(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(close="22000", volume=500)],
        )
        assert resolve_report_datasets(tmp_path, basis="day", scope=resolve_read_scope()) == (
            resolve_report_datasets(tmp_path, basis="day")
        )

    def test_reports_route_serves_scoped_symbols(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Route-threaded (Law-1 serve arm) + the widen path on the SAME lake.
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        rows = client.get("/api/v1/reports/preopen/symbols", params={"basis": "day"}).json()[
            "symbols"
        ]
        assert [r["symbol"] for r in rows] == ["NIFTY"]
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        wide_rows = client.get("/api/v1/reports/preopen/symbols", params={"basis": "day"}).json()[
            "symbols"
        ]
        assert [r["symbol"] for r in wide_rows] == ["NIFTY", "RELIANCE"]


# ── R4: greeks target discovery ───────────────────────────────────────────


def _seed_contract_stub(lake_root: Path, symbol: str) -> None:
    month_dir = lake_root / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024"
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / "06.parquet").touch()


class TestGreeksScope:
    def test_scoped_windows_exclude_stock_contract_counted(self, tmp_path: Path) -> None:
        _seed_contract_stub(tmp_path, "NSE-NIFTY-27Jun24-22000-CE")
        _seed_contract_stub(tmp_path, "NSE-RELIANCE-27Jun24-2900-CE")
        windows, excluded = scoped_option_windows(tmp_path, scope=resolve_read_scope())
        assert [w.symbol for w in windows] == ["NSE-NIFTY-27Jun24-22000-CE"]
        assert excluded == 1
        # The bare discover call stays legitimately UNFILTERED (§6#9 / §2.4).
        bare = discover_option_windows(tmp_path)
        assert sorted(w.underlying for w in bare) == ["NIFTY", "RELIANCE"]

    def test_backfill_scan_gates_stock_contract(self, tmp_path: Path) -> None:
        _seed_fno(tmp_path)
        rate = FixedRateSource()
        scan = scan_stale_greeks_targets(tmp_path, rate_source=rate, scope=resolve_read_scope())
        assert scan.scope_excluded == 1
        assert all("RELIANCE" not in t.symbol for t in scan.targets)
        # The §2.4 default arm — merge-base behavior, zero count.
        legacy = scan_stale_greeks_targets(tmp_path, rate_source=rate)
        assert legacy.scope_excluded == 0
        assert any("RELIANCE" in t.symbol for t in legacy.targets)

    def test_stock_stamps_stay_inert_bytes(self, tmp_path: Path) -> None:
        # Stored stock partitions are never edited by a scoped scan (R4 law).
        _seed_fno(tmp_path)
        stock = (
            tmp_path
            / "bars"
            / "NSE"
            / "FNO"
            / "NSE-RELIANCE-27Jun24-2900-CE"
            / "1minute"
            / "2024"
            / "06.parquet"
        )
        before = stock.read_bytes()
        scan_stale_greeks_targets(
            tmp_path, rate_source=FixedRateSource(), scope=resolve_read_scope()
        )
        assert stock.read_bytes() == before
