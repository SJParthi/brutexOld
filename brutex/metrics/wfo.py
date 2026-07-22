"""Walk-Forward Optimization (WFO) pass-rates — rolling, anchored, expanding.

Implements the triple-variant walk-forward audit of the Stage-4 filter
cascade (``docs/TRACK1_BACKTEST_SPEC.md`` Stage 4 item 7, spec lines
409-431; upgraded from single-rolling to three variants by
DEC-EXTREME-001). A walk slides a *test* window forward through the
time-ordered series; the three schemes differ only in how the *training*
window is anchored:

* **rolling** — fixed-length training window that slides with the test
  window (``train_size`` bars immediately before each test block);
* **anchored** — training window starts at the series origin and
  *expands* every walk (initial length ``train_size``);
* **expanding** — training window also starts at the origin and expands,
  but from a smaller minimum (``min_train_size``, default
  ``train_size // 2`` — the spec's "1-year start" against a 2-year
  rolling train).

For each scheme the **pass-rate** is the fraction of walks whose held-out
test-window Sharpe is positive (``> min_sharpe``), i.e. the spec's
"positive Sharpe in ≥ N of M walks" criterion expressed as a rate. The
caller applies the spec's gate (≥12/16 rolling+anchored, ≥10/14
expanding) to ``(rate, n_walks)``; ``n_walks`` is available from
:func:`walk_forward_splits`.

No look-ahead: by construction every walk's training window lies strictly
*before* its test window (``train_stop <= test_start``), optionally
separated by an ``embargo`` gap. The invariant is asserted in
``tests/property/test_cpcv_wfo_properties.py``.

Method source
-------------
The walk-forward / out-of-sample rolling-origin evaluation is standard
backtest practice; the triple-variant (rolling / anchored / expanding)
robustness battery follows Marcos López de Prado, *Advances in Financial
Machine Learning* (Wiley, 2018), Ch. 11-12, and Robert Pardo,
*The Evaluation and Optimization of Trading Strategies* (Wiley, 2008),
Ch. 9 (walk-forward analysis). Mapped to BruteX by DEC-EXTREME-001
(``docs/MIGRATION.md`` entry #202) and pinned in
``docs/TRACK1_BACKTEST_SPEC.md`` Stage 4 item 7.

Window sizes are passed in **bars** (the series unit); the spec's
calendar windows (2-year train / 6-month test / 3-month slide) are
converted to bars by the Stage-4 driver using the instrument's
bars-per-day — this module stays a pure, calendar-free function over a
returns series. The spec's "~16 / ~16 / ~14" walk counts are its
5-year-daily illustration; the real count is computed from the series
length and the window parameters (honest envelope, Golden Rule #10).

Precision / complexity
----------------------
Full float64, never rounded (statistical surface; DEC-MONEY-2DP-001 is
money-only). Each walk's test-window Sharpe reuses the stable
:func:`brutex.metrics.cpcv.sharpe_of_returns` (one source of truth,
Golden Rule #11 — COMMON). Cost is linear in the covered bar volume
(``O(Σ test-window sizes)``), already optimal since each bar falls in only
a few overlapping test windows — WFO has tens of walks, not the 12,870
folds of CPCV, so the per-window slice is not a hot path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

# The slice-6 Rust-statistics seam (DEC-METRICS-RUST-STATS-002). Imports only
# stdlib + structlog at module scope (its engine-flag import is lazy — no
# import-time cycle; the cpcv.py precedent).
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import sharpe_of_returns

if TYPE_CHECKING:
    # Lazy at runtime (see the matching note in `brutex/metrics/cpcv.py`) so
    # the strict per-module coverage gate does not double-load numpy's C
    # extension on numpy 2.x.
    import numpy as np
    import numpy.typing as npt

__all__ = [
    "WFOPassRates",
    "WFOScheme",
    "WFOWalk",
    "walk_forward_splits",
    "wfo_pass_rate",
    "wfo_pass_rates",
]

#: Expanding-scheme default minimum train window = ``train_size // this``.
#: ``2`` maps the spec's "1-year start" to half the 2-year rolling train
#: (``TRACK1_BACKTEST_SPEC.md`` line 423).
_EXPANDING_MIN_TRAIN_DIVISOR: Final[int] = 2


class WFOScheme(Enum):
    """The three walk-forward window schemes (spec lines 411-431)."""

    ROLLING = "rolling"
    ANCHORED = "anchored"
    EXPANDING = "expanding"


@dataclass(frozen=True, slots=True)
class WFOWalk:
    """One walk-forward step: a training window strictly before a test window.

    All four bounds are observation indices; the ``_stop`` bounds are
    exclusive. ``train_stop <= test_start`` always holds (no look-ahead),
    with equality when ``embargo == 0``.
    """

    train_start: int
    train_stop: int
    test_start: int
    test_stop: int

    @property
    def train(self) -> npt.NDArray[np.intp]:
        """Training observation indices ``[train_start, train_stop)``."""
        import numpy as np

        return np.arange(self.train_start, self.train_stop, dtype=np.intp)

    @property
    def test(self) -> npt.NDArray[np.intp]:
        """Test observation indices ``[test_start, test_stop)``."""
        import numpy as np

        return np.arange(self.test_start, self.test_stop, dtype=np.intp)


@dataclass(frozen=True, slots=True)
class WFOPassRates:
    """Pass-rates for all three walk-forward schemes, each in ``[0, 1]``."""

    rolling: float
    anchored: float
    expanding: float


def _validate_windows(
    train_size: int,
    test_size: int,
    step: int,
    min_train_size: int,
    embargo: int,
) -> None:
    """Validate walk-forward window parameters, raising ``ValueError`` on misuse."""
    if train_size < 1:
        raise ValueError(f"train_size must be >= 1, got {train_size}")
    if test_size < 1:
        raise ValueError(f"test_size must be >= 1, got {test_size}")
    if step < 1:
        raise ValueError(f"step must be >= 1, got {step}")
    if min_train_size < 1:
        raise ValueError(f"min_train_size must be >= 1, got {min_train_size}")
    if embargo < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")


def _resolve_min_train_size(train_size: int, min_train_size: int | None) -> int:
    """Default an unset expanding minimum to ``train_size // 2`` (>= 1)."""
    if min_train_size is not None:
        return min_train_size
    return max(1, train_size // _EXPANDING_MIN_TRAIN_DIVISOR)


def walk_forward_splits(
    n_samples: int,
    scheme: WFOScheme,
    *,
    train_size: int,
    test_size: int,
    step: int,
    min_train_size: int | None = None,
    embargo: int = 0,
) -> list[WFOWalk]:
    """Generate the ordered walk-forward windows for ``scheme``.

    Parameters
    ----------
    n_samples
        Length of the time-ordered series.
    scheme
        :class:`WFOScheme` — rolling, anchored or expanding.
    train_size
        Training window length in bars. For *rolling* this is the fixed
        window; for *anchored* it is the initial (then-expanding) length.
    test_size
        Test (out-of-sample) window length in bars.
    step
        Forward slide between successive test windows, in bars.
    min_train_size
        Expanding-scheme initial training length; defaults to
        ``train_size // 2``. Ignored by rolling/anchored.
    embargo
        Gap in bars between the end of training and the start of testing
        (default 0 → contiguous). Removes serial-correlation leakage
        across the boundary.

    Returns
    -------
    list[WFOWalk]
        Walks in chronological order. **Empty** if the series is too short
        to fit even one walk (the caller decides what to do).

    Raises
    ------
    ValueError
        On any invalid window parameter, or if ``n_samples < 1``.
    """
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1, got {n_samples}")
    resolved_min_train = _resolve_min_train_size(train_size, min_train_size)
    _validate_windows(train_size, test_size, step, resolved_min_train, embargo)

    initial_train = train_size if scheme is not WFOScheme.EXPANDING else resolved_min_train
    first_test_start = initial_train + embargo

    walks: list[WFOWalk] = []
    test_start = first_test_start
    while test_start + test_size <= n_samples:
        train_stop = test_start - embargo
        train_start = 0 if scheme is not WFOScheme.ROLLING else train_stop - train_size
        walks.append(
            WFOWalk(
                train_start=train_start,
                train_stop=train_stop,
                test_start=test_start,
                test_stop=test_start + test_size,
            )
        )
        test_start += step
    return walks


def wfo_pass_rate(
    returns: Sequence[float] | npt.NDArray[np.float64],
    scheme: WFOScheme,
    *,
    train_size: int,
    test_size: int,
    step: int,
    min_train_size: int | None = None,
    embargo: int = 0,
    min_sharpe: float = 0.0,
) -> float:
    """Fraction of ``scheme`` walks whose held-out test Sharpe beats ``min_sharpe``.

    A walk "passes" iff its test-window
    :func:`brutex.metrics.cpcv.sharpe_of_returns` is defined and strictly
    greater than ``min_sharpe`` (default ``0.0`` → "positive Sharpe", the
    spec's walk criterion). Undefined test windows (fewer than two
    samples, or zero variance) count as non-passes (fail-safe).

    The metric is evaluated on the **test** window only; the training
    window is exposed by :func:`walk_forward_splits` for downstream
    model-based callers.

    Returns a value in ``[0, 1]``.

    Raises
    ------
    ValueError
        On an invalid returns series / window parameters, or if the
        configuration yields **no** walks (series too short — a pass-rate
        over zero walks is undefined; size the windows to the series).
    """
    import numpy as np

    arr = np.asarray(returns, dtype=np.float64)
    # Validate the whole series up front (1-D shape + finiteness) via the
    # shared checker inside sharpe_of_returns — Golden Rule #11 (COMMON):
    # one validation implementation, reused, never duplicated. The scalar
    # is intentionally discarded; per-window Sharpes are computed below.
    _ = sharpe_of_returns(arr)
    walks = walk_forward_splits(
        int(arr.size),
        scheme,
        train_size=train_size,
        test_size=test_size,
        step=step,
        min_train_size=min_train_size,
        embargo=embargo,
    )
    if not walks:
        raise ValueError(
            "walk-forward configuration yields no walks: the series "
            f"({arr.size} bars) is too short for train_size={train_size}, "
            f"test_size={test_size}, embargo={embargo} under {scheme.value}"
        )
    n_pass: int | None = None
    if rust_stats.stats_rust_enabled():
        # The flag-gated Rust WFO kernel (DEC-METRICS-RUST-STATS-002 —
        # REPLICATED claim: the numpy pairwise reduction mirrored, burn-in
        # fenced). ``None`` — latched / wheel-absent / kernel error / refused
        # result / the min_sharpe exact-type domain refusal — falls through
        # to the retained-forever Python loop, so wheel state can never
        # change results or error behaviour (the rust_stats envelope).
        n_pass = rust_stats.wfo_pass_count_via_kernel(
            arr,
            [walk.test_start for walk in walks],
            [walk.test_stop for walk in walks],
            min_sharpe,
            n_walks=len(walks),
            oracle=lambda: _walk_pass_count(arr, walks, min_sharpe),
        )
    if n_pass is None:
        n_pass = _walk_pass_count(arr, walks, min_sharpe)
    return n_pass / len(walks)


def _walk_pass_count(
    arr: npt.NDArray[np.float64],
    walks: list[WFOWalk],
    min_sharpe: float,
) -> int:
    """The pure-Python per-walk pass count — the retained-forever oracle.

    Moved VERBATIM from :func:`wfo_pass_rate`
    (DEC-METRICS-RUST-STATS-002; zero arithmetic change): one
    :func:`brutex.metrics.cpcv.sharpe_of_returns` per test window, a pass
    iff the Sharpe is defined and STRICTLY greater than ``min_sharpe``.
    Serves every call when the ``BRUTEX_STATS_RUST`` flag is OFF (the
    default), every fallback when it is ON, and the burn-in self-check
    compares — never deleted.
    """
    n_pass = 0
    for walk in walks:
        sharpe = sharpe_of_returns(arr[walk.test_start : walk.test_stop])
        if sharpe is not None and sharpe > min_sharpe:
            n_pass += 1
    return n_pass


def wfo_pass_rates(
    returns: Sequence[float] | npt.NDArray[np.float64],
    *,
    train_size: int,
    test_size: int,
    step: int,
    min_train_size: int | None = None,
    embargo: int = 0,
    min_sharpe: float = 0.0,
) -> WFOPassRates:
    """Compute :func:`wfo_pass_rate` for all three schemes at once.

    Convenience wrapper for the spec's "all three must pass" rule (spec
    lines 429-431): returns a :class:`WFOPassRates` with one rate per
    scheme. Each scheme is validated independently, so a too-short series
    raises :class:`ValueError` from the first scheme that yields no walks.
    """
    return WFOPassRates(
        rolling=wfo_pass_rate(
            returns,
            WFOScheme.ROLLING,
            train_size=train_size,
            test_size=test_size,
            step=step,
            min_train_size=min_train_size,
            embargo=embargo,
            min_sharpe=min_sharpe,
        ),
        anchored=wfo_pass_rate(
            returns,
            WFOScheme.ANCHORED,
            train_size=train_size,
            test_size=test_size,
            step=step,
            min_train_size=min_train_size,
            embargo=embargo,
            min_sharpe=min_sharpe,
        ),
        expanding=wfo_pass_rate(
            returns,
            WFOScheme.EXPANDING,
            train_size=train_size,
            test_size=test_size,
            step=step,
            min_train_size=min_train_size,
            embargo=embargo,
            min_sharpe=min_sharpe,
        ),
    )
