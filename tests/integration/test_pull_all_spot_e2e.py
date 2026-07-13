# pyright: reportPrivateUsage=none
"""End-to-end scoped-ALL spot-pull chain (DEC-PULL-SCOPED-ALL-ONECLICK-001).

The REAL sequencer (:func:`run_full_pipeline`) driven with the REAL
``pull_all_spot`` (synthetic recording adapter + the REAL ``ParquetBarSink``
writer + the REAL durable window ledger on a tmp lake) under a HAND-COMPOSED
``_Chain`` harness binding the SAME primitives the launcher/binding install —
the pull_scope no-op F&O reports, the note surgery, the crossverify tail
fence, the composed Muhurat belt. The harness proves the CHAIN MECHANISM, not
the launcher wiring: the wiring pins live in the route + nightly suites
(``test_scope_spot_only_market_closed_is_composed_muhurat_belt`` — the R4 M6
mutant killer — + ``test_scope_spot_only_composes_muhurat_belt``). Pins:

* the chain law (no-op gates pass, prebuild fires over the exclusion set,
  spot movers warm after it, ``report.ok`` True);
* auto-incremental convergence (a second run fetches ZERO windows);
* quarantine headline honesty + prebuild exclusion;
* vendor-empty settles honestly; hyphen/ampersand symbols land on disk;
* the note GOLDEN + the "0 expiries" negative drift pin (B H1) through the
  REAL ``_complete_note`` wording;
* mid-run market-hours interrupt → settled windows DURABLE on disk (the
  ledger checkpoint law) + resumable next run;
* the composed Muhurat mid-run belt (F/B: the BARE guard predicate is
  provably blind at the same instant);
* the Monday default end bound (prev calendar day — no today windows).

Fully offline — synthetic adapters, zero network, zero credentials.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.errors import GrowwNotFoundError
from brutex.adapters.ingest_common import SyntheticSpotAdapter
from brutex.calendars.auto_pull_gate import compose_market_closed
from brutex.calendars.market_hours import resolve_market_closed
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pipeline_pull import (
    PipelinePullReport,
    historical_phase_ok,
    metadata_phase_ok,
    run_full_pipeline,
    spot_phase_ok,
)
from brutex.dashboard.pull_controller import pull_all_spot
from brutex.dashboard.pull_scope import (
    CrossverifyTailOutcome,
    build_spot_only_note,
    run_crossverify_tail,
    spot_only_historical_report,
    spot_only_metadata_report,
)
from brutex.data.models import CandleBar
from brutex.data.sink import BarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment

pytest.importorskip("duckdb", reason="the real lake writers need duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_FROM = "2024-01-02"
_TO = "2024-01-03"

_TAIL_OK = CrossverifyTailOutcome(
    status="started", human="producer started (poll /api/v1/crossverify/progress)"
)


def _instrument(symbol: str, kind: InstrumentType, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
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


def _nifty() -> Instrument:
    return _instrument("NIFTY", InstrumentType.IDX, 13)


def _reliance() -> Instrument:
    return _instrument("RELIANCE", InstrumentType.EQ, 25)


class _RecordingAdapter:
    """Synthetic spot adapter recording every fetched (symbol, start, end)."""

    def __init__(
        self,
        *,
        empty_symbols: frozenset[str] = frozenset(),
        raising_symbols: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = SyntheticSpotAdapter()
        self.fetches: list[tuple[str, date, date]] = []
        self._empty = empty_symbols
        self._raising = raising_symbols
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> Any:
        return self._inner.capabilities

    def list_instruments(self, *, cursor: str | None = None) -> Any:
        return self._inner.list_instruments(cursor=cursor)

    def list_derivatives(self, *, underlying_symbol: str, exchange: Exchange) -> Any:
        return self._inner.list_derivatives(underlying_symbol=underlying_symbol, exchange=exchange)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Exchange, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        with self._lock:
            self.fetches.append((symbol, start, end))
        if symbol in self._raising:
            raise GrowwNotFoundError(f"instrument {symbol} not found on the vendor")
        if symbol in self._empty:
            return []
        return self._inner.fetch_ohlcv(
            symbol=symbol, exchange=exchange, interval=interval, start=start, end=end
        )


def _real_sink(lake_root: Path) -> BarSink:
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


class _Chain:
    """One spot-only chain run — the EXACT launcher/binding composition."""

    def __init__(
        self,
        lake_root: Path,
        instruments: list[Instrument],
        *,
        adapter: _RecordingAdapter | None = None,
        market_closed: Any = None,
        from_date: str | None = _FROM,
        to_date: str | None = _TO,
        now: datetime = _NOW,
    ) -> None:
        self.lake_root = lake_root
        self.instruments = instruments
        self.adapter = adapter or _RecordingAdapter()
        self.market_closed = market_closed
        self.from_date = from_date
        self.to_date = to_date
        self.now = now
        self.prebuild_exclusions: list[frozenset[str]] = []
        self.warm_calls = 0

    def _run_spot(self) -> Any:
        return pull_all_spot(
            self.instruments,
            lake_root=self.lake_root,
            dry_run=False,
            now=self.now,
            from_date=self.from_date,
            to_date=self.to_date,
            adapter_factory=lambda root, _insts: (self.adapter, _real_sink(root)),
            market_closed=self.market_closed,
        )

    def _run_prebuild(self, exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        self.prebuild_exclusions.append(exclude_symbols)
        built = len(self.instruments) - len(exclude_symbols)
        return PrebuildOutcome(
            symbols_built=built, total_bars=7, failed=(), failed_count=0, ok=True
        )

    def _warm_spot(self) -> MoversWarmResult:
        self.warm_calls += 1
        return MoversWarmResult(warmed=3, failed=0, total=3)

    def run(self, *, tail: Any = lambda: _TAIL_OK) -> tuple[PipelinePullReport, str]:
        report = run_full_pipeline(
            run_spot=self._run_spot,
            run_metadata=spot_only_metadata_report,
            run_historical=lambda _prior: spot_only_historical_report(),
            warm_spot_movers=self._warm_spot,
            warm_futures_movers=None,
            warm_options_movers=None,
            run_prebuild=self._run_prebuild,
            market_closed=self.market_closed,
        )
        outcome = run_crossverify_tail(tail, dry_run=False, stopped_after=report.stopped_after)
        return report, build_spot_only_note(report.note, outcome)


def test_e2e_spot_only_chain(tmp_path: Path) -> None:
    """C 11: spot → prebuild (spot-only) → spot warm → tail note; the no-op
    reports pass the REAL gates; report.ok True."""
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()])
    report, note = chain.run()
    assert report.stopped_after is None
    assert report.ok is True
    assert report.spot is not None and spot_phase_ok(report.spot)
    assert report.metadata is not None and metadata_phase_ok(report.metadata)
    assert report.historical is not None and historical_phase_ok(report.historical)
    assert chain.prebuild_exclusions == [frozenset()]
    assert chain.warm_calls == 1
    assert report.segment_movers_warmed == {"spot": 3}  # futures/options ABSENT
    assert note.startswith("spot-only pipeline complete — ")
    assert chain.adapter.fetches, "the real spot spine must have fetched"


def test_e2e_second_run_fetches_only_missing(tmp_path: Path) -> None:
    """A: the durable window ledger makes the re-run a ZERO-fetch no-op."""
    lake = tmp_path / "lake"
    first = _Chain(lake, [_nifty(), _reliance()])
    report1, _ = first.run()
    assert report1.ok is True
    first_fetches = len(first.adapter.fetches)
    assert first_fetches > 0
    second = _Chain(lake, [_nifty(), _reliance()])
    report2, note2 = second.run()
    assert report2.ok is True
    assert second.adapter.fetches == []  # every window settled — no vendor hit
    assert report2.spot is not None
    assert report2.spot.windows_skipped > 0
    assert "already-pulled window(s) (no vendor hit)" in note2


def test_e2e_one_bad_symbol_quarantined_prebuild_excluded_headline_leads(
    tmp_path: Path,
) -> None:
    """A/B R3/H5: a terminal per-symbol failure quarantines THAT symbol only —
    the chain completes, the prebuild excludes it, the headline LEADS."""
    adapter = _RecordingAdapter(raising_symbols=frozenset({"RELIANCE"}))
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()], adapter=adapter)
    report, note = chain.run()
    assert report.stopped_after is None
    assert report.quarantined_count == 1
    assert report.quarantined_symbols == ("RELIANCE",)
    assert chain.prebuild_exclusions == [frozenset({"RELIANCE"})]
    assert note.startswith("spot-only pipeline complete — 1 symbol(s) quarantined: RELIANCE — ")
    assert ", F&O skipped by design (spot-only scope)" in note


def test_e2e_vendor_empty_stock_settles_empty(tmp_path: Path) -> None:
    """A: a legit vendor-empty symbol settles honestly — never a quarantine."""
    adapter = _RecordingAdapter(empty_symbols=frozenset({"RELIANCE"}))
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()], adapter=adapter)
    report, _note = chain.run()
    assert report.stopped_after is None
    assert report.ok is True
    assert report.quarantined_count == 0
    assert report.spot is not None
    assert report.spot.total_no_data >= 1


def test_e2e_hyphen_and_ampersand_symbols_pull_spot(tmp_path: Path) -> None:
    """A/B V8: BAJAJ-AUTO + M&M land under their REAL lake dirs."""
    lake = tmp_path / "lake"
    instruments = [
        _instrument("BAJAJ-AUTO", InstrumentType.EQ, 31),
        _instrument("M&M", InstrumentType.EQ, 32),
    ]
    chain = _Chain(lake, instruments)
    report, _note = chain.run()
    assert report.ok is True
    fetched = {sym for sym, _s, _e in chain.adapter.fetches}
    assert fetched == {"BAJAJ-AUTO", "M&M"}
    stored = {p.name for p in (lake / "bars" / "NSE" / "CASH").iterdir()}
    assert "BAJAJ-AUTO" in stored
    assert "M&M" in stored


def test_e2e_note_golden_full_clean(tmp_path: Path) -> None:
    """[gold — C 1] The exact final note through the REAL sequencer wording."""
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()])
    _report, note = chain.run()
    assert note == (
        "spot-only pipeline complete — spot clean (2 pair(s))"
        ", F&O skipped by design (spot-only scope)"
        ", timeframes pre-built for 2 symbol(s) (7 bars)"
        ", movers spot 3 snapshot(s) warmed"
        ", Greeks: not run — no options in this pull"
        ", cross-verify: producer started (poll /api/v1/crossverify/progress)"
    )


def test_e2e_no_zero_expiries_wording_through_real_sequencer(tmp_path: Path) -> None:
    """[neg — B H1, the note-surgery DRIFT guard]: the '0 expiries' no-op
    wording must NEVER survive on ANY spot-only variant — fails loudly if
    ``_complete_note``'s wording moves (forcing a conscious re-pin)."""
    for chain in (
        _Chain(tmp_path / "lake1", [_nifty()]),
        _Chain(
            tmp_path / "lake2",
            [_nifty(), _reliance()],
            adapter=_RecordingAdapter(raising_symbols=frozenset({"RELIANCE"})),
        ),
    ):
        _report, note = chain.run()
        assert "0 expiries" not in note
        assert "0 contracts" not in note
        assert "0 bars over 0 contract(s)" not in note
        assert "metadata" not in note  # the whole F&O clause is gone
        assert "Greeks enrichment" not in note  # never the failure-family text


