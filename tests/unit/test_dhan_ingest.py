"""Tests for :mod:`brutex.adapters.dhan.ingest` — the lean spot spine + honest degrade."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from brutex.adapters.dhan.constants import dhan_history_floor as constants_floor
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanSubscriptionError,
    DhanThrottleError,
    DhanTokenStillStaleError,
)
from brutex.adapters.dhan.ingest import (
    STATUS_FAILED,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_SUCCESS,
    DhanIngestReport,
    DhanWorkItem,
    clamp_dhan_window_end,
    dhan_index_exchange,
    plan_dhan_work,
    run_dhan_spot_ingest_with_retry,
    scan_resolved_all_empty,
)
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange
from structlog.testing import capture_logs

_EPOCH = datetime(2022, 6, 1, 3, 45, tzinfo=UTC)


def _bar() -> CandleBar:
    return CandleBar(
        timestamp=_EPOCH,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=0,
        open_interest=None,
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.appends: list[dict[str, object]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.appends.append(
            {"exchange": exchange, "segment": segment, "symbol": symbol, "n": len(bars)}
        )
        return SinkAppendResult(
            partitions_written=1,
            bars_appended=len(bars),
            write_results=(),
            bars_net_new=len(bars),
        )


class _FakeAdapter:
    """A fake MarketAdapter driven by a per-(symbol) scripted response queue."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self._script = script
        self.calls: list[str] = []

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        _ = (exchange, interval, start, end)
        self.calls.append(symbol)
        response = self._script[symbol].pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _no_filter(bars: Sequence[CandleBar]) -> SimpleNamespace:
    return SimpleNamespace(kept=list(bars))


def test_dhan_index_exchange_mapping() -> None:
    assert dhan_index_exchange("SENSEX") is Exchange.BSE
    assert dhan_index_exchange("NIFTY") is Exchange.NSE


def test_plan_work_resolves_and_omits_unresolved() -> None:
    work, unresolved = plan_dhan_work(
        symbols=["NIFTY", "MYSTERY"],
        security_ids={"NIFTY": 13},
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
    )
    assert unresolved == ["MYSTERY"]
    assert all(isinstance(item, DhanWorkItem) for item in work)
    assert {item.symbol for item in work} == {"NIFTY"}


def test_run_success_appends_and_reports() -> None:
    adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
    )
    assert report.total_bars == 1
    assert report.failed_windows == 0
    assert report.resolved_symbols == ("NIFTY",)
    assert sink.appends[0]["segment"] == "INDEX"
    assert [o.status for o in report.outcomes] == [STATUS_SUCCESS]


def test_run_unresolved_symbol_is_loud_skip_zero_bars() -> None:
    adapter = _FakeAdapter({})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["MYSTERY"],
        adapter=adapter,
        sink=sink,
        security_ids={},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
    )
    assert report.unresolved_symbols == ("MYSTERY",)
    assert report.total_bars == 0
    assert sink.appends == []  # never fetched — no fabricated bar
    assert [o.status for o in report.outcomes] == [STATUS_SKIPPED_UNRESOLVED]
    assert report.outcomes[0].exchange is Exchange.NSE


def test_run_retryable_failure_retried_then_succeeds() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanThrottleError("slow"), [_bar()]]})
    sink = _RecordingSink()
    backoffs: list[int] = []
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
        max_rounds=3,
        backoff=backoffs.append,
    )
    assert report.failed_windows == 0
    assert report.total_bars == 1
    assert adapter.calls == ["NIFTY", "NIFTY"]  # one retry
    assert backoffs == [0]  # one inter-round back-off


def test_run_terminal_failure_stays_failed_repullable() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanBadRequestError("813: bad security id")]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
        max_rounds=3,
    )
    assert report.failed_windows == 1
    assert adapter.calls == ["NIFTY"]  # NOT retried (terminal)
    assert report.outcomes[0].status == STATUS_FAILED
    assert report.outcomes[0].error_type == "DhanBadRequestError"


