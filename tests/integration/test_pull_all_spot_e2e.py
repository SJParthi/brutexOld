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
        real_prebuild: bool = False,
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
        self.real_prebuild = real_prebuild

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
        if self.real_prebuild:
            return self._run_real_prebuild(exclude_symbols)
        built = len(self.instruments) - len(exclude_symbols)
        return PrebuildOutcome(
            symbols_built=built, total_bars=7, failed=(), failed_count=0, ok=True
        )

    def _run_real_prebuild(self, exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        """The REAL prebuild — the binding's ``_run_prebuild`` closure mirrored
        (pipeline_binding.py: per-target ``_materialize`` over the REAL
        ``materialize_lake_timeframes``, one cutoff per run, spot-only's EMPTY
        F&O cascade; workers=1 + no disk guard — the sequential injected path)."""
        from brutex.calendars.nse import IST
        from brutex.dashboard.fno_cascade import build_fno_cascade
        from brutex.dashboard.pipeline_prebuild import (
            PrebuildTarget,
            run_pipeline_prebuild_guarded,
        )
        from brutex.data.timeframe_materialize import (
            MaterializeResult,
            materialize_lake_timeframes,
        )

        prebuild_cutoff = self.now.astimezone(IST).date()

        def _materialize(target: PrebuildTarget) -> MaterializeResult:
            return materialize_lake_timeframes(
                lake_root=self.lake_root,
                exchange=target.exchange,
                segment=target.segment,
                symbol=target.symbol,
                kind=target.kind,
                include_daily=True,
                daily_before=prebuild_cutoff,
                dense_symbol=target.dense,
            )

        return run_pipeline_prebuild_guarded(
            instruments=self.instruments,
            cascade_or_error=lambda: build_fno_cascade(()),
            materialize=_materialize,
            source="pipeline",
            exclude_symbols=exclude_symbols,
            lake_root=self.lake_root,
            workers=1,
            daily_before=prebuild_cutoff,
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


def test_e2e_vendor_empty_stock_real_prebuild_no_source_named_warm_fires(tmp_path: Path) -> None:
    """[fails-before — the CONSCIOUS FLIP of the #1779 inherited-behavior pin
    ``test_e2e_vendor_empty_stock_real_prebuild_withholds_movers_warm``]
    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001: a clean vendor-EMPTY stock
    (settled SKIPPED, zero stored bars — the honest pull outcome, NOT
    quarantined) is classified NO-SOURCE at the REAL
    ``materialize_lake_timeframes`` read (the whole-partition
    ``NoStored1mBarsError`` arm — lake truth, no report field consulted), so
    ``PrebuildOutcome.ok`` stays True and the sequencer's #1145/#1221 warm
    gate now FIRES over the healthy majority instead of withholding the warm
    run-wide forever. The symbol is COUNTED + NAMED in the note (both causes
    — the probe cannot distinguish vendor-empty from never-pulled), never a
    silent drop, never a fabricated snapshot (structural evidence below).
    At the pre-fix head every new pin fails (``ok=False``, ``warm_calls==0``,
    ``prebuild_failed==("RELIANCE",)``, no ``prebuild_no_source`` field)."""
    adapter = _RecordingAdapter(empty_symbols=frozenset({"RELIANCE"}))
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()], adapter=adapter, real_prebuild=True)
    report, note = chain.run()
    assert report.stopped_after is None  # the chain itself completes
    assert report.quarantined_count == 0  # vendor-empty is NOT a quarantine
    # The QUARANTINE exclusion set stays EMPTY — the #1779 rejection honored:
    # the spot report's `no_data` never feeds ANY exclusion set (the verdict
    # is the prebuild read's own zero-stored-bars arm, lake truth).
    assert chain.prebuild_exclusions == [frozenset()]
    # The REAL prebuild: the healthy symbol builds; the empty one is
    # classified no-source — NEVER a failure.
    assert report.prebuild_failed == ()
    assert report.prebuild_no_source == ("RELIANCE",)
    assert report.prebuild_no_source_count == 1
    assert report.prebuild_built == 1
    assert report.prebuild_total_bars is not None and report.prebuild_total_bars > 0
    nifty_1day = tmp_path / "lake" / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day"
    assert nifty_1day.is_dir()  # the healthy symbol's derived TFs are on disk
    # THE behavioral flip: the movers warm FIRES over the healthy set.
    assert chain.warm_calls == 1
    assert report.segment_movers_warmed == {"spot": 3}
    # Note honesty: counted + named (the skip clause) + the warmed clause;
    # never the failure wording, never the movers-warm overclaim (refuter
    # round 2 — the stale-derived-data twin below is the counter-evidence).
    assert (
        ", 1 symbol(s) skipped — no stored 1-minute data (vendor-empty or never pulled), "
        "excluded from timeframe build: RELIANCE" in note
    )
    assert "+ movers warm" not in note
    assert "snapshot(s) warmed" in note
    assert "prebuild failures" not in note
    # STRUCTURAL warm-absence evidence (graft G3 — never a fabricated
    # snapshot): discovery is parquet-file-gated, so the zero-stored-bars
    # symbol has NO dataset row, and the REAL warm-side candidate compute
    # over this lake screens the healthy symbol while RELIANCE is absent.
    from brutex.dashboard.db_view import list_candle_datasets
    from brutex.dashboard.movers_snapshot import compute_spot_candidates
    from brutex.dashboard.movers_warm import latest_movers_date

    lake = tmp_path / "lake"
    discovered = {d["symbol"] for d in list_candle_datasets(lake)}
    assert "NIFTY" in discovered
    assert "RELIANCE" not in discovered
    warm_date = latest_movers_date(lake)
    assert warm_date is not None
    cands = compute_spot_candidates(lake_root=lake, interval="1day", date=warm_date, time=None)
    warmed_symbols = {snap.instrument.symbol for snap in cands.snapshots}
    assert "NIFTY" in warmed_symbols
    assert "RELIANCE" not in warmed_symbols  # structurally absent, now COUNTED upstream


