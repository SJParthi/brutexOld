"""Strictly OUT-OF-FOLD P(win) via the existing CPCV purge/embargo machinery
(DEC-META-LABEL-ANNOTATE-001 — AFML Ch. 7 §7.4 discipline applied to the
meta-label fit).

The split engine is :func:`brutex.metrics.cpcv.cpcv_splits` — REUSED, never
re-minted (CLAUDE.md §11 COMMON: the purge/embargo law has exactly one
home). Every trade's ``p_win`` is the mean of predictions from the CPCV
splits where it sat in a TEST group while the model was fitted on that
split's purged TRAIN set — no trade is ever scored by a model that saw it
(or a label-overlapping neighbour) in training.

## label_span / embargo — the documented TRADE-INDEX approximation

``cpcv_splits`` purges in OBSERVATION-INDEX units. Trades are ordered by
``entry_ts_us``, and both ``label_span`` and ``embargo`` are set to ONE
conservatively-computed value: the MAXIMUM over all trades of the number of
LATER-ENTERING trades whose entry falls inside that trade's
``[entry_ts, exit_ts]`` holding span. This is an APPROXIMATION — the true
per-trade overlap varies, but taking the maximum span for every observation
purges AT LEAST as much as exact per-trade purging would (conservative:
more purging can only shrink training sets, never leak). Timestamps only —
never bar indices or holding-bar counts (the W4 fold-key law: post-PR-B
those become 1-minute-grid quantities).

## Degenerate cases (correct-or-absent, Golden Rule #1)

Fewer than :data:`MIN_TRADES_FOR_FIT` trades, or single-class labels →
every ``p_win`` is ``None`` with a named degenerate reason — never a
fabricated probability. A split whose purged train set is degenerate
(single-class / too small / failed solve) is SKIPPED and counted; trades
covered by NO surviving split annotate ``None``.

## Defaults — the disclosed deviation from the Stage-4 pass-rate shape

``n_groups=8, n_test_groups=2`` → C(8,2) = 28 splits (28 offline IRLS
fits). The Stage-4 ``cpcv_pass_rate`` defaults (16/8 → 12,870 folds) are
O(k)-per-fold moment merges; a MODEL FIT per fold at that count would be
offline-infeasible for an annotation pass, so the fit uses the smaller
deterministic grid — disclosed here and echoed in every sidecar.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brutex.meta.features import FEATURE_NAMES
from brutex.meta.logistic import DEFAULT_MAX_ITER, DEFAULT_RIDGE, fit_logistic
from brutex.metrics.cpcv import cpcv_splits

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.meta.features import TradeFeatures

__all__ = [
    "MIN_TRADES_FOR_FIT",
    "OOF_N_GROUPS",
    "OOF_N_TEST_GROUPS",
    "OofResult",
    "compute_label_span",
    "oof_p_win",
]

#: The documented floor below which no fit is attempted — mirrors the
#: ranking MIN_TRADES=30 statistical-noise gate (fewer trades cannot
#: support a 16-feature fold-fitted model; the sidecar reads the reason).
MIN_TRADES_FOR_FIT: Final[int] = 30

#: The OOF split grid (the disclosed deviation above): C(8, 2) = 28 splits.
OOF_N_GROUPS: Final[int] = 8
OOF_N_TEST_GROUPS: Final[int] = 2

#: The vix column position inside :data:`FEATURE_NAMES` (standardized
#: per-fold on TRAIN rows only — the leakage law).
_VIX_COLUMN: Final[int] = FEATURE_NAMES.index("vix_entry")

#: Degenerate-reason tokens (the sidecar vocabulary).
REASON_TOO_FEW_TRADES: Final[str] = "too_few_trades"
REASON_SINGLE_CLASS: Final[str] = "single_class"

#: A logistic fit needs both classes present (and >= 2 rows) in TRAIN.
_MIN_CLASSES: Final[int] = 2

#: The scorer signature (the meta_rust facade's shape).
if TYPE_CHECKING:
    Scorer = Callable[[list[float], int, list[float], float], list[float]]


@dataclass(frozen=True, slots=True)
class OofResult:
    """One combo's out-of-fold P(win) column + fit provenance.

    ``p_win`` aligns positionally with the INPUT trade sequence; ``None`` =
    no out-of-fold coverage (degenerate combo, or every covering split was
    skipped). ``label_span == embargo`` — the one conservative trade-index
    value (module docstring).
    """

    p_win: tuple[float | None, ...]
    label_span: int
    embargo: int
    n_splits_fit: int
    n_splits_skipped: int
    degenerate_reason: str | None


def compute_label_span(entries_us: Sequence[int], exits_us: Sequence[int]) -> int:
    """The conservative trade-index label span (module docstring law).

    Over trades sorted ascending by entry: the maximum count of
    later-entering trades whose entry lies at or before trade *i*'s exit —
    computed via one sort + a bisect per trade, O(n log n).

    Raises
    ------
    ValueError
        On a length mismatch or a non-ascending entry sequence (the caller
        sorts; a silent re-sort here would desync the caller's alignment).
    """
    if len(entries_us) != len(exits_us):
        raise ValueError(f"compute_label_span: {len(entries_us)} entries vs {len(exits_us)} exits")
    if any(entries_us[i] > entries_us[i + 1] for i in range(len(entries_us) - 1)):
        raise ValueError("compute_label_span: entries must be ascending (caller sorts)")
    entries = list(entries_us)
    span = 0
    for i, exit_us in enumerate(exits_us):
        overlap = bisect_right(entries, exit_us) - (i + 1)
        span = max(span, overlap)
    return span


def _standardize_vix(matrix: list[list[float]], train_rows: Sequence[int]) -> list[list[float]]:
    """Standardize the vix column using TRAIN-row statistics only.

    Population mean/std over the train rows' vix column; ``std <= 0`` (a
    constant column) leaves the column RAW (no divide-by-zero, no
    information anyway). Returns a NEW matrix — the input is never mutated
    (the caller reuses it across splits).
    """
    if not train_rows:
        return [row[:] for row in matrix]
    values = [matrix[i][_VIX_COLUMN] for i in train_rows]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = variance**0.5
    out = [row[:] for row in matrix]
    if std > 0.0:
        for row in out:
            row[_VIX_COLUMN] = (row[_VIX_COLUMN] - mean) / std
    return out


def oof_p_win(
    trades: Sequence[TradeFeatures],
    *,
    scorer: Scorer | None = None,
    n_groups: int = OOF_N_GROUPS,
    n_test_groups: int = OOF_N_TEST_GROUPS,
    ridge: float = DEFAULT_RIDGE,
    max_iter: int = DEFAULT_MAX_ITER,
) -> OofResult:
    """Compute every trade's strictly out-of-fold P(win) — deterministic.

    *trades* may arrive in any order; internally they are ordered by
    ``(entry_ts_us, trade_seq)`` for the CPCV group assignment (time order
    is what purging protects) and the returned ``p_win`` re-aligns with the
    INPUT order. *scorer* is the inference seam — ``None`` binds the
    production :func:`brutex.meta.meta_rust.score_probabilities` facade
    (Rust when enabled, the Python oracle otherwise).

    Determinism: fixed split order (``cpcv_splits``' lexicographic
    combinations), fixed zero-init IRLS, no RNG — the same trades always
    produce bit-identical probabilities.
    """
    if scorer is None:
        from brutex.meta.meta_rust import score_probabilities

        scorer = score_probabilities
    n = len(trades)
    if n < MIN_TRADES_FOR_FIT:
        return OofResult(
            p_win=(None,) * n,
            label_span=0,
            embargo=0,
            n_splits_fit=0,
            n_splits_skipped=0,
            degenerate_reason=REASON_TOO_FEW_TRADES,
        )
    order = sorted(range(n), key=lambda i: (trades[i].entry_ts_us, trades[i].trade_seq))
    entries = [trades[i].entry_ts_us for i in order]
    exits = [trades[i].exit_ts_us for i in order]
    labels = [1.0 if trades[i].label_win else 0.0 for i in order]
    matrix = [list(trades[i].vector) for i in order]
    span = compute_label_span(entries, exits)
    if len(set(labels)) < _MIN_CLASSES:
        return OofResult(
            p_win=(None,) * n,
            label_span=span,
            embargo=span,
            n_splits_fit=0,
            n_splits_skipped=0,
            degenerate_reason=REASON_SINGLE_CLASS,
        )

    import numpy as np

    n_features = len(FEATURE_NAMES)
    sums = [0.0] * n
    counts = [0] * n
    n_fit = 0
    n_skipped = 0
    for split in cpcv_splits(
        n, n_groups=n_groups, n_test_groups=n_test_groups, label_span=span, embargo=span
    ):
        train = [int(i) for i in split.train]
        test = [int(i) for i in split.test]
        if len(train) < _MIN_CLASSES or len({labels[i] for i in train}) < _MIN_CLASSES:
            n_skipped += 1
            continue
        standardized = _standardize_vix(matrix, train)
        x_train = np.asarray([standardized[i] for i in train], dtype=np.float64)
        y_train = np.asarray([labels[i] for i in train], dtype=np.float64)
        fit = fit_logistic(x_train, y_train, ridge=ridge, max_iter=max_iter)
        if fit is None:
            n_skipped += 1
            continue
        flat = [value for i in test for value in standardized[i]]
        predictions = scorer(flat, n_features, list(fit.weights), fit.bias)
        for local, i in enumerate(test):
            sums[i] += predictions[local]
            counts[i] += 1
        n_fit += 1

    p_sorted: list[float | None] = [
        (sums[i] / counts[i]) if counts[i] > 0 else None for i in range(n)
    ]
    p_by_input: list[float | None] = [None] * n
    for sorted_pos, input_pos in enumerate(order):
        p_by_input[input_pos] = p_sorted[sorted_pos]
    return OofResult(
        p_win=tuple(p_by_input),
        label_span=span,
        embargo=span,
        n_splits_fit=n_fit,
        n_splits_skipped=n_skipped,
        degenerate_reason=None,
    )
