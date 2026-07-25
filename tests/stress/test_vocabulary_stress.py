"""Sustained-load determinism soak for the shared condition vocabulary.

The brute-force engine calls :func:`brutex.vocabulary.evaluator.evaluate_atoms`
and the mask operations (:func:`~brutex.vocabulary.conflicts.has_conflict`,
:func:`~brutex.vocabulary.describe.describe_mask`) billions of times across a
multi-day sweep. Two properties MUST hold under that sustained load and are what
this soak gates:

1. **Bit-for-bit determinism (CLAUDE.md §2 r7 idempotence).** The same
   ``(snapshot, proximity, prior)`` input must produce the SAME ``u64`` mask on
   every one of thousands of evaluations — no drift, no order-dependence, no
   hidden mutable state in the shim or the Rust core. A re-evaluation of a
   previously-seen input is checked against a recorded oracle.

2. **No state leak across calls.** ``evaluate_atoms`` is a pure function of one
   bar's snapshot; evaluating an unrelated bar in between must not perturb a
   re-evaluation. The soak interleaves randomized inputs and re-checks recorded
   ones, so any cross-call contamination surfaces as a mismatch.

The mask-operation round-trip is also soaked: for every produced mask,
``has_conflict`` agrees with ``count_conflicts > 0`` and the
``describe_mask`` / ``matched_condition_names`` decode is internally consistent
and stable on repeat.

Honest envelope (Golden Rule #10): this is a DETERMINISM + consistency soak over
a randomized-but-seeded stream, not a memory-leak gate (that class is covered for
the engine hot paths by ``tests/stress/test_memory_growth.py``) — the inputs are
deterministic given the seed so a failure is reproducible. The ``trading_core``
Rust core is exercised through the real wheel.
"""

from __future__ import annotations

import os
import random
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    describe_mask,
    evaluate_atoms,
    matched_condition_names,
)
from brutex.vocabulary.conflicts import count_conflicts, has_conflict

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml) so
#: the nightly ``pytest tests/stress -m stress`` soak selects them (DEC-STRESS-001).
pytestmark = pytest.mark.stress

_IST = ZoneInfo("Asia/Kolkata")


def _resolve_events() -> int:
    """Resolve the sustained-event count.

    An explicit ``STRESS_VOCAB_EVENTS`` wins (local iteration / targeted soak);
    otherwise the floor is a 50K determinism proof, scaled up (never down) by the
    suite-wide ``STRESS_EVENTS`` knob — so no CI default can silently weaken it.
    """
    explicit = os.environ.get("STRESS_VOCAB_EVENTS")
    if explicit is not None:
        return int(explicit)
    return max(50_000, int(os.environ.get("STRESS_EVENTS", "0")))


_EVENTS: Final[int] = _resolve_events()

#: Seed pool of pre-recorded ``(inputs, mask)`` oracles re-checked during the soak.
_ORACLE_POOL_SIZE: Final[int] = 256


def _random_snapshot(
    rnd: random.Random,
) -> tuple[IndicatorSnapshot, ProximityParams, PriorBarHistory]:
    """Build a randomized but VALID evaluator input from a seeded RNG."""
    center = rnd.randint(10_000_00, 60_000_00)
    half = rnd.randint(1, 5_000)
    lo = center - half
    hi = center + half
    o = rnd.randint(lo, hi)
    c = rnd.randint(lo, hi)
    pd_span = rnd.randint(1, 6_000)
    pdl = center - pd_span
    pdh = center + pd_span
    pdc = rnd.randint(pdl, pdh)
    minute = rnd.randint(0, 375)  # 09:15 → 15:30 window
    ts = datetime(2026, 5, 13, 9, 15, tzinfo=_IST).replace(
        hour=9 + (15 + minute) // 60, minute=(15 + minute) % 60
    )
    day = ts.date()
    # EMA may be warming up (None) ~10% of the time, like the real warm-up phase.
    ema20: int | None = None if rnd.random() < 0.1 else rnd.randint(lo, hi)
    ema200: int | None = None if rnd.random() < 0.1 else rnd.randint(lo, hi)
    snap = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=o,
        high_paisa=hi,
        low_paisa=lo,
        close_paisa=c,
        ema20_paisa=ema20,
        ema200_paisa=ema200,
        pivots=compute_pivot_levels(day, pdh, pdl, pdc),
        fib=compute_fib_levels(day, pdh, pdl),
        day_high_paisa=hi,
        day_low_paisa=lo,
        open_of_day_paisa=center,
    )
    prox = ProximityParams(
        pivot_proximity_bps=rnd.choice((5, 10, 25, 50)),
        pdh_pdl_proximity_bps=rnd.choice((5, 10, 25, 50)),
        fib_proximity_bps=rnd.choice((5, 10, 25, 50)),
    )
    prior_n = rnd.choice((1, 2, 3, 4))
    flags = [rnd.random() < 0.5 for _ in range(rnd.randint(0, prior_n))]
    prior = PriorBarHistory(prior_n=prior_n, flags=flags)
    return snap, prox, prior


