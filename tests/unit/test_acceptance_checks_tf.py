"""TF-check pins: fresh PASS, tampered FAILs, 1day law arms, standing OI abstain.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from brutex.acceptance.checks import tf
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _month_file, _twin_bars, poison_tf_high_x10

from tests.fakes.acceptance_twin import DAY1, DAY2, make_ctx, make_twin


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


class TestTfCheck:
    def test_prebuilt_fresh_passes(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = tf.run(make_ctx(lake))
        assert check.verdict is Verdict.PASS
        intraday = _sub(check, "tf.intraday")
        assert intraday.verdict is Verdict.PASS
        assert intraday.evidence["pairs_keyed"] >= 3  # 3 symbols × 5m at least
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.PASS
        assert daily.evidence["equal"] == 3

    def test_oi_standing_structural_abstain(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = tf.run(make_ctx(lake))
        oi = _sub(check, "tf.oi")
        assert oi.verdict is Verdict.ABSTAIN
        assert oi.abstain_reason is AbstainReason.NO_OI_COMPARE_SEAM
        assert oi.structural is True
        assert "F2" in str(oi.evidence["follow_up"])

    def test_tampered_5m_fails_naming_pair(self, tmp_path: Path) -> None:
        # Kills the `!=`-flip and dropped-sort mutants in the compare seam.
        lake = make_twin(tmp_path)
        poison_tf_high_x10(lake, symbol="BANKNIFTY", day=DAY1)
        check = tf.run(make_ctx(lake))
        intraday = _sub(check, "tf.intraday")
        assert intraday.verdict is Verdict.FAIL
        assert any(
            "BANKNIFTY" in pair and "5minute" in pair for pair in intraday.evidence["stale_pairs"]
        )

    def test_tampered_1day_close_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        path = _month_file(lake, "NIFTY", "1day", DAY1)
        frame = pl.read_parquet(path)
        closes = frame.get_column("close").to_list()
        closes[0] = closes[0] + 1.0
        frame.with_columns(pl.Series("close", closes)).write_parquet(path)
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.FAIL
        divergent = [c for c in daily.evidence["cells"] if c["state"] == "divergent"]
        assert divergent and divergent[0]["key"] == "NSE/INDEX/NIFTY"

    def test_forming_day_stored_fails(self, tmp_path: Path) -> None:
        from decimal import Decimal

        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        # A stored 1day bar for the RUN day itself — the cutoff law says it
        # cannot exist yet (the session is still forming at 11:30 IST).
        from datetime import UTC, datetime, time

        stamp = datetime.combine(date(2026, 7, 9), time(18, 30), tzinfo=UTC)  # midnight IST 07-10
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[
                CandleBar(
                    timestamp=stamp,
                    open=Decimal("1.00"),
                    high=Decimal("2.00"),
                    low=Decimal("0.50"),
                    close=Decimal("1.00"),
                    volume=1,
                    open_interest=None,
                )
            ],
        )
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.FAIL
        assert any(c["state"] == "forming_day_stored" for c in daily.evidence["cells"])

    def test_missing_stored_1day_row_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        path = _month_file(lake, "NIFTY", "1day", DAY1)
        frame = pl.read_parquet(path)
        frame.head(1).write_parquet(path)  # drop day-2's stored 1day bar
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.FAIL
        divergent = [c for c in daily.evidence["cells"] if c["state"] == "divergent"]
        assert divergent and divergent[0]["missing_from_stored"]

    def test_muhurat_day_skipped_per_day_rest_still_compared(self, tmp_path: Path) -> None:
        # Review round 1 MAJOR-2 regression: a Muhurat session in the history
        # must NOT abstain the WHOLE symbol — the regular days still get a
        # real 1day verdict, the Muhurat days are skipped + COUNTED.
        lake = tmp_path / "lake"
        from brutex.acceptance.selftest import build_twin_lake

        # 2025-10-21 is the Diwali Muhurat date; 2025-10-22 the next regular day.
        build_twin_lake(lake, days=(date(2025, 10, 21), date(2025, 10, 22)), symbols=("NIFTY",))
        check = tf.run(make_ctx(lake, months=12))  # window reaches Oct 2025
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.PASS
        assert daily.evidence["muhurat_days_skipped"] >= 1
        cell = next(c for c in daily.evidence["cells"] if c["key"] == "NSE/INDEX/NIFTY")
        assert cell["state"] == "equal"
        assert cell["muhurat_days_skipped"] >= 1

    def test_muhurat_day_counts_once_and_is_named_unverified(self, tmp_path: Path) -> None:
        # Round-2 folds: (NIT) one real Muhurat day present on BOTH sides
        # counted 2 pre-fix (expected-side + stored-side increments) — now ONE
        # count per distinct IST date; (MINOR) the skipped dates surface as
        # muhurat_days_unverified — the honest "1day bars on Muhurat dates
        # are NOT verified" disclosure.
        lake = tmp_path / "lake"
        from brutex.acceptance.selftest import build_twin_lake

        build_twin_lake(lake, days=(date(2025, 10, 21), date(2025, 10, 22)), symbols=("NIFTY",))
        check = tf.run(make_ctx(lake, months=12))
        daily = _sub(check, "tf.daily")
        cell = next(c for c in daily.evidence["cells"] if c["key"] == "NSE/INDEX/NIFTY")
        # ONE Muhurat date on both sides → exactly 1, never the double count.
        assert cell["muhurat_days_skipped"] == 1
        assert cell["muhurat_dates"] == ["2025-10-21"]
        assert daily.evidence["muhurat_days_skipped"] == 1
        assert daily.evidence["muhurat_days_unverified"] == ["2025-10-21"]
        assert daily.evidence["muhurat_days_unverified_total"] == 1
        assert any("NOT verified" in limit for limit in daily.limits)

    def test_muhurat_only_symbol_abstains_daily(self, tmp_path: Path) -> None:
        # Only when EVERY comparable day is Muhurat does the cell abstain.
        lake = tmp_path / "lake"
        from brutex.acceptance.selftest import build_twin_lake

        build_twin_lake(lake, days=(date(2025, 10, 21), date(2025, 10, 21)), symbols=("NIFTY",))
        check = tf.run(make_ctx(lake, months=12))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.ABSTAIN
        assert daily.abstain_reason is AbstainReason.DAILY_CUTOFF_AMBIGUOUS
        cell = next(c for c in daily.evidence["cells"] if c["key"] == "NSE/INDEX/NIFTY")
        assert cell["state"] == "daily_cutoff_ambiguous"
        assert cell["muhurat_days_skipped"] >= 1

    def test_no_1m_base_and_no_stored_partition_skips(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        writer = ParquetWriter(lake_root=lake)
        # ONLYDAILY: 1day partition with no 1m base (write via real writer).
        from brutex.data.spot_daily_resample import resample_1m_to_daily
        from brutex.data.tf_autoderive import read_lake_bars_frame

        df_1m = read_lake_bars_frame(lake / "bars" / "NSE" / "INDEX" / "NIFTY", "1minute")
        daily_bars = resample_1m_to_daily(df_1m, before=DAY2 + timedelta(days=1))
        writer.write(
            exchange="NSE", segment="INDEX", symbol="ONLYDAILY", interval="1day", bars=daily_bars
        )
        # ONLYMIN: 1m with no stored 1day.
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="ONLYMIN",
            interval="1minute",
            bars=_twin_bars(DAY2, base=50.0),
        )
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        states = {c["key"]: c["state"] for c in daily.evidence["cells"]}
        assert states["NSE/INDEX/ONLYDAILY"] == "no_1m_base"
        assert states["NSE/INDEX/ONLYMIN"] == "no_stored_partition"
        assert daily.evidence["skipped"] == 2
        assert daily.verdict is Verdict.PASS  # skips disclosed, never failed

    def test_fno_contract_rides_the_same_seam(self, tmp_path: Path) -> None:
        from brutex.data.fno_daily_resample import resample_1m_to_fno_daily
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.spot_daily_resample import frame_to_candle_bars
        from brutex.data.tf_autoderive import read_lake_bars_frame
        from brutex.data.tf_resampler import resample_1m_to_tf

        lake = make_twin(tmp_path)
        writer = ParquetWriter(lake_root=lake)
        contract = "NSE-NIFTY-30Sep26-FUT"
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        contract_dir = lake / "bars" / "NSE" / "FNO" / contract
        df_1m = read_lake_bars_frame(contract_dir, "1minute")
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="5minute",
            bars=frame_to_candle_bars(resample_1m_to_tf(df_1m, 5), absent_open_interest=None),
        )
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1day",
            bars=resample_1m_to_fno_daily(df_1m, before=DAY2 + timedelta(days=1)),
        )
        check = tf.run(make_ctx(lake))
        assert check.verdict is Verdict.PASS
        daily = _sub(check, "tf.daily")
        assert any(
            c["key"] == f"NSE/FNO/{contract}" and c["state"] == "equal"
            for c in daily.evidence["cells"]
        )
        intraday = _sub(check, "tf.intraday")
        assert intraday.evidence["pairs_keyed"] >= 4

    def test_pull_active_abstains(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        check = tf.run(make_ctx(lake, now_utc=lambda: datetime.now(UTC)))
        assert check.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS

    def test_empty_lake_abstains_no_datasets(self, tmp_path: Path) -> None:
        check = tf.run(make_ctx(tmp_path))
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_DATASETS


class TestTfEdges:
    def test_daily_month_cells_pads_predecessor(self) -> None:
        # A midnight-IST 1day bar lives in the PREVIOUS UTC month's file —
        # the stored side pads one month back (incl. across a year boundary).
        from brutex.acceptance.checks.tf import _daily_month_cells

        assert _daily_month_cells(None) is None
        got = _daily_month_cells({(2026, 1), (2026, 3)})
        assert got == {(2025, 12), (2026, 1), (2026, 2), (2026, 3)}

    def test_months_bound_is_real_old_divergence_out_of_window(self, tmp_path: Path) -> None:
        # Review round 1 complexity MAJOR-2 regression: --months must actually
        # bound the 1day compare — a tampered OLD-month stored bar is caught
        # with a wide window and OUT OF SCOPE with a narrow one.
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.spot_daily_resample import resample_1m_to_daily
        from brutex.data.tf_autoderive import read_lake_bars_frame

        lake = make_twin(tmp_path)
        old_day = date(2026, 2, 4)  # a regular Wednesday, 5 months back
        writer = ParquetWriter(lake_root=lake)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_twin_bars(old_day, base=90.0),
        )
        symbol_dir = lake / "bars" / "NSE" / "INDEX" / "NIFTY"
        df_1m = read_lake_bars_frame(symbol_dir, "1minute")
        old_daily = [
            b
            for b in resample_1m_to_daily(df_1m, before=DAY2 + timedelta(days=1))
            if b.timestamp.month == 2 or (b.timestamp.month == 1 and b.timestamp.day == 31)
        ]
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1day", bars=old_daily
        )
        path = _month_file(lake, "NIFTY", "1day", old_day)
        frame = pl.read_parquet(path)
        closes = frame.get_column("close").to_list()
        closes[0] = closes[0] + 1.0
        frame.with_columns(pl.Series("close", closes)).write_parquet(path)
        wide = _sub(tf.run(make_ctx(lake, months=6)), "tf.daily")
        assert wide.verdict is Verdict.FAIL  # Feb is inside the 6-month window
        narrow = _sub(tf.run(make_ctx(lake, months=1)), "tf.daily")
        assert narrow.verdict is Verdict.PASS  # Feb is honestly out of scope
        assert any("1day compare bounded" in note for note in narrow.limits)

    def test_padded_predecessor_rows_filtered_not_divergent(self, tmp_path: Path) -> None:
        # The stored-1day read pads one month back (a July-1 IST bar lives in
        # the June UTC file); the padded file's own out-of-window rows are
        # filtered, never read as "not derivable" divergences.
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.spot_daily_resample import resample_1m_to_daily
        from brutex.data.tf_autoderive import read_lake_bars_frame

        lake = make_twin(tmp_path)
        june_day = date(2026, 6, 24)  # regular Wednesday, previous month
        writer = ParquetWriter(lake_root=lake)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=_twin_bars(june_day, base=95.0),
        )
        symbol_dir = lake / "bars" / "NSE" / "INDEX" / "NIFTY"
        df_1m = read_lake_bars_frame(symbol_dir, "1minute")
        june_daily = [
            b
            for b in resample_1m_to_daily(df_1m, before=DAY2 + timedelta(days=1))
            if b.timestamp.month == 6
        ]
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1day", bars=june_daily
        )
        narrow = _sub(tf.run(make_ctx(lake, months=1)), "tf.daily")
        assert narrow.verdict is Verdict.PASS  # June rows skipped, July compared

    def test_all_data_outside_window_reads_no_1m_base(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="OLDONLY",
            interval="1minute",
            bars=_twin_bars(date(2026, 2, 4), base=80.0),
        )
        # a 1day dir with an out-of-window file so the interval-files gate passes
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="INDEX",
            symbol="OLDONLY",
            interval="1day",
            bars=_twin_bars(date(2026, 2, 4), base=80.0)[:1],
        )
        daily = _sub(tf.run(make_ctx(lake, months=1)), "tf.daily")
        states = {c["key"]: c["state"] for c in daily.evidence["cells"]}
        assert states["NSE/INDEX/OLDONLY"] == "no_1m_base"

    def test_stored_bar_not_derivable_from_1m(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, time

        lake = make_twin(tmp_path)
        path = _month_file(lake, "NIFTY", "1day", DAY1)
        frame = pl.read_parquet(path)
        orphan_ts = datetime.combine(date(2026, 6, 30), time(18, 30), tzinfo=UTC)
        orphan = frame.head(1).with_columns(
            pl.Series("timestamp", [orphan_ts.replace(tzinfo=None)]).cast(frame.schema["timestamp"])
        )
        pl.concat([orphan, frame]).write_parquet(path)
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.FAIL
        divergent = [c for c in daily.evidence["cells"] if c["state"] == "divergent"]
        assert any("not derivable" in d for c in divergent for d in c["divergent"])

    def test_full_mode_keeps_all_contracts(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = tf.run(make_ctx(lake, full=True))
        assert check.coverage.mode == "full"
        assert check.verdict is Verdict.PASS

    def test_engine_future_forced_into_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-3 fix (C-F1): the ADR-0012 NSE-NIFTY-*-FUT engine-track
        # contract dirs are ALWAYS in the tf selection — never left to the
        # rotating 6-contract fill (rotation stubbed to pick NOTHING).
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-31Jul26-FUT",
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        monkeypatch.setattr(tf, "sample_fno_contracts", lambda contracts, *, seed, sample_n: [])
        check = tf.run(make_ctx(lake))
        assert "NSE/FNO/NSE-NIFTY-31Jul26-FUT" in check.coverage.selection
