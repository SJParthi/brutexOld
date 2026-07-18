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

from itertools import pairwise

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
            # The nullable Stage-B axes: both None arms drawn so the growth +
            # win_rate_safe presets' presence-flag laws ride every property.
            log_growth_at_half_kelly=draw(_composite),
            sl_hit_rate=draw(st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False))),
            # The nullable Stage-C recency axes (DEC-METRICS-TRAILING-WINDOW-001)
            # — the recent / recent_decay presence-flag laws ride every property.
            trailing_sharpe_ratio=draw(_composite),
            decay_weighted_net_paisa=draw(
                st.one_of(st.none(), st.floats(-1e9, 1e9, allow_nan=False))
            ),
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


@given(records=_records())
@settings(max_examples=60, deadline=None)
def test_win_rate_safe_orders_win_rate_monotonically(records: list[RankRecord]) -> None:
    # DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B: win rate dominates the
    # key — descending across the whole order; and within any equal win
    # rate, concrete SL-hit rates precede None (presence-flag law) and rank
    # ascending among themselves.
    ordered = PRESETS["win_rate_safe"].sort_records(records)
    win_rates = [record.win_rate for record in ordered]
    assert win_rates == sorted(win_rates, reverse=True)
    for earlier, later in pairwise(ordered):
        if earlier.win_rate != later.win_rate:
            continue
        if earlier.sl_hit_rate is None:
            assert later.sl_hit_rate is None
        elif later.sl_hit_rate is not None:
            assert earlier.sl_hit_rate <= later.sl_hit_rate


@given(records=_records())
@settings(max_examples=60, deadline=None)
def test_recency_presets_order_scored_descending_nulls_last(
    records: list[RankRecord],
) -> None:
    # DEC-METRICS-TRAILING-WINDOW-001: each recency preset orders its own
    # metric descending over the SCORED records, with every None strictly
    # after every scored one (the presence-flag law).
    for token, field in (
        ("recent", "trailing_sharpe_ratio"),
        ("recent_decay", "decay_weighted_net_paisa"),
    ):
        ordered = PRESETS[token].sort_records(records)
        values = [getattr(record, field) for record in ordered]
        scored = [value for value in values if value is not None]
        assert scored == sorted(scored, reverse=True), token
        # Nulls form a contiguous TAIL — no scored record after a None.
        if None in values:
            assert all(value is None for value in values[values.index(None) :]), token