class _FlipAfter:
    """A market_closed predicate flipping True after N consultations."""

    def __init__(self, after: int) -> None:
        self.calls = 0
        self.after = after

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.after


def test_e2e_midrun_market_hours_halt_resumable_ledger_durable(tmp_path: Path) -> None:
    """B C11 + B M2: a mid-run market-hours flip stops the pull; every window
    settled BEFORE the flip is DURABLE on disk (the internal per-window ledger
    checkpoint — verify-then-wire: GREEN with no extra wiring, because
    ``pull_all_spot`` flushes the ledger per processed window), and the next
    open-market run completes the remainder WITHOUT re-fetching them."""
    lake = tmp_path / "lake"
    flip = _FlipAfter(after=2)
    first = _Chain(lake, [_nifty(), _reliance()], market_closed=flip)
    _report1, note1 = first.run()
    first_fetches = len(first.adapter.fetches)
    total_windows = 2  # 2 symbols × ONE window each ([_FROM, _TO] fits one chunk)
    assert first_fetches < total_windows, "the flip must have interrupted the pull"
    assert "cross-verify:" in note1  # the fence still rendered a clause
    # DURABILITY: a brand-new chain (fresh ledger load FROM DISK) skips the
    # already-settled windows and fetches ONLY the remainder.
    second = _Chain(lake, [_nifty(), _reliance()])
    report2, _note2 = second.run()
    assert report2.ok is True
    assert len(second.adapter.fetches) < total_windows
    assert len(second.adapter.fetches) + first_fetches >= total_windows
    assert report2.spot is not None
    assert report2.spot.windows_skipped > 0
    # Convergence: a third run fetches NOTHING.
    third = _Chain(lake, [_nifty(), _reliance()])
    report3, _ = third.run()
    assert third.adapter.fetches == []
    assert report3.ok is True


