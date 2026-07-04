"""In-package Groww probe — every verdict branch (DEC-DHAN-GROWW-REMIGRATE-001).

Drives :mod:`brutex.adapters.groww.probe` with a FAKE ``GrowwClient`` (no
``growwapi``, no network) so each check's OK / ABSENT / ERROR verdict,
``run_probe``, and the bounded-bisection ``probe_depth`` (register R6,
DEC-DOCTOR-LIVE-PROOF-KIT-001) are 100% covered.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta

from brutex.adapters.groww.errors import GrowwError, GrowwNotFoundError
from brutex.adapters.groww.probe import (
    DEPTH_PROBE_MAX_CALLS,
    VERDICT_ABSENT,
    VERDICT_ERROR,
    VERDICT_OK,
    probe_depth,
    probe_granularity,
    probe_options,
    probe_vix,
    run_probe,
)
from hypothesis import given
from hypothesis import strategies as st


class _FakeClient:
    """A fake :class:`GrowwClient` returning canned payloads / raising on demand.

    Each method consults a per-method script: a callable (called with the
    kwargs) returning the payload, or raising. Duck-typed — the probe only calls
    ``historical_candles`` / ``expiries`` / ``contracts``.
    """

    def __init__(
        self,
        *,
        candles: object = None,
        expiries: object = None,
        contracts: object = None,
    ) -> None:
        self._candles = candles
        self._expiries = expiries
        self._contracts = contracts

    def historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        result = self._candles(kwargs) if callable(self._candles) else self._candles
        if isinstance(result, GrowwError):
            raise result
        return result  # type: ignore[return-value]

    def expiries(self, **kwargs: object) -> Mapping[str, object]:
        if isinstance(self._expiries, GrowwError):
            raise self._expiries
        return self._expiries  # type: ignore[return-value]

    def contracts(self, **kwargs: object) -> Mapping[str, object]:
        if isinstance(self._contracts, GrowwError):
            raise self._contracts
        return self._contracts  # type: ignore[return-value]


# ── probe_vix ───────────────────────────────────────────────────────────────


def test_vix_ok_first_candidate() -> None:
    client = _FakeClient(candles={"candles": [[1, 2], [3, 4]]})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.symbol == "NSE-INDIAVIX"
    assert result.rows == 2


def test_vix_ok_second_candidate_after_empty_first() -> None:
    def candles(kwargs: dict[str, object]) -> dict[str, object]:
        if kwargs["groww_symbol"] == "NSE-INDIAVIX":
            return {"candles": []}
        return {"candles": [[1]]}

    client = _FakeClient(candles=candles)
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.symbol == "NSE-INDIA VIX"


def test_vix_absent_when_all_empty() -> None:
    client = _FakeClient(candles={"candles": []})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT
    assert result.symbol is None


def test_vix_absent_when_non_dict_payload() -> None:
    client = _FakeClient(candles="not-a-dict")
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_vix_absent_when_candles_not_a_list() -> None:
    # `candles` key present but not a list exercises _candle_rows's non-list branch.
    client = _FakeClient(candles={"candles": "notalist"})
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_vix_error_on_groww_error() -> None:
    client = _FakeClient(candles=GrowwNotFoundError("boom"))
    result = probe_vix(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "India VIX probe failed" in result.detail


# ── probe_granularity ───────────────────────────────────────────────────────


def test_granularity_ok_with_first_ts() -> None:
    client = _FakeClient(candles={"candles": [["2020-01-01 09:15:00", 1, 2]]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 1
    assert "2020-01-01" in result.detail


def test_granularity_ok_scalar_first_row() -> None:
    # A non-list first row exercises the scalar branch of first_ts.
    client = _FakeClient(candles={"candles": ["barstring"]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert "barstring" in result.detail


def test_granularity_ok_empty_first_row() -> None:
    # An empty list first row exercises the `and first` falsy branch.
    client = _FakeClient(candles={"candles": [[]]})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK


def test_granularity_absent_when_empty() -> None:
    client = _FakeClient(candles={"candles": []})
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT
    assert result.rows == 0


def test_granularity_error() -> None:
    client = _FakeClient(candles=GrowwError("down"))
    result = probe_granularity(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "granularity probe failed" in result.detail


def test_granularity_requests_window_within_30day_cap() -> None:
    # DEC-GROWW-PROBE-1M-WINDOW-001: Groww caps the 1minute interval at 30 days per
    # request, so the probe MUST ask for a single ≤30-day window — the old 90-day
    # (Q1) request was rejected vendor-side as a FALSE ❌ on a valid token.
    captured: dict[str, object] = {}

    def _capture(kwargs: dict[str, object]) -> Mapping[str, object]:
        captured.update(kwargs)
        return {"candles": [["2020-01-01 09:15:00", 1, 2]]}

    result = probe_granularity(_FakeClient(candles=_capture))  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert captured["interval"] == "1minute"
    start = date.fromisoformat(str(captured["start"])[:10])
    end = date.fromisoformat(str(captured["end"])[:10])
    assert (end - start).days <= 30


# ── probe_options ───────────────────────────────────────────────────────────


def test_options_ok() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30", "2020-02-27"]},
        contracts={"contracts": ["c1", "c2", "c3"]},
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 3
    assert "2 expiries" in result.detail


def test_options_ok_non_list_contracts_counts_zero() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30"]},
        contracts={"contracts": "nope"},
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_OK
    assert result.rows == 0


def test_options_absent_no_expiries() -> None:
    client = _FakeClient(expiries={"expiries": []})
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_options_absent_non_dict_expiries() -> None:
    client = _FakeClient(expiries="nope")
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ABSENT


def test_options_error_on_expiries() -> None:
    client = _FakeClient(expiries=GrowwError("exp-boom"))
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "get_expiries failed" in result.detail


def test_options_error_on_contracts() -> None:
    client = _FakeClient(
        expiries={"expiries": ["2020-01-30"]},
        contracts=GrowwError("con-boom"),
    )
    result = probe_options(client)  # type: ignore[arg-type]
    assert result.verdict == VERDICT_ERROR
    assert "get_contracts failed" in result.detail


# ── run_probe ───────────────────────────────────────────────────────────────


def test_run_probe_runs_three_checks() -> None:
    client = _FakeClient(
        candles={"candles": [[1]]},
        expiries={"expiries": ["2020-01-30"]},
        contracts={"contracts": ["c1"]},
    )
    results = run_probe(client)  # type: ignore[arg-type]
    assert [r.name for r in results] == ["vix", "granularity", "options"]
    assert all(r.verdict == VERDICT_OK for r in results)


# ── probe_depth (R6; the kit's 2020-depth probe) ───────────────────────────
# Stem-matched HERE (the module's own per-file coverage gate) rather than in
# the doctor-live suite: scripts/ci/coverage_gate_discovery.py maps
# brutex/adapters/groww/probe.py -> tests/unit/test_groww_probe.py.


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

    def test_bisect_budget_of_one_stops_after_boundary_call(self) -> None:
        """DEC-PROBE-BUDGET-PROLOGUE-001 regression: the hi-at-today prologue
        honours the budget — ``bisect=True, max_calls=1`` issues EXACTLY one
        vendor call and returns the SAME honest ABSENT verdict."""
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True, max_calls=1)
        assert result.verdict == VERDICT_ABSENT
        assert result.calls_used == 1
        assert client.calls == 1
        assert result.first_ts is None
        assert result.probed_floor is None
        assert "call budget (1) is exhausted before locating hi" in result.detail
        assert "true floor unknown" in result.detail

    def test_budget_zero_clamps_up_to_one_boundary_call(self) -> None:
        """``max_calls=0`` clamps UP to 1 — the boundary call is still made
        ("if uncertain, fetch"); no further call follows."""
        client = _DepthClient(date(2020, 1, 1))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True, max_calls=0)
        assert result.verdict == VERDICT_OK
        assert result.boundary_has_data is True
        assert result.calls_used == 1
        assert client.calls == 1

    def test_budget_zero_bisect_empty_boundary_stops_at_one(self) -> None:
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True, max_calls=0)
        assert result.verdict == VERDICT_ABSENT
        assert result.calls_used == 1
        assert client.calls == 1
        assert "call budget (0) is exhausted" in result.detail

    @given(
        max_calls=st.integers(min_value=0, max_value=25),
        bisect=st.booleans(),
        floor_offset=st.one_of(st.none(), st.integers(min_value=0, max_value=2000)),
    )
    def test_property_calls_used_never_exceeds_clamped_budget(
        self, max_calls: int, bisect: bool, floor_offset: int | None
    ) -> None:
        """Invariant: ``calls_used <= max(max_calls, 1)`` over random shapes."""
        boundary = date(2020, 1, 1)
        floor = None if floor_offset is None else boundary + timedelta(days=floor_offset)
        client = _DepthClient(floor)
        result = probe_depth(client, today=date(2026, 7, 2), bisect=bisect, max_calls=max_calls)
        assert result.calls_used <= max(max_calls, 1)
        assert client.calls == result.calls_used

    def test_first_row_ts_defensive_arms(self) -> None:
        from brutex.adapters.groww.probe import _date_of_first_ts, _first_row_ts

        assert _first_row_ts(None) is None
        assert _first_row_ts({"candles": "nope"}) is None
        assert _first_row_ts({"candles": ["2021-06-14T09:15:00"]}) == "2021-06-14T09:15:00"
        assert _date_of_first_ts(None) is None
        assert _date_of_first_ts("1623630300") is None
        assert _date_of_first_ts("2021-06-14T09:15:00") == date(2021, 6, 14)