def test_run_retryable_exhausts_rounds_and_stays_failed() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanThrottleError("a"), DhanThrottleError("b")]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
        max_rounds=2,
    )
    assert report.failed_windows == 1
    assert report.outcomes[0].retryable is True
    assert adapter.calls == ["NIFTY", "NIFTY"]  # two rounds


def test_run_no_windows_when_range_empty() -> None:
    adapter = _FakeAdapter({"NIFTY": []})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 1),  # start >= end → no windows
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
    )
    assert report.outcomes == ()
    assert adapter.calls == []


def test_default_pace_and_session_filter_paths() -> None:
    # Exercise the DEFAULT pace (no-op) + the DEFAULT calendar session filter.
    adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        # no session_filter / pace / backoff → defaults
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
    )
    # The 2022-06-01 03:45 UTC (09:15 IST) bar is in-session → kept.
    assert report.total_bars == 1


def test_resolved_all_empty_symbol_flagged_and_warned() -> None:
    # A RESOLVED index whose only window returns ZERO bars (all clean-empty) is
    # suspicious (wrong secId / unverified servability) — flagged + one WARN.
    adapter = _FakeAdapter({"NIFTY": [[]]})  # one window, empty bars
    sink = _RecordingSink()
    with capture_logs() as logs:
        report = run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY"],
            adapter=adapter,
            sink=sink,
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
            today=date(
                2022, 7, 1
            ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
            session_filter=_no_filter,
        )
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert report.resolved_symbols == ("NIFTY",)
    assert scan_resolved_all_empty(report) == ("NIFTY",)
    warns = [e for e in logs if e.get("event") == "dhan.symbol_all_empty"]
    assert len(warns) == 1
    assert warns[0]["symbol"] == "NIFTY"
    assert warns[0]["log_level"] == "warning"


def test_scan_all_empty_excludes_failed_unresolved_and_nonempty() -> None:
    # NIFTY: clean-empty → flagged. BANKNIFTY: has a bar → NOT flagged.
    # SENSEX: a terminal failure (a FAILED window) → NOT flagged (failure is the
    # story, already surfaced per-window). MYSTERY: unresolved → NOT flagged.
    adapter = _FakeAdapter(
        {
            "NIFTY": [[]],
            "BANKNIFTY": [[_bar()]],
            "SENSEX": [DhanBadRequestError("813: bad security id")],
        }
    )
    sink = _RecordingSink()
    with capture_logs() as logs:
        report = run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY", "BANKNIFTY", "SENSEX", "MYSTERY"],
            adapter=adapter,
            sink=sink,
            security_ids={"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51},
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
            today=date(
                2022, 7, 1
            ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
            session_filter=_no_filter,
        )
    assert scan_resolved_all_empty(report) == ("NIFTY",)
    warned = {e["symbol"] for e in logs if e.get("event") == "dhan.symbol_all_empty"}
    assert warned == {"NIFTY"}


def test_scan_all_empty_empty_report_is_empty() -> None:
    # No resolved symbols / no outcomes → nothing flagged (guards the empty path).
    adapter = _FakeAdapter({})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["MYSTERY"],
        adapter=adapter,
        sink=sink,
        security_ids={},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        session_filter=_no_filter,
    )
    assert scan_resolved_all_empty(report) == ()


def test_scan_all_empty_resolved_without_outcomes_is_skipped() -> None:
    # Defensive: a report claiming a resolved symbol that has NO matching outcome
    # (never produced by the run path — every resolved symbol is processed) is
    # skipped, not flagged. Covers the `if not windows` guard.
    report = DhanIngestReport(
        outcomes=(),
        resolved_symbols=("GHOST",),
        unresolved_symbols=(),
    )
    assert scan_resolved_all_empty(report) == ()


