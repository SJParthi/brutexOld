"""Combinatorial Purged Cross-Validation (CPCV) — splits + pass-rate.

Implements the CPCV scheme of the Stage-4 statistical-validity filter
cascade (``docs/TRACK1_BACKTEST_SPEC.md`` Stage 4 item 3, spec lines
386-388): partition the time-ordered observations into ``S = 16``
contiguous groups, take every ``C(16, 8) = 12,870`` choice of 8 test
groups, and form each training set from the remaining groups with

* **purging** — drop training observations whose *label window* overlaps
  a test label window (a forward-looking label of span ``label_span``
  bars leaks if it straddles the train/test boundary), and
* **embargo** — additionally drop a buffer of ``embargo`` training
  observations immediately *after* each contiguous test block, where
  serial correlation can leak test information forward into training.

Together these remove the look-ahead that plain K-fold leaves in a time
series (the split logic itself is leakage-free; see the property test
``tests/property/test_cpcv_wfo_properties.py`` and the focused
overlap-purge test in ``tests/unit/test_cpcv.py``).

Method source
-------------
Marcos López de Prado, *Advances in Financial Machine Learning*
(Wiley, 2018), Chapter 7 §7.4 (purging ``getTrainTimes`` / embargo
``getEmbargoTimes``) and Chapter 12 (the combinatorial CV construction).
Cited inline in ``docs/TRACK1_BACKTEST_SPEC.md`` Stage 4 item 3 (line
386, "López de Prado, Adv ML in Finance 2018, Ch 12") and as a row in
the ``docs/MIGRATION.md`` methodology-references table (the DEC-P0-040
audit table, ~line 3076: ``CPCV (Combinatorial Purged CV) | López de
Prado, AFML 2018, Ch. 12``). The embargo size is supplied by the caller; the
Stage-4 driver uses the DEC-STAT-001 formula
``max(max_indicator_lookback, max_trade_horizon_bars, 1_trading_day_in_bars)``
(``docs/MIGRATION.md`` entry #123). This module is a pure, deterministic
function over a returns series and never reads indicator state itself.

Precision (DEC-MONEY-2DP-001, money-only)
-----------------------------------------
This is a *statistical* surface, not a money surface: every value here
keeps full float64 precision and is never rounded (rounding a Sharpe or a
pass-rate would corrupt strategy ranking). Fold variance is accumulated
with the numerically stable Chan/Pébay parallel-merge of per-group
``(n, mean, M2)`` triples (Pébay 2008, *Sandia SAND2008-6212*), the same
family used by :mod:`brutex.metrics.compute`, so the combined fold Sharpe
matches a direct two-pass computation to float rounding.

Complexity (Golden Rule #5 / §12 — honest envelope)
---------------------------------------------------
*Honest scope:* CPCV is *combinatorially* expensive by construction —
``C(S, k)`` splits — so the **total** is **not** O(1) and this module
never claims it is. What *is* O(1) is the per-fold work in the series
length ``n``: :func:`cpcv_pass_rate` makes one O(n) pass to build the
per-group moments, then evaluates each of the ``C(S, k)`` folds in
``O(k)`` (a merge of ``k`` group triples) — i.e. **O(1) per fold in n**.
Total ``O(n + C(S, k)·k)``; for fixed ``S, k`` that is linear in ``n``.
The bench gate ``tests/bench/test_cpcv_wfo_o1.py`` asserts the per-fold
cost stays flat as ``n`` grows. :func:`cpcv_splits` additionally
*materialises* the train/test index arrays for downstream model training;
that is inherently ``O(total index volume)`` and is the slow path — use
:func:`cpcv_pass_rate` when only the scalar is needed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING, Final

# The slice-6 Rust-statistics seam (DEC-METRICS-RUST-STATS-001). Imports only
# stdlib + structlog at module scope (numpy stays lazy here), and its own
# engine-flag import is lazy — no import-time cycle.
from brutex.metrics import rust_stats

if TYPE_CHECKING:
    # Imported lazily inside the functions that need it at runtime; kept out
    # of the module top level so the strict per-module coverage gate
    # (`--cov=brutex.metrics.cpcv`) does not eagerly load numpy's C extension
    # twice (numpy 2.x "cannot load module more than once") — mirrors the
    # proven pattern in `brutex/engine/combo_mask.py`.
    import numpy as np
    import numpy.typing as npt

__all__ = [
    "DEFAULT_N_GROUPS",
    "DEFAULT_N_TEST_GROUPS",
    "CPCVSplit",
    "cpcv_pass_rate",
    "cpcv_splits",
    "sharpe_of_returns",
]

#: Default number of CPCV groups ``S`` (``TRACK1_BACKTEST_SPEC.md`` line 387).
DEFAULT_N_GROUPS: Final[int] = 16

#: Default number of test groups ``k`` → ``C(16, 8) = 12,870`` splits
#: (``TRACK1_BACKTEST_SPEC.md`` line 387).
DEFAULT_N_TEST_GROUPS: Final[int] = 8

#: A Sharpe ratio needs at least this many samples to be defined (variance
#: requires ``n >= 2``); folds smaller than this have an undefined metric.
_MIN_SAMPLES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class CPCVSplit:
    """One CPCV train/test split with purge + embargo already applied.

    Attributes
    ----------
    test
        Sorted, unique test observation indices (the union of the chosen
        test groups). Read-only.
    train
        Sorted, unique training indices: every non-test index that
        survived purging and embargo. Read-only; may be empty when the
        purge/embargo window swallows the remaining groups.
    test_groups
        The group ids (``0..n_groups-1``) selected as the test set, in
        ascending order — kept for audit/debugging.
    """

    test: npt.NDArray[np.intp]
    train: npt.NDArray[np.intp]
    test_groups: tuple[int, ...]

    def __post_init__(self) -> None:
        # Freeze the arrays so a downstream consumer cannot mutate a split
        # in place (determinism / reproducibility, Golden Rule #4).
        self.test.flags.writeable = False
        self.train.flags.writeable = False


def _validate_returns(
    returns: Sequence[float] | npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Coerce ``returns`` to a finite 1-D float64 array or raise ``ValueError``."""
    import numpy as np

    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got {arr.ndim}-D shape {arr.shape}")
    if arr.size == 0:
        raise ValueError("returns must be non-empty")
    if not np.all(np.isfinite(arr)):
        raise ValueError("returns must be finite (no NaN/inf)")
    return arr


