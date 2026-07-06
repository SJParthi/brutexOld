"""Spot step-group ordering inside ``pull_all_spot`` (DEC-PULL-SIX-STEP-ORDER-001).

Steps 1-2 of the six-step pull law: the ALWAYS-ON indices-before-stocks
partition, the ``pull.step.*`` events, the per-group global-stop halt, the
group-scoped until-clean settle (``progress_counts(symbols=…)``), dry-run
inertness, rollup fold parity for a mixed universe, and the ``use_live_rate``
trap pin (the partition lives INSIDE ``pull_all_spot``, never at a caller).
Offline only — synthetic adapters, no creds / network.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.errors import GrowwAuthError, GrowwTimeoutError
from brutex.adapters.ingest_common import (
    StepOrderMonitor,
    SyntheticSpotAdapter,
)
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.dashboard import pull_controller
from brutex.dashboard.pull_controller import pull_all_spot
from brutex.data.models import CandleBar
from brutex.data.sink import BarSink, NullBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

_NOW = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
_FROM = "2024-01-02"
_TO = "2024-01-03"


def _instrument(symbol: str, kind: InstrumentType) -> Instrument:
    """Build a minimal spot instrument of the given *kind*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _index(symbol: str) -> Instrument:
    return _instrument(symbol, InstrumentType.IDX)


def _stock(symbol: str) -> Instrument:
    return _instrument(symbol, InstrumentType.EQ)


class _DelegatingAdapter:
    """A stateful :class:`MarketAdapter` delegating to a synthetic inner adapter.

    ``SyntheticSpotAdapter`` is a frozen slots dataclass (no subclass state), so
    the recording fakes COMPOSE it instead: same protocol surface, plus a
    thread-safe ``fetched`` symbol log.
    """

    def __init__(self) -> None:
        self._inner = SyntheticSpotAdapter()
        self.fetched: list[str] = []
        self._record_lock = threading.Lock()

    @property
    def capabilities(self) -> MarketCapabilities:
        """Delegate the capability surface."""
        return self._inner.capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Delegate the instrument page."""
        return self._inner.list_instruments(cursor=cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Delegate the (empty) derivatives iterator."""
        return self._inner.list_derivatives(underlying_symbol=underlying_symbol, exchange=exchange)

    def _record(self, symbol: str) -> None:
        with self._record_lock:
            self.fetched.append(symbol)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Record the symbol, then return the synthetic bars."""
        self._record(symbol)
        return self._inner.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


class _RecordingAdapter(_DelegatingAdapter):
    """A synthetic adapter that records every fetched symbol (thread-safe)."""


class _AuthFailAdapter(_DelegatingAdapter):
    """A recording adapter whose every fetch raises a TERMINAL Groww auth error."""

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Record then die — drives the group-level global-stop halt."""
        del exchange, interval, start, end
        self._record(symbol)
        raise GrowwAuthError("token expired")


class _StockTimeoutAdapter(_DelegatingAdapter):
    """Synthetic bars for indices; a retryable timeout for every STOCK fetch."""

    def __init__(self, stock_symbols: frozenset[str]) -> None:
        super().__init__()
        self._stock_symbols = stock_symbols

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        """Raise a retryable timeout for a stock; synthetic bars otherwise."""
        if symbol in self._stock_symbols:
            raise GrowwTimeoutError("read timed out")
        return super().fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


