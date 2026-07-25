"""Generate the committed ranking parity corpus (slice-4 PR-1) — run ONCE.

Produces ``tests/golden/fixtures/ranking_parity_corpus/corpus.json``: the
hostile leaderboard pools the rank-order-identity gate
(:mod:`tests.golden.test_ranking_parity_corpus`) replays through BOTH
``rank_combos`` arms. Deterministic (fixed seed + hand-crafted hostile
shapes); float values are serialised as ``repr`` strings so ``NaN`` /
``±inf`` / ``-0.0`` round-trip exactly (``float(repr(x))`` is
shortest-roundtrip exact).

Refuses to overwrite an existing corpus unless
``BRUTEX_REGEN_RANKING_PARITY_CORPUS=1`` (the preopen-golden regen-guard
precedent): the COMMITTED corpus is the oracle of record — regeneration is
a deliberate, reviewed act, never a side effect.

Run:  .venv/bin/python -m tests.golden.generate_ranking_parity_corpus
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from pathlib import Path
from typing import Any

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "ranking_parity_corpus" / "corpus.json"

#: The metric fields a corpus row may override (everything else takes the
#: gate-passing ``tests.fakes.metrics_fixtures.make_result_metrics`` base).
FLOAT_FIELDS = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "recovery_factor",
    "win_rate",
    "win_loss_ratio",
    "max_drawdown_pct",
    "skewness",
    "kurtosis",
)
INT_FIELDS = ("total_trades", "max_consecutive_losses", "avg_mfe_paisa", "avg_mae_paisa")


def _enc(value: float | int | None) -> Any:
    """Encode one metric value: None → null, int → int, float → repr str."""
    if value is None or isinstance(value, int):
        return value
    return repr(value)


def _row(mask: int, **metrics: float | int | None) -> dict[str, Any]:
    for name in metrics:
        if name not in FLOAT_FIELDS and name not in INT_FIELDS:
            raise ValueError(f"unknown corpus metric field {name!r}")
    return {"mask": mask, "metrics": {k: _enc(v) for k, v in metrics.items()}}


def build_pools() -> list[dict[str, Any]]:
    """The hostile pool set (the slice-4 design §3.5 shapes)."""
    rng = random.Random(2026_07_11)
    pools: list[dict[str, Any]] = []

    # 1. Near-tie composites: identical metrics → every survivor ties →
    #    the ORDER is decided purely by the mask tie-break law.
    pools.append(
        {
            "name": "near_tie_identical_metrics",
            "overfit_dsr_min": None,
            "rows": [_row(mask, sharpe_ratio=1.5) for mask in (9, 3, 12, 1, 7)],
        }
    )

    # 2. Signed-zero composites: zero-variance columns z-score to exact
    #    0.0 → composite +0.0 → the sort key negates to -0.0 (the
    #    -0.0 == +0.0 comparator law).
    pools.append(
        {
            "name": "signed_zero_composites",
            "overfit_dsr_min": None,
            "rows": [_row(mask) for mask in (2, 5, 3)],
        }
    )

    # 3. Beyond-2^53 paisa: the cr_div fast-path envelope refusal — the ON
    #    arm serves the oracle (CPython's correctly-rounded int/int).
    pools.append(
        {
            "name": "beyond_2_53_paisa",
            "overfit_dsr_min": None,
            "rows": [
                _row(1, avg_mfe_paisa=(1 << 53) + 1, avg_mae_paisa=3),
                _row(2, avg_mfe_paisa=(1 << 53) + 3, avg_mae_paisa=3),
                _row(4, avg_mfe_paisa=(1 << 60) + 7, avg_mae_paisa=(1 << 54) + 5),
                _row(8, avg_mfe_paisa=1_000, avg_mae_paisa=7),
            ],
        }
    )

    # 4. None-heavy rows: undefined metrics keep their None semantics
    #    (gates fail-None, composite terms project to 0.0).
    pools.append(
        {
            "name": "none_heavy",
            "overfit_dsr_min": None,
            "rows": [
                _row(1, sharpe_ratio=None, sortino_ratio=None, calmar_ratio=None),
                _row(2, profit_factor=None),  # gate-rejected (None PF)
                _row(4, win_loss_ratio=None, recovery_factor=None),
                _row(8, sharpe_ratio=2.0),
            ],
        }
    )

    # 5. NaN/±inf gate inputs: the fail-closed guard rejects them ALONE.
    pools.append(
        {
            "name": "nan_inf_gate_inputs",
            "overfit_dsr_min": None,
            "rows": [
                _row(1, profit_factor=math.nan),
                _row(2, max_drawdown_pct=math.inf),
                _row(4, win_rate=-math.inf),
                _row(8, win_loss_ratio=math.nan),
                _row(16, sharpe_ratio=1.25),
            ],
        }
    )

    # 6. At-threshold gate rows: exactly-at-threshold PASSES on every door.
    pools.append(
        {
            "name": "at_threshold_gates",
            "overfit_dsr_min": None,
            "rows": [
                _row(1, total_trades=30, profit_factor=1.2, max_drawdown_pct=30.0),
                _row(2, total_trades=29),  # one below — rejected
                _row(4, win_rate=0.25, win_loss_ratio=0.0),
                _row(8, max_consecutive_losses=10),
                _row(16, max_consecutive_losses=11),  # one above — rejected
            ],
        }
    )

    # 7. The overfit pool: mixed Sharpes, the weak ones deflated out at
    #    dsr_min=0.95 (the byte-frozen sibling golden's shape, widened).
    pools.append(
        {
            "name": "overfit_gate_pool",
            "overfit_dsr_min": 0.95,
            "rows": [
                _row(1, sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2),
                _row(2, sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5),
                _row(4, sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8),
                _row(8, sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5),
                _row(16, sharpe_ratio=0.8, skewness=-0.5, kurtosis=1.5),
            ],
        }
    )

    # 8. Seeded random mixed pools (u128-headroom masks included).
    for pool_idx in range(3):
        rows = []
        masks = rng.sample(range(1, 1 << 60), 8)
        masks[0] |= 1 << 100  # u128 headroom mask — beyond u64
        for mask in masks:
            rows.append(
                _row(
                    mask,
                    sharpe_ratio=rng.choice([None, rng.uniform(-3.0, 4.0)]),
                    sortino_ratio=rng.choice([None, rng.uniform(-3.0, 4.0)]),
                    calmar_ratio=rng.choice([None, rng.uniform(-2.0, 3.0)]),
                    profit_factor=rng.choice([None, rng.uniform(0.5, 4.0)]),
                    recovery_factor=rng.choice([None, rng.uniform(0.0, 5.0)]),
                    win_rate=rng.uniform(0.0, 1.0),
                    win_loss_ratio=rng.choice([None, rng.uniform(0.0, 5.0)]),
                    max_drawdown_pct=rng.uniform(0.0, 45.0),
                    total_trades=rng.randint(20, 80),
                    max_consecutive_losses=rng.randint(0, 14),
                    avg_mfe_paisa=rng.randint(0, 1 << 30),
                    avg_mae_paisa=rng.randint(0, 1 << 20),
                    skewness=rng.choice([None, rng.uniform(-2.0, 2.0)]),
                    kurtosis=rng.choice([None, rng.uniform(-1.0, 6.0)]),
                )
            )
        pools.append(
            {
                "name": f"seeded_random_{pool_idx}",
                "overfit_dsr_min": rng.choice([None, 0.9]),
                "rows": rows,
            }
        )
    return pools


def main() -> int:
    if CORPUS_PATH.exists() and os.environ.get("BRUTEX_REGEN_RANKING_PARITY_CORPUS") != "1":
        sys.stderr.write(
            f"REFUSED: {CORPUS_PATH} exists — the committed corpus is the oracle "
            "of record. Set BRUTEX_REGEN_RANKING_PARITY_CORPUS=1 to regenerate "
            "deliberately.\n"
        )
        return 1
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "pools": build_pools()}
    CORPUS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    sys.stderr.write(f"wrote {CORPUS_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
