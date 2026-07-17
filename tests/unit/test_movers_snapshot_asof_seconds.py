"""As-of seconds seams — HH:MM:SS parsing + second-of-day filters
(DEC-TF-REGISTRY-DYNAMIC-001).

The HH:MM behavior table is byte-identical (pinned); HH:MM:SS is the
sanctioned additive widening. The look-ahead pin is load-bearing: a
mid-bucket seconds pick must NEVER take the bucket-anchor forward
resolution (that would admit bars from after the as-of instant).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from brutex.dashboard.movers_snapshot import (
    _SESSION_CLOSE_MIN,
    _SESSION_OPEN_MIN,
    _cutoff_minute,
    _cutoff_second,
    filter_to_ist_minute,
    filter_to_ist_second,
    resolve_asof_cutoff,
)


class TestCutoffSecond:
    def test_two_part_means_zero_seconds(self) -> None:
        assert _cutoff_second("09:15") == 9 * 3600 + 15 * 60

    def test_three_part_parses_seconds(self) -> None:
        assert _cutoff_second("15:29:59") == 15 * 3600 + 29 * 60 + 59

    def test_none_and_empty_mean_whole_day(self) -> None:
        assert _cutoff_second(None) is None
        assert _cutoff_second("") is None

    def test_lax_unpadded_fields_kept(self) -> None:
        # The pre-registry parser accepted unpadded ints ("9:5"); the widened
        # parser keeps that laxness (only the 3-part arm is NEW).
        assert _cutoff_second("9:5:7") == 9 * 3600 + 5 * 60 + 7

    def test_four_parts_malformed(self) -> None:
        with pytest.raises(ValueError, match="expected HH:MM or HH:MM:SS"):
            _cutoff_second("09:15:30:00")

    def test_non_numeric_malformed(self) -> None:
        with pytest.raises(ValueError, match="expected HH:MM or HH:MM:SS"):
            _cutoff_second("09:xx")


class TestCutoffMinuteDelegation:
    def test_hhmm_byte_identical(self) -> None:
        assert _cutoff_minute("09:15") == 555
        assert _cutoff_minute("15:29") == 929
        assert _cutoff_minute(None) is None

    def test_hhmmss_floors_to_minute(self) -> None:
        assert _cutoff_minute("09:15:30") == 555
        assert _cutoff_minute("09:15:59") == 555


class TestResolveAsofCutoffSeconds:
    def test_hhmm_anchor_law_unchanged(self) -> None:
        assert resolve_asof_cutoff("5minute", "09:20") == 564

    def test_hhmm_instant_law_unchanged(self) -> None:
        assert resolve_asof_cutoff("5minute", "09:21") == 561

    def test_hhmm_close_cap_unchanged(self) -> None:
        assert resolve_asof_cutoff("5minute", "15:25") == 929

    def test_daily_and_unknown_and_blank_unchanged(self) -> None:
        assert resolve_asof_cutoff("1day", "09:20") is None
        assert resolve_asof_cutoff("nonsense", "09:20") is None
        assert resolve_asof_cutoff("5minute", None) is None

    def test_ss00_equals_hhmm(self) -> None:
        assert resolve_asof_cutoff("5minute", "09:20:00") == 564

    def test_seconds_instant_never_anchor_resolves(self) -> None:
        # LOOK-AHEAD PIN (load-bearing): 09:20:30 is a mid-bucket INSTANT even
        # though 09:20 is an anchor — resolving forward to 564 would admit
        # bars from after the pick.
        assert resolve_asof_cutoff("5minute", "09:20:30") == 560

    def test_seconds_instant_off_grid(self) -> None:
        assert resolve_asof_cutoff("5minute", "09:21:59") == 561


def _frame(stamps: list[dt.datetime]) -> pl.DataFrame:
    return pl.DataFrame({"ts_utc": stamps, "v": list(range(len(stamps)))}).with_columns(
        pl.col("ts_utc").cast(pl.Datetime("us", "UTC"))
    )


def _ist(h: int, m: int, s: int = 0) -> dt.datetime:
    return dt.datetime(2024, 6, 3, h, m, s, tzinfo=dt.UTC) - dt.timedelta(hours=5, minutes=30)


class TestFilterToIstSecond:
    def test_second_granular_cut(self) -> None:
        frame = _frame([_ist(9, 15, 0), _ist(9, 15, 30), _ist(9, 16, 10)])
        kept = filter_to_ist_second(frame, 9 * 3600 + 15 * 60 + 30, ts_col="ts_utc")
        assert kept.get_column("v").to_list() == [0, 1]

    def test_minute_parity_on_minute_stamped_frames(self) -> None:
        frame = _frame([_ist(9, 15), _ist(9, 20), _ist(9, 25)])
        by_minute = filter_to_ist_minute(frame, 560, ts_col="ts_utc")
        by_second = filter_to_ist_second(frame, 560 * 60 + 59, ts_col="ts_utc")
        assert by_second.equals(by_minute)


class TestSessionConstantDerivation:
    def test_derived_from_common_session_constants(self) -> None:
        assert _SESSION_OPEN_MIN == 555
        assert _SESSION_CLOSE_MIN == 930
