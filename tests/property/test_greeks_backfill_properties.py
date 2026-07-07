# pyright: reportPrivateUsage=none
"""Hypothesis invariants for the bulk Greeks backfill (DEC-GREEKS-BULK-REENRICH-001).

* The exported staleness predicate agrees with the packed input bits over the
  ENTIRE in-band provenance space — stale ⟺ any INPUT field differs, and the
  per-bar style/status description bits can never flip the verdict (the
  scanner-stale ⊇ driver-recompute over-approximation, modulo the documented
  value-drift blind spot).
* The content-addressed ``backfill_id`` is deterministic and moves on ANY
  single-field mutation of the target set.
* The contract-window derivation always covers every month file (start on the
  first month's day 1; end strictly after the last month, +1 day IST-skew pad).
* The checkpoint high-water mark is monotonic under arbitrary record orders.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.greeks_backfill import BackfillScan, _contract_window, plan_greeks_backfill
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    Q_BIAS_ZERO,
    RATE_SOURCE_ID_FIXED,
    TTE_ACT365_EXPIRY_1530_IST,
    GreeksEnrichTarget,
    pack_greeks_provenance,
    stamp_is_current_inputs,
)
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.options.risk_free import RISK_FREE_SOURCE_VERSION, FixedRateSource
from hypothesis import given, settings
from hypothesis import strategies as st

_RATE = FixedRateSource()

_provenance_fields = st.fixed_dictionaries(
    {
        "formula_version": st.integers(min_value=0, max_value=0xFF),
        "rate_source_id": st.integers(min_value=0, max_value=0xF),
        "rate_version": st.integers(min_value=0, max_value=0xF),
        "q_bias_id": st.integers(min_value=0, max_value=0x3),
        "tte_convention_id": st.integers(min_value=0, max_value=0x3),
        "style_flag": st.integers(min_value=0, max_value=0x7),
        "status": st.integers(min_value=0, max_value=0xF),
    }
)


@given(fields=_provenance_fields)
def test_stale_iff_any_input_field_differs(fields: dict[str, int]) -> None:
    """stamp_is_current_inputs ⟺ the five INPUT fields equal the current pass's."""
    pid = pack_greeks_provenance(**fields)
    inputs_current = (
        fields["formula_version"] == GREEKS_FORMULA_VERSION
        and fields["rate_source_id"] == RATE_SOURCE_ID_FIXED
        and fields["rate_version"] == RISK_FREE_SOURCE_VERSION
        and fields["q_bias_id"] == Q_BIAS_ZERO
        and fields["tte_convention_id"] == TTE_ACT365_EXPIRY_1530_IST
    )
    assert stamp_is_current_inputs(pid, rate_source=_RATE) is inputs_current


@given(fields=_provenance_fields, style=st.integers(0, 0x7), status=st.integers(0, 0xF))
def test_style_and_status_never_flip_the_verdict(
    fields: dict[str, int], style: int, status: int
) -> None:
    base = pack_greeks_provenance(**fields)
    mutated = pack_greeks_provenance(**{**fields, "style_flag": style, "status": status})
    assert stamp_is_current_inputs(base, rate_source=_RATE) == stamp_is_current_inputs(
        mutated, rate_source=_RATE
    )


_targets_strategy = st.lists(
    st.builds(
        GreeksEnrichTarget,
        exchange=st.sampled_from(["NSE", "BSE"]),
        symbol=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Nd")), min_size=1, max_size=12
        ),
        start=st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 1, 1)),
        end=st.dates(min_value=date(2026, 1, 2), max_value=date(2027, 1, 1)),
    ),
    min_size=1,
    max_size=5,
    unique_by=lambda t: (t.exchange, t.symbol),
)


@given(targets=_targets_strategy, data=st.data())
def test_backfill_id_deterministic_and_mutation_sensitive(
    targets: list[GreeksEnrichTarget], data: st.DataObject
) -> None:
    scan = BackfillScan(targets=tuple(targets))
    plan_a = plan_greeks_backfill(scan, rate_source=_RATE, force=False, expired_only=False)
    plan_b = plan_greeks_backfill(scan, rate_source=_RATE, force=False, expired_only=False)
    assert plan_a.backfill_id == plan_b.backfill_id  # deterministic
    index = data.draw(st.integers(min_value=0, max_value=len(targets) - 1))
    victim = targets[index]
    mutated = GreeksEnrichTarget(
        exchange=victim.exchange,
        symbol=victim.symbol + "X",
        start=victim.start,
        end=victim.end,
    )
    changed = (*targets[:index], mutated, *targets[index + 1 :])
    plan_c = plan_greeks_backfill(
        BackfillScan(targets=changed), rate_source=_RATE, force=False, expired_only=False
    )
    assert plan_c.backfill_id != plan_a.backfill_id  # any key change re-keys the plan


@given(
    months=st.lists(
        st.tuples(st.integers(2020, 2030), st.integers(1, 12)), min_size=1, max_size=8, unique=True
    )
)
def test_contract_window_covers_every_month_file(months: list[tuple[int, int]]) -> None:
    files = sorted((y, m, Path(f"{y:04d}/{m:02d}.parquet")) for y, m in months)
    start, end = _contract_window(files)
    first_year, first_month, _ = files[0]
    last_year, last_month, _ = files[-1]
    assert start == date(first_year, first_month, 1)
    for year, month, _path in files:
        assert start <= date(year, month, 1) < end
    # The end pads one day past the first day of the month AFTER the last file
    # (the UTC-file-month vs IST-date skew absorption).
    next_month = (
        date(last_year + 1, 1, 1) if last_month == 12 else date(last_year, last_month + 1, 1)
    )
    assert (end - next_month).days == 1


@settings(deadline=None)  # I/O-bound: up to 30 REAL fsync'd records per example —
# hypothesis's 200 ms wall-clock deadline is pure noise on a loaded shared
# container (observed flaky-failing pre-round-4); the ORACLE is the monotonic
# high-water logic below, which stays fully asserted.
@given(indices=st.lists(st.integers(min_value=0, max_value=49), min_size=1, max_size=30))
def test_checkpoint_high_water_is_monotonic(indices: list[int]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SweepCheckpointStore(
            path=Path(tmp) / "checkpoint.json", sweep_id="prop", total_combos=50
        )
        high = -1
        for index in indices:
            advanced = store.record(index)
            assert advanced is (index > high)
            high = max(high, index)
            assert store.last_completed_index == high