def test_e2e_muhurat_midrun_stop_composed_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B C5 [F/B]: frozen INSIDE the 2025-10-21 afternoon Muhurat session the
    BARE market-hours guard is provably BLIND (the holiday reads non-trading →
    not blocked — exactly the pre-change behavior), while the COMPOSED
    spot-only predicate defers the whole vendor chain."""
    frozen = datetime(2025, 10, 21, 14, 0)  # inside the 13:45-14:45 session
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    bare = resolve_market_closed()
    assert bare() is False  # the guard's structural Muhurat blindness (fails-before)
    composed = compose_market_closed(bare)
    assert composed() is True  # the belt fires (passes-after)
    chain = _Chain(tmp_path / "lake", [_nifty()], market_closed=composed)
    report, note = chain.run()
    assert report.stopped_after == "market_hours"
    assert chain.adapter.fetches == []  # ZERO vendor calls during the session
    assert "cross-verify: skipped (pipeline stopped: market_hours)" in note
    assert "Greeks: not run — no options in this pull" in note


def test_e2e_monday_end_bound_is_sunday_no_today_windows(tmp_path: Path) -> None:
    """B C10: a Monday default-range pull's end bound is the PREVIOUS calendar
    day — no fetched window covers Monday itself."""
    monday = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    assert monday.date().weekday() == 0
    chain = _Chain(
        tmp_path / "lake",
        [_nifty()],
        from_date="2026-07-06",  # bound the tail week — the END law is the subject
        to_date=None,
        now=monday,
    )
    report, _note = chain.run()
    assert report.ok is True
    assert report.spot is not None
    assert report.spot.end_date == "2026-07-12"  # the previous calendar day (Sunday)
    assert chain.adapter.fetches, "the tail week must have fetched"
    for _sym, _start, end in chain.adapter.fetches:
        assert end <= date(2026, 7, 13)  # end-exclusive bound: never past prev-day
        assert _start <= date(2026, 7, 12)
