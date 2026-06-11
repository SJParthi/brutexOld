"""Tests for `brutex.data.tf_autoderive` — pull-settle auto-derivation (DEC-TF-MATERIALIZE-002).

Covers the opt-in strict decoder, the planner selection rule, the
quarantining runner, the report arithmetic, and the freshness compare core
(fresh / stale on value drift / stale on row-count drift).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from brutex.data.tf_autoderive import (
    ENV_AUTO_DERIVE_TFS,
    AutoderiveOutcome,
    AutoderiveReport,
    autoderive_enabled,
    compare_derived_freshness,
    plan_autoderive_symbols,
    run_autoderive,
)
from brutex.data.timeframe_materialize import MaterializeResult


@dataclass(frozen=True)
class _FakeResult:
    symbol: str
    completed: int


class TestOptInFlag:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_AUTO_DERIVE_TFS, raising=False)
        assert not autoderive_enabled()

    @pytest.mark.parametrize("val", ["", "0", "true", "yes", "2", " 1 ", "1"])
    def test_only_literal_one_activates(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(ENV_AUTO_DERIVE_TFS, val)
        assert autoderive_enabled() is (val.strip() == "1")


class TestPlanner:
    def test_selects_only_symbols_with_new_bars(self) -> None:
        results = [
            _FakeResult("NIFTY", 2),
            _FakeResult("BANKNIFTY", 0),  # nothing new — skipped
            _FakeResult("SENSEX", 1),
        ]
        assert plan_autoderive_symbols(results) == ("NIFTY", "SENSEX")

    def test_dedupes_preserving_order(self) -> None:
        results = [_FakeResult("NIFTY", 1), _FakeResult("NIFTY", 2), _FakeResult("SENSEX", 1)]
        assert plan_autoderive_symbols(results) == ("NIFTY", "SENSEX")

    def test_empty_input_plans_nothing(self) -> None:
        assert plan_autoderive_symbols([]) == ()


class TestRunner:
    def test_quarantines_one_failure_without_sinking_the_rest(self) -> None:
        calls: list[str] = []

        def _materialize(symbol: str) -> MaterializeResult:
            calls.append(symbol)
            if symbol == "BANKNIFTY":
                raise ValueError("corrupt partition")
            return MaterializeResult(written={"5m": 10, "15m": 4}, skipped=())

        report = run_autoderive(
            symbols=("NIFTY", "BANKNIFTY", "SENSEX"), materialize_one=_materialize
        )
        # Every symbol still ran — the failure never stopped the loop.
        assert calls == ["NIFTY", "BANKNIFTY", "SENSEX"]
        assert report.symbols_ok == 2
        assert report.symbols_failed == 1
        assert report.total_bars == 28
        by = {o.symbol: o for o in report.outcomes}
        assert by["BANKNIFTY"].error == "corrupt partition"
        assert not by["BANKNIFTY"].ok
        assert by["NIFTY"].written_tfs == 2 and by["NIFTY"].ok

    def test_empty_symbols_is_a_clean_noop(self) -> None:
        report = run_autoderive(symbols=(), materialize_one=lambda s: pytest.fail("never called"))
        assert report == AutoderiveReport(outcomes=())
        assert report.symbols_ok == report.symbols_failed == report.total_bars == 0

    def test_outcome_ok_property(self) -> None:
        assert AutoderiveOutcome(symbol="X", written_tfs=1, bars_written=1).ok
        assert not AutoderiveOutcome(symbol="X", written_tfs=0, bars_written=0, error="boom").ok


_OPEN_UTC = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)


def _df_1m(n_bars: int = 20) -> pl.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(n_bars)]
    return pl.DataFrame(
        {
            "ts_utc": [_OPEN_UTC + timedelta(minutes=i) for i in range(n_bars)],
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000] * n_bars,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


class TestFreshnessCompare:
    def test_fresh_when_stored_equals_rederivation(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5)
        assert compare_derived_freshness(df_1m, stored, 5) == "fresh"

    def test_stale_on_value_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.col("close") + 1.0)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"

    def test_stale_on_row_count_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).slice(1)  # a bucket went missing
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"
