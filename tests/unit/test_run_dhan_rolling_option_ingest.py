"""Tests for the lean Dhan rolling-ATM option ingest spine.

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from brutex.adapters.dhan.constants import dhan_rolling_options_floor
from brutex.adapters.dhan.errors import DhanSubscriptionError, DhanThrottleError
from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.adapters.dhan.rolling_option_chunks import window_days_for_rolling_interval
from brutex.adapters.dhan.rolling_option_parser import RollingOptionBar
from brutex.adapters.dhan.rolling_option_plan import RollingOptionKey
from brutex.adapters.dhan.rolling_option_writer import RollingOptionWriter
from brutex.adapters.dhan.run_dhan_rolling_option_ingest import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    RollingOptionIngestReport,
    RollingOptionWindowOutcome,
    coordinate_for_key,
    plan_rolling_option_work,
    run_dhan_rolling_option_ingest_with_retry,
)
from brutex.adapters.ingest_common import plan_windows
from structlog.testing import capture_logs

#: Frozen IST "today" for every fixed-date plan/spine call — at this date the
#: unified rolling-options floor (DEC-DHAN-ROLLING-FLOOR-UNIFY-001) resolves to
#: the requested 2022-06-01 start, safely below the fixtures' 2024 ranges.
_FROZEN_TODAY = date(2026, 7, 15)

_KEY = RollingOptionKey(
    underlying="NIFTY",
    exchange="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
    option_type="CALL",
)
_KEY2 = RollingOptionKey(
    underlying="BANKNIFTY",
    exchange="NSE",
    exchange_segment="NSE_FNO",
    security_id=25,
    expiry_flag="MONTH",
    expiry_code=1,
    strike_offset="ATM+1",
    option_type="PUT",
)

_PAYLOAD: dict[str, list[object]] = {
    "timestamp": [1_717_386_300.0],
    "open": [100],
    "high": [102],
    "low": [99],
    "close": [101],
}


class _FakeClient:
    """A client returning a fixed payload, or raising a scripted error sequence."""

    def __init__(
        self, payload: Mapping[str, object], *, errors: list[Exception] | None = None
    ) -> None:
        self.payload = payload
        self.errors = list(errors or [])
        self.calls = 0

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.payload


def _adapter(client: _FakeClient) -> RollingOptionAdapter:
    return RollingOptionAdapter(client=client)


# ── coordinate_for_key ────────────────────────────────────────────────────────


def test_coordinate_for_key_echoes_the_selectors() -> None:
    coord = coordinate_for_key(_KEY2)
    assert coord.exch_id == "NSE"
    assert coord.exchange_segment == "NSE_FNO"
    assert coord.security_id == 25
    assert coord.underlying_symbol == "BANKNIFTY"
    assert coord.expiry_flag == "MONTH"
    assert coord.expiry_code == 1
    assert coord.strike_offset == "ATM+1"


# ── plan_rolling_option_work ─────────────────────────────────────────────────


def test_plan_work_tiles_each_key() -> None:
    work = plan_rolling_option_work(
        keys=[_KEY, _KEY2],
        interval="1minute",
        start=date(2024, 6, 1),
        end=date(2024, 7, 16),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
    )
    # 45-day span → 2 windows per key × 2 keys = 4 items.
    assert len(work) == 4
    assert {w.key for w in work} == {_KEY, _KEY2}
    assert work[0].start == date(2024, 6, 1)


# ── run: success ──────────────────────────────────────────────────────────────


def test_run_success_writes_bars(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD)
    writer = RollingOptionWriter(lake_root=tmp_path)
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=writer,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
    )
    assert isinstance(report, RollingOptionIngestReport)
    assert report.market == "dhan-option"
    assert report.total_bars == 1
    assert report.failed_windows == 0
    assert all(o.status == STATUS_SUCCESS for o in report.outcomes)
    assert list(tmp_path.glob("rolling_options/**/*.parquet"))


def test_run_empty_key_returns_success_zero_bars(tmp_path: Path) -> None:
    # EMPTY-FROM-DHAN: a key returning zero bars is a clean SUCCESS with 0 bars.
    client = _FakeClient({})  # empty payload → honest no-data
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
    )
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert report.outcomes[0].status == STATUS_SUCCESS


# ── run: honest failures + retry ─────────────────────────────────────────────


def test_run_terminal_failure_is_not_retried(tmp_path: Path) -> None:
    client = _FakeClient(
        _PAYLOAD, errors=[DhanSubscriptionError("DH-905"), DhanSubscriptionError("DH-905")]
    )
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
        max_rounds=3,
    )
    assert report.failed_windows == 1
    outcome = report.outcomes[0]
    assert outcome.status == STATUS_FAILED
    assert outcome.retryable is False
    assert outcome.error_type == "DhanSubscriptionError"
    # DH-905 is terminal → the client was called exactly ONCE (no retry).
    assert client.calls == 1


def test_run_retryable_failure_then_success_uses_default_backoff(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD, errors=[DhanThrottleError("429")])
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
        max_rounds=2,
    )
    # Round 1 throttled (retryable) → round 2 succeeds.
    assert report.failed_windows == 0
    assert report.total_bars == 1
    assert client.calls == 2


def test_run_retryable_exhausts_rounds_and_backoff_hook(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD, errors=[DhanThrottleError("429"), DhanThrottleError("429")])
    backoffs: list[int] = []
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
        max_rounds=2,
        backoff=backoffs.append,
    )
    # Both rounds throttled → still FAILED (retryable), one back-off between rounds.
    assert report.failed_windows == 1
    assert report.outcomes[0].retryable is True
    assert backoffs == [0]  # backoff fired once, after round 0


def test_run_injected_session_filter_and_pace(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD)
    paced: list[int] = []

    def drop_all(bars: list[RollingOptionBar]) -> list[RollingOptionBar]:
        return []

    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
        session_filter=drop_all,
        pace=lambda: paced.append(1),
    )
    # Session filter dropped every bar → SUCCESS with 0 written.
    assert report.total_bars == 0
    assert report.outcomes[0].status == STATUS_SUCCESS
    assert paced == [1]  # pace hook invoked once


def test_run_no_keys_is_empty_report(tmp_path: Path) -> None:
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[],
        adapter=_adapter(_FakeClient(_PAYLOAD)),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
    )
    assert report.outcomes == ()
    assert report.keys == ()
    assert report.total_bars == 0


def test_run_max_rounds_floor_is_one(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD)
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        today=_FROZEN_TODAY,  # frozen — the unified floor must never eat these fixed 2024 ranges
        max_rounds=0,  # clamped to 1
    )
    assert report.outcomes[0].status == STATUS_SUCCESS


def test_window_outcome_and_report_helpers() -> None:
    o1 = RollingOptionWindowOutcome(
        key=_KEY,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        status=STATUS_SUCCESS,
        bars_appended=3,
    )
    o2 = RollingOptionWindowOutcome(
        key=_KEY,
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        status=STATUS_FAILED,
        bars_appended=0,
        retryable=True,
        error_type="DhanThrottleError",
    )
    report = RollingOptionIngestReport(outcomes=(o1, o2), keys=(_KEY,))
    assert report.total_bars == 3
    assert report.failed_windows == 1


# ─────────────────────────────────────────────────────────────────────────────
# The UNIFIED rolling-options floor at the ONE plan chokepoint
# (DEC-DHAN-ROLLING-FLOOR-UNIFY-001 — the plan_dhan_work posture, options twin).
# ─────────────────────────────────────────────────────────────────────────────


def test_plan_unified_floor_is_byte_identical_at_adoption() -> None:
    """Frozen 2026-07-15: max(2021-07-22, 2022-06-01) = 2022-06-01, so a plan whose
    start sits AT the resolved floor is BYTE-IDENTICAL to the raw pre-law tiling
    (the adoption pin — the unified law changes NO date today)."""
    assert dhan_rolling_options_floor(_FROZEN_TODAY) == date(2022, 6, 1)
    work = plan_rolling_option_work(
        keys=[_KEY],
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2022, 7, 16),
        today=_FROZEN_TODAY,
    )
    # The pre-law oracle: the verbatim plan_windows tiling from the REQUESTED start
    # (the exact expression the chokepoint ran before the clamp landed).
    oracle = plan_windows(
        datetime.combine(date(2022, 6, 1), time.min),
        datetime.combine(date(2022, 7, 16), time.min),
        window_days=window_days_for_rolling_interval("1minute"),
    )
    assert [(w.start, w.end) for w in work] == [
        (date.fromisoformat(o.start[:10]), date.fromisoformat(o.end[:10])) for o in oracle
    ]
    assert work[0].start == date(2022, 6, 1)  # no clamp — max() resolved the same date


def test_plan_pre_floor_start_is_clamped_with_one_info() -> None:
    # The route's blank-date default start (2020-01-01) sits before the unified
    # floor → the first window STARTS at the floor; ONE clamp INFO per plan.
    with capture_logs() as logs:
        work = plan_rolling_option_work(
            keys=[_KEY, _KEY2],
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2022, 8, 1),
            today=_FROZEN_TODAY,
        )
    assert work, "post-floor span must still plan windows"
    assert min(item.start for item in work) == date(2022, 6, 1)
    infos = [e for e in logs if e.get("event") == "dhan.rolling_option.window_floor_clamped"]
    assert len(infos) == 1  # ONE INFO per plan, never per window/key
    assert infos[0]["requested_start"] == "2020-01-01"
    assert infos[0]["effective_start"] == "2022-06-01"
    assert infos[0]["floor"] == "2022-06-01"


def test_plan_entirely_pre_floor_plans_zero_windows_with_one_warning() -> None:
    # end <= floor → ZERO work items — never doomed pre-floor requests recorded
    # as clean-empty SUCCESS windows (the spot pull_entirely_pre_floor posture).
    with capture_logs() as logs:
        work = plan_rolling_option_work(
            keys=[_KEY],
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2022, 6, 1),  # the EXCLUSIVE end ON the floor — wholly pre-floor
            today=_FROZEN_TODAY,
        )
    assert work == []
    warns = [e for e in logs if e.get("event") == "dhan.rolling_option.pull_entirely_pre_floor"]
    assert len(warns) == 1
    assert warns[0]["floor"] == "2022-06-01"
    assert warns[0]["log_level"] == "warning"


def test_plan_binding_term_info_fires_only_when_rolling_term_binds() -> None:
    # Frozen ADOPTION today: the requested term binds → NO binding INFO (the two
    # laws have not diverged; the INFO is the divergence signal, emit-on-binding).
    with capture_logs() as logs:
        plan_rolling_option_work(
            keys=[_KEY],
            interval="1minute",
            start=date(2024, 6, 1),
            end=date(2024, 6, 15),
            today=_FROZEN_TODAY,
        )
    assert [
        e for e in logs if e.get("event") == "dhan.rolling_option.floor_rolling_term_binding"
    ] == []
    # A synthetic FUTURE today: dhan_history_floor(2028-08-01)=2023-08-08 strictly
    # exceeds the requested 2022-06-01 start → the rolling term binds; ONE INFO
    # per plan and the clamp applies the ROLLING floor.
    with capture_logs() as logs:
        work = plan_rolling_option_work(
            keys=[_KEY],
            interval="1minute",
            start=date(2022, 6, 1),
            end=date(2023, 9, 1),
            today=date(2028, 8, 1),
        )
    infos = [e for e in logs if e.get("event") == "dhan.rolling_option.floor_rolling_term_binding"]
    assert len(infos) == 1
    assert infos[0]["rolling_floor"] == "2023-08-08"
    assert infos[0]["requested_start"] == "2022-06-01"
    assert infos[0]["floor"] == "2023-08-08"
    assert min(item.start for item in work) == date(2023, 8, 8)  # the flip pin


def test_plan_crossover_equal_terms_emits_no_binding_info() -> None:
    # The exact crossover day (dhan_history_floor(2027-05-25) == 2022-06-01): equal
    # terms are NOT a divergence — strictly-greater only (the max() of equals is
    # still the adoption value).
    with capture_logs() as logs:
        work = plan_rolling_option_work(
            keys=[_KEY],
            interval="1minute",
            start=date(2022, 6, 1),
            end=date(2022, 7, 1),
            today=date(2027, 5, 25),
        )
    assert [
        e for e in logs if e.get("event") == "dhan.rolling_option.floor_rolling_term_binding"
    ] == []
    assert work[0].start == date(2022, 6, 1)


def test_plan_today_none_resolves_now_ist_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default arm: today=None resolves now_ist().date() ONCE inside the plan
    # (the spot plan_dhan_work precedent) — frozen at the module seam.
    import brutex.adapters.dhan.run_dhan_rolling_option_ingest as roll_mod

    frozen = datetime(2028, 8, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(roll_mod, "now_ist", lambda: frozen)
    work = plan_rolling_option_work(
        keys=[_KEY],
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2023, 9, 1),
    )
    assert min(item.start for item in work) == date(2023, 8, 8)  # the 2028-08-01 floor


def test_with_retry_threads_today_to_the_plan_chokepoint(tmp_path: Path) -> None:
    # The spine threads the surface-resolved today straight to the plan — a
    # future frozen today clamps the issued windows through the WHOLE run.
    client = _FakeClient(_PAYLOAD)
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2022, 6, 1),
        end=date(2023, 9, 5),
        today=date(2028, 8, 1),
    )
    assert report.outcomes, "post-floor span must still pull"
    assert min(o.start for o in report.outcomes) == date(2023, 8, 8)
