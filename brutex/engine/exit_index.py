"""Exit-bar index helpers — the ONE timestamp↔index seam for the sweep tail.

DEC-SWEEP-EXIT-INDEX-FASTPATH-001. Three tiny, stdlib-only primitives shared
by the equity/metrics tail (:mod:`brutex.engine.sweep_driver`) and the two
trade simulators (:mod:`brutex.engine.trade_sim` /
:mod:`brutex.engine.rust_trade_sim`):

* :func:`build_ts_index` — THE ``{bar.timestamp: index}`` map builder
  (last-wins on a malformed duplicate timestamp, exactly like the three
  historical inline copies in ``sweep_driver.py`` it consolidates —
  CLAUDE.md §11 COMMON: one source of truth, downstream imports).
* :func:`bars_strictly_increasing` — the producer-side certification that a
  bar window's timestamps are strictly increasing, i.e. the index↔timestamp
  mapping is a bijection. Only under this certificate may a consumer use a
  trade's TRUE exit index in place of the last-wins dict lookup without any
  possibility of diverging from it (on a duplicate timestamp the dict maps
  to the LAST duplicate while the walker's true index may be an earlier
  one — a divergence the certificate excludes by construction).
* :func:`exit_indices_consistent` — the consumer-side validation belt: a
  per-trade ``O(trades)`` check that a supplied exit-index stream is
  positionally aligned, in range, and lands each trade on a bar whose
  timestamp IS the trade's ``exit_ts``. A failing stream is DEMOTED by the
  caller to the dict path (never trusted, never an error the dict arm would
  not also have produced).

## Why a dedicated module (cycle-freedom)

``trade_sim`` / ``rust_trade_sim`` produce exit indices; ``sweep_driver``
consumes them — a helper living in either would create an import cycle or a
copy. This leaf imports NOTHING from ``brutex`` at runtime (the annotations
are ``TYPE_CHECKING``-only), so every engine module can import it freely.

## Honest envelope (Golden Rule #10)

* :func:`build_ts_index` and :func:`bars_strictly_increasing` are
  ``O(N_bars)`` per call — callers pay them at seams that are already
  ``O(N_bars)`` (an equity fold, a trade walk, a resident construction),
  never inside a per-bar loop.
* :func:`exit_indices_consistent` is ``O(trades)`` — the whole point of the
  fast path it guards is avoiding a per-mask ``O(N_bars)`` dict build.
* Deterministic and pure: no globals, no I/O, no wall-clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence
    from datetime import datetime

    from brutex.data.models import CandleBar
    from brutex.metrics.types import ClosedTrade

__all__ = [
    "bars_strictly_increasing",
    "build_ts_index",
    "exit_indices_consistent",
]


def build_ts_index(bars: Sequence[CandleBar]) -> dict[datetime, int]:
    """Return the ``{bar.timestamp: index}`` map for *bars* (last-wins).

    Byte-identical semantics to the historical inline
    ``{bar.timestamp: i for i, bar in enumerate(bars)}`` comprehensions it
    consolidates: on a malformed duplicate timestamp the LAST duplicate's
    index wins (well-formed windows have unique timestamps by the
    ``build_equity_curve`` contract). ``O(N_bars)`` time and space.
    """
    return {bar.timestamp: i for i, bar in enumerate(bars)}


def bars_strictly_increasing(bars: Sequence[CandleBar]) -> bool:
    """Return ``True`` iff every bar's timestamp is strictly after its predecessor's.

    The producer-side certificate for the exit-index fast path: strictly
    increasing timestamps make the index↔timestamp mapping bijective, so a
    trade's TRUE exit index and the last-wins dict lookup of its ``exit_ts``
    are provably the SAME value — the fast path can then skip the dict with
    zero divergence risk. An empty or single-bar window is trivially
    certified. ``O(N_bars)``, zero allocation.
    """
    return all(bars[i - 1].timestamp < bars[i].timestamp for i in range(1, len(bars)))


def exit_indices_consistent(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    exit_indices: Sequence[int],
) -> bool:
    """Return ``True`` iff *exit_indices* positionally maps *trades* onto *bars*.

    The consumer-side validation belt (defence-in-depth — both producers
    emit true indices by construction): the stream must be exactly one
    index per trade, every index in ``[0, len(bars))``, and
    ``bars[idx].timestamp == trade.exit_ts`` for each pair. ``O(trades)``
    time — never ``O(N_bars)``, which is the cost the fast path exists to
    avoid. A ``False`` verdict means the caller must DEMOTE to the
    :func:`build_ts_index` dict path (the bit-identical slow arm), never
    adopt the stream.
    """
    if len(exit_indices) != len(trades):
        return False
    n = len(bars)
    for trade, idx in zip(trades, exit_indices, strict=True):
        if not 0 <= idx < n or bars[idx].timestamp != trade.exit_ts:
            return False
    return True
