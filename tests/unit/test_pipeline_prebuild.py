"""Unit tests for the pipeline's ONE automatic prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001).

100% line+branch over ``brutex.dashboard.pipeline_prebuild``: the eager
SPOT/FUTURES/OPTIONS discovery from the F&O candle-lake cascade, the
target-shaped injected-materialiser seam (``include_daily`` is the ROUTE's
binding — here the seam is a pure fake), the outcome rollup (built symbols /
total bars / capped failure names / true failed_count / ok), the LOUD
structured logs (``pipeline.prebuild.started`` / ``.phase_done`` / ``.done`` /
``.failed``), the crash guard (``run_pipeline_prebuild_guarded`` turns a
NON-halt crash into an honest ``ok=False`` outcome carrying
``crashed=<ErrorType>`` — never a silent zero), and the global-stop boundary
(a ``LakeWriteHalt`` / quota stop PROPAGATES out of the unguarded composer
AND RE-RAISES out of the guard — ``pull_jobs.is_pull_halt`` — so the job
layer's CRITICAL pull-halt alert can fire).

Pure — a fake ``materialize`` keyed by target symbol; the REAL cascade builder
over an in-memory dataset list. No lake / DuckDB / FastAPI.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.dashboard.fno_cascade import FnoCascadeIndex, build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    MAX_REPORTED_FAILURES,
    PrebuildOutcome,
    PrebuildTarget,
    run_pipeline_prebuild,
    run_pipeline_prebuild_guarded,
)
from brutex.data.fs_probe import AllocationProbe
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
        symbols_built=0, total_bars=0, failed=(), failed_count=0, ok=False, crashed="RuntimeError"
    )
    failed = [e for e in logs if e["event"] == "pipeline.prebuild.failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    assert "cascade discovery exploded" in failed[0]["error"]


def test_guarded_prebuild_crash_marker_distinguishes_crash_from_built_zero() -> None:
    """A swallowed NON-halt crash carries ``crashed=<ErrorType>`` — honestly
    distinguishable from "ran, built 0" (whose ``crashed`` stays None)."""

    def _bad_cascade() -> FnoCascadeIndex:
        raise ValueError("bad dataset row")

    crashed_outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_bad_cascade,
        materialize=_FakeMaterializer(),
    )
    assert crashed_outcome.crashed == "ValueError"
    assert crashed_outcome.ok is False
    # The contrast: a run that COMPLETED with zero built keeps crashed=None.
    ran_zero = run_pipeline_prebuild_guarded(
        instruments=[], cascade_or_error=_cascade, materialize=_FakeMaterializer()
    )
    assert ran_zero.crashed is None
    assert ran_zero.ok is True


def test_guarded_prebuild_reraises_lake_write_halt_from_materialize() -> None:
    """A disk-full LakeWriteHalt during a materialise RE-RAISES out of the
    GUARDED runner (hostile-review F1): it must reach BackgroundPullJob._run's
    halt handling so the CRITICAL Telegram pull-halt alert fires — swallowing
    it would hide the halt behind a benign "built 0". No misleading
    ``pipeline.prebuild.failed`` log is emitted on this path."""
    from brutex.data.errors import LakeWriteHalt

    mat = _FakeMaterializer(fail={"NSE-NIFTY": LakeWriteHalt("disk_full")})
    with capture_logs() as logs, pytest.raises(LakeWriteHalt, match="disk_full"):
        run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=mat,
        )
    assert not [e for e in logs if e["event"] == "pipeline.prebuild.failed"]


def _global_stop_errors() -> list[Exception]:
    """The pull-wide halt set — DERIVED from the spine's ``_GLOBAL_STOP_ERRORS``.

    Previously a hand-copied list that silently went stale when
    DEC-VENDOR-DOWN-BREAKER-001 added ``VendorDownError`` +
    ``GrowwCredentialDeadError`` (the exact drift this pin exists to catch —
    hostile-review MINOR folded). Now built generically from
    ``run_ingest._GLOBAL_STOP_ERRORS`` (every member is bare-message
    constructible by the drift-pin contract), so a NEW global-stop class is
    picked up automatically and can never be missed here again.
    """
    from brutex.adapters.groww.run_ingest import (
        _GLOBAL_STOP_ERRORS,  # pyright: ignore[reportPrivateUsage] — the drift-pin source
    )

    return [cls(f"{cls.__name__} halt") for cls in _GLOBAL_STOP_ERRORS]


@pytest.mark.parametrize("halt", _global_stop_errors(), ids=lambda e: type(e).__name__)
def test_guarded_prebuild_reraises_every_global_stop_class(halt: Exception) -> None:
    """EVERY member of the job layer's ``is_pull_halt`` set re-raises out of the
    guard (one COMMON predicate — no second tuple that could drift), even when
    raised by the cascade builder itself."""

    def _halting_cascade() -> FnoCascadeIndex:
        raise halt

    with pytest.raises(type(halt)):
        run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_halting_cascade,
            materialize=_FakeMaterializer(),
        )


@pytest.mark.parametrize("halt", _global_stop_errors(), ids=lambda e: type(e).__name__)
def test_is_pull_halt_matches_every_spine_global_stop(halt: Exception) -> None:
    """``pull_jobs.is_pull_halt`` matches EVERY spine ``_GLOBAL_STOP_ERRORS`` member.

    The behavioral membership pin the hostile review found missing: a mutation
    deleting ``VendorDownError`` / ``GrowwCredentialDeadError`` (or any other
    member) from the hand-mirrored ``is_pull_halt`` tuple previously passed the
    whole suite, silently dropping the dashboard's generic CRITICAL pull-halt
    alert for that halt class.
    """
    from brutex.dashboard.pull_jobs import is_pull_halt

    assert is_pull_halt(halt) is True


def test_is_pull_halt_rejects_a_plain_domain_error() -> None:
    """A per-symbol domain error is NEVER classified as a pull-wide halt."""
    from brutex.dashboard.pull_jobs import is_pull_halt
    from brutex.models import BruteXError

    assert is_pull_halt(BruteXError("one symbol failed")) is False
    assert is_pull_halt(RuntimeError("code bug")) is False


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


# ── lake-root allocation probe (DEC-LAKE-FS-PROBE-001) ───────────────────────


def test_prebuild_probes_lake_root_and_carries_fs_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A given ``lake_root`` is allocation-probed ONCE; a COARSE note rides the
    outcome + the ``pipeline.prebuild.started`` log (DEC-LAKE-FS-PROBE-001)."""
    probed: list[Path] = []

    def _fake_probe(root: Path) -> AllocationProbe:
        probed.append(root)
        return AllocationProbe(
            allocation_unit_bytes=131072, coarse=True, note="COARSE filesystem allocation (fake)"
        )

    monkeypatch.setattr(
        "brutex.dashboard.pipeline_prebuild.probe_allocation_granularity", _fake_probe
    )
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild(
            instruments=[_spot("NIFTY")],
            cascade=_cascade(),
            materialize=_FakeMaterializer(),
            lake_root=tmp_path / "lake",
        )
    assert probed == [tmp_path / "lake"]
    assert outcome.fs_note == "COARSE filesystem allocation (fake)"
    (started,) = [e for e in logs if e["event"] == "pipeline.prebuild.started"]
    assert started["fs_allocation"] == "COARSE filesystem allocation (fake)"


