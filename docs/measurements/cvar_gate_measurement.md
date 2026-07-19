# CVaR tail-risk gate — OFF vs ON measurement (DEC-RANK-CVAR-TAIL-GATE-001)

> The activation-evidence record for the CVaR (expected-shortfall) rejection
> gate (`brutex/ranking/cvar_gate.py`). The gate SHIPS INERT (default OFF —
> `SweepGridConfig.cvar_gate = None`); flipping any production config to a
> non-`None` `CvarGateConfig` requires its OWN DEC **plus operator sign-off
> on this measurement**. Measured 2026-07-18 on the
> `claude/cvar-tail-gate` branch through the REAL
> `brutex.ranking.cvar_gate.cvar_rejects` law (the same code path the sweep
> spine calls).

## Honest envelope (Golden Rule #10 — read first)

* **The population is SYNTHETIC.** No frozen results-store fixture carries
  per-trade P&L lists (the stores persist aggregate `ResultMetrics` rows
  only), and the in-repo sweep fixtures yield a handful of candidates with
  near-identical trade streams — too small and too homogeneous to
  characterise a tail gate. The population below is therefore a SEEDED,
  fully-reproducible synthetic candidate set with three explicit tail
  regimes; it demonstrates the gate's DISCRIMINATION, not a production
  rejection rate. A production-scale re-measurement over a real multi-day
  sweep's eval spine is part of the activation DEC's evidence bar.
* **Small scale.** 240 candidates × 60–250 trades. Every number below is
  exact (integer law, deterministic seed) but the POPULATION is small.
* The engine-integration behaviour (a REAL `run_persisted_sweep` whose
  fat-tail masks are rejected gate-ON and served gate-OFF, with the OFF arm
  content-hash-identical) is separately pinned by
  `tests/unit/test_engine_sweep_run_cvar_gate.py` — that is the wiring proof;
  THIS file is the threshold-behaviour evidence.

## Method (fully reproducible)

* Law: `alpha_bps = 500` (the worst 5% of trades, `k = max(1,
  ceil(n·500/10_000))`); REJECT iff `tail_sum_paisa < -threshold_paisa · k`.
* Population: `random.Random(42)`, 80 candidates per regime, trade count
  `randint(60, 250)` each, P&L in int paisa:
  * **benign** — 55% winners +1,500..6,000; losers −800..−3,200 (no tail);
  * **moderate** — as benign, plus 2% of trades at −8,000..−16,000;
  * **fat-tail** — 51% winners +1,800..7,000; 41% losers −800..−3,000;
    **4% catastrophic −20,000..−60,000** (the account-blower shape the
    gate exists to reject).
* The generator script is inlined at the end of this file; it imports the
  repo's own `cvar_rejects` / `_cvar_tail_fold_python` and the compiled
  `fno_math.stats_cvar_tail_fold` kernel.

## Results

Candidates evaluated: **240** (80 per regime × 3 regimes; 60–250 trades
each; seed 42).

Kernel-vs-oracle cross-check: **0 divergences** over all 240 folds
(`fno_math.stats_cvar_tail_fold` vs the Python oracle — the integer law is
bit-identical, as constructed).

### Rejections per threshold (alpha_bps = 500)

| threshold (₹/trade tail loss) | rejected total | benign | moderate | fat-tail |
|---|---|---|---|---|
| ₹20 | 240/240 | 80/80 | 80/80 | 80/80 |
| ₹40 | 152/240 | 0/80 | 72/80 | 80/80 |
| ₹60 | 125/240 | 0/80 | 45/80 | 80/80 |
| ₹80 | 97/240 | 0/80 | 18/80 | 79/80 |
| ₹120 | 78/240 | 0/80 | 0/80 | 78/80 |