# ─────────────────────────────────────────────────────────────────────────────
# Terminal-auth escalation (design §3): the pull-wide global-stop carve-out.
# ─────────────────────────────────────────────────────────────────────────────


class TestTerminalPullStop:
    """The FIRST terminal-auth/subscription/still-stale failure stops the WHOLE pull."""

    @staticmethod
    def _three_symbol_run(first_error: Exception) -> tuple[_FakeAdapter, _RecordingSink]:
        adapter = _FakeAdapter(
            {
                "NIFTY": [first_error],
                "BANKNIFTY": [[_bar()]],
                "SENSEX": [[_bar()]],
            }
        )
        return adapter, _RecordingSink()

    @pytest.mark.parametrize(
        "stop_error",
        [
            DhanAuthError("re-read budget exhausted"),
            DhanSubscriptionError("806: Data APIs not subscribed"),
            DhanTokenStillStaleError("re-read returned the same dead token"),
        ],
    )
    def test_first_terminal_stop_halts_spine_and_burns_no_later_window(
        self, stop_error: Exception
    ) -> None:
        """The window-count spy: the FIRST pull-stop failure propagates out of the
        spine and the remaining ~all windows are NEVER processed (pre-fix the spine
        quarantined it per-window and burned every remaining window against a dead
        credential / inactive subscription).
        """
        adapter, sink = self._three_symbol_run(stop_error)
        with pytest.raises(type(stop_error)):
            run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY", "BANKNIFTY", "SENSEX"],
                adapter=adapter,
                sink=sink,
                security_ids={"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51},
                start=date(2022, 6, 1),
                end=date(2022, 6, 20),
                today=date(
                    2022, 7, 1
                ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
                session_filter=_no_filter,
            )
        assert adapter.calls == ["NIFTY"]  # the remaining windows were never burned
        assert sink.appends == []  # nothing written after the stop

    def test_non_stop_terminal_failure_still_quarantines_per_window(self) -> None:
        # A bad-request terminal (813 etc.) is NOT a pull stop: the window fails,
        # the OTHER symbols still process (the pre-existing per-window posture).
        adapter, sink = self._three_symbol_run(DhanBadRequestError("813: bad id"))
        report = run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY", "BANKNIFTY", "SENSEX"],
            adapter=adapter,
            sink=sink,
            security_ids={"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51},
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
            today=date(
                2022, 7, 1
            ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
            session_filter=_no_filter,
        )
        assert report.failed_windows == 1
        assert adapter.calls == ["NIFTY", "BANKNIFTY", "SENSEX"]

    def test_lake_write_halt_propagates_out_of_the_spine(self) -> None:
        # LakeWriteHalt is NOT a DhanError → the spine's except never swallows it
        # (the global-stop posture works by construction — pinned, design §3).
        from brutex.data.errors import LakeWriteHalt

        class _HaltSink(_RecordingSink):
            def append(self, **kwargs: object) -> SinkAppendResult:
                raise LakeWriteHalt("disk_full")

        adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
        with pytest.raises(LakeWriteHalt):
            run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_HaltSink(),
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 20),
                today=date(
                    2022, 7, 1
                ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
                session_filter=_no_filter,
            )

    def test_daily_quota_exceeded_propagates_out_of_the_spine(self) -> None:
        # A pace hook raising the quota stop (the Dhan 100k/day tracker) is not a
        # DhanError either — it propagates to the job layer (design §3 pin).
        from brutex.orchestrator.quota_tracker import DailyQuotaExceeded

        def _quota_pace() -> None:
            raise DailyQuotaExceeded("daily quota exhausted")

        adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
        with pytest.raises(DailyQuotaExceeded):
            run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 20),
                today=date(
                    2022, 7, 1
                ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
                session_filter=_no_filter,
                pace=_quota_pace,
            )


# ─────────────────────────────────────────────────────────────────────────────
# The toDate-inclusivity BELT (design §4): drop_bars_after at the ingest tail.
# ─────────────────────────────────────────────────────────────────────────────


