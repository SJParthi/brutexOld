"""Orchestrator G1/R45 seam coverage (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

Stem-matching supplement so the touched-module discovery gate proves
``brutex.engine.sweep_orchestrator`` at 100% over its OWN suite: the WP-5
additions — the G1 quarantine-then-ONE-retry ladder and the R45 post-persist
diffcheck invocation on :func:`run_one_tuple` — were previously exercised only
by the run-core suites (a foreign stem the gate cannot map).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStoreError
from brutex.engine.sweep_kernel import ResidentKernelCache
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    make_store_for,
    run_one_tuple,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, SweepComboError
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

pytest.importorskip("polars")
pytest.importorskip("lmdb")

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g1_quarantine_then_one_retry_regenerates_the_tuple": ("G1",),
    "test_g1_second_store_failure_demotes_to_combo_error": ("G1",),
    "test_g1_no_quarantine_seam_keeps_the_raw_abort": ("G1",),
    "test_g1_vix_trio_conflict_sails_past_the_quarantine": ("G1",),
    "test_r45_diffcheck_invoked_post_persist_with_kernel_bits": ("R45",),
}

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    price = 100.0
    for m in range(40):
        close = price + 0.6 if m % 3 else price - 0.3
        ts = (datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(minutes=m)).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(max(price, close) + 0.5, 2))),
                low=Decimal(str(round(min(price, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return out


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap-g1",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _run(out: Path, audit_path: Path, **overrides: Any) -> Any:
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        return run_one_tuple(
            grid,
            tuple_,
            bars_for=lambda _i, _t: _bars(),
            store_for=make_store_for(out, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            kernel_cache=ResidentKernelCache(capacity=1),
            **overrides,
        )


def _poison(out: Path) -> Path:
    grid = _grid()
    store_path = tuple_store_path(out, next(iter_sweep_tuples(grid)), grid)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(b"\x00not-a-parquet")
    return store_path


def test_g1_quarantine_then_one_retry_regenerates_the_tuple(tmp_path: Path) -> None:
    out = tmp_path / "out"
    store_path = _poison(out)
    seen: list[tuple[str, str]] = []

    def _quarantine(tuple_: Any, exc: BaseException) -> None:
        seen.append((tuple_.instrument, type(exc).__name__))
        store_path.unlink()  # the trio move — the retry starts FRESH

    summary = _run(out, tmp_path / "a.jsonl", store_quarantine=_quarantine)
    # ResultsStore.load now types a corrupt/undecodable store as the
    # ResultsStoreSchemaError subclass of ResultsStoreError (main fold,
    # DEC-SWEEP-RESULTS-DEPTH-001 ruling 5) — the quarantine still catches the
    # ResultsStoreError family, so the trio move + fresh retry are unchanged.
    assert seen == [("NIFTY", "ResultsStoreSchemaError")]
    assert summary is not None
    assert summary.persisted >= 0  # the retry genuinely re-ran the tuple
    assert store_path.exists()  # regenerated as a REAL store
    assert store_path.read_bytes()[:4] == b"PAR1"


def test_g1_second_store_failure_demotes_to_combo_error(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _poison(out)
    calls = {"n": 0}

    def _quarantine_noop(tuple_: Any, exc: BaseException) -> None:
        calls["n"] += 1  # deliberately LEAVES the poison — the retry fails too

    with pytest.raises(SweepComboError, match="after quarantine \\+ one fresh retry"):
        _run(out, tmp_path / "a.jsonl", store_quarantine=_quarantine_noop)
    assert calls["n"] == 1  # exactly ONE quarantine + ONE retry — never a loop


def test_g1_no_quarantine_seam_keeps_the_raw_abort(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _poison(out)
    with pytest.raises(ResultsStoreError):
        _run(out, tmp_path / "a.jsonl", store_quarantine=None)


def test_g1_vix_trio_conflict_sails_past_the_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VIX-trio conflict is NOT store corruption — it must SKIP the G1 ladder.

    Merge-seam regression (DEC-VIX-STAMP-WIRE-001 x DEC-SWEEP-UNKILLABLE-CORE-001):
    :class:`~brutex.engine.results_store.VixTrioConflictError` subclasses
    ``ResultsStoreError``, so without an explicit passthrough the G1
    quarantine-retry ladder would quarantine the tuple's trio and silently
    REGENERATE it — masking the loud #904 tuple-skip contract the grid
    drivers implement by catching it beside ``SweepComboError``. The typed
    conflict must propagate out of :func:`run_one_tuple` with the quarantine
    seam NEVER invoked (the stored trio stays untouched for forensics).
    """
    from brutex.engine.results_store import VixTrioConflictError

    out = tmp_path / "out"
    quarantined: list[BaseException] = []

    def _quarantine(_tuple: Any, exc: BaseException) -> None:
        quarantined.append(exc)  # pragma: no cover — MUST never fire

    def _vix_moved(*_args: Any, **_kwargs: Any) -> Any:
        raise VixTrioConflictError("VIX trio conflict: the VIX lake moved between runs")

    import brutex.engine.sweep_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "run_persisted_sweep", _vix_moved)
    with pytest.raises(VixTrioConflictError, match="VIX lake moved"):
        _run(out, tmp_path / "a.jsonl", store_quarantine=_quarantine)
    assert quarantined == []  # sailed PAST the quarantine — the grid skips the tuple


def test_r45_diffcheck_invoked_post_persist_with_kernel_bits(tmp_path: Path) -> None:
    out = tmp_path / "out"
    seen: list[dict[str, Any]] = []

    def _diffcheck(*, bars: Any, bar_bits: Any, tuple_: Any, grid: Any) -> None:
        seen.append({"n_bars": len(bars), "n_bits": len(bar_bits), "tuple": tuple_.instrument})

    summary = _run(out, tmp_path / "a.jsonl", diffcheck=_diffcheck)
    assert summary is not None
    assert seen == [{"n_bars": 40, "n_bits": 40, "tuple": "NIFTY"}]
