"""Unit tests for the pipeline's ONE automatic prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001).

100% line+branch over ``brutex.dashboard.pipeline_prebuild``: the eager
SPOT/FUTURES/OPTIONS discovery from the F&O candle-lake cascade, the
target-shaped injected-materialiser seam (``include_daily`` is the ROUTE's
binding — here the seam is a pure fake), the outcome rollup (built symbols /
total bars / capped failure names / true failed_count / ok), the LOUD
structured logs (``pipeline.prebuild.started`` / ``.phase_done`` / ``.done`` /
``.failed``), the crash guard (``run_pipeline_prebuild_guarded`` returns an
honest ``ok=False`` outcome, never a crashed job, never a silent zero), and
the global-stop boundary (a ``LakeWriteHalt`` PROPAGATES out of the unguarded
composer).

Pure — a fake ``materialize`` keyed by target symbol; the REAL cascade builder
over an in-memory dataset list. No lake / DuckDB / FastAPI.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.dashboard.fno_cascade import FnoCascadeIndex, build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    MAX_REPORTED_FAILURES,
    PrebuildOutcome,
    PrebuildTarget,
    run_pipeline_prebuild,
    run_pipeline_prebuild_guarded,
)
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
    """One NIFTY future + one CE + one PE in the lake cascade."""
    return build_fno_cascade(
        [
            _dataset("NSE-NIFTY-30Jan25-FUT", "FUT"),
            _dataset("NSE-NIFTY-30Jan25-20000-CE", "CE", strike="20000"),
            _dataset("NSE-NIFTY-30Jan25-20000-PE", "PE", strike="20000"),
        ]
    )


class _FakeMaterializer:
    """Target-recording materialiser; raises per the injected failure map."""

    def __init__(self, *, fail: dict[str, Exception] | None = None, bars: int = 3) -> None:
        self.fail = fail or {}
        self.bars = bars
        self.targets: list[PrebuildTarget] = []

    def __call__(self, target: PrebuildTarget) -> MaterializeResult:
        self.targets.append(target)
        exc = self.fail.get(target.symbol)
        if exc is not None:
            raise exc
        return MaterializeResult(written={"5m": self.bars, "1day": 1}, skipped=())


# ── the happy path: full SPOT + FUTURES + OPTIONS composition ──────────────


def test_prebuild_covers_spot_futures_and_options_with_daily() -> None:
    mat = _FakeMaterializer()
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        materialize=mat,
    )
    # 1 spot underlying + 1 future + 2 options, each 4 bars (3 intraday + 1 daily).
    assert outcome == PrebuildOutcome(
        symbols_built=4, total_bars=16, failed=(), failed_count=0, ok=True
    )
    by_symbol = {t.symbol: t for t in mat.targets}
    assert by_symbol["NSE-NIFTY"].kind is PrebuildKind.SPOT
    assert by_symbol["NSE-NIFTY"].segment == "INDEX"
    assert by_symbol["NSE-NIFTY-30Jan25-FUT"].kind is PrebuildKind.FUTURES
    assert by_symbol["NSE-NIFTY-30Jan25-20000-CE"].kind is PrebuildKind.OPTIONS
    assert by_symbol["NSE-NIFTY-30Jan25-20000-PE"].kind is PrebuildKind.OPTIONS
    # Contract targets carry the F&O lake segment + the cascade's exchange.
    fut = by_symbol["NSE-NIFTY-30Jan25-FUT"]
    assert (fut.segment, fut.exchange) == ("FNO", "NSE")


def test_prebuild_custom_fno_segment_is_threaded() -> None:
    mat = _FakeMaterializer()
    run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        materialize=mat,
        fno_segment="FNO2",
    )
    contract_segments = {t.segment for t in mat.targets if t.kind is not PrebuildKind.SPOT}
    assert contract_segments == {"FNO2"}


def test_prebuild_no_fno_data_builds_spot_only() -> None:
    mat = _FakeMaterializer()
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-BANKNIFTY")],  # not in the cascade
        cascade=_cascade(),
        materialize=mat,
    )
    assert outcome.symbols_built == 1  # spot only — no fabricated contracts
    assert outcome.ok is True
    assert [t.kind for t in mat.targets] == [PrebuildKind.SPOT]


def test_prebuild_empty_instruments_is_clean_zero() -> None:
    outcome = run_pipeline_prebuild(
        instruments=[], cascade=_cascade(), materialize=_FakeMaterializer()
    )
    assert outcome == PrebuildOutcome(
        symbols_built=0, total_bars=0, failed=(), failed_count=0, ok=True
    )


# ── quarantine + rollup ─────────────────────────────────────────────────────


def test_prebuild_per_symbol_failure_quarantined_and_named() -> None:
    mat = _FakeMaterializer(fail={"NSE-NIFTY-30Jan25-20000-CE": ValueError("poisoned")})
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY")], cascade=_cascade(), materialize=mat
    )
    assert outcome.ok is False
    assert outcome.symbols_built == 3  # spot + future + the healthy PE still built
    assert outcome.failed == ("NSE-NIFTY-30Jan25-20000-CE",)
    assert outcome.failed_count == 1
    # The healthy PE was STILL materialised (one bad symbol never aborts a stage).
    assert any(t.symbol == "NSE-NIFTY-30Jan25-20000-PE" for t in mat.targets)


def test_prebuild_failure_names_capped_at_ten_with_true_count() -> None:
    instruments = [_spot(f"U{i}") for i in range(12)]
    mat = _FakeMaterializer(fail={f"U{i}": ValueError("no 1m") for i in range(12)})
    outcome = run_pipeline_prebuild(
        instruments=instruments, cascade=build_fno_cascade([]), materialize=mat
    )
    assert len(outcome.failed) == MAX_REPORTED_FAILURES  # names capped at 10
    assert outcome.failed_count == 12  # the TRUE total survives
    assert outcome.symbols_built == 0
    assert outcome.ok is False


def test_prebuild_global_stop_propagates_unguarded() -> None:
    """LakeWriteHalt (disk-full) is a global stop — NEVER per-symbol-quarantined."""
    from brutex.data.errors import LakeWriteHalt

    mat = _FakeMaterializer(fail={"NSE-NIFTY": LakeWriteHalt("disk_full")})
    with pytest.raises(LakeWriteHalt, match="disk_full"):
        run_pipeline_prebuild(instruments=[_spot("NSE-NIFTY")], cascade=_cascade(), materialize=mat)


# ── loud structured logs (emitted from the pure module — testable) ─────────


def test_prebuild_emits_started_phase_done_and_done_logs() -> None:
    with capture_logs() as logs:
        run_pipeline_prebuild(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            materialize=_FakeMaterializer(),
        )
    started = [e for e in logs if e["event"] == "pipeline.prebuild.started"]
    assert len(started) == 1
    assert started[0]["spot_symbols"] == 1
    assert started[0]["futures_contracts"] == 1
    assert started[0]["options_contracts"] == 2
    phase_done = [e for e in logs if e["event"] == "pipeline.prebuild.phase_done"]
    assert [e["phase"] for e in phase_done] == ["spot", "futures", "options"]
    assert all({"attempted", "succeeded", "failed", "total_bars"} <= set(e) for e in phase_done)
    done = [e for e in logs if e["event"] == "pipeline.prebuild.done"]
    assert len(done) == 1
    assert done[0]["symbols_built"] == 4
    assert done[0]["failed_count"] == 0
    assert done[0]["duration_s"] >= 0


# ── the crash guard (the route seam) ────────────────────────────────────────


def test_guarded_prebuild_success_passes_outcome_through() -> None:
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_FakeMaterializer(),
    )
    assert outcome.ok is True
    assert outcome.symbols_built == 4


def test_guarded_prebuild_catches_cascade_fault_loudly() -> None:
    """A cascade-build fault is INSIDE the guard: ok=False + a LOUD log."""

    def _boom_cascade() -> FnoCascadeIndex:
        raise RuntimeError("cascade discovery exploded")

    with capture_logs() as logs:
        outcome = run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_boom_cascade,
            materialize=_FakeMaterializer(),
        )
    assert outcome == PrebuildOutcome(
        symbols_built=0, total_bars=0, failed=(), failed_count=0, ok=False
    )
    failed = [e for e in logs if e["event"] == "pipeline.prebuild.failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    assert "cascade discovery exploded" in failed[0]["error"]


def test_guarded_prebuild_catches_global_stop_after_successful_fetch() -> None:
    """Even a LakeWriteHalt is caught AT THE ROUTE SEAM (the fetch already
    succeeded — nothing here may kill the job) and reported ok=False, loudly."""
    from brutex.data.errors import LakeWriteHalt

    mat = _FakeMaterializer(fail={"NSE-NIFTY": LakeWriteHalt("disk_full")})
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=mat,
        )
    assert outcome.ok is False
    assert outcome.symbols_built == 0
    failed = [e for e in logs if e["event"] == "pipeline.prebuild.failed"]
    assert failed and failed[0]["error_type"] == "LakeWriteHalt"


def test_guarded_prebuild_threads_segment_and_source() -> None:
    mat = _FakeMaterializer()
    with capture_logs() as logs:
        run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=mat,
            fno_segment="FNO9",
            source="pipeline",
        )
    assert {t.segment for t in mat.targets if t.kind is not PrebuildKind.SPOT} == {"FNO9"}
    assert any(e["event"] == "pipeline.prebuild.done" for e in logs)