class TestToDateBelt:
    """Inclusive-vendor simulation: a bar dated ON the half-open ``end`` day is DROPPED."""

    def test_inclusive_vendor_bar_on_end_day_never_stored(self) -> None:
        """The bugfix-reversion shape (design §4): the fake returns one in-range bar
        PLUS one bar dated on the exclusive ``to_date`` day (what an INCLUSIVE
        vendor would hand back on the final window — on a prev-day-capped pull
        that day is TODAY'S forming session, which keep-first would freeze, the
        #837/#1774 class). With the belt REVERTED this test FAILS: the end-day
        bar reaches the sink.
        """
        in_range = _bar()  # 2022-06-01 03:45 UTC (window start day)
        end_day_bar = CandleBar(
            timestamp=datetime(2022, 6, 20, 3, 45, tzinfo=UTC),  # ON the exclusive end day
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=0,
            open_interest=None,
        )
        adapter = _FakeAdapter({"NIFTY": [[in_range, end_day_bar]]})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 20),  # half-open — 2022-06-20 is NOT servable
                today=date(
                    2022, 7, 1
                ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
                session_filter=_no_filter,
            )
        assert report.total_bars == 1  # only the in-range bar was stored
        assert sink.appends == [{"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "n": 1}]
        warns = [e for e in logs if e.get("event") == "dhan.future_bar_dropped"]
        assert len(warns) == 1
        assert warns[0]["dropped"] == 1
        assert warns[0]["symbol"] == "NIFTY"

    def test_bar_on_last_servable_day_is_kept(self) -> None:
        # A bar dated exactly end−1 (the last servable day) is KEPT — the belt is
        # strictly-after end−1, never a re-interpretation of the half-open range.
        last_day_bar = CandleBar(
            timestamp=datetime(2022, 6, 19, 3, 45, tzinfo=UTC),  # end − 1 day
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=0,
            open_interest=None,
        )
        adapter = _FakeAdapter({"NIFTY": [[last_day_bar]]})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 20),
                today=date(
                    2022, 7, 1
                ),  # frozen — the rolling floor must never eat these fixed 2022 ranges
                session_filter=_no_filter,
            )
        assert report.total_bars == 1
        assert [e for e in logs if e.get("event") == "dhan.future_bar_dropped"] == []


# ─────────────────────────────────────────────────────────────────────────────
# The rolling-floor clamp (DEC-DHAN-ROLLING-FLOOR-001): the ONE plan chokepoint.
# Frozen today = 2026-07-14 → edge 2021-07-14, floor 2021-07-21 (edge + 7d).
# ─────────────────────────────────────────────────────────────────────────────

_FROZEN_TODAY = date(2026, 7, 14)
_FROZEN_FLOOR = date(2021, 7, 21)


class _StartRecordingAdapter:
    """A fake adapter recording every requested (start, end) — the clamp tripwire spy."""

    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        _ = (symbol, exchange, interval)
        self.windows.append((start, end))
        return []


