"""The `/db` TZ + moneyness stock-drift regressions (DEC-DB-TZ-MONEYNESS-FIXES-001).

Two evidence-backed areas in :mod:`brutex.dashboard.db_view`, one suite:

**(a) session-TZ sign-flip belt (verification of the SHIPPED fix).** The bare
``CAST("timestamp" AS DATE)`` sites that resolved in the ambient DuckDB session
``TimeZone`` were fixed on main by DEC-DISPLAY-TRUTH-HONESTY-001 (MIGRATION.md
#1633 item D2 — every session-date derivation keys on the COMMON
:data:`~brutex.dashboard.db_view._IST_DATE_EXPR`) and the helper-level maps are
already pinned (``test_intraday_readers_ist_date_key_invariant_under_negative_
session_tz``). This suite adds the missing BELT: an END-TO-END
:func:`~brutex.dashboard.db_view.query_candles` pager run whose ``change_pct``
/ ``open_gap_pct`` / prev-close baseline must be byte-identical under a hostile
negative-offset session zone vs the IST-host answer (bars constructed exactly
at the 09:15-IST / 03:45-UTC session boundary — the instant a bare cast rolls
back one calendar day), a direct pin that the ONE common ``_IST_DATE_EXPR``
fragment is session-TZ-independent, and a SOURCE drift pin so an ambient-TZ
date cast can never be silently reintroduced into ``db_view.py``.

**(b) moneyness stock-drift rung (DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 — the
#1482 named follow-up CLOSED).** The `/db` per-bar moneyness spot-join now
threads the SAME evidence-grounded
:data:`~brutex.data.lake_keys.StockDriftMap` the Greeks enrichment ladder
consumes (DEC-GREEKS-SPOTJOIN-STOCK-001): a stock option chain whose on-disk
spot dir drifted by case/space/hyphen (``TATA-MOTORS`` vs the chain's
``TATAMOTORS``) previously read moneyness ``"?"`` on every bar even though the
spot data existed on disk — it now classifies, with the SAME verdict (same
recovered key) as the Greeks ladder, the SAME fail-soft no-master contract
(``None`` → the honest ``"?"``, never a fabricated spot), lazy loading
(exact-key hot path pays zero extra I/O; loader invoked at most once per
join), and the route-level composite-token memo (one
:func:`~brutex.data.spot_drift.load_stock_drift_map` scan per
``(generation, master_revision)``, never per paging click).
"""

# Whitebox: the join helpers + the SQL date fragment are private — imported
# directly so each hard-to-reach branch is pinned (the house private-import
# test pattern).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")

import duckdb
from brutex.dashboard.db_view import (
    _IST_DATE_EXPR,
    _underlying_spot_close_by_ts,
    _utc_instant,
    query_candles,
)
from brutex.data.lake_keys import StockDriftMap, spot_recovery_candidates
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.spot_drift import load_stock_drift_map
from structlog.testing import capture_logs

# 09:15 IST == 03:45 UTC — the session-open instant a bare ambient-TZ DATE cast
# rolls back one calendar day under any negative-offset session zone.
_D1_OPEN = datetime(2024, 3, 1, 3, 45, tzinfo=UTC)
_D1_CLOSE = datetime(2024, 3, 1, 9, 59, tzinfo=UTC)  # 15:29 IST
_D2_OPEN = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # Monday after the Fri 03-01
_D2_CLOSE = datetime(2024, 3, 4, 9, 59, tzinfo=UTC)


def _bar(ts: datetime, close: str, *, open_: str | None = None, oi: int | None = None) -> CandleBar:
    c = Decimal(close)
    o = Decimal(open_) if open_ is not None else c
    return CandleBar(
        timestamp=ts,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=10,
        open_interest=oi,
    )