def test_prebuild_fine_allocation_reads_ok_in_started_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fine-grained probe (note None) keeps ``fs_allocation="ok"`` + fs_note None."""
    monkeypatch.setattr(
        "brutex.dashboard.pipeline_prebuild.probe_allocation_granularity",
        lambda _root: AllocationProbe(allocation_unit_bytes=4096, coarse=False, note=None),
    )
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild(
            instruments=[_spot("NIFTY")],
            cascade=_cascade(),
            materialize=_FakeMaterializer(),
            lake_root=tmp_path / "lake",
        )
    assert outcome.fs_note is None
    (started,) = [e for e in logs if e["event"] == "pipeline.prebuild.started"]
    assert started["fs_allocation"] == "ok"


def test_prebuild_without_lake_root_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFAULT-PRESERVATION: ``lake_root=None`` skips the probe entirely."""

    def _must_not_probe(_root: Path) -> AllocationProbe:  # pragma: no cover — must NOT be called
        raise AssertionError("no lake_root ⇒ no probe")

    monkeypatch.setattr(
        "brutex.dashboard.pipeline_prebuild.probe_allocation_granularity", _must_not_probe
    )
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NIFTY")], cascade=_cascade(), materialize=_FakeMaterializer()
    )
    assert outcome.fs_note is None


def test_guarded_prebuild_threads_lake_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarded route seam passes ``lake_root`` through to the composer."""
    probed: list[Path] = []

    def _fake_probe(root: Path) -> AllocationProbe:
        probed.append(root)
        return AllocationProbe(allocation_unit_bytes=131072, coarse=True, note="coarse (fake)")

    monkeypatch.setattr(
        "brutex.dashboard.pipeline_prebuild.probe_allocation_granularity", _fake_probe
    )
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NIFTY")],
        cascade_or_error=_cascade,
        materialize=_FakeMaterializer(),
        lake_root=tmp_path / "lake",
    )
    assert probed == [tmp_path / "lake"]
    assert outcome.fs_note == "coarse (fake)"


# ── incremental unchanged-skip counting (DEC-PREBUILD-INCREMENTAL-001) ──────


class _UnchangedMaterializer:
    """Marks the given symbols as incremental unchanged-skips (zero writes)."""

    def __init__(self, unchanged: set[str]) -> None:
        self.unchanged = unchanged

    def __call__(self, target: PrebuildTarget) -> MaterializeResult:
        if target.symbol in self.unchanged:
            return MaterializeResult(written={}, skipped=(), unchanged=True)
        return MaterializeResult(written={"5m": 3, "1day": 1}, skipped=())


def test_prebuild_counts_unchanged_skips_honestly() -> None:
    # 1 spot + 1 FUT + 2 options; the FUT + one option are unchanged-skipped.
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        materialize=_UnchangedMaterializer({"NSE-NIFTY-30Jan25-FUT", "NSE-NIFTY-30Jan25-20000-CE"}),
    )
    assert outcome.unchanged_skips == 2
    assert outcome.symbols_built == 4  # a skipped symbol is still current + counted built
    assert outcome.total_bars == 8  # only the two FULL derives wrote bars
    assert outcome.ok is True


def test_prebuild_zero_unchanged_skips_by_default() -> None:
    outcome = run_pipeline_prebuild(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        materialize=_FakeMaterializer(),
    )
    assert outcome.unchanged_skips == 0


def test_prebuild_done_log_carries_unchanged_skips() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        run_pipeline_prebuild(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            materialize=_UnchangedMaterializer({"NSE-NIFTY-30Jan25-FUT"}),
        )
    done = [e for e in logs if e["event"] == "pipeline.prebuild.done"]
    assert len(done) == 1
    assert done[0]["unchanged_skips"] == 1