def _validate_params(
    n_samples: int,
    n_groups: int,
    n_test_groups: int,
    label_span: int,
    embargo: int,
) -> None:
    """Validate CPCV structural parameters, raising ``ValueError`` on misuse."""
    if n_groups < _MIN_SAMPLES:
        raise ValueError(f"n_groups must be >= 2, got {n_groups}")
    if n_groups > n_samples:
        raise ValueError(
            f"n_groups ({n_groups}) cannot exceed n_samples ({n_samples}) — "
            "every group must hold at least one observation"
        )
    if not 1 <= n_test_groups < n_groups:
        raise ValueError(
            f"n_test_groups must be in [1, n_groups - 1] = [1, {n_groups - 1}], got {n_test_groups}"
        )
    if label_span < 0:
        raise ValueError(f"label_span must be >= 0, got {label_span}")
    if embargo < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")


def _group_bounds(n_samples: int, n_groups: int) -> list[int]:
    """Return ``n_groups + 1`` contiguous group boundaries over ``[0, n_samples)``.

    Replicates the ``numpy.array_split`` convention: the first
    ``n_samples % n_groups`` groups carry one extra observation, so group
    ``g`` owns the half-open index range ``[bounds[g], bounds[g + 1])``.
    """
    base, remainder = divmod(n_samples, n_groups)
    bounds = [0]
    for g in range(n_groups):
        size = base + 1 if g < remainder else base
        bounds.append(bounds[-1] + size)
    return bounds


def sharpe_of_returns(returns: Sequence[float] | npt.NDArray[np.float64]) -> float | None:
    """Per-observation Sharpe ratio of a returns series, or ``None`` if undefined.

    Defined as ``mean(returns) / std(returns)`` with the **population**
    standard deviation (divide by ``n``) and a zero risk-free rate — the
    same convention as :mod:`brutex.metrics.compute` (DEC-METRICS-001),
    with no annualisation multiplier. Computed two-pass (mean, then mean
    squared deviation) for numerical stability.

    Returns ``None`` when the series has fewer than two samples or zero
    variance (a flat series), mirroring the spec's "no inf/NaN in result
    rows" rule (``TRACK1_BACKTEST_SPEC.md`` line 746).

    Notes
    -----
    Inputs are assumed to satisfy ``|x| << ~1e150``: beyond that the
    float64 squared-deviation ``(x - mean) ** 2`` overflows to ``inf``
    (and ``inf - inf`` to ``NaN``), in which case the non-finite-variance
    guard simply returns ``None`` rather than propagating a non-finite
    Sharpe — a deliberate fail-safe, not a defect. A near-constant series
    of a value that is not exactly representable in float64 (e.g.
    ``[3.3] * n``) can have a tiny but **real** population variance
    (~1e-31) and therefore a large *finite* Sharpe; this matches the
    population-variance formula above and is likewise not a bug.
    """
    import numpy as np

    arr = _validate_returns(returns)
    if arr.size < _MIN_SAMPLES:
        return None
    mean = float(arr.mean())
    variance = float(np.mean((arr - mean) ** 2))
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    return mean / math.sqrt(variance)


