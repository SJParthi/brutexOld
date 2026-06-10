"""Regression tests for DEC-OPTIONS-UNTIL-CLEAN-001 — options window-guarantee parity.

The options pull now carries the SAME escalating guarantee stack as spot:
the shared AuthGuard fail-fast latch (trip-at-source + fan-out short-circuit),
honest aborted/skipped series accounting (an all-or-nothing abort is never a
completion), the re-pull-until-clean auto-resume loop over the SHARED window
ledger (``stop_when_stuck`` OFF — hostile finding 1), the mid-round
``DailyQuotaExceeded`` → ``quota_exhausted`` verdict mapping with the
``finally``-guaranteed ledger persist (hostile finding 2), and the loud
``PullVerdict`` built from the REPORT (hostile finding 5). All offline: fake
clients, no network, no real sleeps (the in-run retry backoff is monkeypatched
to a no-op and the inter-round sleep is injected).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan import option_ingest as option_ingest_module
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanRateLimitError,
)
from brutex.adapters.dhan.option_ingest import JsonPoster, OptionBarSink
from brutex.adapters.dhan.option_universe_ingest import (
    OptionUnderlying,
    run_dhan_options_ingest,
)
from brutex.dashboard.option_pull_controller import pull_options
from brutex.data.models import OptionBar
from brutex.data.window_ledger_store import ledger_path, load_window_ledger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15, 15:30).
_TS_IN = 1609732800

#: Injected "now" → the pull range collapses to [2021-01-01, 2021-01-05] —
#: ONE 30-day window per series, so the fan-out stays tiny + fast.
_NOW = datetime(2021, 1, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_real_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the in-run cursor retry backoff so 429 tests never really sleep."""
    monkeypatch.setattr(option_ingest_module, "_default_retry_backoff", lambda _i: None)


def _payload() -> dict[str, object]:
    """A one-in-session-bar CE side; PE null — the rolling-option shape."""
    return {
        "data": {
            "ce": {
                "timestamp": [_TS_IN],
                "open": [100],
                "high": [110],
                "low": [90],
                "close": [105],
                "volume": [250],
                "oi": [1200],
                "strike": [17500],
                "spot": [17480.5],
                "iv": [0.15],
            },
            "pe": None,
        }
    }


class _Sink:
    """Counts appended bars per series (the OptionBarSink seam)."""

    def __init__(self) -> None:
        self.series = 0

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        self.series += 1
        return len(bars)


class _SerialRun:
    """A deterministic, synchronous RunParallel fake (mirrors the pool's capture)."""

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterable[ItemResult[ItemT, ResultT]]:
        out: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            try:
                out.append(ItemResult(item=item, ok=True, result=fn(item), exc=None))
            except Exception as exc:
                out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        return out


def _underlying() -> OptionUnderlying:
    """A MONTH-only ATM-only underlying — the smallest fan-out unit."""
    return OptionUnderlying(
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        strike_offsets=0,
        expiry_flags=("MONTH",),
    )