def _write(
    lake: Path, *, segment: str, symbol: str, bars: list[CandleBar], interval: str = "1minute"
) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_master(lake: Path, *symbols: str) -> None:
    """Write a REAL DuckDB instrument master at ``<lake>/instruments.duckdb``
    (the ``test_spot_drift._seed_master`` fixture pattern)."""
    from brutex.data.instrument_store import InstrumentStore
    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    lake.mkdir(parents=True, exist_ok=True)
    with InstrumentStore.open(lake / "instruments.duckdb") as store:
        store.upsert_many(
            [
                Instrument(
                    exch_id=Exchange.NSE,
                    security_id=abs(hash(s)) % 100_000,
                    symbol_name=s,
                    display_name=s,
                    instrument=InstrumentType.EQ,
                    segment=Segment.CASH,
                    series="EQ",
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
                for s in symbols
            ],
            snapshot_date=date(2024, 1, 1),
            run_id="test-db-tz-moneyness",
            csv_source_sha256="0" * 64,
        )


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


# ── (a) session-TZ sign-flip belt ───────────────────────────────────────────


class TestSessionTzInvariance:
    """The /db session-date derivations are pinned session-TZ-independent.

    VERIFICATION VERDICT (item (a) of DEC-DB-TZ-MONEYNESS-FIXES-001): the bare
    ``CAST(ts AS DATE)`` sites the breaker evidence named were already fixed on
    main by MIGRATION.md #1633 item D2 (DEC-SPOT-SESSION-DAYS-IST-TZ-001 law) —
    this class is the ADDITIVE belt (end-to-end pager + expression pin + source
    drift pin), not a re-fix.
    """

    HOSTILE_ZONES = ("America/New_York", "America/Adak", "Australia/Sydney", "UTC")

    @pytest.mark.parametrize("zone", HOSTILE_ZONES)
    def test_ist_date_expr_session_tz_independent(
        self, conn: duckdb.DuckDBPyConnection, zone: str
    ) -> None:
        """The ONE common IST-date SQL fragment yields the IST date in EVERY
        ambient session zone — pinned at the exact 09:15-IST boundary instant."""
        conn.execute(f"SET TimeZone = '{zone}'")
        (got,) = conn.execute(
            f"SELECT {_IST_DATE_EXPR} "  # noqa: S608 — module constant, no user text
            "FROM (SELECT TIMESTAMPTZ '2024-03-01 03:45:00+00' AS \"timestamp\")"
        ).fetchall()[0]
        assert got == date(2024, 3, 1)

    def _two_session_lake(self, lake: Path) -> None:
        # Session 1 (Fri 2024-03-01): opens 98→100 at 09:15 IST, last close 200.
        # Session 2 (Mon 2024-03-04): opens at 09:15 IST close 210 → change_pct
        # vs the prior session's LAST close (200) = +5%.
        _write(
            lake,
            segment="CASH",
            symbol="NSE-NIFTY",
            bars=[
                _bar(_D1_OPEN, "100", open_="98"),
                _bar(_D1_CLOSE, "200"),
                _bar(_D2_OPEN, "210", open_="205"),
                _bar(_D2_CLOSE, "220"),
            ],
        )

    def _page(self, conn: duckdb.DuckDBPyConnection, lake: Path) -> dict[str, Any]:
        return query_candles(
            conn,
            lake,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=10,
        )

    def test_query_candles_page_matches_ist_host_answer_under_hostile_tz(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """END-TO-END: the whole pager payload — change_pct / open_gap_pct /
        prev-close contiguity included — is byte-identical under a hostile
        negative-offset session zone vs the IST host, on bars that straddle the
        midnight-IST-adjacent boundary (03:45 UTC). A reintroduced ambient-TZ
        cast mis-keys the day-2 baseline here and diverges."""
        self._two_session_lake(tmp_path)
        (original_tz,) = conn.execute("SELECT current_setting('TimeZone')").fetchall()[0]
        try:
            conn.execute("SET TimeZone = 'Asia/Kolkata'")
            ist_host = self._page(conn, tmp_path)
            hostile: dict[str, dict[str, Any]] = {}
            for zone in self.HOSTILE_ZONES:
                conn.execute(f"SET TimeZone = '{zone}'")
                hostile[zone] = self._page(conn, tmp_path)
        finally:
            conn.execute(f"SET TimeZone = '{original_tz}'")
        for zone, payload in hostile.items():
            assert payload == ist_host, f"session zone {zone} diverged from the IST host"
        # Independent value teeth on the IST-host answer itself: the day-2 rows
        # baseline on day-1's LAST close (200), day-1 rows have no predecessor.
        # Rows are keyed by their (unique) close cell — the timestamp cell is an
        # IST-rendered display string, deliberately not re-parsed here.
        cols = ist_host["columns"]
        close_i, chg_i = cols.index("close"), cols.index("change_pct")
        by_close = {row[close_i]: row[chg_i] for row in ist_host["rows"]}
        assert len(by_close) == 4
        assert by_close[100.0] is None and by_close[200.0] is None  # first session
        assert float(by_close[210.0]) == pytest.approx(5.0)  # (210-200)/200
        assert float(by_close[220.0]) == pytest.approx(10.0)  # (220-200)/200

    def test_db_view_source_has_no_ambient_tz_date_casts(self) -> None:
        """SOURCE drift pin: every ``::DATE`` in ``db_view.py`` is one of the two
        blessed session-TZ-independent constants (``AT TIME ZONE 'UTC'``-anchored),
        and no executable line carries a bare ``CAST(... AS DATE)``. Heuristic
        line-scan (comments / doc markup carrying `` are exempt) — honest
        envelope: a teeth-bearing ratchet, not a full SQL parser."""
        from brutex.dashboard import db_view as db_view_module

        source = Path(db_view_module.__file__).read_text(encoding="utf-8")
        blessed = re.compile(r"_(IST_DATE|UTC_DATE)_EXPR: Final\[str\]")
        for n, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            documentation = stripped.startswith("#") or "``" in line
            if "::DATE" in line:
                assert blessed.search(line) or documentation, (
                    f"db_view.py:{n}: unblessed ::DATE cast — use _IST_DATE_EXPR/_UTC_DATE_EXPR"
                )
                if blessed.search(line):
                    assert "AT TIME ZONE 'UTC'" in line or "_IST_TS_EXPR" in line
            if "AS DATE" in line:
                assert documentation, (
                    f"db_view.py:{n}: bare 'CAST(... AS DATE)' resolves in the ambient "
                    "session TimeZone — use the COMMON _IST_DATE_EXPR/_UTC_DATE_EXPR"
                )


# ── (b) moneyness stock-drift rung ──────────────────────────────────────────

_OPT = "NSE-TATAMOTORS-30Jun25-1000-CE"
_T1 = datetime(2025, 6, 2, 9, 15, tzinfo=UTC)
_T2 = datetime(2025, 6, 2, 9, 16, tzinfo=UTC)


def _drifted_stock_lake(lake: Path, *, master: bool = True) -> None:
    """An option chain whose underlying spot lives under a DRIFTED dir name.

    The chain's F&O underlying token is ``TATAMOTORS``; the on-disk spot dir is
    the hyphenated ``TATA-MOTORS`` (the DEC-GREEKS-SPOTJOIN-STOCK-001 BAJAJ-AUTO
    drift class — reachable by NO alias-ladder rung, only the master-verified
    stock-drift fold). Spot 990 @T1 (strike 1000 → OTM) then 1010 @T2 (→ ITM).
    """
    _write(
        lake, segment="FNO", symbol=_OPT, bars=[_bar(_T1, "50", oi=100), _bar(_T2, "60", oi=100)]
    )
    _write(lake, segment="CASH", symbol="TATA-MOTORS", bars=[_bar(_T1, "990"), _bar(_T2, "1010")])
    if master:
        _seed_master(lake, "TATA-MOTORS")


class TestMoneynessStockDrift:
    """DEC-DB-MONEYNESS-STOCK-EVIDENCE-001 — the /db moneyness twin of the
    Greeks ladder's stock arm. FAILS BEFORE THE FIX: the spot-join walked the
    2-arg ladder only, so the drifted stock lake read ``"?"`` on every bar."""

    def _page(
        self,
        conn: duckdb.DuckDBPyConnection,
        lake: Path,
        *,
        loader: Any = None,
    ) -> dict[str, Any]:
        return query_candles(
            conn,
            lake,
            exchange="NSE",
            segment="FNO",
            symbol=_OPT,
            interval="1minute",
            limit=10,
            stock_drift_loader=loader,
        )

    def test_drifted_stock_spot_recovers_moneyness(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _drifted_stock_lake(tmp_path)
        with capture_logs() as logs:
            page = self._page(conn, tmp_path, loader=lambda: load_stock_drift_map(tmp_path))
        cols = page["columns"]
        m_i, s_i = cols.index("moneyness"), cols.index("spot")
        cells = {row[cols.index("close")]: (row[m_i], row[s_i]) for row in page["rows"]}
        # strike 1000: spot 990 → OTM, spot 1010 → ITM — classified, never "?".
        assert cells[50.0][0].startswith("OTM") and cells[50.0][1] == "990.00"
        assert cells[60.0][0].startswith("ITM") and cells[60.0][1] == "1010.00"
        recovered = [e for e in logs if e["event"] == "db_view.spot_key_recovered"]
        assert len(recovered) == 1  # exactly ONE recovery log per join.
        assert recovered[0]["underlying"] == "TATAMOTORS"
        assert recovered[0]["recovered_key"] == "TATA-MOTORS"
        assert recovered[0]["rung"] == "stock_drift"

    def test_same_verdict_as_greeks_ladder(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """The /db join and the Greeks enrichment recover the SAME key from the
        SAME loader + ladder over the SAME lake — one verification law, never a
        second fold (CLAUDE.md §11 COMMON)."""
        from brutex.data.fno_symbol import parse_fno_symbol
        from brutex.data.greeks_enrich import GreeksEnrichTarget, read_spot_join_with_recovery

        _drifted_stock_lake(tmp_path)
        drift = load_stock_drift_map(tmp_path)
        assert drift == {("NSE", "TATAMOTORS"): ("TATA-MOTORS",)}
        # The ladder consumed by BOTH surfaces offers the drifted dir last.
        assert spot_recovery_candidates("TATAMOTORS", "NSE", stock_drift=drift)[-1] == (
            "TATA-MOTORS"
        )
        # Greeks-side verdict over the SAME lake + map.
        parts = parse_fno_symbol(_OPT)
        assert parts is not None
        target = GreeksEnrichTarget(
            exchange="NSE", symbol=_OPT, start=date(2025, 6, 2), end=date(2025, 6, 3)
        )
        with capture_logs() as greeks_logs:
            join = read_spot_join_with_recovery(tmp_path, target, parts, stock_drift=drift)
        greeks_rec = [e for e in greeks_logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(greeks_rec) == 1
        assert greeks_rec[0]["recovered_key"] == "TATA-MOTORS"
        assert greeks_rec[0]["rung"] == "stock_drift"
        # /db-side verdict: the SAME key, the SAME spot values at the shared instants.
        with capture_logs() as db_logs:
            spots, joined = _underlying_spot_close_by_ts(
                conn,
                tmp_path,
                exchange="NSE",
                underlying="TATAMOTORS",
                interval="1minute",
                ts_values=[_T1, _T2],
                stock_drift_loader=lambda: drift,
            )
        db_rec = [e for e in db_logs if e["event"] == "db_view.spot_key_recovered"]
        assert len(db_rec) == 1
        assert db_rec[0]["recovered_key"] == greeks_rec[0]["recovered_key"]
        assert db_rec[0]["rung"] == "stock_drift"
        assert joined == "1minute"
        assert spots[_utc_instant(_T1)] == Decimal("990.00")
        assert join.close_by_ts[_utc_instant(_T1)] == spots[_utc_instant(_T1)]
        assert join.close_by_ts[_utc_instant(_T2)] == spots[_utc_instant(_T2)]

    def test_no_master_stays_unknown_honest(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """The loader's fail-soft contract rides through: no instrument master →
        ``load_stock_drift_map`` → ``None`` → the ladder is byte-identical to the
        pre-stock walk → the honest ``"?"`` (never a fabricated spot/verdict)."""
        _drifted_stock_lake(tmp_path, master=False)
        page = self._page(conn, tmp_path, loader=lambda: load_stock_drift_map(tmp_path))
        cols = page["columns"]
        m_i = cols.index("moneyness")
        assert [row[m_i] for row in page["rows"]] == ["?", "?"]

    def test_loader_lazy_and_invoked_at_most_once(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """Hot path: an exact-key lake never invokes the loader (zero extra I/O).
        Cold path: BOTH `_walk` passes (display interval + the 1minute fallback)
        share ONE memoised load — the loader fires at most once per join."""
        calls: list[int] = []

        def _spy() -> StockDriftMap | None:
            calls.append(1)
            return load_stock_drift_map(tmp_path)

        # Exact-key lake (no drift): the loader must never run.
        _write(tmp_path, segment="CASH", symbol="RELIANCE", bars=[_bar(_T1, "2900")])
        spots, _ = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="RELIANCE",
            interval="1minute",
            ts_values=[_T1],
            stock_drift_loader=_spy,
        )
        assert spots and calls == []
        # Drifted lake on a COARSE display interval: walk(5minute) exhausts (no
        # 5minute spot partition) then walk(1minute) recovers — one load total.
        _drifted_stock_lake(tmp_path)
        spots, joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="TATAMOTORS",
            interval="5minute",
            ts_values=[_T1],
            stock_drift_loader=_spy,
        )
        assert spots[_utc_instant(_T1)] == Decimal("990.00")
        assert joined == "1minute"  # the documented base-feed fallback arm
        assert len(calls) == 1

    def test_route_wires_composite_memoised_loader(self, tmp_path: Path) -> None:
        """The REAL `/api/v1/db/candles` route classifies the drifted stock lake
        AND memoises the drift-map load behind the composite
        ``(generation, master_revision)`` token — two paging clicks, ONE
        ``load_stock_drift_map`` scan (the #1482-prescribed memo law)."""
        pytest.importorskip("fastapi")
        from unittest import mock

        from brutex.dashboard.app import DashboardState, build_app
        from brutex.data.instrument_store import InstrumentStore
        from fastapi.testclient import TestClient

        _drifted_stock_lake(tmp_path)  # seeds <tmp_path>/instruments.duckdb too
        calls: list[int] = []

        def _counting_load(lake: Path) -> StockDriftMap | None:
            calls.append(1)
            return load_stock_drift_map(lake)

        with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
            client = TestClient(build_app(DashboardState(), instrument_store=store))
            # String-form patch of app.py's imported name — the route lambda
            # resolves it through the app module namespace at call time.
            with mock.patch("brutex.dashboard.app.load_stock_drift_map", _counting_load):
                url = (
                    "/api/v1/db/candles?exchange=NSE&segment=FNO"
                    f"&symbol={_OPT}&interval=1minute&limit=1"
                )
                first = client.get(url)
                second = client.get(url + "&offset=1")
            assert first.status_code == 200 and second.status_code == 200
            for resp in (first, second):
                payload = resp.json()
                m_i = payload["columns"].index("moneyness")
                labels = {row[m_i] for row in payload["rows"]}
                assert labels and "?" not in labels  # classified, never UNKNOWN
            assert len(calls) == 1  # composite-token memo: ONE scan, not per click