def _group_moments(
    returns: npt.NDArray[np.float64], bounds: list[int]
) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Per-group ``(count, mean, M2)`` triples in one O(n) pass.

    ``M2`` is the sum of squared deviations from the group mean (two-pass,
    stable). Folds combine these triples with the Chan/Pébay parallel
    merge in :func:`_combine_moments`.
    """
    import numpy as np

    n_groups = len(bounds) - 1
    count = np.empty(n_groups, dtype=np.intp)
    mean = np.empty(n_groups, dtype=np.float64)
    m2 = np.empty(n_groups, dtype=np.float64)
    for g in range(n_groups):
        chunk = returns[bounds[g] : bounds[g + 1]]
        count[g] = chunk.size
        mu = float(chunk.mean())
        mean[g] = mu
        m2[g] = float(np.sum((chunk - mu) ** 2))
    return count, mean, m2


def _combine_moments(
    count: npt.NDArray[np.intp],
    mean: npt.NDArray[np.float64],
    m2: npt.NDArray[np.float64],
    group_ids: tuple[int, ...],
) -> float | None:
    """Sharpe of the union of ``group_ids`` via stable moment merge, or ``None``.

    Folds the selected per-group ``(n, mean, M2)`` triples pairwise with
    the Chan/Pébay parallel-variance update (Pébay 2008). ``O(k)`` in the
    number of test groups, independent of the series length.

    As in :func:`sharpe_of_returns`, inputs are assumed ``|x| << ~1e150``:
    the cross-group merge term ``delta * delta * n_total * n_b / combined``
    overflows to ``inf`` (and ``inf - inf`` to ``NaN``) when group means
    differ by more than ~1e150, in which case the non-finite-variance
    guard returns ``None`` (the fold is a non-pass) — a fail-safe, not a
    defect.
    """
    n_total = 0
    mean_total = 0.0
    m2_total = 0.0
    for gid in group_ids:
        n_b = int(count[gid])
        if n_b == 0:  # pragma: no cover - guarded by _validate_params (no empty groups)
            continue
        mean_b = float(mean[gid])
        m2_b = float(m2[gid])
        combined = n_total + n_b
        delta = mean_b - mean_total
        mean_total += delta * n_b / combined
        m2_total += m2_b + delta * delta * n_total * n_b / combined
        n_total = combined
    if n_total < _MIN_SAMPLES:
        return None
    variance = m2_total / n_total
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    return mean_total / math.sqrt(variance)


def cpcv_splits(
    n_samples: int,
    *,
    n_groups: int = DEFAULT_N_GROUPS,
    n_test_groups: int = DEFAULT_N_TEST_GROUPS,
    label_span: int = 0,
    embargo: int = 0,
) -> list[CPCVSplit]:
    """Generate every CPCV train/test split with purging + embargo applied.

    Parameters
    ----------
    n_samples
        Number of time-ordered observations (length of the returns series).
    n_groups
        Number of contiguous groups ``S`` (default 16, the spec value).
    n_test_groups
        Number of test groups ``k`` per split (default 8 → ``C(16, 8) =
        12,870`` splits). The train set is the other ``S - k`` groups,
        minus purged/embargoed indices.
    label_span
        Forward label horizon ``h`` in bars: observation ``i`` carries a
        label over ``[i, i + h]``. Training indices whose label overlaps a
        test label are purged. ``0`` (default) means single-bar labels —
        no overlap purging beyond the group split.
    embargo
        Additional bars removed immediately after each contiguous test
        block (DEC-STAT-001). ``0`` (default) means no embargo buffer.

    Returns
    -------
    list[CPCVSplit]
        One split per combination, in deterministic
        ``itertools.combinations`` (lexicographic) order.

    Raises
    ------
    ValueError
        On any invalid structural parameter (see :func:`_validate_params`).
    """
    import numpy as np

    _validate_params(n_samples, n_groups, n_test_groups, label_span, embargo)
    bounds = _group_bounds(n_samples, n_groups)
    all_indices = np.arange(n_samples, dtype=np.intp)
    splits: list[CPCVSplit] = []
    for test_groups in combinations(range(n_groups), n_test_groups):
        test_mask = np.zeros(n_samples, dtype=bool)
        for gid in test_groups:
            test_mask[bounds[gid] : bounds[gid + 1]] = True
        excluded = test_mask.copy()
        # Find maximal contiguous test blocks via the rising/falling edges
        # of the padded mask, then purge + embargo around each block.
        padded = np.concatenate(([False], test_mask, [False]))
        edges = np.diff(padded.astype(np.int8))
        block_starts = np.flatnonzero(edges == 1)
        block_ends = np.flatnonzero(edges == -1) - 1  # inclusive
        for start, end in zip(block_starts, block_ends, strict=True):
            lo = max(0, int(start) - label_span)
            hi = min(n_samples - 1, int(end) + label_span + embargo)
            excluded[lo : hi + 1] = True
        test_idx = all_indices[test_mask]
        train_idx = all_indices[~excluded]
        splits.append(CPCVSplit(test=test_idx, train=train_idx, test_groups=test_groups))
    return splits


def _fold_pass_count(
    count: npt.NDArray[np.intp],
    mean: npt.NDArray[np.float64],
    m2: npt.NDArray[np.float64],
    n_groups: int,
    n_test_groups: int,
    min_sharpe: float,
) -> int:
    """The pure-Python fold loop — the retained-forever parity oracle.

    Moved VERBATIM from :func:`cpcv_pass_rate` (DEC-METRICS-RUST-STATS-001;
    zero arithmetic change): one Chan/Pébay merge per
    ``itertools.combinations`` fold, a pass iff the merged Sharpe is defined
    and STRICTLY greater than ``min_sharpe``. Serves every call when the
    ``BRUTEX_STATS_RUST`` flag is OFF (the default), every fallback when it
    is ON, and the burn-in self-check compares — the operator-excepted
    Python oracle is NEVER deleted.
    """
    n_pass = 0
    for test_groups in combinations(range(n_groups), n_test_groups):
        sharpe = _combine_moments(count, mean, m2, test_groups)
        if sharpe is not None and sharpe > min_sharpe:
            n_pass += 1
    return n_pass


def cpcv_pass_rate(
    returns: Sequence[float] | npt.NDArray[np.float64],
    *,
    n_groups: int = DEFAULT_N_GROUPS,
    n_test_groups: int = DEFAULT_N_TEST_GROUPS,
    min_sharpe: float = 0.0,
) -> float:
    """Fraction of CPCV test folds whose held-out Sharpe beats ``min_sharpe``.

    For a *signal-only* returns series (Track 1 has no model to fit), the
    CPCV pass-rate measures robustness: across all ``C(S, k)`` held-out
    combinations, how often is the strategy's return on the *unseen* fold
    healthy? A fold "passes" iff its :func:`sharpe_of_returns` is defined
    and strictly greater than ``min_sharpe`` (default ``0.0`` → "positive
    Sharpe", matching the Stage-4 walk-forward criterion). Undefined folds
    (fewer than two samples, or zero variance) count as non-passes
    (fail-safe).

    The metric is evaluated on the **test** folds only; purging/embargo
    govern the *training* set (see :func:`cpcv_splits`) and so do not
    change a test fold's return — they are exposed there for downstream
    model-based callers.

    Returns a value in ``[0, 1]``: ``passing_folds / C(n_groups,
    n_test_groups)``. Computed in ``O(n)`` (one moments pass) plus
    ``O(C(S, k)·k)`` fold merges — O(1) per fold in the series length.

    Raises
    ------
    ValueError
        On an invalid returns series or invalid structural parameters.
    """
    arr = _validate_returns(returns)
    n_samples = int(arr.size)
    _validate_params(n_samples, n_groups, n_test_groups, label_span=0, embargo=0)
    bounds = _group_bounds(n_samples, n_groups)
    count, mean, m2 = _group_moments(arr, bounds)
    n_total = math.comb(n_groups, n_test_groups)
    n_pass: int | None = None
    if rust_stats.stats_rust_enabled():
        # The flag-gated Rust fold kernel (DEC-METRICS-RUST-STATS-001).
        # ``None`` — latched / wheel-absent / kernel error / refused result /
        # the min_sharpe exact-type+exactness domain refusal (foreign types,
        # inexact or float-unconvertible ints, NaN) — falls through to the
        # retained-forever Python oracle, and the flag resolution above
        # degrades to the default (warn-once) when the engine package itself
        # cannot import — so wheel state (fno_math OR trading_core) can never
        # change results or error behaviour (within the documented exception
        # envelope — the rust_stats module docstring).
        n_pass = rust_stats.cpcv_pass_count_via_kernel(
            count,
            mean,
            m2,
            n_test_groups,
            min_sharpe,
            n_total=n_total,
            oracle=lambda: _fold_pass_count(count, mean, m2, n_groups, n_test_groups, min_sharpe),
        )
    if n_pass is None:
        n_pass = _fold_pass_count(count, mean, m2, n_groups, n_test_groups, min_sharpe)
    # The one exact division — produced by the same expression on both arms.
    return n_pass / n_total
