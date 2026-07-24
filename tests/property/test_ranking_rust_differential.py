"""Rust-vs-Python bit-differential properties for the ranking kernels (slice 4).

The hypothesis half of the PR-1 parity gates: every converted function's
Rust kernel is compared against its retained Python oracle over adversarial
domains (``None``-heavy rows, NaN/±inf gate inputs, at-threshold values,
``> 2**53`` paisa, ±0.0 composites, near-tie pools) with ``struct.pack``
BIT equality — never float ``==`` (the
``test_sweep_rust_metrics_equivalence`` shape).

Per-class assertion strength (the slice-4 design §3.5):

* **(a)/(b) rows** — gates, gate reasons, term inputs, ``cr_div_i64``, the
  rank-order permutation, overfit verdicts: HARD ZERO divergence on every
  host (comparisons / correctly-rounded ops only — theorem-class).
* **(b*) rows** — z-scores, the composite pipeline, the DSR z-term: HARD
  bit equality ONLY on a host whose libm satisfies the modeled pow
  identities (:mod:`tests.fakes.pow_identity` — the M4 probe); on a
  non-identity host (glibc measured live) the bit legs are gated off and
  the CLAMP/short-circuit semantics stay hard-pinned. The wire-level
  invariant that replaces bit-parity there — the flag-ON leaderboard is
  bit-identical to the flag-OFF one on EVERY host (always-on differential,
  ship-the-oracle) — is asserted UNCONDITIONALLY at the end of this file.
"""

# pyright: reportCallIssue=false
# Rationale (the DEC-PYRIGHT-UNMASK-001 precedent): Hypothesis `@st.composite`
# strategies are called without the runtime-injected `draw` argument; pyright
# does not model the transform. Scoped to this file only.

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any

import fno_math
import pytest
from brutex.ranking import (
    RankedCombo,
    _risk_efficiency,
    _zscores,
    composite_scores,
    composite_term_inputs,
    dsr_z_contributions,
    gate_reasons,
    passes_gates,
    rank_combos,
)
from brutex.ranking.overfit import OverfitGateConfig, passes_overfit_gate
from brutex.ranking.rust_ranking import (
    RANKING_RUST_ENV,
    _row_streak_threshold,
    reset_ranking_rust_for_tests,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.metrics_fixtures import make_result_metrics
from tests.fakes.pow_identity import pow_identity_holds

_POW_IDENTITY = pow_identity_holds()

_bits = struct.pack


def _pack_list(values: Sequence[float]) -> tuple[bytes, ...]:
    return tuple(_bits("<d", v) for v in values)


# ── strategies ──────────────────────────────────────────────────────────

_maybe_ratio = st.one_of(
    st.none(),
    st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf, 1.2, 2.0, 0.0, -0.0]),
)
_pct = st.one_of(
    st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf, 30.0]),
)
_rate = st.one_of(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf, 0.25]),
)
#: Finite metric values (the composite-term envelope) incl. signed zeros.
_finite = st.one_of(
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    st.sampled_from([0.0, -0.0, 1.0, -1.0]),
)
_maybe_finite = st.one_of(st.none(), _finite)


@st.composite
def _gate_metrics(draw: st.DrawFn) -> Any:
    total = draw(st.integers(min_value=0, max_value=100))
    return make_result_metrics(
        total_trades=total,
        winning_trades=0,
        losing_trades=total,
        profit_factor=draw(_maybe_ratio),
        max_drawdown_pct=draw(_pct),
        win_rate=draw(_rate),
        win_loss_ratio=draw(_maybe_ratio),
        max_consecutive_losses=draw(st.integers(min_value=0, max_value=50)),
        # Door 5 (derived binomial-null threshold from the OWN loss rate) +
        # Door 6 (opt-in SL-hit-rate ceiling) inputs — drawn adversarially:
        # ``loss_rate`` spans finite/NaN/±inf/None (the junk-guard domain,
        # DEC-METRICS-KELLY-GROWTH-001), ``sl_hit_rate`` finite/NaN/±inf/None.
        loss_rate=draw(st.one_of(st.none(), _rate)),
        sl_hit_rate=draw(st.one_of(st.none(), _rate)),
    )


