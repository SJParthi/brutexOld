"""Property gates for :mod:`brutex.ranking.presets` (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

The R30 determinism law: every preset is a STRICT total order over records
with unique run_ids — the sorted output is IDENTICAL for any input
permutation (discovery-order freedom, Golden Rule #4), is a permutation of
the input, and re-sorting is idempotent.
"""

# pyright: reportCallIssue=false
# Rationale (the DEC-PYRIGHT-UNMASK-001 precedent, test_combo_hash_uniqueness):
# the Hypothesis `@st.composite` strategy `_records` is called WITHOUT the
# `draw` argument — Hypothesis injects it at runtime; pyright does not model
# the `@composite` signature transform and reports a false "Argument missing
# for parameter draw" at every `@given(records=_records())` site. Scoped to
# this file only; `reportCallIssue` stays a hard error everywhere else.
from __future__ import annotations

from brutex.ranking.presets import PRESET_NAMES, PRESETS, RankRecord
from hypothesis import given, settings
from hypothesis import strategies as st

_run_ids = st.integers(min_value=0, max_value=2**32 - 1).map(lambda n: f"{n:064x}")
_metric = st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False)
_composite = st.one_of(st.none(), _metric)


@st.composite
def _records(draw: st.DrawFn) -> list[RankRecord]:
    ids = draw(st.lists(_run_ids, min_size=0, max_size=12, unique=True))
    return [
        RankRecord(
            run_id=run_id,
            max_drawdown_pct=draw(st.floats(0.0, 100.0, allow_nan=False)),
            composite_score=draw(_composite),
            max_consecutive_losses=draw(st.integers(0, 40)),
            profit_factor=draw(st.floats(0.0, 20.0, allow_nan=False)),
            win_rate=draw(st.floats(0.0, 1.0, allow_nan=False)),
            calmar_ratio=draw(_metric),
        )
        for run_id in ids
    ]


@given(records=_records(), seed=st.randoms(use_true_random=False))
@settings(max_examples=60, deadline=None)
def test_every_preset_is_permutation_invariant(records: list[RankRecord], seed: object) -> None:
    for name in PRESET_NAMES:
        preset = PRESETS[name]
        baseline = preset.sort_records(records)
        shuffled = list(records)
        seed.shuffle(shuffled)  # type: ignore[attr-defined]
        assert preset.sort_records(shuffled) == baseline, name


@given(records=_records())
@settings(max_examples=60, deadline=None)
def test_output_is_a_permutation_and_resort_is_idempotent(records: list[RankRecord]) -> None:
    for name in PRESET_NAMES:
        preset = PRESETS[name]
        ordered = preset.sort_records(records)
        # Nothing invented, nothing dropped (unique run_ids ⇒ set equality).
        assert sorted(r.run_id for r in ordered) == sorted(r.run_id for r in records), name
        # A total order is a fixed point of its own sort.
        assert preset.sort_records(ordered) == ordered, name


@given(records=_records())
@settings(max_examples=60, deadline=None)
def test_min_dd_first_orders_drawdown_monotonically(records: list[RankRecord]) -> None:
    ordered = PRESETS["min_dd_first"].sort_records(records)
    drawdowns = [record.max_drawdown_pct for record in ordered]
    assert drawdowns == sorted(drawdowns)