class TestRollingFloorClamp:
    def test_from_before_floor_clamped_with_info_and_report_stamps(self) -> None:
        adapter = _StartRecordingAdapter()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=date(2020, 1, 1),  # the blank-date default — before the floor
                end=date(2021, 8, 21),
                session_filter=_no_filter,
                today=_FROZEN_TODAY,
            )
        infos = [e for e in logs if e.get("event") == "dhan.window_floor_clamped"]
        assert len(infos) == 1  # ONE INFO per pull, at plan time
        assert infos[0]["requested_start"] == "2020-01-01"
        assert infos[0]["effective_start"] == _FROZEN_FLOOR.isoformat()
        assert infos[0]["floor"] == _FROZEN_FLOOR.isoformat()
        assert infos[0]["margin_days"] == 7
        # Report stamps computed from the SAME threaded today.
        assert report.requested_start == date(2020, 1, 1)
        assert report.effective_start == _FROZEN_FLOOR
        assert report.floor == _FROZEN_FLOOR
        assert report.entirely_pre_floor is False
        # The FIRST window STARTS at the floor — no window straddles it.
        assert adapter.windows[0][0] == _FROZEN_FLOOR

    def test_from_equal_floor_untouched_no_clamp_note(self) -> None:
        # The margin edge: from == edge + 7d EXACTLY → untouched, no INFO.
        adapter = _StartRecordingAdapter()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=_FROZEN_FLOOR,
                end=date(2021, 8, 21),
                session_filter=_no_filter,
                today=_FROZEN_TODAY,
            )
        assert [e for e in logs if e.get("event") == "dhan.window_floor_clamped"] == []
        assert report.effective_start == report.requested_start == _FROZEN_FLOOR
        assert adapter.windows[0][0] == _FROZEN_FLOOR

    def test_from_after_floor_untouched(self) -> None:
        adapter = _StartRecordingAdapter()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=date(2024, 1, 1),
                end=date(2024, 2, 1),
                session_filter=_no_filter,
                today=_FROZEN_TODAY,
            )
        assert [e for e in logs if e.get("event") == "dhan.window_floor_clamped"] == []
        assert report.effective_start == date(2024, 1, 1)
        assert adapter.windows[0][0] == date(2024, 1, 1)

    def test_entirely_pre_floor_plans_zero_windows_with_one_warning(self) -> None:
        # The spine's defensive-totality arm (headless callers): end <= floor plans
        # ZERO windows — never doomed requests recorded as clean-empty SUCCESS.
        adapter = _StartRecordingAdapter()
        with capture_logs() as logs:
            report = run_dhan_spot_ingest_with_retry(
                symbols=["NIFTY"],
                adapter=adapter,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=date(2020, 1, 1),
                end=date(2020, 6, 1),  # wholly before the 2021-07-21 floor
                session_filter=_no_filter,
                today=_FROZEN_TODAY,
            )
        warns = [e for e in logs if e.get("event") == "dhan.pull_entirely_pre_floor"]
        assert len(warns) == 1
        assert warns[0]["floor"] == _FROZEN_FLOOR.isoformat()
        assert warns[0]["log_level"] == "warning"
        assert adapter.windows == []  # ZERO vendor requests
        assert report.entirely_pre_floor is True
        assert report.effective_start is None
        assert report.outcomes == ()

    def test_clamp_tripwire_no_request_before_floor_across_full_pull(self) -> None:
        """The BELT is this TEST (design §1, B's ruling — no adapter-level runtime
        belt): a full multi-symbol pull over the blank-date default range issues
        ZERO requests with ``from_date < floor`` under a frozen today.
        """
        adapter = _StartRecordingAdapter()
        run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"],
            adapter=adapter,
            sink=_RecordingSink(),
            security_ids={"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "INDIAVIX": 21},
            start=date(2020, 1, 1),
            end=date(2026, 7, 14),  # the full default range → many 90-day tiles
            session_filter=_no_filter,
            today=_FROZEN_TODAY,
        )
        assert adapter.windows, "the pull must have planned windows"
        assert all(start >= _FROZEN_FLOOR for start, _ in adapter.windows), (
            f"a request crossed the floor: {sorted(self_w for self_w in adapter.windows)[:3]}"
        )

    def test_plan_dhan_work_today_none_resolves_now_ist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default arm: today=None resolves now_ist().date() ONCE inside the plan.
        import brutex.adapters.dhan.ingest as ingest_mod

        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(ingest_mod, "now_ist", lambda: frozen)
        work, unresolved = plan_dhan_work(
            symbols=["NIFTY"],
            security_ids={"NIFTY": 13},
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2026, 7, 14),
        )
        assert unresolved == []
        assert min(item.start for item in work) == constants_floor(frozen.date())

    def test_ist_date_pin_late_utc_evening_is_next_ist_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The IST pin (design §11): a frozen 23:00 UTC clock is ALREADY the next
        IST day (+05:30 → 04:30 next day), so the floor keys on the IST date, not
        the UTC date — the real now_ist() returns an IST-zoned datetime whose
        .date() IS the IST day; this pins the plan's default arm consuming it.
        """
        from zoneinfo import ZoneInfo

        import brutex.adapters.dhan.ingest as ingest_mod

        # 2026-07-14 23:00 UTC == 2026-07-15 04:30 IST.
        frozen_utc = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
        frozen_ist = frozen_utc.astimezone(ZoneInfo("Asia/Kolkata"))
        assert frozen_ist.date() == date(2026, 7, 15)  # the NEXT IST day
        monkeypatch.setattr(ingest_mod, "now_ist", lambda: frozen_ist)
        work, _ = plan_dhan_work(
            symbols=["NIFTY"],
            security_ids={"NIFTY": 13},
            interval="1minute",
            start=date(2020, 1, 1),
            end=date(2026, 7, 14),
        )
        # floor keys on the IST 2026-07-15, one day past the UTC date's floor.
        assert min(item.start for item in work) == constants_floor(date(2026, 7, 15))


class TestTodayEndClamp:
    """`clamp_dhan_window_end` — the DEC-DHAN-TODAY-CLAMP-001 prev-day END law.

    BOTH Dhan window laws (`plan_dhan_work` + the native-daily
    `plan_dhan_daily_work`) consult this ONE helper: a blank-default pull
    (half-open ``end == today``) is BYTE-IDENTICAL (no clamp, no note); an
    explicit ``to_date=today`` (half-open ``end == today + 1``) is CLAMPED to
    ``today`` — the last INCLUSIVE day requested from the vendor is the
    PREVIOUS day — with ONE honest ``dhan.window_end_clamped`` INFO.
    """

    def test_end_at_today_is_byte_identical_no_note(self) -> None:
        # The blank-default shape: half-open end == today — no clamp, no note.
        with capture_logs() as logs:
            assert clamp_dhan_window_end(_FROZEN_TODAY, today=_FROZEN_TODAY) == _FROZEN_TODAY
        assert [e for e in logs if e["event"] == "dhan.window_end_clamped"] == []

    def test_end_before_today_is_byte_identical_no_note(self) -> None:
        earlier = date(2026, 7, 1)
        with capture_logs() as logs:
            assert clamp_dhan_window_end(earlier, today=_FROZEN_TODAY) == earlier
        assert [e for e in logs if e["event"] == "dhan.window_end_clamped"] == []

    def test_end_past_today_clamps_to_today_with_one_info(self) -> None:
        # The explicit to_date=today shape: half-open end == today + 1 → clamped.
        requested = date(2026, 7, 15)  # _FROZEN_TODAY + 1 day
        with capture_logs() as logs:
            assert clamp_dhan_window_end(requested, today=_FROZEN_TODAY) == _FROZEN_TODAY
        notes = [e for e in logs if e["event"] == "dhan.window_end_clamped"]
        assert len(notes) == 1
        assert notes[0]["requested_end"] == "2026-07-15"
        assert notes[0]["effective_end"] == "2026-07-14"
        assert notes[0]["log_level"] == "info"

    def test_plan_dhan_work_explicit_today_end_last_day_is_prev_day(self) -> None:
        # The 1-minute plan law consults the clamp: an explicit to_date=today
        # (half-open end == today + 1) plans windows whose HALF-OPEN end is
        # today — i.e. the last inclusive vendor day is the PREVIOUS day.
        with capture_logs() as logs:
            work, unresolved = plan_dhan_work(
                symbols=["NIFTY"],
                security_ids={"NIFTY": 13},
                interval="1minute",
                start=date(2026, 6, 1),
                end=date(2026, 7, 15),  # today + 1 — the explicit to_date=today shape
                today=_FROZEN_TODAY,
            )
        assert unresolved == []
        assert work, "the plan must have produced windows"
        assert max(item.end for item in work) == _FROZEN_TODAY
        notes = [e for e in logs if e["event"] == "dhan.window_end_clamped"]
        assert len(notes) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Ledger-less crash-recovery convergence (design §6, condition 1 of the
# DEC-FEED-SPINE-GENERALIZE-001 deferral re-ratification).
# ─────────────────────────────────────────────────────────────────────────────


class _DeterministicAdapter:
    """Serves one deterministic bar per (symbol, window-start) — replay-stable."""

    def __init__(self, *, crash_after: int | None = None) -> None:
        self.crash_after = crash_after
        self.calls = 0

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        _ = (exchange, interval, end)
        self.calls += 1
        if self.crash_after is not None and self.calls > self.crash_after:
            raise RuntimeError("simulated crash (SIGKILL-shaped: not a DhanError)")
        # One bar at 09:15 IST on the window's first day, price keyed on the window.
        ts = datetime(start.year, start.month, start.day, 3, 45, tzinfo=UTC)
        price = Decimal(100 + (start.toordinal() % 50))
        return [
            CandleBar(
                timestamp=ts,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=0,
                open_interest=None,
            )
        ]


def _tree_bytes(root: object) -> dict[str, bytes]:
    from pathlib import Path as PathAlias

    base = PathAlias(str(root))
    return {
        str(f.relative_to(base)): f.read_bytes() for f in sorted(base.rglob("*")) if f.is_file()
    }


def test_crash_convergence_interrupted_then_full_rerun_matches_control(tmp_path: object) -> None:
    """Interrupt a REAL-sink pull after N windows → whole-range re-run → the
    ``feeds/dhan/`` tree is BYTE-IDENTICAL to an uninterrupted control run.

    The ledger-less deferral's honest envelope (DEC-FEED-SPINE-GENERALIZE-001):
    re-pull is a whole-range operator re-run, idempotent via the keep-first sink —
    this test IS the convergence proof (a crash loses no durability, a re-run
    restores completeness, bars never duplicate or restate).
    """
    from pathlib import Path as PathAlias

    from brutex.data.feed_root import feed_lake_root
    from brutex.feeds.binding import build_feed_sink
    from brutex.feeds.dhan_binding import build_dhan_binding

    base = PathAlias(str(tmp_path))
    frozen_today = date(2026, 7, 14)
    span = {"start": date(2024, 1, 1), "end": date(2024, 12, 31)}  # 4+ windows/symbol

    def _run(lake: PathAlias, adapter: _DeterministicAdapter) -> None:
        run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY", "BANKNIFTY"],
            adapter=adapter,
            sink=build_feed_sink(lake_root=lake, binding=build_dhan_binding()),
            security_ids={"NIFTY": 13, "BANKNIFTY": 25},
            start=span["start"],
            end=span["end"],
            session_filter=_no_filter,
            today=frozen_today,
        )

    # CONTROL: one uninterrupted run.
    control = base / "control"
    _run(control, _DeterministicAdapter())

    # INTERRUPTED: crash after 3 windows (mid-pull), then a full re-run.
    crashed = base / "crashed"
    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(crashed, _DeterministicAdapter(crash_after=3))
    partial = _tree_bytes(feed_lake_root(crashed, feed="dhan") / "bars")
    assert partial, "the interrupted run must have durably written its first windows"
    _run(crashed, _DeterministicAdapter())  # the whole-range operator re-run

    control_tree = _tree_bytes(feed_lake_root(control, feed="dhan") / "bars")
    crashed_tree = _tree_bytes(feed_lake_root(crashed, feed="dhan") / "bars")
    assert control_tree.keys() == crashed_tree.keys()
    for rel, payload in control_tree.items():
        assert crashed_tree[rel] == payload, f"byte divergence at {rel}"