@st.composite
def _term_metrics(draw: st.DrawFn) -> Any:
    return make_result_metrics(
        sharpe_ratio=draw(_maybe_finite),
        sortino_ratio=draw(_maybe_finite),
        calmar_ratio=draw(_maybe_finite),
        profit_factor=draw(st.one_of(st.none(), st.floats(1.2, 50.0))),
        recovery_factor=draw(_maybe_finite),
        win_rate=draw(st.floats(0.0, 1.0)),
        win_loss_ratio=draw(_maybe_finite),
        avg_mfe_paisa=draw(st.integers(min_value=0, max_value=(1 << 52))),
        avg_mae_paisa=draw(st.integers(min_value=0, max_value=(1 << 52))),
    )


def _gate_columns(pool: list[Any], max_sl_hit_rate: float | None = None) -> tuple[Any, ...]:
    """The 10 kernel gate columns, fold-in-Rust/combine-in-Python: the
    transcendental Door-5 ``streak_threshold`` is computed by the SHARED
    Python oracle seam (:func:`_row_streak_threshold` — one source of truth,
    Golden Rule #11 COMMON), so this end-to-end differential proves the
    kernel's host-invariant integer comparison against that finished bound
    equals the full Python ``passes_gates``."""
    return (
        [m.total_trades for m in pool],
        [m.profit_factor for m in pool],
        [m.max_drawdown_pct for m in pool],
        [m.win_rate for m in pool],
        [m.win_loss_ratio for m in pool],
        [m.max_consecutive_losses for m in pool],
        [_row_streak_threshold(m) for m in pool],
        [m.loss_rate for m in pool],
        [m.sl_hit_rate for m in pool],
        max_sl_hit_rate,
    )


# ── (a) gates + reasons — HARD on every host ────────────────────────────


@settings(max_examples=200)
@given(
    pool=st.lists(_gate_metrics(), min_size=0, max_size=8),
    knob=st.one_of(st.none(), st.floats(0.0, 1.0), st.sampled_from([0.2, 0.5])),
)
def test_gates_and_reasons_differential(pool: list[Any], knob: float | None) -> None:
    cols = _gate_columns(pool, knob)
    assert fno_math.ranking_passes_gates(*cols) == [
        passes_gates(m, max_sl_hit_rate=knob) for m in pool
    ]
    assert fno_math.ranking_gate_reasons(*cols) == [
        list(gate_reasons(m, max_sl_hit_rate=knob)) for m in pool
    ]


@settings(max_examples=200)
@given(
    pool=st.lists(_gate_metrics(), min_size=0, max_size=8),
    knob=st.one_of(st.none(), st.floats(0.0, 1.0)),
)
def test_gate_twin_law_holds_in_rust_too(pool: list[Any], knob: float | None) -> None:
    cols = _gate_columns(pool, knob)
    verdicts = fno_math.ranking_passes_gates(*cols)
    reasons = fno_math.ranking_gate_reasons(*cols)
    for ok, why in zip(verdicts, reasons, strict=True):
        assert ok == (why == [])


# ── (b) cr_div_i64 — HARD inside the envelope, refusal outside ──────────


@settings(max_examples=300)
@given(
    a=st.integers(min_value=-(1 << 53), max_value=1 << 53),
    b=st.integers(min_value=1, max_value=1 << 53),
)
def test_cr_div_matches_cpython_truediv_bitwise(a: int, b: int) -> None:
    assert _bits("<d", fno_math.ranking_cr_div_i64(a, b)) == _bits("<d", a / b)


