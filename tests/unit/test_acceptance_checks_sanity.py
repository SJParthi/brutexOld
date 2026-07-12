"""Sanity-check pins: every poison class FAILs; disclosed escapes stay named.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
from brutex.acceptance.checks import sanity
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _month_file, _twin_bars

from tests.fakes.acceptance_twin import (
    DAY1,
    DAY2,
    make_ctx,
    make_master_present_twin,
    make_twin,
)


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


def _poison_column(lake: Path, symbol: str, day: date, column: str, value: float) -> None:
    path = _month_file(lake, symbol, "1minute", day)
    frame = pl.read_parquet(path)
    values = frame.get_column(column).to_list()
    values[0] = value
    frame.with_columns(pl.Series(column, values)).write_parquet(path)


class TestSanityCheck:
    def test_clean_twin_all_pass(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = sanity.run(make_ctx(lake))
        for sub_id in (
            "sanity.price",
            "sanity.bad_tick",
            "sanity.tick_grid",
            "sanity.session_window",
            "sanity.contiguity",
            "sanity.vix_presence",
        ):
            assert _sub(check, sub_id).verdict is Verdict.PASS, sub_id
        assert _sub(check, "sanity.completeness").abstain_reason is (
            AbstainReason.NO_INSTRUMENT_STORE
        )

    def test_negative_low_fails_price(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _poison_column(lake, "NIFTY", DAY2, "low", -5.0)
        check = sanity.run(make_ctx(lake))
        price = _sub(check, "sanity.price")
        assert price.verdict is Verdict.FAIL
        assert any("NIFTY" in o for o in price.evidence["offenders"])

    def test_high_lt_low_fails_price(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _poison_column(lake, "NIFTY", DAY2, "high", 0.01)
        assert _sub(sanity.run(make_ctx(lake)), "sanity.price").verdict is Verdict.FAIL

    def test_x10_run_fails_bad_tick_but_2bar_shape_escapes(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        # Scale a mid-session RUN of closes ×10 — the screen must flag it.
        path = _month_file(lake, "NIFTY", "1minute", DAY2)
        frame = pl.read_parquet(path)
        closes = frame.get_column("close").to_list()
        day2_offset = 31  # day-2 bars follow day-1's 31 rows in the month file
        for i in range(day2_offset + 5, day2_offset + 8):
            closes[i] = closes[i] * 10
        frame.with_columns(pl.Series("close", closes)).write_parquet(path)
        check = sanity.run(make_ctx(lake))
        bad_tick = _sub(check, "sanity.bad_tick")
        assert bad_tick.verdict is Verdict.FAIL
        # The #1616 disclosed escape stays NAMED in limits.
        assert any("#1616" in limit for limit in bad_tick.limits)

    def test_sub_4x_escape_disclosed_not_flagged(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        path = _month_file(lake, "NIFTY", "1minute", DAY2)
        frame = pl.read_parquet(path)
        closes = frame.get_column("close").to_list()
        closes[35] = closes[34] * 2  # ×2 — under the 4x screen BY DESIGN
        frame.with_columns(pl.Series("close", closes)).write_parquet(path)
        assert _sub(sanity.run(make_ctx(lake)), "sanity.bad_tick").verdict is Verdict.PASS

    def test_three_decimal_price_fails_tick_grid(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _poison_column(lake, "NIFTY", DAY2, "close", 100.005)
        check = sanity.run(make_ctx(lake))
        assert _sub(check, "sanity.tick_grid").verdict is Verdict.FAIL

    def test_weekend_bar_fails_session_window(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        # Saturday 2026-07-11 — a non-trading date bar planted via the writer.
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_twin_bars(date(2026, 7, 11), base=99.0)[:1],
        )
        check = sanity.run(make_ctx(lake))
        session = _sub(check, "sanity.session_window")
        assert session.verdict is Verdict.FAIL
        assert any("non-trading" in o for o in session.evidence["offenders"])

    def test_out_of_session_bar_fails(self, tmp_path: Path) -> None:
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        # 08:00 IST on a trading day — before the real session opens.
        ts = datetime.combine(DAY2, time(2, 30), tzinfo=UTC)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=ts,
                    open=Decimal("1.00"),
                    high=Decimal("1.10"),
                    low=Decimal("0.90"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                )
            ],
        )
        check = sanity.run(make_ctx(lake))
        session = _sub(check, "sanity.session_window")
        assert session.verdict is Verdict.FAIL
        assert any("outside session" in o for o in session.evidence["offenders"])

    def test_muhurat_evening_bars_kept(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import build_twin_lake
        from brutex.calendars.nse import MUHURAT_SESSIONS
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter

        lake = tmp_path / "lake"
        build_twin_lake(lake, days=(DAY1, DAY2), symbols=("NIFTY", "INDIAVIX"))
        muhurat_day, (open_ist, _close_ist) = sorted(MUHURAT_SESSIONS.items())[-1]
        open_utc = datetime.combine(muhurat_day, open_ist, tzinfo=UTC) - timedelta(
            hours=5, minutes=30
        )
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=open_utc,
                    open=Decimal("1.00"),
                    high=Decimal("1.10"),
                    low=Decimal("0.90"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                )
            ],
        )
        check = sanity.run(make_ctx(lake))
        assert _sub(check, "sanity.session_window").verdict is Verdict.PASS

    def test_interior_hole_fails_contiguity(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        far_day = DAY1 - timedelta(days=16)  # ≥7 bracketed empty sessions
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_twin_bars(far_day, base=90.0),
        )
        check = sanity.run(make_ctx(lake))
        contiguity = _sub(check, "sanity.contiguity")
        assert contiguity.verdict is Verdict.FAIL
        assert any("NIFTY" in o for o in contiguity.evidence["offenders"])

    def test_vix_silent_empty_fails(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import build_twin_lake

        lake = tmp_path / "lake"
        build_twin_lake(lake, days=(DAY1, DAY2), symbols=("NIFTY", "BANKNIFTY"))
        check = sanity.run(make_ctx(lake))
        vix = _sub(check, "sanity.vix_presence")
        assert vix.verdict is Verdict.FAIL
        assert vix.evidence["failure_class"] == "vix_silent_empty"

    def test_vix_lagging_anchor_session_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        # VIX stored but its day-2 bars removed: silent stop mid-history.
        path = _month_file(lake, "INDIAVIX", "1minute", DAY2)
        frame = pl.read_parquet(path)
        frame.head(31).write_parquet(path)  # keep only day-1 bars
        check = sanity.run(make_ctx(lake))
        assert _sub(check, "sanity.vix_presence").verdict is Verdict.FAIL

    def test_empty_lake_vix_probe_abstains(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        check = sanity.run(make_ctx(tmp_path))
        vix = _sub(check, "sanity.vix_presence")
        assert vix.verdict is Verdict.ABSTAIN
        assert vix.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_pull_active_abstains(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        check = sanity.run(make_ctx(lake, now_utc=lambda: datetime.now(UTC)))
        assert check.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS


class TestCompleteness:
    def test_master_present_healthy_lake_all_pass_real_chain(self, tmp_path: Path) -> None:
        # Round-6 BLOCKER regression (the operator's exact nightmare): a
        # REALISTIC master-present HEALTHY lake — real 1m/derived bars, a real
        # settled window ledger for the engine indices, a real
        # instruments.duckdb master — drives the REAL summarize→rollup→
        # per_symbol chain (NOT a monkeypatch). The pre-fix plain
        # `summarize_spot_datasets` dropped the `completeness` chip so every
        # engine index read a fabricated `none`→FAIL on EVERY master-present
        # lake, contradicting the operator's own /completeness page. With the
        # ledger-rollup enrichment each index reads its TRUE spot verdict and the
        # WHOLE sanity check PASSes — the "clean twin → 0 false FAILs" claim now
        # proven on a MASTER-PRESENT lake, matching production.
        lake = make_master_present_twin(tmp_path)
        check = sanity.run(make_ctx(lake))
        completeness = _sub(check, "sanity.completeness")
        assert completeness.verdict is Verdict.PASS, completeness.evidence
        assert completeness.evidence["engine_bad"] == []
        # NIFTY + BANKNIFTY are on the scoreboard (present engine indices).
        assert completeness.evidence["symbols_on_scoreboard"] >= 2
        # And the whole sanity check is green — no false FAIL anywhere.
        assert check.verdict is Verdict.PASS
        assert all(s.verdict is Verdict.PASS for s in check.sub_checks)

    def test_engine_rows_gate(self, tmp_path: Path) -> None:
        from brutex.data.instrument_store import InstrumentStore

        lake = make_twin(tmp_path)
        InstrumentStore.open(lake / "instruments.duckdb").close()
        check = sanity.run(make_ctx(lake))
        completeness = _sub(check, "sanity.completeness")
        # An EMPTY master has no per-symbol rows: honest nothing_to_check.
        assert completeness.verdict is Verdict.ABSTAIN
        assert completeness.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_cell_verdict_tokens_walker(self) -> None:
        from brutex.acceptance.checks.sanity import _cell_verdict_tokens

        cell = {"one_minute": "full", "nested": {"daily": "failed"}, "count": 3}
        assert _cell_verdict_tokens(cell) == {"full", "failed"}
        assert _cell_verdict_tokens("not-a-dict") == set()

    def test_readonly_never_migrates_older_schema_store(self, tmp_path: Path) -> None:
        # Round 1 M1 regression: the completeness sub opens the master
        # READ-ONLY — an older-schema store survives byte-identical (the old
        # RW `InstrumentStore.open` DROPped its instruments table) and the
        # verdict is a named ABSTAIN, never a migration.
        import hashlib

        from brutex.data.instrument_store import InstrumentStore

        lake = make_twin(tmp_path)
        path = lake / "instruments.duckdb"
        store = InstrumentStore.open(path)
        store._conn.execute("DROP TABLE instruments")  # a foreign/older shape
        store._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        store.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        check = sanity.run(make_ctx(lake))
        completeness = _sub(check, "sanity.completeness")
        assert completeness.verdict is Verdict.ABSTAIN
        assert completeness.abstain_reason is AbstainReason.NO_INSTRUMENT_STORE
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    def test_rw_holder_abstains_never_crashes(self, tmp_path: Path) -> None:
        import duckdb
        from brutex.data.instrument_store import InstrumentStore

        lake = make_twin(tmp_path)
        path = lake / "instruments.duckdb"
        InstrumentStore.open(path).close()
        holder = duckdb.connect(str(path))  # a live RW holder (pull/dashboard)
        try:
            check = sanity.run(make_ctx(lake))
        finally:
            holder.close()
        completeness = _sub(check, "sanity.completeness")
        assert completeness.verdict is Verdict.ABSTAIN
        assert completeness.abstain_reason is AbstainReason.NO_INSTRUMENT_STORE
        assert "read-only" in str(completeness.evidence["reason"])


class TestSanityEdges:
    def test_out_of_coverage_bars_and_hole_abstain(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        # 2027 is beyond holiday coverage: session check abstain-counts the
        # bars and the interior-gap census abstains for the dataset.
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="FUTUREYEAR",
            interval="1minute",
            bars=[
                *_twin_bars(date(2027, 1, 4), base=70.0),
                *_twin_bars(date(2027, 1, 5), base=71.0),
            ],
        )
        # --full lifts the backward month bound so the 2027 file is in scope.
        check = sanity.run(make_ctx(lake, full=True))
        session = _sub(check, "sanity.session_window")
        assert session.verdict is Verdict.PASS
        assert session.evidence["out_of_coverage_bars_abstained"] == 62
        contiguity = _sub(check, "sanity.contiguity")
        assert contiguity.evidence["datasets_abstained"] >= 1

    def test_months_bound_is_real_old_poison_out_of_window(self, tmp_path: Path) -> None:
        # Round 1 complexity MAJOR-2 regression: --months actually bounds the
        # sanity reads — an old-month poison is caught with a wide window and
        # honestly OUT OF SCOPE (disclosed in limits) with a narrow one.
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        old_day = date(2026, 2, 4)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_twin_bars(old_day, base=90.0),
        )
        _poison_column(lake, "NIFTY", old_day, "low", -5.0)
        wide = sanity.run(make_ctx(lake, months=6))
        assert _sub(wide, "sanity.price").verdict is Verdict.FAIL
        narrow = sanity.run(make_ctx(lake, months=1))
        assert _sub(narrow, "sanity.price").verdict is Verdict.PASS
        assert any("bounded to the last 1 months" in note for note in narrow.limits)

    def test_partial_interval_datasets_skipped_cleanly(self, tmp_path: Path) -> None:
        from datetime import timedelta as _td

        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.spot_daily_resample import resample_1m_to_daily
        from brutex.data.tf_autoderive import read_lake_bars_frame

        lake = make_twin(tmp_path)
        writer = ParquetWriter(lake_root=lake)
        # 1m-only dataset (no stored 1day) + 1day-only dataset (no 1m).
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="ONLYMIN",
            interval="1minute",
            bars=_twin_bars(DAY2, base=50.0),
        )
        df_1m = read_lake_bars_frame(lake / "bars" / "NSE" / "INDEX" / "NIFTY", "1minute")
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="ONLYDAILY",
            interval="1day",
            bars=resample_1m_to_daily(df_1m, before=DAY2 + _td(days=1)),
        )
        check = sanity.run(make_ctx(lake))
        assert _sub(check, "sanity.price").verdict is Verdict.PASS

    def test_engine_failed_verdict_gates_real_chain(self, tmp_path: Path) -> None:
        # Round-6 BLOCKER true-positive tooth — the REAL summarize→rollup→
        # per_symbol chain over a master-present lake whose NIFTY spot window is
        # genuinely FAILED (NOT a monkeypatch of per_symbol_completeness — the
        # #1631 fixture-only blindspot one level up, which masked the false
        # positive on healthy lakes). NIFTY's spot verdict reads `failed` ⇒ FAIL.
        lake = make_master_present_twin(tmp_path, nifty_status="FAILED")
        completeness = _sub(sanity.run(make_ctx(lake)), "sanity.completeness")
        assert completeness.verdict is Verdict.FAIL
        assert any("NIFTY" in bad for bad in completeness.evidence["engine_bad"])
        # BANKNIFTY stayed SUCCESS ⇒ it is NOT in engine_bad (only the genuinely
        # incomplete index is flagged, never the healthy sibling).
        assert not any("BANKNIFTY" in bad for bad in completeness.evidence["engine_bad"])
