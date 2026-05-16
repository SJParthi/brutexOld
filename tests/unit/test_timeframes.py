"""Tests for `brutex.data.timeframes` — 21-TF canonical list."""

from __future__ import annotations

import pytest

from brutex.data.timeframes import (
    ALL_TF_MINUTES,
    DAILY_TF_MINUTES,
    INTRADAY_MINUTE_TFS,
    INTRADAY_MULTIMINUTE_TFS,
    TF_LABELS,
    is_intraday,
    is_minute_multiple_of,
    label,
    parse_label,
)


class TestCount:
    def test_21_timeframes_total(self) -> None:
        assert len(ALL_TF_MINUTES) == 21

    def test_15_minute_level_tfs(self) -> None:
        assert len(INTRADAY_MINUTE_TFS) == 15

    def test_5_multiminute_tfs(self) -> None:
        assert len(INTRADAY_MULTIMINUTE_TFS) == 5

    def test_session_is_375_minutes(self) -> None:
        # 09:15 - 15:30 = 6h15m = 375m
        assert DAILY_TF_MINUTES == 375


class TestLabel:
    @pytest.mark.parametrize(
        "tf,expected",
        [
            (1,   "1m"),
            (5,   "5m"),
            (15,  "15m"),
            (30,  "30m"),
            (60,  "60m"),
            (240, "240m"),
            (375, "1d"),
        ],
    )
    def test_labels(self, tf: int, expected: str) -> None:
        assert label(tf) == expected

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            label(7777)


class TestParseLabel:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("1m",  1),
            ("5m",  5),
            ("15m", 15),
            ("60m", 60),
            ("1d",  375),
        ],
    )
    def test_parse(self, s: str, expected: int) -> None:
        assert parse_label(s) == expected

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown timeframe"):
            parse_label("nope")

    def test_round_trip(self) -> None:
        for tf in ALL_TF_MINUTES:
            assert parse_label(label(tf)) == tf


class TestIsIntraday:
    def test_1m_is_intraday(self) -> None:
        assert is_intraday(1)

    def test_240m_is_intraday(self) -> None:
        assert is_intraday(240)

    def test_daily_is_not_intraday(self) -> None:
        assert not is_intraday(375)


class TestIsMultiple:
    @pytest.mark.parametrize(
        "target,source,expected",
        [
            (5, 1, True),    # 1m -> 5m is fine
            (15, 1, True),
            (15, 5, True),   # 5m -> 15m is fine
            (15, 3, True),
            (7, 5, False),   # 5m -> 7m is NOT a multiple
            (1, 1, True),    # no-op
        ],
    )
    def test_multiple(self, target: int, source: int, expected: bool) -> None:
        assert is_minute_multiple_of(target, source) == expected

    def test_zero_source_raises(self) -> None:
        with pytest.raises(ValueError):
            is_minute_multiple_of(5, 0)


class TestSpecAlignment:
    """Verify the TF list exactly matches `docs/TRACK1_BACKTEST_SPEC.md`."""

    def test_intraday_minute_tfs_are_1_to_15(self) -> None:
        assert INTRADAY_MINUTE_TFS == tuple(range(1, 16))

    def test_multiminute_tfs_are_30_60_120_180_240(self) -> None:
        assert INTRADAY_MULTIMINUTE_TFS == (30, 60, 120, 180, 240)

    def test_all_labels_unique(self) -> None:
        assert len(set(TF_LABELS.values())) == len(TF_LABELS)

    def test_no_duplicates_in_all_tfs(self) -> None:
        assert len(set(ALL_TF_MINUTES)) == len(ALL_TF_MINUTES)