@settings(max_examples=100)
@given(
    a=st.integers(min_value=(1 << 53) + 1, max_value=(1 << 62)),
    b=st.integers(min_value=1, max_value=1 << 53),
)
def test_cr_div_beyond_envelope_is_refused_both_directions(a: int, b: int) -> None:
    with pytest.raises(ValueError, match="2\\^53"):
        fno_math.ranking_cr_div_i64(a, b)
    with pytest.raises(ValueError, match="2\\^53"):
        fno_math.ranking_cr_div_i64(b, a)


def test_cr_div_zero_denominator_raises() -> None:
    with pytest.raises(ValueError, match="division by zero"):
        fno_math.ranking_cr_div_i64(1, 0)


def test_cr_div_adversarial_boundary_values() -> None:
    for a, b in [(1 << 53, 1), (1, 1 << 53), (-(1 << 53), 3), (0, 7), (3, 7), (-1, 3)]:
        assert _bits("<d", fno_math.ranking_cr_div_i64(a, b)) == _bits("<d", a / b)


# ── (b) composite term inputs — HARD on every host ─────────────────────


@settings(max_examples=200)
@given(pool=st.lists(_term_metrics(), min_size=0, max_size=6))
def test_term_inputs_differential(pool: list[Any]) -> None:
    rows = fno_math.ranking_composite_term_inputs(
        [m.sharpe_ratio for m in pool],
        [m.sortino_ratio for m in pool],
        [m.calmar_ratio for m in pool],
        [m.profit_factor for m in pool],
        [m.recovery_factor for m in pool],
        [m.win_rate for m in pool],
        [m.win_loss_ratio for m in pool],
        [m.avg_mfe_paisa for m in pool],
        [m.avg_mae_paisa for m in pool],
    )
    expected = [composite_term_inputs(m) for m in pool]
    assert [_pack_list(r) for r in rows] == [_pack_list(e) for e in expected]


@settings(max_examples=50)
@given(mfe=st.integers(min_value=(1 << 53) + 1, max_value=1 << 62))
def test_term_inputs_beyond_envelope_raise_while_python_stays_exact(mfe: int) -> None:
    # The kernel refuses (the wrapper then serves the oracle); the Python
    # original keeps computing CPython's correctly-rounded division.
    m = make_result_metrics(avg_mfe_paisa=mfe, avg_mae_paisa=3)
    with pytest.raises(ValueError, match="2\\^53"):
        fno_math.ranking_composite_term_inputs(
            [m.sharpe_ratio],
            [m.sortino_ratio],
            [m.calmar_ratio],
            [m.profit_factor],
            [m.recovery_factor],
            [m.win_rate],
            [m.win_loss_ratio],
            [m.avg_mfe_paisa],
            [m.avg_mae_paisa],
        )
    assert math.isfinite(_risk_efficiency(m))


# ── (b*) z-scores / composite / dsr-z — semantics HARD everywhere,
#    bit equality gated on the host pow-identity probe ───────────────────

_z_values = st.one_of(
    st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False),
    st.sampled_from([math.nan, math.inf, -math.inf, 0.0, -0.0, 5e-324]),
)


@settings(max_examples=300)
@given(values=st.lists(_z_values, min_size=1, max_size=12))
def test_zscores_differential(values: list[float]) -> None:
    rust = fno_math.ranking_zscores(values)
    py = _zscores(values)
    assert len(rust) == len(py)
    if _POW_IDENTITY:
        assert _pack_list(rust) == _pack_list(py)
    else:
        # Non-identity host: the clamp bounds + NaN law still bind exactly
        # (the wire is protected by the always-on differential — the
        # unconditional end-to-end property below).
        for r, p in zip(rust, py, strict=True):
            assert (math.isnan(r) and math.isnan(p)) or (-3.0 <= r <= 3.0 and -3.0 <= p <= 3.0)