Reading: ₹20/trade is degenerately tight (rejects everything — every
candidate's worst-5% tail loses more than ₹20/trade on average); from ₹40
up the benign regime passes CLEANLY (0/80 rejected at every threshold ≥
₹40) while the fat-tail regime stays essentially fully rejected (78–80/80)
all the way to ₹120 — the separation the gate exists to provide. The
moderate regime slides 72 → 0 across ₹40 → ₹120: the threshold is a genuine
operator dial, not a cliff.

### Survivor-vs-rejected tail statistics (threshold ₹60/trade)

| population | n | mean tail loss (₹/trade) | mean worst trade (₹) | mean max drawdown (₹) |
|---|---|---|---|---|
| survivors | 115 | −35.53 | −57.37 | 159.34 |
| rejected  | 125 | −227.55 | −398.08 | 945.95 |

The rejected population's expected tail loss is **6.4×** worse, its worst
single trade **6.9×** worse, and its max drawdown (cumulative P&L path)
**5.9×** worse — the gate's verdict correlates with exactly the
account-blower properties it is meant to screen.

## Activation guidance (NOT a flip — recorded for the future DEC)

* At alpha_bps=500, a threshold in the ₹40–₹120/trade band (4,000–12,000
  paisa) is the discriminating range ON THIS SYNTHETIC POPULATION; the
  production threshold must be re-derived from a real sweep's trade-P&L
  scale (index-point paisa magnitudes differ from this fixture's).
* Both interactions disclosed in the DEC apply at activation: the overfit
  gate's DSR trial pool `N` and the trial-stats sidecar become post-CVaR
  populations, and grid-level `cvar_rejected` aggregation is a named
  follow-up.

## Reproduction

```python
"""CVaR gate OFF-vs-ON measurement (DEC-RANK-CVAR-TAIL-GATE-001, Phase 4).

Run from the repo root with the project venv (fno_math wheel installed):
    .venv/bin/python <this-script>
Emits the tables above (seed-deterministic; byte-stable output).
"""

from __future__ import annotations

import random

import fno_math

from brutex.ranking.cvar_gate import CvarGateConfig, _cvar_tail_fold_python, cvar_rejects

SEED = 42
ALPHA_BPS = 500
THRESHOLDS = [2_000, 4_000, 6_000, 8_000, 12_000]
N_PER_REGIME = 80

rng = random.Random(SEED)


def _trades_benign(n: int) -> list[int]:
    out = []
    for _ in range(n):
        if rng.random() < 0.55:
            out.append(rng.randint(1_500, 6_000))
        else:
            out.append(-rng.randint(800, 3_200))
    return out


def _trades_moderate(n: int) -> list[int]:
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.02:
            out.append(-rng.randint(8_000, 16_000))
        elif r < 0.47:
            out.append(-rng.randint(800, 3_200))
        else:
            out.append(rng.randint(1_500, 6_500))
    return out


def _trades_fat(n: int) -> list[int]:
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.04:
            out.append(-rng.randint(20_000, 60_000))
        elif r < 0.45:
            out.append(-rng.randint(800, 3_000))
        else:
            out.append(rng.randint(1_800, 7_000))
    return out


population: list[tuple[str, list[int]]] = []
for regime, maker in (
    ("benign", _trades_benign),
    ("moderate", _trades_moderate),
    ("fat", _trades_fat),
):
    for _ in range(N_PER_REGIME):
        population.append((regime, maker(rng.randint(60, 250))))


def _max_drawdown(pnls: list[int]) -> int:
    eq = peak = dd = 0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


divergences = 0
for _, pnls in population:
    got = fno_math.stats_cvar_tail_fold(pnls, ALPHA_BPS)
    exp = _cvar_tail_fold_python(pnls, ALPHA_BPS)
    if got != (exp.k, exp.var_paisa, exp.tail_sum_paisa):
        divergences += 1
print(f"candidates={len(population)} kernel_divergences={divergences}")

for thr in THRESHOLDS:
    cfg = CvarGateConfig(threshold_paisa=thr, alpha_bps=ALPHA_BPS)
    rej = {"benign": 0, "moderate": 0, "fat": 0}
    for regime, pnls in population:
        if cvar_rejects(pnls, cfg):
            rej[regime] += 1
    print(thr, sum(rej.values()), rej)

cfg_mid = CvarGateConfig(threshold_paisa=6_000, alpha_bps=ALPHA_BPS)
for label, keep in (("survivors", False), ("rejected", True)):
    rows = [p for _, p in population if cvar_rejects(p, cfg_mid) is keep]
    tails = [
        _cvar_tail_fold_python(p, ALPHA_BPS).tail_sum_paisa
        / _cvar_tail_fold_python(p, ALPHA_BPS).k
        for p in rows
    ]
    print(
        label,
        len(rows),
        sum(tails) / len(rows) / 100,
        sum(min(p) for p in rows) / len(rows) / 100,
        sum(_max_drawdown(p) for p in rows) / len(rows) / 100,
    )
```
