"""Golden byte-equality: the AUTOMATIC and MANUAL cascade paths agree (DEC-CASCADE-WIRE-001).

The key correctness guarantee of the cascade wiring (CLAUDE.md §11 COMMON): the
on-sweep automatic path (:func:`brutex.engine.cascade_wire.run_cascade_on_store`)
and the manual ``brutex cascade`` path
(:func:`brutex.engine.cascade_wire.manual_cascade_report`) must produce a
**byte-identical** :class:`~brutex.metrics.cascade.CascadeReport` for the SAME
inputs — because both delegate to the SAME
:func:`~brutex.engine.cascade_wire.run_cascade_for_survivors`, and every
per-candidate series is a deterministic function of ``(mask_bits, bars, knobs)``
(Golden Rule #4). If the two paths ever diverged, the verdict an operator sees by
hand would not match the one persisted on-sweep — exactly the dishonesty this test
forbids.

We drive both paths from the same persisted :class:`ResultsStore` (the auto path's
input) + the same default knobs (the manual path's defaults) + the same seed, then
assert the full report — verdicts, survivor order, per-stage survivor counts — is
equal field-for-field, and that the persisted sidecar bytes are reproducible.

Engine imports pull the ``fno_math`` / ``trading_core`` wheels, so this is a
CI-3.12 golden test; CI is the oracle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from brutex.data.models import CandleBar
from brutex.engine.cascade_sidecar import write_cascade_sidecar
from brutex.engine.cascade_wire import manual_cascade_report, run_cascade_on_store
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.metrics.cascade import CascadeConfig
from brutex.vocabulary.bits import BIT_BAR_BULLISH

_IST = timezone(timedelta(hours=5, minutes=30))
_MASK = 1 << BIT_BAR_BULLISH
_SEED = 11
_EQUITY = 10_000_000
_RID_A = "a" * 64
_RID_B = "b" * 64


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(max(o, c) + 0.5, 2))),
        low=Decimal(str(round(min(o, c) - 0.5, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _bars() -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(90):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _row(run_id: str, mask: int) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=1_000,
        gross_profit_paisa=2_000,
        gross_loss_paisa=1_000,
        max_drawdown_paisa=500,
        expectancy_paisa=10,
        total_trades=100,
        winning_trades=60,
        losing_trades=40,
        sharpe_ratio=1.2,
        sortino_ratio=1.4,
        calmar_ratio=0.8,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=5.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


_DEFAULT_KNOBS = {
    "tf_minutes": 1,
    "sl_paisa": None,
    "tp_paisa": None,
    "proximity": None,
    "prior_n": 3,
    "start_equity_paisa": _EQUITY,
}


def test_auto_and_manual_cascade_reports_are_identical(tmp_path: Path) -> None:
    bars = _bars()
    store = ResultsStore(path=tmp_path / "r.parquet")
    store.append(_row(_RID_A, _MASK))
    store.append(_row(_RID_B, _MASK | (1 << 1)))
    config = CascadeConfig(seed=_SEED)

    # AUTOMATIC path: on-sweep cascade over the store's top-K (default knobs).
    auto = run_cascade_on_store(
        store, bars, dict(_DEFAULT_KNOBS), config, sidecar_path=tmp_path / "auto.parquet"
    )
    assert auto.report is not None

    # MANUAL path: brutex cascade over the same persisted rows (default knobs).
    manual = manual_cascade_report(
        store.rows(),
        lambda _instrument, _tf: bars,
        config,
        start_equity_paisa=_EQUITY,
        top_k=len(store),
    )

    # The full report must match field-for-field (the COMMON-seam guarantee).
    assert auto.report.verdicts == manual.verdicts
    assert auto.report.survivor_ids == manual.survivor_ids
    assert auto.report.n_input == manual.n_input
    assert auto.report.n_survived == manual.n_survived
    assert auto.report.stage_survivors == manual.stage_survivors


def test_sidecar_bytes_reproducible_across_paths(tmp_path: Path) -> None:
    """The verdict sidecar written from either path is byte-identical."""
    bars = _bars()
    store = ResultsStore(path=tmp_path / "r.parquet")
    store.append(_row(_RID_A, _MASK))
    store.append(_row(_RID_B, _MASK | (1 << 1)))
    config = CascadeConfig(seed=_SEED)

    auto = run_cascade_on_store(
        store, bars, dict(_DEFAULT_KNOBS), config, sidecar_path=tmp_path / "auto.parquet"
    )
    assert auto.report is not None and auto.sidecar is not None

    manual = manual_cascade_report(
        store.rows(),
        lambda _i, _t: bars,
        config,
        start_equity_paisa=_EQUITY,
        top_k=len(store),
    )
    manual_flush = write_cascade_sidecar(manual, tmp_path / "manual.parquet")

    assert auto.sidecar.content_hash == manual_flush.content_hash