def test_e2e_incremental_rerun_never_classified_no_source(tmp_path: Path) -> None:
    """[the #1779 TRAP twin — the permanent regression net] a HEALTHY
    fully-settled symbol on an incremental re-run reads the byte-identical
    per-symbol ``no_data == 1`` shape a vendor-empty symbol reads (the
    conflation is kept VISIBLE here) — yet it is NEVER classified no-source:
    the verdict is the lake's stored bars (run 1 stored them), not any report
    field. Any future report-scoped predicate fails this test."""
    lake = tmp_path / "lake"
    first = _Chain(lake, [_nifty(), _reliance()], real_prebuild=True)
    report1, _ = first.run()
    assert report1.ok is True
    assert report1.prebuild_no_source == ()
    second = _Chain(lake, [_nifty(), _reliance()], real_prebuild=True)
    report2, note2 = second.run()
    assert second.adapter.fetches == []  # every window settled — no vendor hit
    assert report2.spot is not None
    # THE TRAP, visible: each healthy settled symbol reads no_data == 1 —
    # byte-identical to the vendor-empty report shape (#1779's executed
    # evidence). It must NEVER feed the no-source verdict.
    assert [(s.completed, s.failed, s.no_data) for s in report2.spot.symbols] == [
        (0, 0, 1),
        (0, 0, 1),
    ]
    assert report2.prebuild_no_source == ()
    assert report2.prebuild_no_source_count == 0
    assert report2.ok is True
    assert second.warm_calls == 1  # the warm fires — nothing withheld
    assert "symbol(s) skipped — no stored 1-minute data" not in note2


