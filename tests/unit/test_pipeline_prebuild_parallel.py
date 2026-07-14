"""Unit tests for the PARALLEL pipeline prebuild orchestration (DEC-HARDWARE-ENVELOPE-001).

Covers :func:`brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_parallel`
+ the guard dispatch — the SPOT/FUTURES/OPTIONS discovery, the thread-pool fan
out, the parent-side tally fold, and the two merge-blocking mitigations:

* **M1** — the daily forming-session cutoff is resolved ONCE per run and threaded
  identically into every worker (byte-identity across midnight IST).
* **M4** — work items are deduped by ``(exchange, segment, symbol)`` so a #911
  drift pair never materialises the same partition twice.

Offline — ``timeframe_materialize.materialize_lake_timeframes`` is monkeypatched
to a symbol-keyed fake (visible in the worker threads), so no real lake/polars
is touched. The shared discovery / report / outcome code is the SAME COMMON path
the route uses.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.calendars.nse import IST
from brutex.dashboard.fno_cascade import FnoCascadeIndex, build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    PrebuildTarget,
    run_pipeline_prebuild_guarded,
    run_pipeline_prebuild_parallel,
)
from brutex.data.errors import LakeWriteHalt
from brutex.data.lake_footprint import DiskGuardDecision
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs


def _spot(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(name)) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _dataset(symbol: str, itype: str, *, strike: str | None = None) -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange": "NSE",
        "underlying": "NIFTY",
        "expiry": "2025-01-30",
        "strike": strike,
        "option_type": itype if itype in ("CE", "PE") else None,
        "instrument_type": itype,
        "interval": "1minute",
    }


def _cascade() -> FnoCascadeIndex:
    return build_fno_cascade(
        [
            _dataset("NSE-NIFTY-30Jan25-FUT", "FUT"),
            _dataset("NSE-NIFTY-30Jan25-20000-CE", "CE", strike="20000"),
            _dataset("NSE-NIFTY-30Jan25-20000-PE", "PE", strike="20000"),
        ]
    )


class _FakeMaterialize:
    """A thread-safe symbol-keyed fake for ``materialize_lake_timeframes``.

    Records every call's kwargs; raises per an injected failure map; returns a
    :class:`MaterializeResult` carrying per-symbol ``unchanged`` /
    ``opens_substituted`` when configured.
    """

    def __init__(
        self,
        *,
        fail: dict[str, Exception] | None = None,
        unchanged: set[str] | None = None,
        opens: dict[str, int] | None = None,
    ) -> None:
        self.fail = fail or {}
        self.unchanged = unchanged or set()
        self.opens = opens or {}
        self.calls: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs: object) -> MaterializeResult:
        with self._lock:
            self.calls.append(dict(kwargs))
        symbol = str(kwargs["symbol"])
        exc = self.fail.get(symbol)
        if exc is not None:
            raise exc
        return MaterializeResult(
            written={"5m": 3, "1day": 1},
            skipped=(),
            unchanged=symbol in self.unchanged,
            opens_substituted=self.opens.get(symbol, 0),
        )

    def by_symbol(self, symbol: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c["symbol"] == symbol]


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> _FakeMaterialize:
    fake = _FakeMaterialize()
    monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", fake)
    return fake


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeMaterialize) -> None:
    monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", fake)


# ── happy path: threaded fan-out over SPOT + FUTURES + OPTIONS ──────────────


@pytest.mark.parametrize("workers", [1, 3])
def test_parallel_covers_all_phases(
    patched: _FakeMaterialize, tmp_path: Path, workers: int
) -> None:
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=workers,
        daily_before=date(2025, 2, 1),
    )
    # 1 spot + 1 future + 2 options, each 4 bars (3 intraday + 1 daily).
    assert outcome.symbols_built == 4
    assert outcome.total_bars == 16
    assert outcome.failed_count == 0
    assert outcome.ok is True
    kinds = {str(c["symbol"]): c["kind"] for c in patched.calls}
    assert kinds["NSE-NIFTY"] is PrebuildKind.SPOT
    assert kinds["NSE-NIFTY-30Jan25-FUT"] is PrebuildKind.FUTURES
    assert kinds["NSE-NIFTY-30Jan25-20000-CE"] is PrebuildKind.OPTIONS


def test_parallel_include_spot_false_runs_fno_phases_only(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    """[round-6 two-button split] the PARALLEL twin of the spot-exclusion arm:
    ZERO spot work items are planned, the spot phase carries the COMMON
    by-design note, and the F&O contract phases run for real."""
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=3,
            daily_before=date(2025, 2, 1),
            include_spot=False,
        )
    assert outcome.symbols_built == 3  # 1 future + 2 options — never the spot
    assert outcome.ok is True
    assert all(c["kind"] is not PrebuildKind.SPOT for c in patched.calls)
    started = [e for e in logs if e["event"] == "pipeline.prebuild.started"]
    assert started[0]["spot_symbols"] == 0
    spot_phase = [
        e for e in logs if e["event"] == "pipeline.prebuild.phase_done" and e["phase"] == "spot"
    ]
    assert spot_phase[0]["attempted"] == 0
    # The by-design note itself rides the internal spot PhasePrebuildReport
    # (the SAME COMMON constant on both paths) — note-shape pinned at the
    # gated_prebuild level, where the report is the public surface.


def test_guard_forwards_include_spot_to_parallel(patched: _FakeMaterialize, tmp_path: Path) -> None:
    """The guard forwards ``include_spot`` to the parallel path (workers>1)."""
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=lambda target: (_ for _ in ()).throw(AssertionError("unused")),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
        include_spot=False,
    )
    assert outcome.symbols_built == 3
    assert all(c["kind"] is not PrebuildKind.SPOT for c in patched.calls)


def test_dense_flag_set_for_index_spot_only(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    spot = patched.by_symbol("NSE-NIFTY")[0]
    fut = patched.by_symbol("NSE-NIFTY-30Jan25-FUT")[0]
    assert spot["dense_symbol"] is True  # index spot
    assert fut["dense_symbol"] is False  # contract


# ── M1: daily cutoff resolved ONCE per run ─────────────────────────────────


def test_m1_daily_cutoff_resolved_once_and_identical(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # daily_before=None → the orchestrator pins the run's IST INSTANT ONCE and
    # threads it into EVERY worker as daily_cutoff_now (byte-identity across
    # midnight IST). REPOINTED by DEC-DAILY-CUTOFF-GROUNDED-001: the pinned
    # value is the INSTANT (each worker resolves the session-close-aware
    # daily_derive_cutoff from it), no longer a plain today-IST DATE — the
    # #1401 M1 regression this corrects; daily_before stays None so the dense
    # symbols' data-grounded arm remains reachable.
    from brutex.calendars.nse import daily_derive_cutoff

    before = datetime.now(UTC).astimezone(IST)
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
    )
    after = datetime.now(UTC).astimezone(IST)
    assert {c["daily_before"] for c in patched.calls} == {None}
    instants = {c["daily_cutoff_now"] for c in patched.calls}
    assert len(instants) == 1  # ONE instant per run — the M1 invariant, kept
    (instant,) = instants
    assert isinstance(instant, datetime)
    assert before <= instant <= after
    # The cutoff every worker derives from the pinned instant is the
    # session-close-aware law — well-defined and identical across workers.
    assert daily_derive_cutoff(instant) in {instant.date(), instant.date() + timedelta(days=1)}


def test_m1_explicit_instant_threaded_unchanged(patched: _FakeMaterialize, tmp_path: Path) -> None:
    # A caller-pinned daily_cutoff_now (the route pins ONE instant for BOTH
    # the sequential closure and this parallel path) is threaded verbatim —
    # the orchestrator never re-pins over it.
    pinned = datetime(2026, 7, 1, 16, 0, tzinfo=IST)
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_cutoff_now=pinned,
    )
    assert {c["daily_cutoff_now"] for c in patched.calls} == {pinned}
    assert {c["daily_before"] for c in patched.calls} == {None}


def test_m1_explicit_cutoff_threaded_unchanged(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2024, 12, 25),
    )
    assert {c["daily_before"] for c in patched.calls} == {date(2024, 12, 25)}


def test_m1_no_daily_keeps_none(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        include_daily=False,
    )
    assert {c["daily_before"] for c in patched.calls} == {None}
    assert {c["include_daily"] for c in patched.calls} == {False}


# ── M4: dedup drift pairs by partition coords ──────────────────────────────


def test_m4_drift_pair_materialises_contract_once(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # Two spot instruments (NSE-NIFTY + bare NIFTY) both match cascade underlying
    # NIFTY → both discover the SAME contract partitions. M4 dedups them so each
    # contract materialises exactly ONCE (no double-write / stamp race).
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY"), _spot("NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    # 2 distinct spot partitions + 1 future + 2 options = 5 unique partitions.
    assert len(patched.by_symbol("NSE-NIFTY-30Jan25-FUT")) == 1
    assert len(patched.by_symbol("NSE-NIFTY-30Jan25-20000-CE")) == 1
    assert outcome.symbols_built == 5


def test_m3_sequential_path_dedups_identically(tmp_path: Path) -> None:
    # m-3: the SEQUENTIAL path applies the SAME partition-coords dedup, so a
    # #911 drift pair reports IDENTICAL counts on both paths (not just bytes).
    calls: list[str] = []

    def _seq(target: PrebuildTarget) -> MaterializeResult:
        calls.append(target.symbol)
        return MaterializeResult(written={"5m": 3, "1day": 1}, skipped=())

    from brutex.dashboard.pipeline_prebuild import run_pipeline_prebuild

    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY"), _spot("NIFTY")],
        cascade=_cascade(),
        materialize=_seq,
    )
    # Same counts as the parallel drift-pair test above: 2 spots + 3 contracts.
    assert outcome.symbols_built == 5
    assert calls.count("NSE-NIFTY-30Jan25-FUT") == 1
    assert calls.count("NSE-NIFTY-30Jan25-20000-CE") == 1


def test_m4_duplicate_spot_instrument_materialised_once(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # The spot arm of the dedup (COMMON `_unique_spot_instruments`): the same
    # instrument passed twice materialises its partition exactly once — on the
    # parallel path here, and the sequential path shares the helper.
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY"), _spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    assert len(patched.by_symbol("NSE-NIFTY")) == 1
    assert outcome.symbols_built == 4  # 1 spot + 1 future + 2 options


# ── per-symbol quarantine + tallies ────────────────────────────────────────


def test_per_symbol_failure_quarantined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeMaterialize(fail={"NSE-NIFTY-30Jan25-FUT": ValueError("bad partition")})
    _install(monkeypatch, fake)
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.ok is False
    assert outcome.failed_count == 1
    assert outcome.failed[0].startswith("NSE-NIFTY-30Jan25-FUT")
    assert outcome.symbols_built == 3  # the other 3 still built


def test_tallies_unchanged_and_opens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeMaterialize(
        unchanged={"NSE-NIFTY-30Jan25-20000-CE"},
        opens={"NSE-NIFTY": 4},
    )
    _install(monkeypatch, fake)
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.unchanged_skips == 1
    assert outcome.opens_substituted == 4


# ── exclude_symbols ────────────────────────────────────────────────────────


def test_exclude_partial(patched: _FakeMaterialize, tmp_path: Path) -> None:
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY"), _spot("NSE-RELIANCE")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
        exclude_symbols=frozenset({"NSE-RELIANCE"}),
    )
    built = {str(c["symbol"]) for c in patched.calls}
    assert "NSE-RELIANCE" not in built
    assert "NSE-NIFTY" in built
    assert outcome.ok is True


def test_exclude_all_degrades_honestly(patched: _FakeMaterialize, tmp_path: Path) -> None:
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=2,
            daily_before=date(2025, 2, 1),
            exclude_symbols=frozenset({"NSE-NIFTY"}),
        )
    assert outcome.symbols_built == 0
    assert outcome.ok is True
    assert any(log["event"] == "pipeline.prebuild.all_excluded" for log in logs)
    assert patched.calls == []


def test_empty_note_when_no_contracts(patched: _FakeMaterialize, tmp_path: Path) -> None:
    # A cascade with no matching underlying → futures/options phases empty + noted.
    empty_cascade = build_fno_cascade([])
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=empty_cascade,
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.symbols_built == 1  # spot only
    assert outcome.ok is True


def test_started_log_names_parallel_workers(patched: _FakeMaterialize, tmp_path: Path) -> None:
    with capture_logs() as logs:
        run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=4,
            daily_before=date(2025, 2, 1),
        )
    started = next(log for log in logs if log["event"] == "pipeline.prebuild.started")
    assert started["parallel_workers"] == 4


# ── guard dispatch (sequential vs parallel) ────────────────────────────────


def test_guard_routes_to_parallel_when_workers_gt_1(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    sentinel_called = {"seq": False}

    def _seq_materialize(_target: PrebuildTarget) -> MaterializeResult:
        sentinel_called["seq"] = True
        return MaterializeResult(written={}, skipped=())

    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.symbols_built == 4  # parallel path ran
    assert sentinel_called["seq"] is False  # the injected sequential fake was NOT used


def test_guard_forwards_pinned_cutoff_instant_to_parallel_workers(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # The route seam (DEC-DAILY-CUTOFF-GROUNDED-001): app.py pins ONE
    # post-close instant and threads it via the guard — every parallel worker
    # sees the SAME daily_cutoff_now with daily_before=None (grounding
    # reachable), so a post-close pipeline run's cutoff is session-close-aware
    # (daily_derive_cutoff(pinned) = run day + 1, no longer today's date).
    from brutex.calendars.nse import daily_derive_cutoff

    pinned = datetime(2026, 7, 1, 16, 0, tzinfo=IST)  # post-close Wednesday
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=lambda _t: MaterializeResult(written={}, skipped=()),
        lake_root=tmp_path,
        workers=3,
        daily_cutoff_now=pinned,
    )
    assert outcome.ok is True
    assert {c["daily_cutoff_now"] for c in patched.calls} == {pinned}
    assert {c["daily_before"] for c in patched.calls} == {None}
    assert daily_derive_cutoff(pinned) == date(2026, 7, 2)


def test_guard_routes_to_sequential_when_workers_none(tmp_path: Path) -> None:
    seq_targets: list[PrebuildTarget] = []

    def _seq_materialize(target: PrebuildTarget) -> MaterializeResult:
        seq_targets.append(target)
        return MaterializeResult(written={"5m": 2}, skipped=())

    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=tmp_path,
        workers=None,
    )
    assert outcome.ok is True
    assert seq_targets  # the SEQUENTIAL injected materialiser WAS used


def test_guard_sequential_when_no_lake_root(tmp_path: Path) -> None:
    def _seq_materialize(_target: PrebuildTarget) -> MaterializeResult:
        return MaterializeResult(written={"5m": 2}, skipped=())

    # workers>1 but lake_root None → still sequential (parallel needs a lake_root).
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=None,
        workers=4,
    )
    assert outcome.ok is True


def test_guard_parallel_global_stop_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeMaterialize(fail={"NSE-NIFTY": LakeWriteHalt("disk_full")})
    _install(monkeypatch, fake)
    with pytest.raises(LakeWriteHalt):
        run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=lambda _t: MaterializeResult(written={}, skipped=()),
            lake_root=tmp_path,
            workers=3,
            daily_before=date(2025, 2, 1),
        )


# ── the pre-run disk guard gates the PARALLEL path too (COMMON preflight) ───


def _guard_decision(*, blocked: bool) -> DiskGuardDecision:
    return DiskGuardDecision(
        blocked=blocked,
        free_bytes=1024,
        estimated_need_bytes=4096,
        source_1m_bytes=512,
        derived_bytes=128,
        multiplier=Decimal("6.5"),
        multiplier_source="fallback",
        margin=Decimal("0.10"),
    )


class TestParallelDiskGuard:
    """DEC-LAKE-FOOTPRINT-MEASURED-001 × DEC-HARDWARE-ENVELOPE-001: ``workers > 1``
    can never bypass the disk-space preflight (`_disk_guard_outcome` is the ONE
    shared gate)."""

    def test_blocked_guard_skips_the_parallel_prebuild(
        self, patched: _FakeMaterialize, tmp_path: Path
    ) -> None:
        decision = _guard_decision(blocked=True)
        with capture_logs() as logs:
            outcome = run_pipeline_prebuild_parallel(
                instruments=[_spot("NSE-NIFTY")],
                cascade=_cascade(),
                lake_root=tmp_path,
                workers=3,
                daily_before=date(2025, 2, 1),
                disk_guard=lambda: decision,
            )
        assert outcome.ok is False
        assert outcome.disk_guard_blocked is True
        assert outcome.disk_guard_detail == decision.detail
        assert outcome.symbols_built == 0
        assert patched.calls == []  # nothing materialised — trip is BEFORE the pool
        warned = [e for e in logs if e["event"] == "pipeline.prebuild.disk_guard"]
        assert len(warned) == 1

    def test_clear_guard_runs_parallel_normally(
        self, patched: _FakeMaterialize, tmp_path: Path
    ) -> None:
        outcome = run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=3,
            daily_before=date(2025, 2, 1),
            disk_guard=lambda: _guard_decision(blocked=False),
        )
        assert outcome.ok is True
        assert outcome.disk_guard_blocked is False
        assert outcome.symbols_built == 4

    def test_guarded_forwards_disk_guard_to_parallel_path(
        self, patched: _FakeMaterialize, tmp_path: Path
    ) -> None:
        decision = _guard_decision(blocked=True)
        outcome = run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=lambda _t: MaterializeResult(written={}, skipped=()),
            lake_root=tmp_path,
            workers=3,
            daily_before=date(2025, 2, 1),
            disk_guard=lambda: decision,
        )
        assert outcome.disk_guard_blocked is True
        assert patched.calls == []  # the parallel path was gated, not entered