def test_zscores_clamp_law_exact_on_every_host() -> None:
    # NaN → exactly +3.0; +inf → +3.0; −inf → −3.0 — pure comparison
    # semantics (py_min/py_max argument order), independent of libm.
    rust = fno_math.ranking_zscores([1.0, math.nan, 2.0])
    py = _zscores([1.0, math.nan, 2.0])
    assert _pack_list(rust) == _pack_list(py) == (_bits("<d", 3.0),) * 3
    # A huge outlier clamps at exactly ±3.0 on both sides.
    pool = [0.0] * 100 + [1e15]
    assert fno_math.ranking_zscores(pool)[-1] == _zscores(pool)[-1] == 3.0
    pool_neg = [0.0] * 100 + [-1e15]
    assert fno_math.ranking_zscores(pool_neg)[-1] == _zscores(pool_neg)[-1] == -3.0


def test_zscores_zero_variance_and_symmetric_pair_exact_on_every_host() -> None:
    assert _pack_list(fno_math.ranking_zscores([2.5] * 4)) == _pack_list(_zscores([2.5] * 4))
    # The symmetric pair's arithmetic is exact (mean 2.0, deviations ±1.0,
    # variance 1.0, sqrt 1.0) — bit-identical even on a non-identity host.
    assert _pack_list(fno_math.ranking_zscores([1.0, 3.0])) == _pack_list(_zscores([1.0, 3.0]))


def test_zscores_empty_raises_valueerror_where_python_zerodivisions() -> None:
    with pytest.raises(ValueError, match="empty input"):
        fno_math.ranking_zscores([])
    with pytest.raises(ZeroDivisionError):
        _zscores([])


@settings(max_examples=150)
@given(
    columns=st.integers(min_value=1, max_value=9).flatmap(
        lambda n: st.tuples(
            st.lists(st.lists(_finite, min_size=n, max_size=n), min_size=7, max_size=7),
            st.lists(_finite, min_size=n, max_size=n),
        )
    )
)
def test_composite_scores_differential(
    columns: tuple[list[list[float]], list[float]],
) -> None:
    term_columns, dsr = columns
    rust = fno_math.ranking_composite_scores(term_columns, dsr)
    py = composite_scores(tuple(term_columns), dsr)
    assert len(rust) == len(py)
    if _POW_IDENTITY:
        assert _pack_list(rust) == _pack_list(py)


def test_composite_wrong_column_count_message_is_byte_identical() -> None:
    with pytest.raises(ValueError, match="composite_scores expects 7 term columns, got 6"):
        fno_math.ranking_composite_scores([[1.0]] * 6, [0.0])
    with pytest.raises(ValueError, match="composite_scores expects 7 term columns, got 6"):
        composite_scores(tuple([[1.0]] * 6), [0.0])


@settings(max_examples=150)
@given(
    dsr=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=10),
    weight=st.floats(0.0, 1.0),
)
def test_dsr_z_differential(dsr: list[float], weight: float) -> None:
    rust = fno_math.ranking_dsr_z_contributions(dsr, weight)
    # The oracle ALWAYS gets an active config: the Rust kernel is the active
    # (weighted-zscores) arm's twin, and the strategy draws weight from
    # [0.0, 1.0] — a `cfg if weight >= 0 else None` ternary here would be
    # dead (bounded hypothesis floats are never NaN, and -0.0 >= 0 is True)
    # AND wrong if ever reached (the None arm is the inactive [0.0]*n
    # Python path, which the Rust kernel deliberately does not implement —
    # the seam keeps it Python-side, `rank_combos_via_rust`).
    py = dsr_z_contributions(dsr, len(dsr), OverfitGateConfig(weight=weight))
    assert len(rust) == len(py)
    if _POW_IDENTITY:
        assert _pack_list(rust) == _pack_list(py)


# ── (a) rank order + overfit verdicts — HARD on every host ─────────────