def test_e2e_old_stored_bars_with_empty_pull_today_not_no_source(tmp_path: Path) -> None:
    """[the delisted-mid-history twin] a symbol with OLD stored bars whose
    pull TODAY returns vendor-empty (the adapter IS consulted — the widened
    range plans a new window) is NOT no-source: the lake holds run-1 bars, the
    derive runs off them (zero stored bars is a CUMULATIVE lake property,
    never a this-pull delta)."""
    lake = tmp_path / "lake"
    first = _Chain(lake, [_nifty(), _reliance()], real_prebuild=True)
    report1, _ = first.run()
    assert report1.ok is True
    # Run 2 widens the range by one day and the vendor now returns EMPTY for
    # RELIANCE — the new window genuinely consults the adapter.
    adapter = _RecordingAdapter(empty_symbols=frozenset({"RELIANCE"}))
    second = _Chain(
        lake, [_nifty(), _reliance()], adapter=adapter, to_date="2024-01-04", real_prebuild=True
    )
    report2, note2 = second.run()
    assert any(sym == "RELIANCE" for sym, _s, _e in second.adapter.fetches)
    assert report2.prebuild_no_source == ()  # old stored bars ⇒ NOT vendor-empty
    assert report2.ok is True
    assert second.warm_calls == 1
    assert "symbol(s) skipped — no stored 1-minute data" not in note2
    # The derive genuinely ran off the stored bars — the derived 1day exists.
    assert (lake / "bars" / "NSE" / "CASH" / "RELIANCE" / "1day").is_dir()


def test_e2e_stale_derived_1day_no_source_note_never_claims_warm_exclusion(
    tmp_path: Path,
) -> None:
    """[refuter round 2 — the stale-derived-data twin] a symbol whose 1-minute
    partition was emptied AFTER a healthy run (post-pull removal — e.g. a
    lake-heal quarantine MOVE, operator pruning, a legacy vendor-fetched-1day
    lake) while a previously DERIVED ``1day`` partition survives still
    classifies ``no_source`` (zero stored 1-MINUTE bars is the law) and the
    warm still fires — but the note's skip clause claims ONLY the
    timeframe-build exclusion: warm discovery is parquet-file-gated per
    DATASET, so the surviving stored ``1day`` legitimately keeps the symbol in
    the warmed snapshot (genuine stored bars — /movers would serve them with
    or without the warm; never fabricated). A "+ movers warm" exclusion claim
    here would be false (executed refuter-round-2 evidence) — this pin fails
    on the pre-fix wording. The window ledger keeps the run-1 settled SUCCESS
    ``bars>0`` rows (the heal never rewrites the ledger) — the post-pull
    removal signature the runbook §7 third-shape branch documents."""
    import shutil

    lake = tmp_path / "lake"
    first = _Chain(lake, [_nifty(), _reliance()], real_prebuild=True)
    report1, _ = first.run()
    assert report1.ok is True
    rel_1m = lake / "bars" / "NSE" / "CASH" / "RELIANCE" / "1minute"
    rel_1d = lake / "bars" / "NSE" / "CASH" / "RELIANCE" / "1day"
    assert any(rel_1m.rglob("*.parquet")) and any(rel_1d.rglob("*.parquet"))
    shutil.rmtree(rel_1m)  # the post-pull 1m removal; the derived 1day SURVIVES
    second = _Chain(
        lake,
        [_nifty(), _reliance()],
        adapter=_RecordingAdapter(empty_symbols=frozenset({"RELIANCE"})),
        real_prebuild=True,
    )
    report2, note2 = second.run()
    assert report2.prebuild_no_source == ("RELIANCE",)
    assert report2.ok is True
    assert second.warm_calls == 1  # the warm fires — nothing withheld
    # The honest clause: timeframe-build exclusion ONLY.
    assert (
        ", 1 symbol(s) skipped — no stored 1-minute data (vendor-empty or never pulled), "
        "excluded from timeframe build: RELIANCE" in note2
    )
    assert "+ movers warm" not in note2
    # The overclaim's counter-evidence, kept VISIBLE: the surviving derived
    # 1day keeps the symbol in warm discovery AND the REAL warm-side candidate
    # compute — genuine stored bars served as any stored data is.
    from brutex.dashboard.db_view import list_candle_datasets
    from brutex.dashboard.movers_snapshot import compute_spot_candidates
    from brutex.dashboard.movers_warm import latest_movers_date

    discovered = {d["symbol"] for d in list_candle_datasets(lake)}
    assert "RELIANCE" in discovered
    warm_date = latest_movers_date(lake)
    assert warm_date is not None
    cands = compute_spot_candidates(lake_root=lake, interval="1day", date=warm_date, time=None)
    assert "RELIANCE" in {snap.instrument.symbol for snap in cands.snapshots}
    # The runbook §7 third-shape /coverage/windows signature: the durable
    # ledger keeps settled SUCCESS bars>0 rows for the post-pull-removed
    # symbol — never the vendor-empty "settled SKIPPED, bars=0" shape.
    from brutex.data.window_ledger_store import load_window_ledger

    rel_rows = [r for r in load_window_ledger(lake).records() if r.security_id == "RELIANCE"]
    assert rel_rows and all(r.status == "SUCCESS" and r.bars > 0 for r in rel_rows)


