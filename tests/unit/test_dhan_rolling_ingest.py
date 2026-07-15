"""Tests for the lean Dhan rolling-ATM option ingest spine.

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanError,
    DhanSubscriptionError,
    DhanThrottleError,
    DhanTokenStillStaleError,
)
from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
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
    """A client returning a fixed payload, or raising a scripted error sequence.

    ``errors`` is consumed one entry per call; a ``None`` entry means THAT call
    succeeds (so a mid-pull failure can be scripted after a healthy window).
    """

    def __init__(
        self, payload: Mapping[str, object], *, errors: list[Exception | None] | None = None
    ) -> None:
        self.payload = payload
        self.errors = list(errors or [])
        self.calls = 0

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        self.calls += 1
        if self.errors:
            scripted = self.errors.pop(0)
            if scripted is not None:
                raise scripted
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
        keys=[_KEY, _KEY2], interval="1minute", start=date(2024, 6, 1), end=date(2024, 7, 16)
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
    )
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert report.outcomes[0].status == STATUS_SUCCESS


# ── run: honest failures + retry ─────────────────────────────────────────────


def test_run_terminal_failure_is_not_retried(tmp_path: Path) -> None:
    # A PER-WINDOW terminal failure (the DH-905 bad-request/servability class — NOT
    # an is_terminal_pull_stop member) folds into FAILED and is never retried in-run.
    client = _FakeClient(
        _PAYLOAD, errors=[DhanBadRequestError("DH-905"), DhanBadRequestError("DH-905")]
    )
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        max_rounds=3,
    )
    assert report.failed_windows == 1
    outcome = report.outcomes[0]
    assert outcome.status == STATUS_FAILED
    assert outcome.retryable is False
    assert outcome.error_type == "DhanBadRequestError"
    # DH-905 is terminal → the client was called exactly ONCE (no retry).
    assert client.calls == 1


def test_run_nonterminal_error_folds_per_window_and_pull_continues(tmp_path: Path) -> None:
    """Regression guard (DEC-DHAN-ROLLING-TERMINAL-STOP-001): a NON-pull-stop
    ``DhanError`` still folds into a per-window FAILED and the pull CONTINUES to the
    next window — the terminal carve-out never widens past ``is_terminal_pull_stop``.
    """
    client = _FakeClient(_PAYLOAD, errors=[DhanBadRequestError("DH-905")])
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY, _KEY2],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
        max_rounds=1,
    )
    # Window 1 FAILED per-window; window 2 still fetched + written.
    assert client.calls == 2
    assert report.failed_windows == 1
    assert report.total_bars == 1
    statuses = {o.key: o.status for o in report.outcomes}
    assert statuses[_KEY] == STATUS_FAILED
    assert statuses[_KEY2] == STATUS_SUCCESS


@pytest.mark.parametrize(
    "terminal",
    [
        DhanAuthError("807: token expired"),
        DhanTokenStillStaleError("DH-901: re-read returned the same dead token"),
        DhanSubscriptionError("806: Data-API subscription inactive"),
    ],
    ids=["auth", "token-still-stale", "subscription"],
)
def test_run_terminal_pull_stop_halts_and_never_burns_later_windows(
    tmp_path: Path, terminal: DhanError
) -> None:
    """DEC-DHAN-ROLLING-TERMINAL-STOP-001 (the spot spine's exact mirror): a terminal
    pull-stop raised on window 2 of 3 PROPAGATES on its first occurrence — windows
    3..N are NEVER fetched, nothing is marked retried, no report is fabricated.
    """
    key3 = RollingOptionKey(
        underlying="SENSEX",
        exchange="BSE",
        exchange_segment="BSE_FNO",
        security_id=51,
        expiry_flag="WEEK",
        expiry_code=0,
        strike_offset="ATM-1",
        option_type="CALL",
    )
    # Call 1 succeeds; call 2 raises the terminal pull-stop; call 3 must never happen.
    client = _FakeClient(_PAYLOAD, errors=[None, terminal])
    with pytest.raises(type(terminal)):
        run_dhan_rolling_option_ingest_with_retry(
            keys=[_KEY, _KEY2, key3],
            adapter=_adapter(client),
            writer=RollingOptionWriter(lake_root=tmp_path),
            start=date(2024, 6, 1),
            end=date(2024, 6, 15),
            max_rounds=3,
        )
    # The pull HALTED at the raising window: exactly 2 fetches — key3's window was
    # never fetched and no round retried the terminal error.
    assert client.calls == 2


def test_run_retryable_failure_then_success_uses_default_backoff(tmp_path: Path) -> None:
    client = _FakeClient(_PAYLOAD, errors=[DhanThrottleError("429")])
    report = run_dhan_rolling_option_ingest_with_retry(
        keys=[_KEY],
        adapter=_adapter(client),
        writer=RollingOptionWriter(lake_root=tmp_path),
        start=date(2024, 6, 1),
        end=date(2024, 6, 15),
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