def test_evaluate_atoms_is_deterministic_under_sustained_load() -> None:
    """Same input → same u64 across thousands of interleaved evaluations."""
    rnd = random.Random(20260627)
    # Record an oracle pool up front: (snapshot, prox, prior, mask).
    oracles: list[tuple[IndicatorSnapshot, ProximityParams, PriorBarHistory, int]] = []
    for _ in range(_ORACLE_POOL_SIZE):
        snap, prox, prior = _random_snapshot(rnd)
        mask = evaluate_atoms(snap, prox, prior)
        oracles.append((snap, prox, prior, mask))

    drift = 0
    for i in range(_EVENTS):
        # Evaluate a fresh random bar (the "noise" between oracle re-checks)…
        snap, prox, prior = _random_snapshot(rnd)
        fresh = evaluate_atoms(snap, prox, prior)
        # …and re-evaluating the SAME fresh input must be bit-identical.
        if evaluate_atoms(snap, prox, prior) != fresh:
            drift += 1
        # …then re-check a recorded oracle: pure-function ⇒ unchanged.
        o_snap, o_prox, o_prior, o_mask = oracles[i % _ORACLE_POOL_SIZE]
        if evaluate_atoms(o_snap, o_prox, o_prior) != o_mask:
            drift += 1
    assert drift == 0, f"{drift} non-deterministic evaluations across {_EVENTS} events"


def test_mask_operations_are_consistent_under_sustained_load() -> None:
    """has_conflict / count_conflicts / decode agree + are stable on every mask."""
    rnd = random.Random(20260628)
    violations = 0
    for _ in range(_EVENTS):
        snap, prox, prior = _random_snapshot(rnd)
        mask = evaluate_atoms(snap, prox, prior)

        # has_conflict ⟺ count_conflicts > 0 (the two views must never disagree).
        if has_conflict(mask) != (count_conflicts(mask) > 0):
            violations += 1
        # A real bar with a MULTI-bar prior window (prior_n >= 2) must NEVER
        # co-set a conflict pair — the Stage-1B exactness invariant the property
        # test proves, re-soaked here over a wider random input domain. The
        # single-bar window (prior_n == 1) is deliberately EXCLUDED: a 1-bar
        # window is BOTH all-bullish/bearish AND trivially "alternating" (the
        # n==1 alternating-window convention in the shared Rust core,
        # rust/trading-core/src/evaluator.rs), so it co-sets
        # (prior_n_{bull,bear}ish, prior_alternating) — a documented evaluator
        # corner, NOT pruned here (see test_vocabulary.py for the explicit pin).
        if prior.prior_n >= 2 and has_conflict(mask):
            violations += 1

        # The decode must be internally consistent: flattening describe_mask's
        # per-family conditions yields exactly matched_condition_names, and both
        # are stable on a repeat call (no hidden ordering/state).
        flat = tuple(name for grp in describe_mask(mask) for name in grp.conditions)
        if flat != tuple(matched_condition_names(mask)):
            violations += 1
        if describe_mask(mask) != describe_mask(mask):
            violations += 1
    assert violations == 0, f"{violations} mask-operation inconsistencies across {_EVENTS} events"
