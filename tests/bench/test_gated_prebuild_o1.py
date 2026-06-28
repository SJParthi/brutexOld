"""O(targets) bench for the gated pre-build orchestrator (DEC-PREBUILD-SELECT-001).

:func:`brutex.dashboard.gated_prebuild.run_gated_prebuild` is a SINGLE pass over
the selected instruments (group-by-kind once, then iterate each phase's list with
an O(1) classification + the injected per-symbol materialise), so the PER-SYMBOL
orchestration overhead is FLAT as the selection scales. This gate measures the
mean ns-per-symbol of the orchestration ALONE (a no-op ``materialize_one``) at a
small and a large selection and asserts the ratio holds the ceiling — a
regression that made the orchestration scale super-linearly (e.g. an O(n²) merge)
blows it.

Honest envelope (Golden Rule #10): NOT an O(1)-TOTAL claim — total orchestration
work is O(targets) (one pass). The gate proves the PER-SYMBOL overhead is
constant; the real per-symbol materialise cost (a 1m read + O(bars) resample) is
gated separately by ``test_fno_resample_streaming_o1`` / ``test_tf_resampler_o1``.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from brutex.dashboard.gated_prebuild import run_gated_prebuild
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment

from tests.bench._o1_tiers import ratio_ceiling

N_SMALL = 100
N_BIG = 20_000


def _make(idx: int) -> Instrument:
    """A SPOT/FUTURES/OPTIONS round-robin instrument so all three phases run."""
    kind = idx % 3
    if kind == 0:
        return Instrument(
            exch_id=Exchange.NSE,
            security_id=idx,
            symbol_name=f"SPOT{idx}",
            display_name=f"SPOT{idx}",
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
    if kind == 1:
        return Instrument(
            exch_id=Exchange.NSE,
            security_id=1_000_000 + idx,
            symbol_name=f"FUT{idx}",
            display_name=f"FUT{idx}",
            instrument=InstrumentType.FUT,
            segment=Segment.FNO,
            series=None,
            isin=None,
            underlying_symbol="NIFTY",
            underlying_security_id=1,
            lot_size=75,
            tick_size=Decimal("0.05"),
            sm_freeze_qty=1800,
            sm_expiry_date=date(2025, 1, 30),
            strike_price=None,
        )
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=2_000_000 + idx,
        symbol_name=f"OPT{idx}",
        display_name=f"OPT{idx}",
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("20000"),
    )


def _noop(_inst: Instrument) -> MaterializeResult:
    return MaterializeResult(written={"5m": 1}, skipped=())


def _time_per_symbol(n: int) -> float:
    targets = [_make(i) for i in range(n)]
    run_gated_prebuild(targets=targets[: min(64, n)], materialize_one=_noop)  # warm up
    t0 = time.perf_counter_ns()
    report = run_gated_prebuild(targets=targets, materialize_one=_noop)
    t1 = time.perf_counter_ns()
    assert report.ok
    return (t1 - t0) / n


def test_orchestration_per_symbol_cost_is_flat() -> None:
    """Per-symbol gated-orchestration overhead holds the ceiling 100 → 20K symbols."""
    t_small = _time_per_symbol(N_SMALL)
    t_big = _time_per_symbol(N_BIG)
    ratio = t_big / max(t_small, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"gated pre-build per-symbol orchestration ratio {ratio:.2f}× "
        f"({N_BIG} vs {N_SMALL}) exceeds the CI ceiling — the orchestration may "
        f"have regressed away from a single O(targets) pass. "
        f"t_small={t_small:.2f}ns, t_big={t_big:.2f}ns."
    )


def test_options_phase_skips_materialize() -> None:
    """OPTIONS adds zero materialise calls (the no-op honesty the bench relies on)."""
    calls = {"n": 0}

    def counting(_inst: Instrument) -> MaterializeResult:
        calls["n"] += 1
        return MaterializeResult(written={}, skipped=())

    targets = [_make(i) for i in range(30)]  # 10 spot + 10 fut + 10 opt
    run_gated_prebuild(targets=targets, materialize_one=counting)
    # Only the 20 non-OPTIONS symbols materialise.
    n_options = sum(1 for t in targets if t.instrument is InstrumentType.CE)
    assert calls["n"] == len(targets) - n_options
    assert PrebuildKind.OPTIONS.value == "options"