def test_e2e_no_source_note_survives_spot_only_surgery(tmp_path: Path) -> None:
    """[T21 — the note-surgery anchor pin] a note CARRYING the no-source skip
    clause round-trips ``build_spot_only_note`` with the clause preserved and
    NO ``pull_scope.note_surgery_miss`` WARN (the clause contains neither
    surgery anchor)."""
    from structlog.testing import capture_logs

    adapter = _RecordingAdapter(empty_symbols=frozenset({"RELIANCE"}))
    chain = _Chain(tmp_path / "lake", [_nifty(), _reliance()], adapter=adapter, real_prebuild=True)
    with capture_logs() as logs:
        _report, note = chain.run()
    assert note.startswith("spot-only pipeline complete — ")
    assert ", F&O skipped by design (spot-only scope)" in note  # the surgery applied
    assert (
        ", 1 symbol(s) skipped — no stored 1-minute data (vendor-empty or never pulled), "
        "excluded from timeframe build: RELIANCE" in note
    )
    assert not [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]


def test_vendor_empty_materialize_remediation_names_both_causes(tmp_path: Path) -> None:
    """[refuter-r1 r3 F-2] the ``materialize_lake_timeframes`` no-stored-bars
    refusal names BOTH candidate causes — "pull the spot first" was the WRONG
    remediation when the spot WAS pulled and the vendor cleanly returned
    nothing (there is nothing to pull)."""
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    with pytest.raises(ValueError) as exc:
        materialize_lake_timeframes(
            lake_root=tmp_path / "lake",
            exchange="NSE",
            segment="CASH",
            symbol="RELIANCE",
            kind=PrebuildKind.SPOT,
            include_daily=True,
        )
    msg = str(exc.value)
    assert "no stored 1minute bars for NSE/CASH/RELIANCE" in msg
    assert "if the 1-minute spot was never pulled, pull it first" in msg
    assert "vendor cleanly returned no data" in msg
    assert "no action needed" in msg


def test_e2e_evening_muhurat_deferral_note_wording_and_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[refuter-r1 r2 MINOR-1 — the reproduced evening instant] frozen
    2024-11-01 18:30 IST (inside the listed 18:00-19:00 evening session) the
    composed belt defers the whole chain, and the surged note no longer
    claims "inside NSE market-hours window (08:45-15:35 IST)" (FALSE at
    18:30) nor "resume on the next out-of-hours run" (self-contradictory) —
    while the ``auto_pull_gate.muhurat_belt_halt`` WARN attributes the halt
    in the logs. The 2025 AFTERNOON twin above stays green unchanged."""
    from structlog.testing import capture_logs

    frozen = datetime(2024, 11, 1, 18, 30)  # inside the 18:00-19:00 session
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    bare = resolve_market_closed()
    assert bare() is False  # 18:30 is genuinely OUT of the 08:45-15:35 window
    composed = compose_market_closed(bare)
    chain = _Chain(tmp_path / "lake", [_nifty()], market_closed=composed)
    with capture_logs() as logs:
        report, note = chain.run()
    assert report.stopped_after == "market_hours"
    assert chain.adapter.fetches == []  # ZERO vendor calls during the session
    assert (
        "market-session protection window active "
        "(NSE hours 08:45-15:35 IST or a Muhurat session)" in note
    )
    assert "inside NSE market-hours window (08:45-15:35 IST)" not in note  # the false claim
    assert "F&O not applicable — spot-only scope" in note
    assert "resume on the next out-of-hours run" not in note
    assert "resume once the protection window clears" in note
    belt = [e for e in logs if e["event"] == "auto_pull_gate.muhurat_belt_halt"]
    assert len(belt) == 1  # machine attribution — the logs stop misattributing
    assert belt[0]["instant"] == frozen.isoformat()


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
