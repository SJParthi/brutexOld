"""Opt-in runtime look-ahead barrier for the brute-force SWEEP path.

DEC-SWEEP-LOOKAHEAD-GUARD-001: both pre-existing runtime look-ahead guards
protect only the library-strategy path — the timestamp-based feed-boundary
guard (:meth:`brutex.feed.replay.ReplayFeed.ensure_no_lookahead`) and the
runner-wired :class:`~brutex.feed.lookahead.LookAheadBarrier` cursor
invariant (the deleted ``BacktestDriver`` was the historical second wiring
site — DEC-EXECUTION-TRIO-DELETE-001). The brute-force sweep spine
(``run_persisted_sweep`` →
Stage-1A ``build_bar_bits`` → Stage-1B → ``metrics_for_mask`` /
``simulate_trades`` → ``ranked_combo_to_result_row``) had NO runtime guard;
it is structurally causal today (verified: Stage-1A updates prior-bar history
only after evaluation; the trade sim enters at the next bar's open), so this
module is **defense-in-depth against future regressions**, not a bug fix.

## What this module provides

* :func:`sweep_lookahead_guard_enabled` — reads the opt-in env flag
  ``BRUTEX_SWEEP_LOOKAHEAD_GUARD`` (truthy literals: ``1/true/yes/on``,
  case-insensitive). Default **OFF**.
* :class:`GuardedBarWindow` — an index-guarded, read-only ``Sequence`` view
  over a sweep's bar window. It REUSES the COMMON
  :class:`brutex.feed.lookahead.LookAheadBarrier` (``record=False`` — O(1)
  memory; CLAUDE.md §11 COMMON: no second barrier implementation).
* :func:`maybe_guard_bars` — the ONE wiring seam
  :func:`brutex.engine.sweep_run.run_persisted_sweep` calls at setup.

## Zero-cost OFF contract (the hard constraint)

The flag is resolved **once per sweep run** at setup, never per bar: when
OFF, :func:`maybe_guard_bars` returns the caller's *bars* object **itself**
(``result is bars``), so every downstream loop runs over the identical,
unwrapped sequence — byte-identical results AND cost-unchanged hot path (no
per-bar flag check exists anywhere; proven structurally by the identity
contract and by the ``tests/bench/test_sweep_lookahead_off_cost.py`` A/B
bench plus the pre-existing sweep O(1) gates).

## Honest envelope (Golden Rule #10) — what the ON-mode barrier catches

The guard interposes at the sweep's **bar-window sequence seam** — the one
seam where a genuine future-index read is mechanically observable:

* Sequential iteration (the sweep's normal consumption) advances a
  forward-only **consumption front** (the barrier cursor).
* Any *indexed* / *sliced* / *reversed* access to a bar at an index AHEAD of
  that front raises :class:`~brutex.feed.errors.LookAheadError` — e.g. a
  future regression that adds a ``bars[i + 1]`` peek fires the barrier the
  moment it executes on the **first sequential pass** over the window: the
  Stage-1A build when ``kernel=None``, or the FIRST trade-sim walk on the
  resident-kernel path (where Stage-1A is skipped).
* The front is monotone across the WHOLE sweep run (phases legitimately
  re-walk the same already-consumed window: the first full pass, then one
  trade-sim walk per surviving mask, then the finalist re-simulation), so
  after the first full pass every in-window index is "known data" and
  random access is permitted — the barrier does NOT re-arm per phase; a
  peek introduced in a LATER phase is inside the front and is the property
  gates' layer to catch, not the barrier's.
* It does NOT interpose on **derived per-bar state** (the ``bar_bits``
  truth-matrix, walker-internal state, indicator streaming state): a
  future-information flow smuggled through derived state is caught by the
  permanent suffix-independence property gates
  (``tests/property/test_sweep_suffix_independence.py``), not by this
  runtime barrier. The two layers are complementary, exactly like the
  feed-path pairing documented in :mod:`brutex.feed.lookahead`.

## No global mutable state

The env flag is read (never written) at the one setup seam; the barrier and
the guarded view are plain per-run instances handed down by the caller.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Final, overload

import structlog

# Since DEC-PREBUILD-FRAME-WRITE-001 the env-flag vocabulary (the
# truthy/falsy literal sets + the `env_flag_tristate` classifier) LIVES in
# the wheel-free `brutex.config.env_flags` leaf (the prebuild frame-write
# kill switch in `brutex.data` needs the same vocabulary WITHOUT importing
# the engine package, whose `__init__` requires the compiled `fno_math`
# wheel); this module re-exports the classifier byte-compatibly (in
# `__all__`, identity-pinned by tests/unit/test_config_env_flags.py) so
# every existing `from brutex.engine.sweep_lookahead import
# env_flag_tristate` consumer (`rust_metrics` / `rust_stage1a` /
# `sweep_apriori` / `results_depth_pass`) is untouched. The literal SETS
# themselves are consumed only through the classifier — import them from
# `brutex.config.env_flags` directly if a new consumer ever needs them.
from brutex.config.env_flags import FALSY_LITERALS, TRUTHY_LITERALS, env_flag_tristate
from brutex.feed.lookahead import LookAheadBarrier

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    # Annotation-only — keep CandleBar out of this module's runtime import
    # graph so the strict per-module coverage gate stays light (mirrors the
    # convention in sweep_run.py / sweep_kernel.py).
    from brutex.data.models import CandleBar

_LOG = structlog.get_logger("brutex.engine.sweep_lookahead")

#: The opt-in env flag. Unset / falsy ⇒ the guard is OFF (the default) and
#: the sweep hot path is byte-identical and cost-unchanged.
SWEEP_LOOKAHEAD_GUARD_ENV: Final[str] = "BRUTEX_SWEEP_LOOKAHEAD_GUARD"

#: Identity re-exports of the ONE env-flag vocabulary (the SAME frozenset
#: objects as :mod:`brutex.config.env_flags` — aliases, never copies, so
#: CLAUDE.md §11 COMMON holds): four leaf modules whose import posture
#: forbids the engine package (``feed_rust`` / ``money_rust`` /
#: ``strategies.combo_rust`` / ``nightly.box_update``) carry inline
#: vocabulary twins mirror-PINNED against THIS module's historical
#: vocabulary home (their suites assert
#: ``<leaf>._TRUTHY == sweep_lookahead._TRUTHY``), so the private names
#: are deliberately KEPT here after the DEC-PREBUILD-FRAME-WRITE-001 hoist.
_TRUTHY: Final[frozenset[str]] = TRUTHY_LITERALS
_FALSY: Final[frozenset[str]] = FALSY_LITERALS

__all__ = [
    "SWEEP_LOOKAHEAD_GUARD_ENV",
    # _FALSY/_TRUTHY are exported so static analysis counts the aliases as
    # USED (CodeQL py/unused-global-variable, alerts 1287/1115): they are
    # consumed cross-module by the mirror-pin suites (test_feed_rust /
    # test_money_rust / test_strategies_combo_rust /
    # test_nightly_box_update / test_lake_rust_render) — see the alias
    # comment above; never delete without re-homing those pins.
    "_FALSY",
    "_TRUTHY",
    "GuardedBarWindow",
    "env_flag_tristate",
    "maybe_guard_bars",
    "sweep_lookahead_guard_enabled",
]


def sweep_lookahead_guard_enabled() -> bool:
    """Return ``True`` iff ``BRUTEX_SWEEP_LOOKAHEAD_GUARD`` is set truthy.

    Truthy literals are ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive,
    surrounding whitespace ignored). Anything else — including unset, empty,
    ``0``, ``false`` or garbage — is OFF: an unrecognised value must never
    silently enable an instrumented mode (fail-safe default). Classification
    rides the COMMON :func:`env_flag_tristate` (an explicit falsy literal and
    an unrecognised value are equally OFF here — only ``True`` enables).
    """
    return env_flag_tristate(os.environ.get(SWEEP_LOOKAHEAD_GUARD_ENV, "")) is True


class GuardedBarWindow(Sequence["CandleBar"]):
    """Index-guarded read-only view over one sweep's bar window (P-06 teeth).

    Wraps the sweep's chronological bar sequence and routes every access
    through a :class:`~brutex.feed.lookahead.LookAheadBarrier`:

    * ``for bar in window`` — the sweep's normal sequential consumption —
      advances the barrier's forward-only cursor (the *consumption front*)
      to each yielded index; re-walking already-consumed indices validates
      them as permitted reads instead of advancing.
    * ``window[i]`` / ``window[a:b]`` — an indexed or sliced read — is
      validated against the front FIRST: any index strictly ahead of it
      raises :class:`~brutex.feed.errors.LookAheadError` before the bar is
      returned. A negative index is normalised (``window[-1]`` is a read of
      the LAST bar and is therefore rejected until the front reaches it).

    The wrapper never copies the bars and adds O(1) work per access — this
    is the opt-in instrumented mode, never the production hot path.

    Parameters
    ----------
    bars
        The chronologically ordered bar window to guard (any sequence; it
        is held by reference, never copied).
    barrier
        Optional pre-built barrier (the tests inject a ``record=True`` one
        to assert the touched-index trail). Defaults to a fresh O(1)-memory
        ``record=False`` :class:`~brutex.feed.lookahead.LookAheadBarrier`.
    """

    __slots__ = ("_barrier", "_bars")

    def __init__(
        self,
        bars: Sequence[CandleBar],
        *,
        barrier: LookAheadBarrier | None = None,
    ) -> None:
        self._bars = bars
        self._barrier = barrier if barrier is not None else LookAheadBarrier(record=False)

    @property
    def barrier(self) -> LookAheadBarrier:
        """The underlying barrier (its ``current_index`` is the consumption front)."""
        return self._barrier

    def __len__(self) -> int:
        return len(self._bars)

    def __iter__(self) -> Iterator[CandleBar]:
        """Yield bars in order, advancing the consumption front monotonically.

        The first walk moves the front forward bar-by-bar; a later re-walk of
        an already-consumed prefix validates each index via
        :meth:`~brutex.feed.lookahead.LookAheadBarrier.access` (a permitted
        past read) instead of moving the front backwards — the barrier's
        cursor is forward-only by contract.
        """
        barrier = self._barrier
        for i, bar in enumerate(self._bars):
            if i > barrier.current_index:
                barrier.advance_to(i)
            else:
                barrier.access(i)
            yield bar

    # The two ``@overload`` stubs are typing-only declarations: their def
    # lines execute once at class creation but the stub bodies can never run,
    # so they sit outside the measured surface (standard overload exclusion —
    # the real, fully-covered implementation follows immediately below).
    # Docstring bodies (not bare ``...`` expression statements) keep CodeQL's
    # no-effect-statement check satisfied with identical typing semantics.
    @overload
    def __getitem__(self, index: int) -> CandleBar:  # pragma: no cover
        """Overload stub: an integer index returns one guarded bar."""

    @overload
    def __getitem__(self, index: slice) -> Sequence[CandleBar]:  # pragma: no cover
        """Overload stub: a slice returns the underlying sub-sequence."""

    def __getitem__(self, index: int | slice) -> CandleBar | Sequence[CandleBar]:
        """Return the bar(s) at *index* iff every touched index is at/behind the front.

        Raises
        ------
        brutex.feed.errors.LookAheadError
            If any touched index is strictly ahead of the consumption front
            (or nothing has been consumed yet — an indexed read before the
            first sequential consumption is a future read by definition).
        IndexError
            If an integer *index* is out of range (checked BEFORE the
            barrier so an out-of-range read keeps the standard sequence
            contract).
        """
        n = len(self._bars)
        if isinstance(index, slice):
            for i in range(*index.indices(n)):
                self._barrier.access(i)
            return self._bars[index]
        i = index + n if index < 0 else index
        if not 0 <= i < n:
            raise IndexError(f"GuardedBarWindow index {index} out of range for {n} bars")
        self._barrier.access(i)
        return self._bars[i]


def maybe_guard_bars(
    bars: Sequence[CandleBar],
    *,
    enabled: bool | None = None,
) -> Sequence[CandleBar]:
    """Wrap *bars* in a :class:`GuardedBarWindow` iff the opt-in guard is ON.

    The flag is resolved exactly ONCE per call — at sweep setup, never inside
    any per-bar loop. When OFF (the default) the caller's *bars* object is
    returned **unchanged** (``result is bars``), so the production hot path
    is byte-identical and cost-unchanged (the zero-cost OFF contract).

    Parameters
    ----------
    bars
        The sweep's bar window.
    enabled
        Explicit override for the flag (tests / programmatic callers);
        ``None`` (the default) reads :func:`sweep_lookahead_guard_enabled`.

    Returns
    -------
    Sequence[CandleBar]
        *bars* itself when OFF; a fresh :class:`GuardedBarWindow` over
        *bars* when ON (one structlog event records the activation —
        an instrumented run is a material, observable choice).
    """
    on = sweep_lookahead_guard_enabled() if enabled is None else enabled
    if not on:
        return bars
    _LOG.info("sweep_lookahead.guard_enabled", n_bars=len(bars))
    return GuardedBarWindow(bars)
