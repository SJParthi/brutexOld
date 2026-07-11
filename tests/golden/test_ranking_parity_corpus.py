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
* a **per-family bit-agreement census** for the (b*) composite surface —
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
import struct
from typing import Any

import fno_math
import pytest
import structlog
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, RankedCombo, gate_reasons, passes_gates, rank_combos
from brutex.ranking.rust_ranking import RANKING_RUST_ENV, reset_ranking_rust_for_tests

from tests.fakes.metrics_fixtures import make_result_metrics
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
        cols = (
            [m.total_trades for m in metrics],
            [m.profit_factor for m in metrics],
            [m.max_drawdown_pct for m in metrics],
            [m.win_rate for m in metrics],
            [m.win_loss_ratio for m in metrics],
            [m.max_consecutive_losses for m in metrics],
        )
        assert fno_math.ranking_passes_gates(*cols) == [passes_gates(m) for m in metrics], pool[
            "name"
        ]
        assert fno_math.ranking_gate_reasons(*cols) == [list(gate_reasons(m)) for m in metrics], (
            pool["name"]
        )

        # REPORTED census: the (b*) composite surface's direct kernel
        # bit-agreement on this host (informational — the wire law above is
        # the hard gate; a disagreement here is the modeled host-libm arm).
        agree = int(_keys(on) == _keys(off))
        census[pool["name"]] = {
            "rows": len(candidates),
            "ranked": len(on),
            "wire_bit_agree": agree,
        }
    _LOG.info("ranking_parity_corpus.census", census=census)
