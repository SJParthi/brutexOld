"""Unit tests for the resurrected bounded-bisection ``probe_depth`` (R6 seam).

DEC-DOCTOR-LIVE-PROOF-KIT-001. Stem-matched to
``brutex/adapters/groww/probe.py`` so the strict per-module coverage discovery
gate scopes these tests to the module they cover (moved out of the combined
``test_doctor_live_seams.py``, whose stem matched no touched module).
"""

# Whitebox test of the probe's defensive ts-parsing arms — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from brutex.adapters.groww.probe import (
    DEPTH_PROBE_MAX_CALLS,
    VERDICT_ABSENT,
    VERDICT_ERROR,
    VERDICT_OK,
    probe_depth,
)


class _DepthClient:
    """A minimal fake vendor whose 1m history starts at *floor* (or never)."""

    def __init__(self, floor: date | None, *, epoch: bool = False, error: bool = False) -> None:
        self.floor = floor
        self.epoch = epoch
        self.error = error
        self.calls = 0

    def historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        if self.error:
            from brutex.adapters.groww.errors import GrowwError

            raise GrowwError("probe transport down")
        start = date.fromisoformat(str(kwargs["start"])[:10])
        end = date.fromisoformat(str(kwargs["end"])[:10])
        if self.floor is None or end < self.floor:
            return {"candles": []}
        first_day = max(start, self.floor)
        stamp: object = f"{first_day.isoformat()}T09:15:00"
        if self.epoch:
            stamp = int(datetime(first_day.year, first_day.month, first_day.day, 9, 15).timestamp())
        return {"candles": [[stamp, 100.0, 101.0, 99.0, 100.5, 0, None]]}


class TestProbeDepth:
    def test_boundary_data_confirms_floor_in_one_call(self) -> None:
        client = _DepthClient(date(2020, 1, 1))
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_OK
        assert result.boundary_has_data is True
        assert result.probed_floor == date(2020, 1, 1)
        assert result.calls_used == 1

    def test_empty_boundary_without_bisect_reports_unknown(self) -> None:
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_ABSENT
        assert "re-run with --depth-bisect" in result.detail
        assert result.calls_used == 1

    def test_bisect_locates_exact_floor(self) -> None:
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_OK
        assert result.probed_floor == date(2021, 6, 14)
        assert result.first_ts == "2021-06-14T09:15:00"
        assert result.calls_used <= DEPTH_PROBE_MAX_CALLS

    def test_bisect_epoch_stamps_fall_back_to_window_floor(self) -> None:
        true_floor = date(2021, 6, 14)
        client = _DepthClient(true_floor, epoch=True)
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_OK
        assert result.probed_floor is not None
        # Window-granularity floor: within one window of the true first bar.
        assert abs((result.probed_floor - true_floor).days) < 15

    def test_bisect_with_no_data_at_all(self) -> None:
        client = _DepthClient(None)
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_ABSENT
        assert "no probed 1m data at all" in result.detail
        assert result.calls_used == 2

    def test_transport_error_is_error_verdict(self) -> None:
        client = _DepthClient(date(2020, 1, 1), error=True)
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_ERROR
        assert "probe transport down" in result.detail

    def test_call_budget_cap_is_honest_about_non_convergence(self) -> None:
        client = _DepthClient(date(2024, 3, 8))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True, max_calls=4)
        assert result.verdict == VERDICT_OK
        assert result.calls_used == 4
        assert "call budget hit — floor is an UPPER bound" in result.detail

    def test_first_row_ts_defensive_arms(self) -> None:
        from brutex.adapters.groww.probe import _date_of_first_ts, _first_row_ts

        assert _first_row_ts(None) is None
        assert _first_row_ts({"candles": "nope"}) is None
        assert _first_row_ts({"candles": ["2021-06-14T09:15:00"]}) == "2021-06-14T09:15:00"
        assert _date_of_first_ts(None) is None
        assert _date_of_first_ts("1623630300") is None
        assert _date_of_first_ts("2021-06-14T09:15:00") == date(2021, 6, 14)