def _real_sink(lake_root: Path) -> BarSink:
    """A real catalog-free ``ParquetBarSink`` so the window ledger is exercised."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


#: The captured-log event shape (structlog ``capture_logs`` yields these).
type _LogEvent = MutableMapping[str, object]


def _events(logs: Sequence[_LogEvent], name: str) -> list[_LogEvent]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestSpotStepOrdering:
    """(a) The ordering teeth + the ``pull.step.*`` event stream."""

    def test_indices_spot_fetches_precede_stock_spot_fetches(self, tmp_path: Path) -> None:
        adapter = _RecordingAdapter()
        indices = {"NIFTY", "SENSEX"}
        pull_all_spot(
            # Deliberately stock-first input — the partition must reorder.
            [_stock("ADANIENT"), _index("NIFTY"), _stock("TCS"), _index("SENSEX")],
            lake_root=tmp_path,
            dry_run=False,
            now=_NOW,
            from_date=_FROM,
            to_date=_TO,
            adapter_factory=lambda root, _insts: (adapter, _real_sink(root)),
            # The HARD test oracle (raise_on_violation): any rank regression raises.
            step_monitor=StepOrderMonitor(raise_on_violation=True),
        )
        assert adapter.fetched, "the recording adapter saw no fetches"
        kinds = ["index" if sym in indices else "stock" for sym in adapter.fetched]
        assert "index" in kinds and "stock" in kinds
        last_index = max(i for i, kind in enumerate(kinds) if kind == "index")
        first_stock = min(i for i, kind in enumerate(kinds) if kind == "stock")
        assert last_index < first_stock, (
            f"an index fetch (pos {last_index}) landed after a stock fetch "
            f"(pos {first_stock}): {adapter.fetched}"
        )

    def test_step_events_started_settled_in_order(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            pull_all_spot(
                [_stock("ADANIENT"), _index("NIFTY")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                adapter_factory=lambda root, _insts: (
                    SyntheticSpotAdapter(),
                    _real_sink(root),
                ),
            )
        started = _events(logs, "pull.step.started")
        settled = _events(logs, "pull.step.settled")
        assert [e["step"] for e in started] == ["indices-spot", "stocks-spot"]
        assert [e["ordinal"] for e in started] == [1, 2]
        assert [e["step"] for e in settled] == ["indices-spot", "stocks-spot"]
        assert all(e["clean"] is True for e in settled)
        assert all(e["symbols"] == 1 for e in started)
        # settled(1) strictly precedes started(2) — step-major by construction.
        idx_settled_1 = logs.index(settled[0])
        idx_started_2 = logs.index(started[1])
        assert idx_settled_1 < idx_started_2

    def test_settled_event_carries_group_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Synchronous write path: in async-pipeline mode the FIRST-pass report
        # defers per-pair bar counts to the post-drain reconcile (they read 0 at
        # the step boundary — the documented producer-side settle envelope), so
        # the deterministic count assertions pin the sync path.
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        with capture_logs() as logs:
            pull_all_spot(
                [_index("NIFTY"), _stock("ADANIENT")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                adapter_factory=lambda root, _insts: (
                    SyntheticSpotAdapter(),
                    _real_sink(root),
                ),
            )
        settled = _events(logs, "pull.step.settled")
        for event in settled:
            assert isinstance(event["bars"], int) and event["bars"] > 0
            assert isinstance(event["windows"], int) and event["windows"] > 0
            assert event["still_failed"] == 0


class TestHomogeneousGroup:
    """(b) A homogeneous list is ONE group — byte-identical to the ungrouped pull."""

    def test_indices_only_is_one_group(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("13"), _index("25")], lake_root=tmp_path, dry_run=True, now=_NOW
            )
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot"]
        assert started[0]["symbols"] == 2
        # The pre-change report shape (the existing dry-run fixture expectations).
        assert report.total_symbols == 2
        assert report.total_completed == 2
        assert report.total_failed == 0
        assert report.start_date == "2020-01-01"
        assert report.end_date == "2026-05-29"

    def test_stocks_only_is_one_group(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            report = pull_all_spot(
                [_stock("ADANIENT"), _stock("TCS")],
                lake_root=tmp_path,
                dry_run=True,
                now=_NOW,
            )
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["stocks-spot"]
        assert report.total_symbols == 2
        assert report.total_completed == 2

    def test_empty_universe_emits_no_step_events(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            report = pull_all_spot([], lake_root=tmp_path, dry_run=True, now=_NOW)
        assert _events(logs, "pull.step.started") == []
        assert report.total_symbols == 0


class TestMixedRollupFoldParity:
    """(c) The mixed grouped rollup equals the per-class pulls — the zero-change fold pin."""

    def test_mixed_rollup_equals_union_of_class_pulls(self, tmp_path: Path) -> None:
        instruments = [_index("NIFTY"), _stock("ADANIENT"), _index("SENSEX"), _stock("TCS")]
        mixed = pull_all_spot(
            instruments,
            lake_root=tmp_path / "mixed",
            dry_run=True,
            now=_NOW,
            from_date=_FROM,
            to_date=_TO,
        )
        indices_only = pull_all_spot(
            [i for i in instruments if i.is_index],
            lake_root=tmp_path / "idx",
            dry_run=True,
            now=_NOW,
            from_date=_FROM,
            to_date=_TO,
        )
        stocks_only = pull_all_spot(
            [i for i in instruments if not i.is_index],
            lake_root=tmp_path / "stk",
            dry_run=True,
            now=_NOW,
            from_date=_FROM,
            to_date=_TO,
        )
        by_symbol = {r.symbol: r for r in mixed.symbols}
        for part in (indices_only, stocks_only):
            for row in part.symbols:
                assert by_symbol[row.symbol] == row
        assert mixed.total_completed == indices_only.total_completed + stocks_only.total_completed
        assert mixed.total_failed == indices_only.total_failed + stocks_only.total_failed
        assert mixed.windows_skipped == indices_only.windows_skipped + stocks_only.windows_skipped
        assert (
            mixed.still_failed_windows
            == indices_only.still_failed_windows + stocks_only.still_failed_windows
        )


class TestGlobalStopHaltsLaterSteps:
    """(d) Edge 8 (spot arm): a dead token in the indices group halts the stock group."""

    def test_auth_halt_in_indices_halts_stocks(self, tmp_path: Path) -> None:
        adapter = _AuthFailAdapter()
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("NIFTY"), _stock("ADANIENT")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                adapter_factory=lambda _root, _insts: (adapter, NullBarSink()),
            )
        assert report.auth_aborted is True
        assert report.verdict.kind == "auth_expired"
        halted = _events(logs, "pull.step.halted")
        assert [e["step"] for e in halted] == ["stocks-spot"]
        assert halted[0]["reason"] == "auth_expired"
        assert halted[0]["ordinal"] == 2
        # The stock group never fetched a single window.
        assert set(adapter.fetched) == {"NIFTY"}
        # The stocks-spot step never started.
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot"]
        # Rollup honesty: the index is failed; the stock's outcome is UNKNOWN (zeros).
        by_symbol = {r.symbol: r for r in report.symbols}
        assert by_symbol["NIFTY"].failed == 1
        assert (
            by_symbol["ADANIENT"].completed,
            by_symbol["ADANIENT"].failed,
            by_symbol["ADANIENT"].no_data,
        ) == (0, 0, 0)


class TestDryRunInert:
    """(e) Edge 12: a dry-run applies the order, vendor-free, with no ledger."""

    def test_dry_run_orders_steps_without_ledger(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            report = pull_all_spot(
                [_stock("ADANIENT"), _index("NIFTY")],
                lake_root=tmp_path,
                dry_run=True,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
            )
        assert report.dry_run is True
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot", "stocks-spot"]
        # No durable ledger is written on a dry-run (begin_pull_ledger → None).
        assert not (tmp_path / "window_ledger").exists()
        assert report.total_completed == 2


class TestGroupScopedUntilClean:
    """(f) The group-scoped until-clean settle + the bounded stuck-group envelope."""

    def test_indices_group_exits_stop_clean_not_no_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ledger-pollution regression: the indices group must exit ``stop_clean``.

        Planning is single-pass over ALL instruments, so when the indices group's
        until-clean loop reads its outstanding, the stock group's windows are still
        PLANNED in the SAME ledger. Unscoped, the loop read them as its own residual
        and burned a vendor-call-free ``stop_no_progress`` round; group-scoped
        (``progress_counts(symbols=…)``) it exits ``stop_clean`` on round 1.
        Synchronous write path — in async-pipeline mode the tail records land
        after the round boundary (the producer-side settle envelope), which is
        a separate, documented effect this regression must not conflate.
        """
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("NIFTY"), _stock("ADANIENT")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                adapter_factory=lambda root, _insts: (
                    SyntheticSpotAdapter(),
                    _real_sink(root),
                ),
                until_clean=True,
                until_clean_rounds=5,
            )
        assert report.verdict.kind == "clean"
        done = _events(logs, "until_clean.done")
        assert len(done) == 2  # one loop per step group
        assert [e["verdict"] for e in done] == ["stop_clean", "stop_clean"]
        assert [e["rounds"] for e in done] == [1, 1]

    def test_stuck_stock_group_self_terminates_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders envelope: a stuck group stops after +1 no-progress round."""
        # Kill the real inter-round / retry backoff sleeps (bounded but seconds-scale).
        monkeypatch.setattr("brutex.adapters.ingest_common._time_sleep", lambda _s: None)
        # Synchronous write path for deterministic round accounting (see above).
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        with capture_logs() as logs:
            report = pull_all_spot(
                [_index("NIFTY"), _stock("ADANIENT")],
                lake_root=tmp_path,
                dry_run=False,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                adapter_factory=lambda root, _insts: (
                    _StockTimeoutAdapter(frozenset({"ADANIENT"})),
                    _real_sink(root),
                ),
                until_clean=True,
                until_clean_rounds=10,
            )
        done = _events(logs, "until_clean.done")
        assert len(done) == 2
        # Indices group: clean on round 1. Stock group: stuck → self-terminates
        # after ONE extra no-progress round, never churning to the 10-round cap.
        assert done[0]["verdict"] == "stop_clean"
        assert done[1]["verdict"] == "stop_no_progress"
        assert done[1]["rounds"] == 2
        assert report.still_failed_windows > 0


class TestDualIntervalPhaseRestart:
    """(h) The dual-interval defensive path is a LEGAL phase restart (review round).

    ``intervals=("1day", "1minute")`` runs the ordered 1→2 group loop once per
    interval phase; ``_run_grouped`` rewinds the step cursor (``begin_phase``)
    so the minute phase's ``begin_step(INDICES_SPOT)`` after the daily phase's
    STOCKS_SPOT never reads as a rank regression. Before the rewind this pull
    raised ``StepOrderError`` under the test oracle (and logged a false
    ``pull.step.order_violation`` ERROR in production).
    """

    def test_dual_interval_pull_does_not_violate_under_the_hard_oracle(
        self, tmp_path: Path
    ) -> None:
        monitor = StepOrderMonitor(raise_on_violation=True)
        with capture_logs() as logs:
            report = pull_all_spot(
                [_stock("ADANIENT"), _index("NIFTY")],
                lake_root=tmp_path,
                dry_run=True,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                intervals=("1day", "1minute"),
                step_monitor=monitor,  # raises on any rank regression
            )
        assert report.verdict.kind == "clean"
        assert _events(logs, "pull.step.order_violation") == []
        # Both phases ran the full ordered 1→2 sequence.
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == [
            "indices-spot",
            "stocks-spot",
            "indices-spot",
            "stocks-spot",
        ]
        assert [e["ordinal"] for e in started] == [1, 2, 1, 2]
        # The rewind never fabricates late writes on a healthy pull.
        assert monitor.late_writes == 0

    def test_single_phase_pull_unchanged_by_the_rewind(self, tmp_path: Path) -> None:
        """Production (``("1minute",)`` — one phase): the rewind is a no-op."""
        with capture_logs() as logs:
            pull_all_spot(
                [_stock("ADANIENT"), _index("NIFTY")],
                lake_root=tmp_path,
                dry_run=True,
                now=_NOW,
                from_date=_FROM,
                to_date=_TO,
                step_monitor=StepOrderMonitor(raise_on_violation=True),
            )
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-spot", "stocks-spot"]
        assert _events(logs, "pull.step.order_violation") == []


class TestGroupScopedStreakPin:
    """(i) The until-clean STREAK read is GROUP-scoped, like the outstanding read.

    A foreign group's late consumer tail (EMPTY bump / SUCCESS reset) makes the
    ledger-wide ``streak_total()`` non-constant across the current group's
    rounds — the behavioural invariance teeth live in
    ``tests/unit/test_window_ledger.py`` (``TestStreakTotal``); this pin holds
    the controller to the scoped read (the same source-pin pattern as (g)).
    """

    def test_round_runner_reads_group_scoped_streak_and_progress(self) -> None:
        src = inspect.getsource(pull_controller.pull_all_spot)
        assert "streak = ledger.streak_total(symbols=group_symbols)" in src
        assert "prog = ledger.progress_counts(symbols=group_symbols)" in src
        # The unscoped fold must NOT reappear in the round runner.
        assert "ledger.streak_total()" not in src


class TestUseLiveRateTrapPin:
    """(g) The partition lives INSIDE ``pull_all_spot`` — the ``use_live_rate`` trap."""

    def test_partition_is_inside_pull_all_spot(self) -> None:
        src = inspect.getsource(pull_controller.pull_all_spot)
        assert "use_live_rate = adapter_factory is None and not dry_run" in src
        assert "partition_spot_steps(instruments)" in src

    def test_no_production_site_injects_adapter_factory(self) -> None:
        """No app.py ``pull_all_spot`` call passes ``adapter_factory`` — a sequencer-level
        two-call split would silently disable the live rate runtime (the :1502 trap)."""
        import brutex.dashboard.app as app_module

        source = Path(inspect.getfile(app_module)).read_text(encoding="utf-8")
        starts = [i for i in range(len(source)) if source.startswith("pull_all_spot(", i)]
        assert starts, "no pull_all_spot call sites found in app.py"
        for pos in starts:
            # Bound each call site to its argument block (calls are < 1200 chars).
            call_text = source[pos : pos + 1200]
            assert "adapter_factory" not in call_text
