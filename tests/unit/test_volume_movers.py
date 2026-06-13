"""Unit tests for the volume/volatility movers screener (DEC-MOVERS-VOLUME-001).

Drives every branch of :mod:`brutex.screeners.volume_movers` — the pure metric
computation, the two rankings (descending + stable tiebreak), the report
assembly, and the injected-reader orchestration — including every edge / empty /
boundary / failure path the 100%-line+branch gate requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from brutex.money import format_inr
from brutex.screeners.volume_movers import (
    MoverMetrics,
    MoversReport,
    ScreenTarget,
    compute_mover_metrics,
    rank_by_realised_volatility,
    rank_by_volume_surge,
    rank_movers,
    screen_movers,
)

from tests.fakes.movers_fakes import FakeLakeBarReader, make_candle

_START = datetime(2025, 1, 2, tzinfo=UTC)
_END = datetime(2025, 1, 3, tzinfo=UTC)

_NIFTY = ScreenTarget("NSE", "INDEX", "NSE-NIFTY", "1minute")
_BANKNIFTY = ScreenTarget("NSE", "INDEX", "NSE-BANKNIFTY", "1minute")
_SENSEX = ScreenTarget("BSE", "INDEX", "BSE-SENSEX", "1minute")


def _metrics(**overrides: Any) -> MoverMetrics:
    """A valid :class:`MoverMetrics`; override fields per scenario."""
    base: dict[str, Any] = {
        "target": _NIFTY,
        "bar_count": 5,
        "recent_avg_volume": Decimal(50),
        "baseline_avg_volume": Decimal(10),
        "volume_surge_ratio": Decimal(5),
        "volatility_bar_count": 5,
        "realised_range_volatility": Decimal("0.144"),
        "last_close_paisa": 10_000,
    }
    base.update(overrides)
    return MoverMetrics(**base)


class TestScreenTarget:
    def test_valid_target_ok(self) -> None:
        target = ScreenTarget("NSE", "INDEX", "NSE-NIFTY", "1minute")
        assert target.symbol == "NSE-NIFTY"

    @pytest.mark.parametrize("field", ["exchange", "segment", "symbol", "interval"])
    def test_empty_coordinate_rejected(self, field: str) -> None:
        kwargs: dict[str, str] = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"ScreenTarget.{field}"):
            ScreenTarget(**kwargs)

    def test_hashable_usable_as_mapping_key(self) -> None:
        assert {_NIFTY: 1}[_NIFTY] == 1


class TestComputeMoverMetrics:
    def test_surge_and_volatility_basic(self) -> None:
        bars = [
            make_candle(high="101", low="99", close="100", volume=10, minute=0),
            make_candle(high="102", low="98", close="100", volume=10, minute=1),
            make_candle(high="103", low="97", close="100", volume=10, minute=2),
            make_candle(high="110", low="90", close="100", volume=50, minute=3),
            make_candle(high="120", low="80", close="100", volume=50, minute=4),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=2)
        assert metric.recent_avg_volume == Decimal(50)
        assert metric.baseline_avg_volume == Decimal(10)
        assert metric.volume_surge_ratio == Decimal(5)
        # mean of (0.02, 0.04, 0.06, 0.20, 0.40) = 0.144
        assert metric.realised_range_volatility == Decimal("0.144")
        assert metric.volatility_bar_count == 5
        assert metric.bar_count == 5

    def test_recent_bars_below_one_rejected(self) -> None:
        bars = [make_candle(high="1", low="1", close="1", volume=1)]
        with pytest.raises(ValueError, match="recent_bars must be >= 1"):
            compute_mover_metrics(_NIFTY, bars, recent_bars=0)

    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one bar"):
            compute_mover_metrics(_NIFTY, [], recent_bars=2)

    def test_window_too_short_for_surge(self) -> None:
        # bar_count == recent_bars → no baseline → surge + averages all None,
        # but volatility is still defined.
        bars = [
            make_candle(high="101", low="99", close="100", volume=10, minute=0),
            make_candle(high="104", low="96", close="100", volume=20, minute=1),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=2)
        assert metric.recent_avg_volume is None
        assert metric.baseline_avg_volume is None
        assert metric.volume_surge_ratio is None
        assert metric.realised_range_volatility == Decimal("0.05")

    def test_zero_baseline_volume_surge_undefined(self) -> None:
        bars = [
            make_candle(high="101", low="99", close="100", volume=0, minute=0),
            make_candle(high="101", low="99", close="100", volume=0, minute=1),
            make_candle(high="101", low="99", close="100", volume=7, minute=2),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=1)
        assert metric.baseline_avg_volume == Decimal(0)
        assert metric.recent_avg_volume == Decimal(7)
        assert metric.volume_surge_ratio is None

    def test_zero_recent_volume_surge_is_zero(self) -> None:
        bars = [
            make_candle(high="101", low="99", close="100", volume=8, minute=0),
            make_candle(high="101", low="99", close="100", volume=8, minute=1),
            make_candle(high="101", low="99", close="100", volume=0, minute=2),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=1)
        assert metric.volume_surge_ratio == Decimal(0)

    def test_volatility_skips_nonpositive_close(self) -> None:
        bars = [
            make_candle(high="101", low="99", close="100", volume=5, minute=0),
            make_candle(high="103", low="97", close="100", volume=5, minute=1),
            make_candle(high="0", low="0", close="0", volume=5, minute=2),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=1)
        # Only the two positive-close bars contribute: mean(0.02, 0.06) = 0.04.
        assert metric.volatility_bar_count == 2
        assert metric.realised_range_volatility == Decimal("0.04")

    def test_all_nonpositive_close_volatility_undefined(self) -> None:
        bars = [
            make_candle(high="0", low="0", close="0", volume=5, minute=0),
            make_candle(high="0", low="0", close="0", volume=5, minute=1),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=1)
        assert metric.volatility_bar_count == 0
        assert metric.realised_range_volatility is None
        assert metric.last_close_paisa == 0

    def test_last_close_money_via_brutex_money(self) -> None:
        bars = [
            make_candle(high="100", low="100", close="100", volume=1, minute=0),
            make_candle(high="14020.80", low="14020.80", close="14020.80", volume=1, minute=1),
        ]
        metric = compute_mover_metrics(_NIFTY, bars, recent_bars=1)
        assert metric.last_close_paisa == 1_402_080
        assert metric.last_close_inr == format_inr(1_402_080)
        assert metric.last_close_inr == "₹14,020.80"


class TestMoverMetricsValidation:
    def test_bar_count_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="bar_count must be >= 1"):
            _metrics(bar_count=0)

    def test_volatility_bar_count_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="volatility_bar_count must be in"):
            _metrics(volatility_bar_count=-1)

    def test_volatility_bar_count_above_bar_count_rejected(self) -> None:
        with pytest.raises(ValueError, match="volatility_bar_count must be in"):
            _metrics(bar_count=3, volatility_bar_count=4)

    def test_negative_last_close_paisa_rejected(self) -> None:
        with pytest.raises(ValueError, match="last_close_paisa must be >= 0"):
            _metrics(last_close_paisa=-1)

    def test_negative_volume_surge_rejected(self) -> None:
        with pytest.raises(ValueError, match="volume_surge_ratio must be >= 0"):
            _metrics(volume_surge_ratio=Decimal(-1))

    def test_negative_volatility_rejected(self) -> None:
        with pytest.raises(ValueError, match="realised_range_volatility must be >= 0"):
            _metrics(realised_range_volatility=Decimal("-0.01"))

    def test_all_none_optionals_allowed(self) -> None:
        metric = _metrics(
            recent_avg_volume=None,
            baseline_avg_volume=None,
            volume_surge_ratio=None,
            realised_range_volatility=None,
            volatility_bar_count=0,
        )
        assert metric.has_volume_surge is False
        assert metric.has_realised_volatility is False

    def test_present_flags_and_display(self) -> None:
        metric = _metrics()
        assert metric.has_volume_surge is True
        assert metric.has_realised_volatility is True
        assert metric.last_close_inr == "₹100.00"


class TestRankings:
    def test_volume_surge_descending(self) -> None:
        low = _metrics(target=_NIFTY, volume_surge_ratio=Decimal(2))
        high = _metrics(target=_BANKNIFTY, volume_surge_ratio=Decimal(9))
        ranked = rank_by_volume_surge([low, high], top_n=5)
        assert [m.target for m in ranked] == [_BANKNIFTY, _NIFTY]

    def test_volatility_descending(self) -> None:
        low = _metrics(target=_NIFTY, realised_range_volatility=Decimal("0.01"))
        high = _metrics(target=_BANKNIFTY, realised_range_volatility=Decimal("0.09"))
        ranked = rank_by_realised_volatility([low, high], top_n=5)
        assert [m.target for m in ranked] == [_BANKNIFTY, _NIFTY]

    def test_undefined_metric_excluded(self) -> None:
        present = _metrics(target=_NIFTY, volume_surge_ratio=Decimal(3))
        absent = _metrics(target=_BANKNIFTY, volume_surge_ratio=None)
        ranked = rank_by_volume_surge([present, absent], top_n=5)
        assert [m.target for m in ranked] == [_NIFTY]

    def test_top_n_limits_results(self) -> None:
        metrics = [
            _metrics(target=_NIFTY, volume_surge_ratio=Decimal(3)),
            _metrics(target=_BANKNIFTY, volume_surge_ratio=Decimal(2)),
            _metrics(target=_SENSEX, volume_surge_ratio=Decimal(1)),
        ]
        ranked = rank_by_volume_surge(metrics, top_n=1)
        assert [m.target for m in ranked] == [_NIFTY]

    def test_tiebreak_is_stable_by_target(self) -> None:
        # Equal surge → ordered by (exchange, segment, symbol, interval): BSE < NSE.
        nifty = _metrics(target=_NIFTY, volume_surge_ratio=Decimal(4))
        sensex = _metrics(target=_SENSEX, volume_surge_ratio=Decimal(4))
        ranked = rank_by_volume_surge([nifty, sensex], top_n=5)
        assert [m.target for m in ranked] == [_SENSEX, _NIFTY]

    def test_top_n_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            rank_by_volume_surge([_metrics()], top_n=0)

    def test_volatility_top_n_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            rank_by_realised_volatility([_metrics()], top_n=0)


class TestRankMovers:
    def _series(self) -> dict[ScreenTarget, list[Any]]:
        return {
            _NIFTY: [
                make_candle(high="101", low="99", close="100", volume=10, minute=0),
                make_candle(high="120", low="80", close="100", volume=90, minute=1),
            ],
            _BANKNIFTY: [
                make_candle(high="100.5", low="99.5", close="100", volume=10, minute=0),
                make_candle(high="105", low="95", close="100", volume=20, minute=1),
            ],
        }

    def test_full_report(self) -> None:
        report = rank_movers(
            self._series(),
            window_start=_START,
            window_end=_END,
            recent_bars=1,
            top_n=5,
        )
        assert isinstance(report, MoversReport)
        assert report.recent_bars == 1
        assert report.top_n == 5
        # NIFTY: surge 90/10 = 9, vol mean(0.01, 0.40)=0.205;
        # BANKNIFTY: surge 20/10 = 2, vol mean(0.01, 0.10)=0.055.
        assert [m.target for m in report.top_volume_surge] == [_NIFTY, _BANKNIFTY]
        assert [m.target for m in report.top_realised_volatility] == [_NIFTY, _BANKNIFTY]
        assert report.skipped == ()

    def test_empty_series_is_skipped(self) -> None:
        series: dict[ScreenTarget, list[Any]] = {
            _NIFTY: [make_candle(high="101", low="99", close="100", volume=10)],
            _BANKNIFTY: [],
        }
        report = rank_movers(series, window_start=_START, window_end=_END, recent_bars=1, top_n=5)
        assert report.skipped == (_BANKNIFTY,)
        assert [m.target for m in report.metrics] == [_NIFTY]

    def test_metrics_sorted_independent_of_input_order(self) -> None:
        series = self._series()
        reversed_series = dict(reversed(list(series.items())))
        report = rank_movers(
            reversed_series, window_start=_START, window_end=_END, recent_bars=1, top_n=5
        )
        # Sorted by target key regardless of insertion order: BANKNIFTY < NIFTY.
        assert [m.target for m in report.metrics] == [_BANKNIFTY, _NIFTY]

    def test_determinism_rerun_is_byte_stable(self) -> None:
        kwargs: dict[str, Any] = {
            "window_start": _START,
            "window_end": _END,
            "recent_bars": 1,
            "top_n": 5,
        }
        first = rank_movers(self._series(), **kwargs)
        second = rank_movers(self._series(), **kwargs)
        assert first == second

    def test_naive_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            rank_movers(
                {}, window_start=datetime(2025, 1, 2), window_end=_END, recent_bars=1, top_n=5
            )

    def test_naive_end_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            rank_movers(
                {}, window_start=_START, window_end=datetime(2025, 1, 3), recent_bars=1, top_n=5
            )

    def test_end_before_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >= window_start"):
            rank_movers({}, window_start=_END, window_end=_START, recent_bars=1, top_n=5)

    def test_recent_bars_validation(self) -> None:
        with pytest.raises(ValueError, match="recent_bars must be >= 1"):
            rank_movers({}, window_start=_START, window_end=_END, recent_bars=0, top_n=5)

    def test_top_n_validation(self) -> None:
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            rank_movers({}, window_start=_START, window_end=_END, recent_bars=1, top_n=0)

    def test_empty_universe_is_empty_report(self) -> None:
        report = rank_movers({}, window_start=_START, window_end=_END, recent_bars=1, top_n=5)
        assert report.metrics == ()
        assert report.top_volume_surge == ()
        assert report.top_realised_volatility == ()
        assert report.skipped == ()


class TestScreenMovers:
    def test_reads_via_injected_reader_and_ranks(self) -> None:
        reader = FakeLakeBarReader(
            {
                _NIFTY: [
                    make_candle(high="101", low="99", close="100", volume=10, minute=0),
                    make_candle(high="130", low="70", close="100", volume=80, minute=1),
                ],
                _BANKNIFTY: [
                    make_candle(high="100.5", low="99.5", close="100", volume=10, minute=0),
                    make_candle(high="102", low="98", close="100", volume=12, minute=1),
                ],
            }
        )
        report = screen_movers(
            reader=reader,
            targets=[_NIFTY, _BANKNIFTY],
            start=_START,
            end=_END,
            recent_bars=1,
            top_n=5,
        )
        assert [m.target for m in report.top_volume_surge] == [_NIFTY, _BANKNIFTY]
        # Reader was consulted for each target with the screen window.
        assert reader.calls == [(_NIFTY, _START, _END), (_BANKNIFTY, _START, _END)]

    def test_missing_target_is_skipped(self) -> None:
        reader = FakeLakeBarReader({})
        report = screen_movers(
            reader=reader,
            targets=[_NIFTY],
            start=_START,
            end=_END,
            recent_bars=1,
            top_n=5,
        )
        assert report.skipped == (_NIFTY,)
        assert report.metrics == ()

    def test_duplicate_target_rejected(self) -> None:
        reader = FakeLakeBarReader({})
        with pytest.raises(ValueError, match="duplicate target"):
            screen_movers(
                reader=reader,
                targets=[_NIFTY, _NIFTY],
                start=_START,
                end=_END,
                recent_bars=1,
                top_n=5,
            )

    def test_window_validation_fails_fast_without_reads(self) -> None:
        reader = FakeLakeBarReader({})
        with pytest.raises(ValueError, match="must be >= window_start"):
            screen_movers(
                reader=reader,
                targets=[_NIFTY],
                start=_END,
                end=_START,
                recent_bars=1,
                top_n=5,
            )
        assert reader.calls == []

    def test_recent_bars_validation_fails_fast(self) -> None:
        reader = FakeLakeBarReader({})
        with pytest.raises(ValueError, match="recent_bars must be >= 1"):
            screen_movers(
                reader=reader,
                targets=[_NIFTY],
                start=_START,
                end=_END,
                recent_bars=0,
                top_n=5,
            )
        assert reader.calls == []

    def test_top_n_validation_fails_fast(self) -> None:
        reader = FakeLakeBarReader({})
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            screen_movers(
                reader=reader,
                targets=[_NIFTY],
                start=_START,
                end=_END,
                recent_bars=1,
                top_n=0,
            )
        assert reader.calls == []
