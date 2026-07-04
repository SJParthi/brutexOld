"""Integration: an operator Ctrl-C mid-sweep ABORTS the grid (never a tuple skip).

DEC-ENGINE-DEDUP-NAN-CANON-001 (audit H1). The brute-force grid is "Resilient:
skip + continue" ONLY for a :class:`~brutex.engine.sweep_run.SweepComboError` —
the engine catches that narrow class, bumps ``tuples_failed``, audits
``TUPLE_FAILED``, and grinds on through the (33B-trial) grid. A
``KeyboardInterrupt`` is a :class:`BaseException` (NOT an :class:`Exception`), so
it is caught by NEITHER the per-tuple ``except SweepComboError`` NOR a broad
``except Exception`` — an operator Ctrl-C must propagate BARE and abort the WHOLE
grid, not be silently downgraded to a single-tuple skip (the failure the
companion ``MemoryError`` regression guards).

This drives the REAL :func:`~brutex.engine.sweep_orchestrator.run_sweep_grid`
end-to-end (real shared :class:`~brutex.engine.sweep_run.ComboDedup`, real
per-tuple :class:`~brutex.engine.results_store.ResultsStore` sinks, real
hash-chained :class:`~brutex.orchestrator.audit.AuditWriter`) over a 2-tuple grid
whose SECOND tuple raises ``KeyboardInterrupt`` from inside the combo evaluation
loop, and asserts the interrupt escapes the grid bare: the first tuple persisted
its Parquet, the interrupted tuple wrote NOTHING + did NOT bump ``tuples_failed``,
no ``TUPLE_FAILED`` / ``GRID_SWEEP_COMPLETE`` was audited, and the partial audit
chain still verifies (Golden Rule #8).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def test_keyboard_interrupt_mid_grid_aborts_not_a_tuple_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C from the 2nd tuple's eval loop aborts the whole grid (bare raise)."""
    pytest.importorskip("polars")
    # The canonical definition lives in sweep_driver; sweep_run re-imports it.
    # Since DEC-SWEEP-RUST-TRADESIM-WIRE-001 the wired eval loop's per-mask seam
    # is the METRICS TAIL (`metrics_from_trades`), not `metrics_for_mask`.
    from brutex.engine.sweep_driver import metrics_from_trades as real_tail

    bars = _trending_bars()
    grid = SweepGridConfig(
        instruments=["NIFTY", "BANKNIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        top_k=5,
    )
    tuples = list(iter_sweep_tuples(grid))
    assert [t.instrument for t in tuples] == ["NIFTY", "BANKNIFTY"]

    # Wrap the REAL per-mask metrics TAIL so the FIRST tuple (NIFTY) runs the
    # genuine pipeline, but the SECOND tuple (BANKNIFTY) raises
    # KeyboardInterrupt the moment its eval loop touches the per-mask tail —
    # exactly an operator Ctrl-C landing mid-combo.
    instrument_box: dict[str, str] = {}

    def _patched_metrics(*args: object, **kwargs: object) -> object:
        if instrument_box.get("current") == "BANKNIFTY":
            raise KeyboardInterrupt("operator ctrl-c")
        return real_tail(*args, **kwargs)  # type: ignore[arg-type]

    # String target avoids the re-export attr check (as the unit tests do).
    monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _patched_metrics)

    root = tmp_path / "results"
    audit_path = tmp_path / "grid.jsonl"
    dedup = ComboDedup(OptionDedupIndex(capacity=200_000, fpr_target=1e-7))

    def _bars_for(instrument: str, _tf: str) -> list[CandleBar]:
        # Track which tuple's eval is in flight (bars_for fires per tuple, just
        # before its combo loop, with the instrument as the first arg), so the
        # patched metric fires only for BANKNIFTY.
        instrument_box["current"] = instrument
        return bars

    with (
        AuditWriter.open(audit_path) as audit,
        pytest.raises(KeyboardInterrupt, match="operator ctrl-c") as excinfo,
    ):
        run_sweep_grid(
            grid,
            bars_for=_bars_for,
            store_for=make_store_for(root, grid),
            dedup=dedup,
            audit=audit,
        )

    # Bare propagation: NOT downgraded to a (per-tuple) Exception.
    assert not isinstance(excinfo.value, Exception)

    # The first tuple (NIFTY) persisted its Parquet; the interrupted tuple
    # (BANKNIFTY) wrote NOTHING (the raise is before its store flush).
    nifty_path = tuple_store_path(root, tuples[0], grid)
    bank_path = tuple_store_path(root, tuples[1], grid)
    assert nifty_path.exists()
    assert not bank_path.exists()
    # The NIFTY finalists are re-loadable (the real pipeline ran for it).
    assert len(ResultsStore.load(nifty_path)) >= 0

    # The interrupt was NOT recorded as a tuple failure, and the grid never
    # reached its terminal completion sentinel.
    actions = [e.action for e in read_entries(audit_path)]
    assert "TUPLE_FAILED" not in actions
    assert "GRID_SWEEP_COMPLETE" not in actions
    # The partial hash-chained audit still verifies end-to-end (Golden Rule #8).
    assert verify_chain(audit_path) == len(actions)
