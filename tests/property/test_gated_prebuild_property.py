"""Property invariants for the gated pre-build (DEC-PREBUILD-SELECT-001).

Hypothesis over :func:`brutex.dashboard.gated_prebuild.run_gated_prebuild` +
the SPOT byte-identity of :func:`brutex.data.timeframe_materialize.materialize_timeframes`:

* any subset / permutation of selected symbols never crashes;
* the GATE invariant — a phase runs ⇒ every prior RAN phase had ZERO failures;
* SPOT output is byte-identical via the default (``resampler=None``) and the
  explicit SPOT resampler (the golden bit-identity guard).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from brutex.dashboard.gated_prebuild import PREBUILD_PHASE_ORDER, run_gated_prebuild
from brutex.data.spot_resample import derive_intraday_tfs
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    intraday_resampler_for,
    materialize_timeframes,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    import polars as pl  # type-only: real module so `pl.X` annotations resolve
else:
    pl = pytest.importorskip("polars")  # runtime: frame construction needs the [data] extra


def _make(kind: PrebuildKind, idx: int) -> Instrument:
    if kind is PrebuildKind.SPOT:
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
    if kind is PrebuildKind.FUTURES:
        return Instrument(
            exch_id=Exchange.NSE,
            security_id=1000 + idx,
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
        security_id=2000 + idx,
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


_kinds = st.sampled_from([PrebuildKind.SPOT, PrebuildKind.FUTURES, PrebuildKind.OPTIONS])


@given(
    spec=st.lists(st.tuples(_kinds, st.booleans()), max_size=12),
)
def test_any_selection_never_crashes_and_respects_the_gate(
    spec: list[tuple[PrebuildKind, bool]],
) -> None:
    targets = [_make(kind, i) for i, (kind, _ok) in enumerate(spec)]
    fail_names = {
        f"{'SPOT' if k is PrebuildKind.SPOT else 'FUT'}{i}"
        for i, (k, ok) in enumerate(spec)
        if not ok and k is not PrebuildKind.OPTIONS
    }

    def materialize_one(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name in fail_names:
            raise ValueError("forced")
        return MaterializeResult(written={"5m": 1}, skipped=())

    report = run_gated_prebuild(targets=targets, materialize_one=materialize_one)

    # GATE invariant: every RAN phase EXCEPT possibly the last (the halting one)
    # had zero failures; a later phase ran ⇒ all earlier ran phases were ok.
    ran = report.phases
    for i, phase in enumerate(ran):
        is_last = i == len(ran) - 1
        if not is_last:
            assert phase.ok, "a non-terminal ran phase must have had zero failures"
    # halted_after set ⇔ the last ran phase failed AND not all phases ran.
    if report.halted_after is not None:
        assert not ran[-1].ok
        assert len(ran) < len(PREBUILD_PHASE_ORDER) or not report.ok
    else:
        # No halt ⇒ every phase ran in order.
        assert [p.phase for p in ran] == [k.value for k in PREBUILD_PHASE_ORDER]


def _df_1m(n: int) -> pl.DataFrame:
    base = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)
    return pl.DataFrame(
        {
            "ts_utc": [base + timedelta(minutes=i) for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        }
    )


@given(n=st.integers(min_value=1, max_value=240))
def test_spot_output_byte_identical_default_vs_explicit(n: int) -> None:
    df = _df_1m(n)
    captured_default: dict[str, pl.DataFrame] = {}
    captured_explicit: dict[str, pl.DataFrame] = {}

    def _wd(tf: str, frame: pl.DataFrame) -> int:
        captured_default[tf] = frame
        return frame.height

    def _we(tf: str, frame: pl.DataFrame) -> int:
        captured_explicit[tf] = frame
        return frame.height

    materialize_timeframes(tfs=("5m", "15m"), read_1m=lambda: df, write_tf=_wd)
    materialize_timeframes(
        tfs=("5m", "15m"),
        read_1m=lambda: df,
        write_tf=_we,
        resampler=intraday_resampler_for(PrebuildKind.SPOT),
    )

    assert captured_default.keys() == captured_explicit.keys()
    oracle = derive_intraday_tfs(df)
    for tf, value in captured_default.items():
        assert value.equals(captured_explicit[tf])
        # And both equal the canonical spot resampler (the golden oracle).
        assert value.equals(oracle[tf])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
