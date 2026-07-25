"""The rank-order-identity parity corpus gate (slice-4 PR-1, design §3.5).

Replays the COMMITTED hostile corpus
(``tests/golden/fixtures/ranking_parity_corpus/corpus.json`` — near-tie
composites, signed-zero pools, ``> 2**53`` paisa, ``None``-heavy rows,
NaN/±inf gate inputs, at-threshold doors, an overfit pool, seeded random
mixes with u128-headroom masks) through BOTH ``rank_combos`` arms and
asserts, per pool:

* **identical ranked mask SEQUENCES** (the rank-order-identity law) and
  identical packed composite/DSR bit keys — HARD on every host (the (b*)
  always-on differential ships oracle bits, so a libm divergence can never
  become an order or wire delta);
* **identical gate-verdict + gate-reason tuples** per row, Python vs the
  Rust gates twins — HARD ZERO divergence (class (a));
* a **DIRECT-kernel bit-agreement census** for the (b*) composite
  surfaces — the RAW ``fno_math`` composite / DSR-z kernels re-run on the
  Python pipeline's own inputs and bit-compared against the Python
  oracle, deliberately BYPASSING ``run_with_parity`` (which ships the
  oracle value on any mismatch, so a two-arm WIRE comparison is bit-equal
  by construction and can never observe a host-libm divergence) —
  REPORTED (one structlog INFO), with the hard requirement carried by the
  order/verdict assertions above, not by the census count (a (b*) bit
  delta is legal only as a logged, latching event — the design's
  anti-designed-red rule).

The corpus file itself is sha-pinned: editing it is a deliberate, reviewed
act (regenerate via ``tests.golden.generate_ranking_parity_corpus`` under
its explicit env guard).
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

import fno_math
import pytest
import structlog
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import (
    N_COMPOSITE_TERMS,
    OverfitGateConfig,
    RankedCombo,
    composite_scores,
    composite_term_inputs,
    dsr_z_contributions,
    gate_reasons,
    overfit_gate_positions,
    passes_gates,
    rank_combos,
)
from brutex.ranking.rust_ranking import (
    RANKING_RUST_ENV,
    _row_streak_threshold,
    reset_ranking_rust_for_tests,
)

from tests.fakes.metrics_fixtures import make_result_metrics
from tests.fakes.pow_identity import pow_identity_holds
from tests.golden.generate_ranking_parity_corpus import CORPUS_PATH

_LOG = structlog.get_logger("tests.ranking_parity_corpus")

#: SHA-256 of the committed corpus bytes — regeneration is deliberate.
_CORPUS_SHA256 = "d0a08997917d2a7d81e95ac0ebff75fea4435d5a8be5532e9b1513bcdb1c951e"


def _load_corpus() -> dict[str, Any]:
    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    return data


def _decode_metrics(encoded: dict[str, Any]) -> ResultMetrics:
    decoded: dict[str, Any] = {}
    for name, value in encoded.items():
        if isinstance(value, str):
            decoded[name] = float(value)  # repr round-trip (NaN/inf/-0.0 exact)
        else:
            decoded[name] = value
    return make_result_metrics(**decoded)


def _candidates(pool: dict[str, Any]) -> list[tuple[int, ResultMetrics]]:
    return [(row["mask"], _decode_metrics(row["metrics"])) for row in pool["rows"]]


def _keys(ranked: list[RankedCombo]) -> list[tuple[int, bytes, object]]:
    return [
        (
            rc.mask,
            struct.pack("<d", rc.composite_score),
            None if rc.deflated_sharpe is None else struct.pack("<d", rc.deflated_sharpe),
        )
        for rc in ranked
    ]


def _pack_column(values: list[float]) -> tuple[bytes, ...]:
    return tuple(struct.pack("<d", v) for v in values)


def _direct_kernel_census(
    candidates: list[tuple[int, ResultMetrics]],
    cfg: OverfitGateConfig | None,
) -> dict[str, int]:
    """The (b*) DIRECT-kernel bit-agreement census for one pool (REPORTED).

    Re-derives the Python pipeline's composite inputs (gates → opt-in
    overfit gate → term columns → DSR contribution) and calls the RAW
    ``fno_math`` (b*) kernels on them, bit-comparing against the Python
    oracle — deliberately BYPASSING ``run_with_parity``, which ships the
    oracle value on any bit mismatch, so a wire comparison between the two
    ``rank_combos`` arms is bit-equal BY CONSTRUCTION and can never observe
    the modeled host-libm arm this census exists to survey. Emit-on-
    applicable: a pool with no rank-reaching survivors computes composite
    bits on NEITHER arm, so it contributes no census keys (the ``dsr_z``
    key appears only when the 8th term was active for the pool).
    """
    survivors = [(mask, m) for mask, m in candidates if passes_gates(m)]
    dsr_by_survivor: list[float] | None = None
    if cfg is not None and survivors:
        outcome = overfit_gate_positions(
            [m.sharpe_ratio for _, m in candidates],
            [(m.sharpe_ratio, m.total_trades, m.skewness, m.kurtosis) for _, m in survivors],
            cfg,
        )
        if outcome is not None:  # a non-deflatable pool ABSTAINS (dsr stays None)
            kept_positions, dsr_by_survivor = outcome
            survivors = [survivors[position] for position in kept_positions]
    if not survivors:
        return {}
    term_rows = [composite_term_inputs(m) for _, m in survivors]
    term_columns = [[row[term] for row in term_rows] for term in range(N_COMPOSITE_TERMS)]
    dsr_contrib = dsr_z_contributions(dsr_by_survivor, len(survivors), cfg)
    py_composites = composite_scores(tuple(term_columns), dsr_contrib)
    rust_composites = fno_math.ranking_composite_scores(term_columns, dsr_contrib)
    entry = {
        "composite_kernel_bit_agree": int(
            _pack_column(py_composites) == _pack_column(rust_composites)
        ),
    }
    if dsr_by_survivor is not None:
        assert cfg is not None  # dsr_by_survivor is set only under an active cfg
        rust_dsr = fno_math.ranking_dsr_z_contributions(dsr_by_survivor, cfg.weight)
        entry["dsr_z_kernel_bit_agree"] = int(_pack_column(dsr_contrib) == _pack_column(rust_dsr))
    return entry


def test_corpus_fixture_is_sha_pinned() -> None:
    assert CORPUS_PATH.exists(), "the parity corpus must be committed"
    digest = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert digest == _CORPUS_SHA256, (
        "the committed ranking parity corpus changed — regeneration must be a "
        f"deliberate, reviewed act (got sha256 {digest})"
    )


def test_corpus_covers_the_hostile_shapes() -> None:
    names = {pool["name"] for pool in _load_corpus()["pools"]}
    assert {
        "near_tie_identical_metrics",
        "signed_zero_composites",
        "beyond_2_53_paisa",
        "none_heavy",
        "nan_inf_gate_inputs",
        "at_threshold_gates",
        "overfit_gate_pool",
    } <= names


def test_rank_order_identity_and_gate_census_across_both_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _load_corpus()
    census: dict[str, dict[str, int]] = {}
    for pool in corpus["pools"]:
        candidates = _candidates(pool)
        dsr_min = pool["overfit_dsr_min"]
        cfg = None if dsr_min is None else OverfitGateConfig(dsr_min=dsr_min)

        # Both arms, fresh parity state per pool (burn-in + latches reset).
        monkeypatch.setenv(RANKING_RUST_ENV, "0")
        off = rank_combos(candidates, overfit=cfg)
        reset_ranking_rust_for_tests()
        monkeypatch.setenv(RANKING_RUST_ENV, "1")
        on = rank_combos(candidates, overfit=cfg)
        reset_ranking_rust_for_tests()

        # HARD: identical ranked mask sequence + identical wire bits.
        assert [rc.mask for rc in on] == [rc.mask for rc in off], pool["name"]
        assert _keys(on) == _keys(off), pool["name"]

        # HARD (class (a)): per-row gate verdicts + reason tuples, Python
        # vs the Rust twins — zero divergence on every host.
        metrics = [m for _, m in candidates]
        # Door 5/6 fold-in-Rust/combine-in-Python columns: the transcendental
        # ``streak_threshold`` is computed by the shared Python seam
        # (``_row_streak_threshold``), so the kernel does only host-invariant
        # integer/finite comparisons (max_sl_hit_rate ``None`` — Door 6 inert,
        # matching ``rank_combos``'s default).
        cols = (
            [m.total_trades for m in metrics],
            [m.profit_factor for m in metrics],
            [m.max_drawdown_pct for m in metrics],
            [m.win_rate for m in metrics],
            [m.win_loss_ratio for m in metrics],
            [m.max_consecutive_losses for m in metrics],
            [_row_streak_threshold(m) for m in metrics],
            [m.loss_rate for m in metrics],
            [m.sl_hit_rate for m in metrics],
            None,
        )
        assert fno_math.ranking_passes_gates(*cols) == [passes_gates(m) for m in metrics], pool[
            "name"
        ]
        assert fno_math.ranking_gate_reasons(*cols) == [list(gate_reasons(m)) for m in metrics], (
            pool["name"]
        )

        # REPORTED census: the (b*) surfaces' DIRECT kernel bit-agreement on
        # this host — the raw fno_math kernels vs the Python oracle on the
        # pipeline's own inputs (informational; the wire law above is the
        # hard gate). Deliberately NOT a two-arm wire comparison: the
        # always-on differential ships the oracle value on any mismatch, so
        # ON-vs-OFF wire bits are equal by construction and a wire census
        # could never observe the modeled host-libm arm.
        entry: dict[str, int] = {"rows": len(candidates), "ranked": len(on)}
        entry.update(_direct_kernel_census(candidates, cfg))
        census[pool["name"]] = entry
    _LOG.info("ranking_parity_corpus.census", census=census)


def test_direct_kernel_census_can_observe_a_kernel_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-tautology teeth for the census (hostile-review round 1, R1.3).

    The census compares RAW ``fno_math`` kernel bits against the Python
    oracle, so a genuinely divergent (b*) kernel is REPORTED as ``0`` — a
    two-arm wire comparison could never show this (``run_with_parity``
    ships the oracle value on any mismatch, making ON-vs-OFF wire bits
    equal by construction).

    Host-independence (hostile-review round 10, R10.1): the RAW kernel's
    bit agreement is a (b*) libm surface the design declares
    HOST-DEPENDENT — this pool's only variant term column is the sharpe
    z-score whose std is ``sqrt(2/3)`` (Python libm ``pow`` vs Rust
    ``f64::sqrt``), and the shipped probe documents THIS container as a
    non-identity host — so hard-asserting real-kernel agreement
    unconditionally was a designed-red that passed only by per-value
    coincidence. The census MACHINERY is instead pinned with kernel
    stand-ins that are agree/disagree BY CONSTRUCTION on every host: one
    returning the Python oracle's exact bits (census must read ``1``) and
    its last-ULP perturbation (census must flip to ``0``). The REAL
    kernel's agreement stays a hard assertion on identity hosts — NO
    measured host currently qualifies (the production M4-Pro measured
    non-identity, verify-m4pro run 29194011032, coordinator-ruled
    2026-07-12) — per the pow-probe gating idiom of the golden legs.
    """
    candidates = [
        (1, make_result_metrics(sharpe_ratio=1.0)),
        (2, make_result_metrics(sharpe_ratio=2.0)),
        (3, make_result_metrics(sharpe_ratio=3.0)),
    ]
    if pow_identity_holds():
        # Identity host (none measured so far — the M4-Pro measured
        # non-identity, run 29194011032): the REAL kernel must
        # bit-agree — hard, no coincidence involved.
        assert _direct_kernel_census(candidates, None) == {"composite_kernel_bit_agree": 1}

    def _oracle_bits(term_columns: list[list[float]], dsr_contrib: list[float]) -> list[float]:
        # A kernel returning the Python oracle's exact bits: agreement by
        # construction, on EVERY host — isolates the census machinery from
        # the host-libm arm.
        return list(composite_scores(tuple(term_columns), dsr_contrib))

    monkeypatch.setattr(fno_math, "ranking_composite_scores", _oracle_bits)
    assert _direct_kernel_census(candidates, None) == {"composite_kernel_bit_agree": 1}

    def _one_ulp_off(term_columns: list[list[float]], dsr_contrib: list[float]) -> list[float]:
        # One ULP off the ORACLE bits: bit-different by construction for
        # the pool's finite composites (perturbing the real kernel instead
        # could — on a hostile host — land back ON the oracle bits).
        return [math.nextafter(v, math.inf) for v in _oracle_bits(term_columns, dsr_contrib)]

    monkeypatch.setattr(fno_math, "ranking_composite_scores", _one_ulp_off)
    assert _direct_kernel_census(candidates, None) == {"composite_kernel_bit_agree": 0}