@settings(max_examples=300)
@given(
    scores=st.lists(
        st.one_of(
            st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            st.sampled_from([0.0, -0.0, 1.5, -1.5]),
        ),
        min_size=0,
        max_size=12,
    ),
    data=st.data(),
)
def test_rank_order_differential(scores: list[float], data: st.DataObject) -> None:
    masks = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=(1 << 128) - 1),
            min_size=len(scores),
            max_size=len(scores),
            unique=True,
        )
    )
    rust = fno_math.ranking_rank_order(scores, masks)
    py = sorted(range(len(scores)), key=lambda i: (-scores[i], masks[i]))
    assert rust == py


def test_rank_order_signed_zero_tie_matches_python() -> None:
    # Python tuple compare treats -0.0 == +0.0 → the mask decides; the Rust
    # comparator must NOT split the zeros (never total_cmp).
    scores = [0.0, -0.0, 0.0]
    masks = [5, 1, 3]
    assert fno_math.ranking_rank_order(scores, masks) == sorted(
        range(3), key=lambda i: (-scores[i], masks[i])
    )


def test_rank_order_non_finite_is_refused() -> None:
    with pytest.raises(ValueError, match="non-finite composite"):
        fno_math.ranking_rank_order([math.nan], [1])


@settings(max_examples=200)
@given(
    dsrs=st.lists(
        st.one_of(st.none(), st.floats(0.0, 1.0), st.just(math.nan)),
        min_size=0,
        max_size=10,
    ),
    dsr_min=st.floats(0.0, 1.0),
)
def test_overfit_verdicts_differential(dsrs: list[float | None], dsr_min: float) -> None:
    cfg = OverfitGateConfig(dsr_min=dsr_min)
    assert fno_math.ranking_overfit_verdicts(dsrs, dsr_min) == [
        passes_overfit_gate(d, cfg) for d in dsrs
    ]


# ── THE unconditional wire invariant: flag-ON == flag-OFF, bitwise ──────


@st.composite
def _rankable_pool(draw: st.DrawFn) -> list[tuple[int, Any]]:
    n = draw(st.integers(min_value=0, max_value=8))
    masks = draw(
        st.lists(
            st.integers(min_value=0, max_value=(1 << 60) - 1),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    pool: list[tuple[int, Any]] = []
    for mask in masks:
        kind = draw(st.integers(min_value=0, max_value=2))
        if kind == 0:
            pool.append((mask, draw(_gate_metrics())))
        else:
            pool.append((mask, draw(_term_metrics())))
    return pool


def _rank_keys(ranked: list[RankedCombo]) -> list[tuple[int, bytes, object]]:
    return [
        (
            rc.mask,
            _bits("<d", rc.composite_score),
            None if rc.deflated_sharpe is None else _bits("<d", rc.deflated_sharpe),
        )
        for rc in ranked
    ]


@settings(max_examples=150, deadline=None)
@given(
    pool=_rankable_pool(),
    use_overfit=st.booleans(),
)
def test_rank_combos_on_arm_is_bit_identical_to_off_arm_on_every_host(
    pool: list[tuple[int, Any]], use_overfit: bool
) -> None:
    """The ship-the-oracle wire law: whatever this host's libm does, the
    flag-ON leaderboard carries EXACTLY the flag-OFF bits — order, composite
    scores and DSRs alike (a (b*) divergence is only ever a logged,
    latching event, never a wire delta)."""
    import os

    cfg = OverfitGateConfig(dsr_min=0.5) if use_overfit else None
    reset_ranking_rust_for_tests()
    prior = os.environ.get(RANKING_RUST_ENV)
    try:
        os.environ[RANKING_RUST_ENV] = "0"
        off = rank_combos(pool, overfit=cfg)
        os.environ[RANKING_RUST_ENV] = "1"
        on = rank_combos(pool, overfit=cfg)
    finally:
        if prior is None:
            del os.environ[RANKING_RUST_ENV]
        else:
            os.environ[RANKING_RUST_ENV] = prior
        reset_ranking_rust_for_tests()
    assert _rank_keys(on) == _rank_keys(off)
