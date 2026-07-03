"""Property invariants for the pre-build (DEC-PREBUILD-SELECT-001 + DEC-PIPELINE-AUTOPREBUILD-001).

Hypothesis over :func:`brutex.dashboard.gated_prebuild.run_gated_prebuild` +
the SPOT byte-identity of :func:`brutex.data.timeframe_materialize.materialize_timeframes`:

* any subset / permutation of selected symbols never crashes;
* the NON-HALTING invariant (DEC-PIPELINE-AUTOPREBUILD-001 — supersedes the
  halt gate of DEC-PREBUILD-SELECT-001): EVERY phase always runs in order, no
  matter which symbols fail; every failure is honestly recorded in its phase's
  ``failed`` and ``report.ok`` is exactly "zero failures anywhere";
* SPOT output is byte-identical via the default (``resampler=None``) and the
  explicit SPOT resampler (the golden bit-identity guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _underlying(idx: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=idx,
        symbol_name=f"U{idx}",
        display_name=f"U{idx}",
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


# Per underlying: (spot_ok, n_futures, futures_all_ok).
_spec = st.tuples(st.booleans(), st.integers(min_value=0, max_value=3), st.booleans())


@given(specs=st.lists(_spec, max_size=8))
def test_any_selection_never_crashes_and_every_phase_always_runs(
    specs: list[tuple[bool, int, bool]],
) -> None:
    underlyings = [_underlying(i) for i in range(len(specs))]
    spot_fail = {f"U{i}" for i, (ok, _n, _fok) in enumerate(specs) if not ok}
    futures_by_u: dict[str, list[str]] = {
        f"U{i}": [f"U{i}-FUT{j}" for j in range(n)] for i, (_ok, n, _fok) in enumerate(specs)
    }
    fut_fail = {
        f"U{i}-FUT0"
        for i, (_ok, n, fok) in enumerate(specs)
        if not fok and n > 0  # the first future of a "futures-fail" underlying raises
    }

    def materialize_spot(inst: Instrument) -> MaterializeResult:
        if inst.symbol_name in spot_fail:
            raise ValueError("forced spot")
        return MaterializeResult(written={"5m": 1}, skipped=())

    def futures_for(inst: Instrument) -> list[str]:
        return futures_by_u[inst.symbol_name]

    def materialize_future(contract: str) -> MaterializeResult:
        if contract in fut_fail:
            raise ValueError("forced future")
        return MaterializeResult(written={"5m": 1}, skipped=())

    report = run_gated_prebuild(
        underlyings=underlyings,
        materialize_spot=materialize_spot,
        futures_contracts_for=futures_for,
        materialize_future=materialize_future,
    )

    # NON-HALTING invariant (DEC-PIPELINE-AUTOPREBUILD-001): EVERY phase always
    # runs in order, no matter which symbols fail.
    assert [p.phase for p in report.phases] == [k.value for k in PREBUILD_PHASE_ORDER]
    # Every forced failure is honestly recorded in its phase's ``failed`` and
    # every healthy symbol still succeeded (one bad symbol never aborts a stage).
    spot_phase, futures_phase, _options_phase = report.phases
    assert {s for s, _ in spot_phase.failed} == spot_fail
    assert set(spot_phase.succeeded) == {u.symbol_name for u in underlyings} - spot_fail
    assert {s for s, _ in futures_phase.failed} == fut_fail
    expected_fut_ok = {c for lst in futures_by_u.values() for c in lst} - fut_fail
    assert set(futures_phase.succeeded) == expected_fut_ok
    # ok ⇔ zero failures anywhere.
    assert report.ok == (not spot_fail and not fut_fail)


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
