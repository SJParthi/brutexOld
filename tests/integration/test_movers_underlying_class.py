"""Integration tests for the ``/movers`` underlying-class filter (DEC-MOVERS-UNDERLYING-CLASS-001).

Drives the REAL ``/api/v1/movers`` route over a REAL Parquet + DuckDB lake to prove:

1. **Default byte-identity** — a request with NO ``?underlying_class=`` (and an
   explicit ``All``) carries NO ``underlying_class`` / ``excluded_unclassified``
   payload keys and no ``Class:`` note suffix, on all THREE tabs.
2. **Indices vs Stocks over the SAME coordinate return DIFFERENT row sets** — the
   class is a RANK-time filter over the ONE shared cached candidate set (the class
   is NOT in the candidate key), so ``movers_cache`` holds a single entry yet the two
   requests filter it to disjoint rows.
3. **Classification source** — SPOT from the lake segment; FUTURES/OPTIONS from the
   stored-master ``is_index`` kind map. An underlying absent from the master is
   ``unknown`` → included in ``All`` but excluded from both Indices AND Stocks, and
   the exclusion is COUNTED.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

# pyright: reportPrivateUsage=none

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

from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _min_bar(*, close: str, volume: int, oi: int | None = None) -> CandleBar:
    """A flat 09:15 IST (03:45 UTC) 1-minute bar on ``_DAY``."""
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


def _day_bar(*, close: str, volume: int, day: date = _DAY) -> CandleBar:
    """A 1-day OHLCV bar at 09:15 IST (03:45 UTC) on *day*."""
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
    """Master with NIFTY (index) + RELIANCE / BAJAJ-AUTO (stocks); FINNIFTY deliberately ABSENT.

    ``BAJAJ-AUTO`` is the hyphenated-spot-spelling case (review round 1): its kind-map
    key is the ``primary_fno_underlying`` form ``BAJAJAUTO`` while its contract symbols
    parse to the hyphen-preserving ``BAJAJ-AUTO`` token.
    """
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [
            _inst("NIFTY", kind=InstrumentType.IDX),
            _inst("RELIANCE", kind=InstrumentType.EQ),
            _inst("BAJAJ-AUTO", kind=InstrumentType.EQ),
        ],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _client(store: InstrumentStore, cache: MoversSnapshotCache | None = None) -> Any:
    """Build the dashboard ASGI app bound to *store* (callers wrap it in ``TestClient``).

    ``build_app`` returns the lazily-imported FastAPI app typed ``Any`` (the sibling
    movers integration suites' idiom) — the review-round-1 fix: the prior
    ``-> TestClient`` annotation + masking ignore double-lied about the return type
    and failed CI's ``pyright brutex tests`` with 7 ``reportArgumentType`` errors.
    """
    return build_app(
        DashboardState(), instrument_store=store, movers_cache=cache or MoversSnapshotCache()
    )


# ── spot ──────────────────────────────────────────────────────────────────


def _seed_spot(tmp_path: Path) -> None:
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


class TestSpotUnderlyingClass:
    @pytest.fixture(autouse=True)
    def _full_scope_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 (explicit FULL arm): these #1650
        route pins assert STOCK rows served — a class no in-scope symbol can
        reproduce under the shipped index4 default, so the machinery keeps its
        net under the widened scope (never index-rewritten, never deleted).
        The shipped-default guard is TestIndex4DefaultScope below."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")

    def test_default_omits_class_keys(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_spot(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}
        default = client.get("/api/v1/movers", params=base).json()
        explicit_all = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "All"}
        ).json()
        for payload in (default, explicit_all):
            assert "underlying_class" not in payload
            assert "excluded_unclassified" not in payload
            assert "Class:" not in payload["note"]
            assert {r["symbol"] for r in payload["volume"]} == {"NIFTY", "RELIANCE"}

    def test_indices_vs_stocks_share_one_cached_set_but_differ(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_spot(tmp_path)
        cache = MoversSnapshotCache()
        client = TestClient(_client(store, cache))
        base = {"asset": "spot", "interval": "1day", "date": "2024-06-13", "top_n": 5}
        indices = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Indices"}
        ).json()
        stocks = client.get("/api/v1/movers", params={**base, "underlying_class": "Stocks"}).json()
        # DIFFERENT row sets (the cache-independence guarantee)…
        assert {r["symbol"] for r in indices["volume"]} == {"NIFTY"}
        assert {r["symbol"] for r in stocks["volume"]} == {"RELIANCE"}
        # …served from ONE shared candidate set (class is NOT in candidate_key).
        assert cache.size() == 1
        assert indices["underlying_class"] == "indices"
        assert stocks["underlying_class"] == "stocks"


# ── futures ───────────────────────────────────────────────────────────────


def _seed_futures(tmp_path: Path) -> None:
    for underlying, close, oi in (("NIFTY", "22010", 1000), ("RELIANCE", "2905", 700)):
        _write(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=f"NSE-{underlying}-27Jun24-FUT",
            interval="1minute",
            bars=[_min_bar(close=close, volume=100, oi=oi)],
        )
    # FINNIFTY future — underlying ABSENT from the master → unknown class.
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-FINNIFTY-27Jun24-FUT",
        interval="1minute",
        bars=[_min_bar(close="23000", volume=60, oi=500)],
    )


class TestFuturesUnderlyingClass:
    @pytest.fixture(autouse=True)
    def _full_scope_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 (explicit FULL arm): these #1650
        route pins assert STOCK rows served — a class no in-scope symbol can
        reproduce under the shipped index4 default, so the machinery keeps its
        net under the widened scope (never index-rewritten, never deleted).
        The shipped-default guard is TestIndex4DefaultScope below."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")

    def test_default_ranks_all_three(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        default = client.get("/api/v1/movers", params=base).json()
        assert "underlying_class" not in default
        assert {r["symbol"] for r in default["volume"]} == {
            "NSE-NIFTY-27Jun24-FUT",
            "NSE-RELIANCE-27Jun24-FUT",
            "NSE-FINNIFTY-27Jun24-FUT",
        }

    def test_indices_excludes_unknown_and_counts_it(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        indices = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Indices"}
        ).json()
        # NIFTY is an index; RELIANCE is a stock; FINNIFTY is unknown → only NIFTY.
        assert {r["symbol"] for r in indices["volume"]} == {"NSE-NIFTY-27Jun24-FUT"}
        # FINNIFTY (unknown) was excluded from Indices AND counted.
        assert indices["excluded_unclassified"] == 1
        assert "unclassified excluded" in indices["note"]

    def test_stocks_keeps_only_stock(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get("/api/v1/movers", params={**base, "underlying_class": "Stocks"}).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-RELIANCE-27Jun24-FUT"}


# ── options ───────────────────────────────────────────────────────────────


def _seed_options(tmp_path: Path) -> None:
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-24000-CE",
        interval="1minute",
        bars=[_min_bar(close="120", volume=100, oi=1000)],
    )
    _write(
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-RELIANCE-27Jun24-2900-CE",
        interval="1minute",
        bars=[_min_bar(close="45", volume=80, oi=700)],
    )


class TestOptionsUnderlyingClass:
    @pytest.fixture(autouse=True)
    def _full_scope_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 (explicit FULL arm): these #1650
        route pins assert STOCK rows served — a class no in-scope symbol can
        reproduce under the shipped index4 default, so the machinery keeps its
        net under the widened scope (never index-rewritten, never deleted).
        The shipped-default guard is TestIndex4DefaultScope below."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")

    def test_default_omits_class_keys(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_options(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        default = client.get("/api/v1/movers", params=base).json()
        assert "underlying_class" not in default
        assert "Class:" not in default["note"]
        assert {r["symbol"] for r in default["volume"]} == {
            "NSE-NIFTY-27Jun24-24000-CE",
            "NSE-RELIANCE-27Jun24-2900-CE",
        }

    def test_stocks_filter(self, store: InstrumentStore, tmp_path: Path) -> None:
        _seed_options(tmp_path)
        client = TestClient(_client(store))
        base = {"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get("/api/v1/movers", params={**base, "underlying_class": "Stocks"}).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-RELIANCE-27Jun24-2900-CE"}
        assert stocks["underlying_class"] == "stocks"


# ── hyphenated underlyings (review round 1, finding 1) ────────────────────


class TestHyphenatedUnderlyingClass:
    @pytest.fixture(autouse=True)
    def _full_scope_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 (explicit FULL arm): these #1650
        route pins assert STOCK rows served — a class no in-scope symbol can
        reproduce under the shipped index4 default, so the machinery keeps its
        net under the widened scope (never index-rewritten, never deleted).
        The shipped-default guard is TestIndex4DefaultScope below."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")

    """A hyphenated F&O underlying classifies as ``stocks`` — never ``unknown``.

    Review round 1 (the MAJOR): the kind map keys the ``primary_fno_underlying``
    form (``BAJAJAUTO``) while the candidate lookup passes the RAW parsed token
    (``BAJAJ-AUTO`` — hyphen-preserving on options via ``parse_fno_symbol`` and, post
    the round-1 right-anchored fix, on futures too), so without the normalise-at-
    lookup fix every hyphenated F&O stock silently dropped out of BOTH Indices and
    Stocks and inflated ``excluded_unclassified``.
    """

    def test_hyphenated_futures_contract_classifies_as_stock(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        for symbol, close, oi in (
            ("NSE-NIFTY-27Jun24-FUT", "22010", 1000),
            ("NSE-BAJAJ-AUTO-27Jun24-FUT", "9600", 400),
        ):
            _write(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                bars=[_min_bar(close=close, volume=100, oi=oi)],
            )
        client = TestClient(_client(store))
        base = {"asset": "futures", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get("/api/v1/movers", params={**base, "underlying_class": "Stocks"}).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-BAJAJ-AUTO-27Jun24-FUT"}
        # In the master → classified, NOT counted unclassified.
        assert stocks["excluded_unclassified"] == 0
        # The right-anchored parse also preserves the display underlying intact.
        (row,) = stocks["volume"]
        assert "BAJAJ-AUTO" in row["name"]
        # And it never leaks into Indices.
        indices = client.get(
            "/api/v1/movers", params={**base, "underlying_class": "Indices"}
        ).json()
        assert {r["symbol"] for r in indices["volume"]} == {"NSE-NIFTY-27Jun24-FUT"}
        assert indices["excluded_unclassified"] == 0

    def test_hyphenated_option_contract_classifies_as_stock(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        for symbol, close, oi in (
            ("NSE-NIFTY-27Jun24-24000-CE", "120", 1000),
            ("NSE-BAJAJ-AUTO-27Jun24-9500-CE", "85", 300),
        ):
            _write(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                bars=[_min_bar(close=close, volume=100, oi=oi)],
            )
        client = TestClient(_client(store))
        base = {"asset": "options", "interval": "1day", "date": "2024-06-13", "top_n": 10}
        stocks = client.get("/api/v1/movers", params={**base, "underlying_class": "Stocks"}).json()
        assert {r["symbol"] for r in stocks["volume"]} == {"NSE-BAJAJ-AUTO-27Jun24-9500-CE"}
        assert stocks["excluded_unclassified"] == 0


# ── kind-map freshness (review round 1, finding 2) ────────────────────────


class TestKindMapMasterRevisionFreshness:
    @pytest.fixture(autouse=True)
    def _full_scope_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 (explicit FULL arm): these #1650
        route pins assert STOCK rows served — a class no in-scope symbol can
        reproduce under the shipped index4 default, so the machinery keeps its
        net under the widened scope (never index-rewritten, never deleted).
        The shipped-default guard is TestIndex4DefaultScope below."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")

    """The kind map + the stamped candidates track the MASTER content revision.

    Review round 1 (the memo-key staleness class): the map cache — and the F&O
    candidate cache whose rows carry the map's verdict STAMPED per row — must
    refresh when a keyless ``load-instruments`` moves the instrument master
    WITHOUT bumping the dashboard write generation (the documented
    ``master_revision`` trap the sibling nav-latency caches close).
    """

    def test_master_revision_move_refreshes_classification_without_generation_bump(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _seed_futures(tmp_path)
        client = TestClient(_client(store))
        base = {
            "asset": "futures",
            "interval": "1day",
            "date": "2024-06-13",
            "top_n": 10,
            "underlying_class": "Indices",
        }
        first = client.get("/api/v1/movers", params=base).json()
        # FINNIFTY is absent from the master → unknown → excluded + counted.
        assert {r["symbol"] for r in first["volume"]} == {"NSE-NIFTY-27Jun24-FUT"}
        assert first["excluded_unclassified"] == 1
        # A keyless load-instruments analogue: upsert FINNIFTY as an INDEX row
        # directly on the store — moves master_revision, bumps NO generation.
        store.upsert_many(
            [_inst("FINNIFTY", kind=InstrumentType.IDX)],
            snapshot_date=date(2024, 1, 3),
            run_id="r2",
            csv_source_sha256="1" * 64,
        )
        second = client.get("/api/v1/movers", params=base).json()
        assert {r["symbol"] for r in second["volume"]} == {
            "NSE-NIFTY-27Jun24-FUT",
            "NSE-FINNIFTY-27Jun24-FUT",
        }
        assert second["excluded_unclassified"] == 0

    def test_kind_map_built_once_across_polls_within_one_revision(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Call-count spy (review round 1 NIT): within ONE (generation,
        # master_revision) the O(underlyings) map build runs at most once across
        # repeated futures AND options polls — the memo actually memoises.
        # ONE import style per module (CodeQL py/import-and-import-from): the
        # sibling movers-suite `from … import app as app_module` idiom, beside
        # the file's top-level `from brutex.dashboard.app import …`.
        from brutex.dashboard import app as app_module

        _seed_futures(tmp_path)
        _seed_options(tmp_path)
        client = TestClient(_client(store))
        calls = {"n": 0}
        real_build = app_module.build_underlying_kind_map

        def _counting(instruments: Any) -> dict[str, str]:
            calls["n"] += 1
            return real_build(instruments)

        monkeypatch.setattr(app_module, "build_underlying_kind_map", _counting)
        for params in (
            {"asset": "futures", "underlying_class": "Indices"},
            {"asset": "futures", "underlying_class": "Stocks"},
            {"asset": "options", "underlying_class": "Indices"},
        ):
            resp = client.get(
                "/api/v1/movers",
                params={"interval": "1day", "date": "2024-06-13", "top_n": 10, **params},
            )
            assert resp.status_code == 200
        assert calls["n"] == 1