def _stock_instrument() -> Instrument:
    """A spot equity underlying (EQ → OPTSTK / ATM±3 / MONTH-only)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1333,
        symbol_name="HDFCBANK",
        display_name="HDFCBANK",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE040A01034",
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=550,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class TestAuthGuardFanout:
    """The dead-token latch: trip-at-source + short-circuit + one loud alert."""

    def test_auth_death_short_circuits_fanout(self) -> None:
        """First auth failure stops the doomed-call flood: 1 call, the rest skip."""

        class _AuthDeadClient:
            def __init__(self) -> None:
                self.calls = 0

            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                self.calls += 1
                raise DhanAuthError("HTTP 401 unauthorized: token expired")

        client = _AuthDeadClient()
        # 3 codes × 2 types × ATM-only = 6 series; serial runner ⇒ deterministic.
        report = run_dhan_options_ingest(
            client,
            _Sink(),
            underlyings=[_underlying()],
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            expiry_codes=(1, 2, 3),
            option_types=("CALL", "PUT"),
            run_parallel=_SerialRun(),
        )
        assert client.calls == 1  # series 1 fires once; series 2..6 never call Dhan
        assert report.auth_aborted is True
        assert report.auth_cause is not None and "401" in report.auth_cause
        assert report.series_skipped_auth == 5
        assert report.failed_series == 1
        assert report.real_failed_series == 1
        assert report.no_data_series == 0
        assert report.series_count == 0

    def test_auth_alert_fires_exactly_once_under_racing_workers(self) -> None:
        """Two workers past the latch check both fail auth — ONE loud alert, no tear."""
        barrier = threading.Barrier(2)

        class _RacingAuthClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                barrier.wait(timeout=10)  # both workers in-flight BEFORE either trips
                raise DhanAuthError("HTTP 401 unauthorized: token expired")

        with capture_logs() as logs:
            report = run_dhan_options_ingest(
                _RacingAuthClient(),
                _Sink(),
                underlyings=[_underlying()],
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                expiry_codes=(1,),
                option_types=("CALL", "PUT"),
                run_parallel=PoolRunParallel(max_workers=2),
            )
        alerts = [e for e in logs if e["event"] == "ingest.auth_aborted"]
        assert len(alerts) == 1  # the flood-stopper: one alert, later failures silent
        assert report.auth_aborted is True
        assert report.failed_series == 2
        assert report.series_skipped_auth == 0

    def test_aborted_series_is_never_a_completion(self) -> None:
        """An all-or-nothing abort is residual work, not a completed series."""

        class _OneSeries429Client:
            def __init__(self) -> None:
                self.calls = 0

            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint
                self.calls += 1
                if body["expiryCode"] == 2:
                    raise DhanRateLimitError("HTTP 429 too many requests")
                return _payload()

        report = run_dhan_options_ingest(
            _OneSeries429Client(),
            _Sink(),
            underlyings=[_underlying()],
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            expiry_codes=(1, 2),
            option_types=("CALL",),
            run_parallel=_SerialRun(),
        )
        assert report.series_count == 2  # both reported …
        assert report.aborted_series == 1  # … but the 429-stormed one is ABORTED
        assert report.still_failed_windows == 1  # its window is the round residual
        assert report.bars_appended == 1  # only the healthy series committed
        assert report.failed_series == 0  # an abort is NOT a quarantine

    def test_no_data_series_completes_gap_only_not_failure(self) -> None:
        """A DH-905 series completes GAP-ONLY in one pass — never a quarantine.

        DEC-OPTION-DH905-CONTINUE-001 (supersedes the quarantine split this test
        previously pinned): the spine settles each DH-905 cursor SKIPPED and
        continues, so the dead series is COMPLETE with zero bars — counted in
        ``gap_only_series``, with the quarantine counters all zero.
        """

        class _NoDataClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")

        report = run_dhan_options_ingest(
            _NoDataClient(),
            _Sink(),
            underlyings=[_underlying()],
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            expiry_codes=(1,),
            option_types=("CALL",),
            run_parallel=_SerialRun(),
        )
        assert report.failed_series == 0
        assert report.no_data_series == 0
        assert report.real_failed_series == 0
        assert report.gap_only_series == 1
        assert report.no_data_windows == 1
        assert report.still_failed_windows == 0  # a gap is never residual work
        assert report.auth_aborted is False


class TestUntilCleanLoop:
    """The options auto-resume: rounds drain the residual over the shared ledger."""

    def test_second_round_refetches_only_the_residual(self, tmp_path: Path) -> None:
        """Round 2 re-fetches ONLY the aborted series; settled windows skip in O(1)."""
        calls: dict[str, int] = {}
        lock = threading.Lock()

        class _StormThenClearClient:
            """ATM+1 429s through round 1's whole in-run budget, then recovers."""

            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint
                strike = str(body["strike"])
                with lock:
                    calls[strike] = calls.get(strike, 0) + 1
                    n = calls[strike]
                if strike == "ATM+1" and n <= 3:  # 1 attempt + 2 in-run retries
                    raise DhanRateLimitError("HTTP 429 too many requests")
                return _payload()

        def _factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _StormThenClearClient(), _Sink()

        sleeps: list[int] = []
        report = pull_options(
            [_stock_instrument()],  # EQ → ATM±3 = 7 strikes, MONTH-only
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,  # → exactly ONE 30-day window per series
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory,
            until_clean=True,
            until_clean_sleep=sleeps.append,
        )
        assert report.rounds_run == 2
        assert sleeps == [2]  # one inter-round wait, before round 2 only
        assert report.still_failed_windows == 0  # the FINAL state is clean
        assert report.verdict.kind == "clean"
        # Per-symbol rollup is the FIRST pass (the spot until-clean contract):
        # 6 committed + 1 aborted — the abort is never reported as completed.
        assert report.total_series_completed == 6
        assert report.series_aborted == 1
        # Incrementality proof: the 6 settled series were fetched ONCE; only the
        # aborted one was re-fetched (3 storm attempts + 1 clean refetch).
        assert calls["ATM+1"] == 4
        assert all(n == 1 for strike, n in calls.items() if strike != "ATM+1")
        assert ledger_path(tmp_path).is_file()  # the ledger persisted

    def test_quota_death_mid_round_maps_verdict_and_keeps_ledger(self, tmp_path: Path) -> None:
        """A mid-round DailyQuotaExceeded becomes the loud quota verdict, not a crash."""
        lock = threading.Lock()
        state = {"calls": 0}

        class _QuotaDiesClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                with lock:
                    state["calls"] += 1
                    n = state["calls"]
                if n > 2:
                    raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                return _payload()

        def _factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _QuotaDiesClient(), _Sink()

        report = pull_options(
            [_stock_instrument()],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory,
            until_clean=True,
            until_clean_sleep=lambda _i: None,
        )
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"
        assert report.rounds_run == 1
        # The finally-guaranteed persist: settled windows survive the abort.
        assert ledger_path(tmp_path).is_file()

    def test_token_death_stops_the_loop_with_auth_verdict(self, tmp_path: Path) -> None:
        """stop_dead: re-firing rounds on a dead token is futile — halt loudly."""

        class _AuthDeadClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                raise DhanAuthError("HTTP 401 unauthorized: DH-906 invalid token")

        def _factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _AuthDeadClient(), _Sink()

        sleeps: list[int] = []
        report = pull_options(
            [_stock_instrument()],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory,
            until_clean=True,
            until_clean_sleep=sleeps.append,
        )
        assert report.auth_aborted is True
        assert report.auth_cause is not None
        assert report.verdict.kind == "auth_expired"
        assert report.rounds_run == 1
        assert sleeps == []  # no second round on a dead token
        # Every series is accounted for: quarantined or latch-skipped, never lost.
        assert report.real_failed_series + report.series_skipped_auth == 7
        assert report.total_series_completed == 0

    def test_dead_series_universe_drains_in_one_round(self, tmp_path: Path) -> None:
        """The DEC-OPTION-DH905-CONTINUE-001 root fix: a fully-dead universe drains
        in ONE paced pass — every window settled SKIPPED, residual 0, stop_clean
        after round 1, and the verdict reads ``known_gap_only`` (never a lying
        ``clean`` and never the pre-fix ~one-window-per-pull crawl)."""
        lock = threading.Lock()
        state = {"calls": 0}

        class _DeadClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                with lock:
                    state["calls"] += 1
                raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")

        def _factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _DeadClient(), _Sink()

        sleeps: list[int] = []
        report = pull_options(
            [_stock_instrument()],  # EQ → ATM±3 = 7 series, one window each
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory,
            until_clean=True,
            until_clean_sleep=sleeps.append,
        )
        assert state["calls"] == 7  # ONE paced call per window — the whole drain
        assert report.rounds_run == 1  # round 1 is already clean
        assert sleeps == []  # no inter-round waits — nothing residual
        assert report.still_failed_windows == 0
        assert report.gap_only_series == 7
        assert report.total_series_completed == 7  # gap-only series ARE complete
        assert report.symbols[0].series_gap_only == 7  # attributed per symbol
        assert report.total_series_failed == 0  # never a quarantine
        assert report.verdict.kind == "known_gap_only"  # loud, honest, actionable
        assert ledger_path(tmp_path).is_file()
        # Every settled known gap is durable — the next pull skips all 7.
        persisted = load_window_ledger(tmp_path)
        assert persisted.count == 7
        assert all(r.status == "SKIPPED" for r in persisted.records())

    def test_quota_death_mid_round_keeps_gap_settles_durable(self, tmp_path: Path) -> None:
        """Hostile finding 3: gap settles made BEFORE a mid-round quota death are
        persisted by the ``finally`` ledger flush and SKIPPED on the next pull —
        the quota reset never re-burns a settled known gap."""
        # ATM's single window answers DH-905 on its first fetch (settling the
        # gap), then ATM's SECOND window quota-dies — deterministic because both
        # events live on the SAME series job thread (settle happens-before the
        # job ends, which happens-before the drain raise + the finally persist).
        lock = threading.Lock()
        seen_atm = {"windows": 0}

        class _GapThenQuotaClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint
                if str(body["strike"]) == "ATM":
                    with lock:
                        seen_atm["windows"] += 1
                        nth = seen_atm["windows"]
                    if nth == 1:
                        raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")
                    raise DailyQuotaExceeded("daily quota of 100000 calls spent")
                return _payload()

        def _factory(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _GapThenQuotaClient(), _Sink()

        # now=2021-03-01 → [2021-01-01, 2021-02-28] → TWO 30-day windows/series.
        report = pull_options(
            [_stock_instrument()],
            lake_root=tmp_path,
            dry_run=False,
            now=datetime(2021, 3, 1, tzinfo=UTC),
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory,
            until_clean=True,
            until_clean_sleep=lambda _i: None,
        )
        assert report.quota_exhausted is True
        assert report.verdict.kind == "quota_exhausted"
        # The settle survived the mid-round death (finally-guaranteed persist).
        persisted = load_window_ledger(tmp_path)
        skipped = [r for r in persisted.records() if r.status == "SKIPPED"]
        assert any(r.last_error is not None and "DH-905" in r.last_error for r in skipped)
        gap_row = next(r for r in skipped if r.last_error and "DH-905" in r.last_error)

        # After the IST reset: a re-pull over the SAME lake never re-fetches the
        # settled gap window (it skips in O(1) off the durable ledger row).
        calls: list[tuple[str, str]] = []

        class _OkRecordingClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint
                with lock:
                    calls.append((str(body["strike"]), str(body["fromDate"])))
                return _payload()

        def _factory2(_lake_root: Path) -> tuple[JsonPoster, OptionBarSink]:
            return _OkRecordingClient(), _Sink()

        resumed = pull_options(
            [_stock_instrument()],
            lake_root=tmp_path,
            dry_run=False,
            now=datetime(2021, 3, 1, tzinfo=UTC),
            expiry_codes=(1,),
            option_types=("CALL",),
            client_factory=_factory2,
            until_clean=True,
            until_clean_sleep=lambda _i: None,
        )
        assert resumed.verdict.kind == "clean"
        assert resumed.still_failed_windows == 0
        # The settled known-gap window was never re-fetched (fromDate is the
        # body's daily-format date string).
        assert ("ATM", gap_row.window_start) not in calls

    def test_dry_run_until_clean_is_one_clean_round(self, tmp_path: Path) -> None:
        """The synthetic path: round 1 clean → stop_clean; report carries the fields."""
        ticks: list[int] = []
        sleeps: list[int] = []
        report = pull_options(
            [_stock_instrument()],
            lake_root=tmp_path,
            dry_run=True,
            now=_NOW,
            expiry_codes=(1,),
            option_types=("CALL",),
            on_series_tick=lambda: ticks.append(1),
            until_clean=True,
            until_clean_sleep=sleeps.append,
        )
        assert report.rounds_run == 1
        assert sleeps == []
        assert ticks  # the chained tick still fires (observability preserved)
        assert report.verdict.kind == "clean"
        payload = report.to_dict()
        for key in (
            "still_failed_windows",
            "auth_aborted",
            "auth_cause",
            "quota_exhausted",
            "series_aborted",
            "series_skipped_auth",
            "no_data_series",
            "real_failed_series",
            "gap_only_series",
            "rounds_run",
        ):
            assert key in payload
        assert all("series_gap_only" in s for s in payload["symbols"])
