"""Property invariants for the sweep data-preflight + estimator
(DEC-SWEEP-PREFLIGHT-ESTIMATOR-001, BUILD-SPEC §10.3).

* **Parity — the license for ZERO ``sweep_orchestrator.py`` edits:** over
  randomized small axes (INCLUDING duplicate tokens), real
  :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig`s satisfy
  ``estimate_trials(selection) == bloom_capacity_hint(grid)`` and
  ``estimate_tuples(selection) == count_sweep_tuples(grid)`` bit-for-bit —
  the shipped grid oracles are REUSED as test oracles, never edited.
* **Clamp laws:** idempotence (re-clamping a clamped range is a no-op),
  the floor bound (``start >= SWEEP_HISTORY_FLOOR``) and the
  pull-deliverable end bound (``end <= today − 1``).
* **Span partition law:** the missing spans PARTITION ``expected − covered``
  exactly — disjoint, ordered, session counts summing to the missing total.
* **Estimator monotonicity:** adding an axis value never decreases the
  trial count, the byte need, or the fallback seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

from brutex.data.session_coverage import PairScan, ScanKind
from brutex.engine.combo_mask import count_combos
from brutex.engine.sweep_data_preflight import (
    SWEEP_HISTORY_FLOOR,
    clamp_dates,
    preflight_disk_coverage,
)
from brutex.engine.sweep_estimate import (
    SweepSelection,
    estimate_selection,
    estimate_trials,
    estimate_tuples,
    selection_from_raw,
)
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    bloom_capacity_hint,
    count_sweep_tuples,
)
from brutex.strategies.types import FibAnchorKind, Side
from brutex.vocabulary.evaluator import ProximityParams
from hypothesis import given
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001 convention): the Hypothesis `@st.composite`
# strategy below is invoked with no args (`_raw_axes()`) because the decorator
# injects `draw` at runtime; pyright does not model the `@composite` signature
# rewrite and emits a spurious "missing draw". Scoped to this file only — the
# same convention as tests/property/test_engine_stage1a_properties.py.

_START = date(2025, 1, 1)
_END = date(2025, 12, 31)
_TODAY = date(2026, 7, 7)

#: Injective label ↔ Timeframe map: equal labels ⇔ equal Timeframe objects,
#: so both sides' order-preserving dedups collapse duplicates identically.
_TF_BY_LABEL: dict[str, Timeframe] = {
    "1minute": Timeframe("1minute", 1),
    "5minute": Timeframe("5minute", 5),
    "15minute": Timeframe("15minute", 15),
    "1hour": Timeframe("1hour", 60),
}
#: Injective float-token ↔ ProximityParams map (the 8th axis — never dropped).
_PROX_BY_TOKEN: dict[float | None, ProximityParams | None] = {
    None: None,
    5.0: ProximityParams(5, 5, 5),
    10.0: ProximityParams(10, 10, 10),
}


@dataclass(frozen=True)
class _Axes:
    """One randomized RAW axis draw — duplicates deliberately allowed."""

    symbols: list[str]
    tf_labels: list[str]
    sides: list[Side]
    condition_bits: list[int]
    k_values: list[int]
    stop_losses: list[int | None]
    take_profits: list[int | None]
    prox_tokens: list[float | None]
    anchors: list[FibAnchorKind]


@st.composite
def _raw_axes(draw: st.DrawFn) -> _Axes:
    """Draw randomized raw selection axes (duplicate tokens included)."""
    condition_bits = draw(st.lists(st.integers(0, 59), min_size=1, max_size=8))
    n_unique = len(set(condition_bits))
    return _Axes(
        symbols=draw(
            st.lists(st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"]), min_size=1, max_size=4)
        ),
        tf_labels=draw(st.lists(st.sampled_from(sorted(_TF_BY_LABEL)), min_size=1, max_size=4)),
        sides=draw(st.lists(st.sampled_from([Side.BUY, Side.SELL]), min_size=1, max_size=3)),
        condition_bits=condition_bits,
        k_values=draw(st.lists(st.integers(0, n_unique), min_size=1, max_size=3)),
        stop_losses=draw(st.lists(st.sampled_from([None, 5, 100, 200]), min_size=1, max_size=4)),
        take_profits=draw(st.lists(st.sampled_from([None, 10, 250, 500]), min_size=1, max_size=4)),
        prox_tokens=draw(st.lists(st.sampled_from([None, 5.0, 10.0]), min_size=1, max_size=3)),
        anchors=draw(st.lists(st.sampled_from(list(FibAnchorKind)), min_size=1, max_size=3)),
    )


def _grid_from_axes(axes: _Axes) -> SweepGridConfig:
    """Build the REAL grid (the oracle side) from the raw axes."""
    return SweepGridConfig(
        instruments=axes.symbols,
        timeframes=[_TF_BY_LABEL[label] for label in axes.tf_labels],
        directions=axes.sides,
        k_values=axes.k_values,
        condition_bits=axes.condition_bits,
        stop_losses=axes.stop_losses,
        take_profits=axes.take_profits,
        proximities=[_PROX_BY_TOKEN[token] for token in axes.prox_tokens],
        fib_anchors=axes.anchors,
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _selection_from_axes(axes: _Axes) -> SweepSelection:
    """Build the estimator selection (the twin side) from the SAME raw axes."""
    return selection_from_raw(
        symbols=axes.symbols,
        intervals=axes.tf_labels,
        directions=[side.value for side in axes.sides],
        k_values=axes.k_values,
        stop_losses=axes.stop_losses,
        take_profits=axes.take_profits,
        proximities=axes.prox_tokens,
        fib_anchors=[anchor.value for anchor in axes.anchors],
        condition_bits=axes.condition_bits,
        top_k=5,
        start=_START,
        end=_END,
    )


@given(axes=_raw_axes())
def test_parity_estimate_trials_equals_bloom_capacity_hint(axes: _Axes) -> None:
    """§10.3 parity: the raw-axes trial math == the grid oracle, bit-for-bit."""
    assert estimate_trials(_selection_from_axes(axes)) == bloom_capacity_hint(_grid_from_axes(axes))


@given(axes=_raw_axes())
def test_parity_estimate_tuples_equals_count_sweep_tuples(axes: _Axes) -> None:
    """§10.3 parity: the raw-axes tuple math == the grid oracle, bit-for-bit."""
    assert estimate_tuples(_selection_from_axes(axes)) == count_sweep_tuples(_grid_from_axes(axes))


def test_parity_duplicate_tokens_collapse_identically() -> None:
    """Deterministic duplicate-token regression: dup axes == deduped closed forms."""
    dup = _Axes(
        symbols=["NIFTY", "NIFTY", "BANKNIFTY"],
        tf_labels=["1minute", "1minute"],
        sides=[Side.BUY, Side.BUY, Side.SELL],
        condition_bits=[0, 1, 1, 2],
        k_values=[1, 1, 2],
        stop_losses=[None, None, 5],
        take_profits=[10, 10],
        prox_tokens=[None, None, 5.0],
        anchors=[FibAnchorKind.PREV_DAY, FibAnchorKind.PREV_DAY],
    )
    grid = _grid_from_axes(dup)
    sel = _selection_from_axes(dup)
    assert estimate_trials(sel) == bloom_capacity_hint(grid)
    assert estimate_tuples(sel) == count_sweep_tuples(grid)
    # And both equal the hand-deduped closed forms (8-axis product / Σ_k C(3,k) × 7).
    assert estimate_tuples(sel) == 2 * 1 * 2 * 2 * 2 * 1 * 2 * 1
    other_axes = 2 * 1 * 2 * 2 * 1 * 2 * 1
    assert estimate_trials(sel) == (count_combos(1, 3) + count_combos(2, 3)) * other_axes


@given(
    start=st.dates(min_value=date(2015, 1, 1), max_value=date(2030, 12, 31)),
    end=st.dates(min_value=date(2015, 1, 1), max_value=date(2030, 12, 31)),
    observed=st.frozensets(
        st.dates(min_value=date(2024, 1, 1), max_value=date(2026, 12, 31)), max_size=5
    ),
)
def test_clamp_idempotent_and_bounded(start: date, end: date, observed: frozenset[date]) -> None:
    """§10.3: ``clamp(clamp(x)) == clamp(x)``; floor + pull-deliverable bounds hold."""
    first = clamp_dates(start, end, today=_TODAY, observed=observed)
    second = clamp_dates(first.start, first.end, today=_TODAY, observed=observed)
    assert (second.start, second.end) == (first.start, first.end)
    assert second.expected_sessions == first.expected_sessions
    assert (second.reversed_range, second.zero_sessions, second.out_of_calendar) == (
        first.reversed_range,
        first.zero_sessions,
        first.out_of_calendar,
    )
    assert first.start >= SWEEP_HISTORY_FLOOR
    assert first.end <= _TODAY - timedelta(days=1)
    # A re-clamp of an already-clamped range never needs a note.
    assert second.notes == ()


#: The canonical expected list one fixed clamp produces — the span-partition
#: property draws covered subsets of it.
_EXPECTED = clamp_dates(date(2025, 1, 1), date(2025, 3, 31), today=_TODAY).expected_sessions


@given(covered=st.frozensets(st.sampled_from(_EXPECTED)))
def test_missing_spans_partition_expected_minus_covered(covered: frozenset[date]) -> None:
    """§10.3 span partition law: disjoint, ordered, session-counts sum exactly."""
    scan = PairScan(
        symbol="NSE-NIFTY",
        interval="1minute",
        kind=ScanKind.DATES,
        covered=covered,
        present=bool(covered),
        corrupt_files=(),
        bars_found=len(covered),
        months_probed=0,
    )
    report = preflight_disk_coverage(
        _EXPECTED, {("NSE-NIFTY", "1minute"): scan}, symbols=["NSE-NIFTY"], intervals=["1minute"]
    )
    missing = [day for day in _EXPECTED if day not in covered]
    if not missing:
        assert report.ok
        return
    assert not report.ok
    (verdict,) = report.not_ready
    spans = verdict.spans
    # Ordered + disjoint (each span strictly after the previous one).
    for left, right in pairwise(spans):
        assert left.end < right.start
    # Each span is a contiguous run of MISSING expected sessions...
    reconstructed: list[date] = []
    for span in spans:
        members = [day for day in _EXPECTED if span.start <= day <= span.end]
        assert span.sessions == len(members)
        assert all(day in missing for day in members)
        reconstructed.extend(members)
    # ...and together they partition expected − covered EXACTLY.
    assert reconstructed == missing
    assert sum(span.sessions for span in spans) == len(missing)


@given(
    axes=_raw_axes(),
    grow_axis=st.sampled_from(
        [
            "symbols",
            "intervals",
            "directions",
            "stop_losses",
            "take_profits",
            "proximities",
            "fib_anchors",
        ]
    ),
)
def test_estimator_monotonicity_adding_an_axis_value_never_decreases(
    axes: _Axes, grow_axis: str
) -> None:
    """§10.3: adding a NEW distinct axis value never decreases X / Z / fallback Y."""
    base = _selection_from_axes(axes)
    grown_kwargs: dict[str, list[object]] = {
        "symbols": list(base.symbols),
        "intervals": list(base.intervals),
        "directions": list(base.directions),
        "stop_losses": list(base.stop_losses),
        "take_profits": list(base.take_profits),
        "proximities": list(base.proximities),
        "fib_anchors": list(base.fib_anchors),
    }
    extra_by_axis: dict[str, object] = {
        "symbols": "EXTRA-SYMBOL",
        "intervals": "59minute",
        "directions": "EXTRA-DIRECTION",
        "stop_losses": 999,
        "take_profits": 999,
        "proximities": 25.0,
        "fib_anchors": "extra_anchor",
    }
    grown_kwargs[grow_axis] = [*grown_kwargs[grow_axis], extra_by_axis[grow_axis]]
    grown = selection_from_raw(
        symbols=[str(s) for s in grown_kwargs["symbols"]],
        intervals=[str(s) for s in grown_kwargs["intervals"]],
        directions=[str(s) for s in grown_kwargs["directions"]],
        k_values=base.k_values,
        stop_losses=grown_kwargs["stop_losses"],  # type: ignore[arg-type]
        take_profits=grown_kwargs["take_profits"],  # type: ignore[arg-type]
        proximities=grown_kwargs["proximities"],  # type: ignore[arg-type]
        fib_anchors=[str(s) for s in grown_kwargs["fib_anchors"]],
        condition_bits=axes.condition_bits,
        top_k=5,
        start=_START,
        end=_END,
    )
    assert estimate_trials(grown) >= estimate_trials(base)
    assert estimate_tuples(grown) >= estimate_tuples(base)
    est_base = estimate_selection(base, calibration=None, env={})
    est_grown = estimate_selection(grown, calibration=None, env={})
    assert est_grown.est_result_bytes >= est_base.est_result_bytes
    assert est_grown.est_seconds_fallback >= est_base.est_seconds_fallback
