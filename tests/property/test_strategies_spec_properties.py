"""Property tests for :mod:`brutex.strategies.spec` — DEC-SPEC-IDENTITY-WIRING-001.

Hypothesis invariants over the identity surface:

* the v1 canonical encode is ALWAYS 57 bytes with the ``01 00 00 00`` version
  prefix, for every valid spec;
* identical inputs ⇒ identical run-id; perturbing any single spec/context
  field (including ``fib_anchor``) ⇒ a different run-id — the Golden Rule
  #4/#7 injectivity contract, mirroring the ``mask_run_id`` property suite;
* the generated ``"spec:" + hex`` strategy_id can never trip ``combo_hash``'s
  ``\\x1f`` unit-separator guard (hex alphabet by construction).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from brutex.strategies.fib_definitions import Direction
from brutex.strategies.spec import (
    MAX_PROXIMITY_BPS,
    SPEC_STRATEGY_ID_PREFIX,
    SYMBOL_IDS,
    StrategySpec,
    spec_run_id,
)
from brutex.strategies.types import FibAnchorKind
from brutex.vocabulary.bits import IMPLEMENTED_MASK
from hypothesis import given, settings
from hypothesis import strategies as st

valid_masks = st.integers(min_value=0, max_value=IMPLEMENTED_MASK).map(
    lambda m: m & IMPLEMENTED_MASK
)
valid_bps = st.integers(min_value=0, max_value=MAX_PROXIMITY_BPS)
valid_prior = st.integers(min_value=0, max_value=2**64 - 1)
valid_symbol_ids = st.sampled_from(sorted(SYMBOL_IDS.values()))
directions = st.sampled_from(list(Direction))
anchors = st.sampled_from(list(FibAnchorKind))


@st.composite
def specs(draw: st.DrawFn) -> StrategySpec:
    return StrategySpec(
        condition_mask=draw(valid_masks),
        direction=draw(directions),
        pivot_proximity_bps=draw(valid_bps),
        pdh_pdl_proximity_bps=draw(valid_bps),
        fib_proximity_bps=draw(valid_bps),
        prior_n=draw(valid_prior),
        symbol_id=draw(valid_symbol_ids),
    )


def _ctx(anchor: FibAnchorKind = FibAnchorKind.PREV_DAY) -> dict[str, Any]:
    return {
        "tf": "15m",
        "mode": "B",
        "sl_paisa": None,
        "tp_paisa": None,
        "start_equity_paisa": 10_000_000_00,
        "fib_anchor": anchor,
        "data_snapshot_hash": "a" * 64,
        "indicator_version": "ind-v1",
        "code_commit_sha": "b" * 40,
    }


@settings(max_examples=50, deadline=None)
@given(spec=specs())
def test_canonical_bytes_always_57_with_v1_prefix(spec: StrategySpec) -> None:
    raw = spec.canonical_bytes()
    assert len(raw) == 57
    assert raw[:4] == b"\x01\x00\x00\x00"


@settings(max_examples=50, deadline=None)
@given(spec=specs(), anchor=anchors)
def test_identical_inputs_identical_run_id(spec: StrategySpec, anchor: FibAnchorKind) -> None:
    ctx = _ctx(anchor)
    assert spec_run_id(spec, **ctx) == spec_run_id(spec, **ctx)


@settings(max_examples=50, deadline=None)
@given(spec=specs())
def test_anchor_perturbation_changes_run_id(spec: StrategySpec) -> None:
    ids = {spec_run_id(spec, **_ctx(anchor)) for anchor in FibAnchorKind}
    assert len(ids) == len(FibAnchorKind)


@settings(max_examples=50, deadline=None)
@given(spec=specs(), data=st.data())
def test_single_spec_field_perturbation_changes_run_id(
    spec: StrategySpec, data: st.DataObject
) -> None:
    field = data.draw(
        st.sampled_from(
            [
                "condition_mask",
                "direction",
                "pivot_proximity_bps",
                "pdh_pdl_proximity_bps",
                "fib_proximity_bps",
                "prior_n",
                "symbol_id",
            ]
        ),
        label="field",
    )
    current = getattr(spec, field)
    new: Any
    if field == "condition_mask":
        new = data.draw(valid_masks.filter(lambda m: m != current), label="new")
    elif field == "direction":
        new = Direction.SHORT if current is Direction.LONG else Direction.LONG
    elif field == "prior_n":
        new = data.draw(valid_prior.filter(lambda v: v != current), label="new")
    elif field == "symbol_id":
        new = data.draw(valid_symbol_ids.filter(lambda v: v != current), label="new")
    else:
        new = data.draw(valid_bps.filter(lambda v: v != current), label="new")
    # dataclasses.replace re-runs __post_init__, so the perturbed spec is
    # re-validated exactly like a fresh construction.
    perturbed = dataclasses.replace(spec, **{field: new})
    ctx = _ctx()
    assert spec_run_id(perturbed, **ctx) != spec_run_id(spec, **ctx)


@settings(max_examples=50, deadline=None)
@given(spec=specs())
def test_strategy_id_hex_is_separator_safe(spec: StrategySpec) -> None:
    strategy_id = SPEC_STRATEGY_ID_PREFIX + spec.canonical_bytes().hex()
    assert "\x1f" not in strategy_id
    assert len(strategy_id) == len(SPEC_STRATEGY_ID_PREFIX) + 114
