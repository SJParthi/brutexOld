"""TF-check pins: fresh PASS, tampered FAILs, 1day law arms, standing OI abstain.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

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
        assert any("BANKNIFTY" in pair and "5minute" in pair for pair in intraday.evidence["stale_pairs"])

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

    def test_muhurat_session_abstains_daily(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        from brutex.acceptance.selftest import build_twin_lake

        # 2025-10-21 is the Diwali Muhurat date; 2025-10-22 the next regular day.
        build_twin_lake(lake, days=(date(2025, 10, 21), date(2025, 10, 22)), symbols=("NIFTY",))
        check = tf.run(make_ctx(lake))
        daily = _sub(check, "tf.daily")
        assert daily.verdict is Verdict.ABSTAIN
        assert daily.abstain_reason is AbstainReason.DAILY_CUTOFF_AMBIGUOUS

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
        from brutex.data.tf_autoderive import read_lake_bars_frame
        from brutex.data.tf_resampler import resample_1m_to_tf
        from brutex.data.spot_daily_resample import frame_to_candle_bars

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
        assert any(c["key"] == f"NSE/FNO/{contract}" and c["state"] == "equal" for c in daily.evidence["cells"])
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
    def test_ist_dates_empty_frame(self) -> None:
        import polars as pl

        from brutex.acceptance.checks.tf import _ist_dates

        empty = pl.DataFrame(
            {"ts_utc": pl.Series([], dtype=pl.Datetime(time_unit="us", time_zone="UTC"))}
        )
        assert _ist_dates(empty) == []

    def test_stored_bar_not_derivable_from_1m(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, time

        lake = make_twin(tmp_path)
        path = _month_file(lake, "NIFTY", "1day", DAY1)
        frame = pl.read_parquet(path)
        orphan_ts = datetime.combine(date(2026, 6, 30), time(18, 30), tzinfo=UTC)
        orphan = frame.head(1).with_columns(
            pl.Series("timestamp", [orphan_ts.replace(tzinfo=None)]).cast(
                frame.schema["timestamp"]
            )
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
