"""Independent naive futures reference simulator — the R45 differential check.

DEC-FUTURES-EXEC-TRANSLATION-001: every futures sim re-simulates a sampled
subset of its signals through THIS separate, deliberately naive path and
requires bit-for-bit agreement with the production executor — so the
compositional proof never becomes faith (R45). A mismatch marks the run
SUSPECT, loudly.

## Independence contract (pinned by an import test)

This module shares ONLY the primitive READERS with production — the injected
:class:`~brutex.futures.executor.FrontMonthResolver` /
:class:`~brutex.futures.executor.FuturesBarSource` seams (per the design: the
differential verifies the ARITHMETIC, not the readers) — and NONE of the
executor's pricing logic: it never imports
:func:`~brutex.costs.futures_costs.apply_futures_fill`,
:func:`~brutex.costs.futures_costs.compute_futures_trade_cost`, or
:func:`~brutex.futures.executor.materialize_futures_trade`. The fill/P&L
arithmetic below is re-derived from scratch in exact :class:`~decimal.Decimal`
RUPEES (production computes in int64 paisa) — two different number pipelines
that must land on the same integer. The worst-case fill LAW — the ONLY law,
DEC-FILL-WORST-CASE-ONLY-001 — is independently RE-STATED below (entry HIGH
+ 0.05 / exit LOW − 0.05 for a bull; the exact mirror for a bear; every
fill floored at one tick), never imported, so the differential stays a
genuine cross-derivation, not a tautology.

## Scope (honest)

The naive path prices COST-FREE INDEX futures only (the live production path,
DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001); any signal the production executor would
reject (unknown lot, cost-bearing refusal, no contract, no bar) yields ``None``
here too, so a rejected trade compares as ``None == None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from brutex.calendars.nse import IST
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size
from brutex.options.strike_rules import TradeDirection
from brutex.universe import BRUTE_FORCE_INDEX_UNDERLYINGS

if TYPE_CHECKING:  # pragma: no cover — annotation-only imports
    from collections.abc import Sequence

    from brutex.futures.executor import FrontMonthResolver, FuturesBarSource, FuturesSignal

__all__ = ["DifferentialReport", "differential_spot_check", "naive_reference_futures_pnl"]

#: One tick in exact rupees (₹0.05) — deliberately re-derived here (the naive
#: path re-states its constants; agreement with the production TICK_PAISA=5 is
#: exactly what the differential proves).
_NAIVE_TICK: Final[Decimal] = Decimal("0.05")

#: The cost-free index labels — read from the ONE engine-scope frozenset
#: (primitive DATA, not executor logic).
_NAIVE_INDEX_LABELS: Final[frozenset[str]] = frozenset(
    underlying for _exchange, underlying in BRUTE_FORCE_INDEX_UNDERLYINGS
)


@dataclass(frozen=True, slots=True)
class DifferentialReport:
    """The R45 differential verdict for one run's sampled signals.

    Attributes
    ----------
    mismatches
        One human-readable line per diverging sample (index + both values).
    n_checked
        How many samples were arithmetically compared (policy-skipped samples
        excluded).
    n_skipped_policy
        Samples excluded because production rejected them on the STALE_BAR
        POLICY gate the naive path deliberately does not model —
        counted openly, never silently absorbed.
    """

    mismatches: tuple[str, ...]
    n_checked: int
    n_skipped_policy: int

    @property
    def suspect(self) -> bool:
        """``True`` iff any compared sample diverged — the run is SUSPECT (R45)."""
        return bool(self.mismatches)


def naive_reference_futures_pnl(
    signal: FuturesSignal,
    *,
    resolver: FrontMonthResolver,
    source: FuturesBarSource,
    n_lots: int = 1,
) -> int | None:
    """Naively re-price one signal; net P&L in paisa, or ``None`` if unpriceable.

    The from-scratch re-derivation: resolve the front month through the shared
    reader seam, read the exact entry/exit bars, fill ± one tick adverse
    (floored at one tick) around the bar's ADVERSE EXTREME — the ONLY fill
    law (DEC-FILL-WORST-CASE-ONLY-001, independently RE-STATED here: bull
    buys at entry HIGH + 0.05 and sells at exit LOW − 0.05; bear mirrored —
    sells at entry LOW − 0.05, covers at exit HIGH + 0.05), P&L =
    (sell − buy) × lot × n_lots — all in exact Decimal rupees, converted to
    paisa only at the very end. Costs are zero (cost-free index scope);
    anything outside that scope, or any missing lot/contract/bar, returns
    ``None`` (production rejects those trades).

    Parameters
    ----------
    signal
        The spot signal to re-price.
    resolver, source
        The SHARED primitive reader seams.
    n_lots
        Lots traded (must match the production run's).

    Returns
    -------
    int | None
        Net P&L in int64 paisa, or ``None`` when the trade is unpriceable.
    """
    if signal.instrument not in _NAIVE_INDEX_LABELS:
        return None
    entry_ist = signal.entry_ts.astimezone(IST)
    try:
        lot = get_lot_size(OptionsUnderlying(signal.instrument), entry_ist.date())
    except ValueError:
        return None
    symbol = resolver.resolve(instrument=signal.instrument, on_date=entry_ist.date())
    if symbol is None:
        return None
    window = source.load_window(
        contract_symbol=symbol, entry_ts=signal.entry_ts, exit_ts=signal.exit_ts
    )
    if window is None:
        return None
    if signal.direction == TradeDirection.BULL:
        buy = max(window.entry_bar.high + _NAIVE_TICK, _NAIVE_TICK)
        sell = max(window.exit_bar.low - _NAIVE_TICK, _NAIVE_TICK)
    else:
        sell = max(window.entry_bar.low - _NAIVE_TICK, _NAIVE_TICK)
        buy = max(window.exit_bar.high + _NAIVE_TICK, _NAIVE_TICK)
    pnl_rupees = (sell - buy) * lot * n_lots
    # Bars are 2dp-snapped at the lake-write boundary and the tick is 2dp, so
    # ×100 is exact — no rounding decision hides here.
    return int(pnl_rupees * 100)


def differential_spot_check(
    sample: Sequence[FuturesSignal],
    production: Sequence[int | None],
    *,
    resolver: FrontMonthResolver,
    source: FuturesBarSource,
    n_lots: int = 1,
    policy_skipped: Sequence[bool] | None = None,
) -> DifferentialReport:
    """Compare production per-signal nets against the naive reference (R45).

    Parameters
    ----------
    sample
        The sampled signals.
    production
        The production executor's per-signal net P&L (``None`` = rejected),
        aligned 1:1 with *sample*.
    resolver, source, n_lots
        Threaded to :func:`naive_reference_futures_pnl`.
    policy_skipped
        Optional 1:1 flags marking samples production rejected on the
        ``STALE_BAR`` policy — those are excluded from the arithmetic compare
        and counted in ``n_skipped_policy``.

    Returns
    -------
    DifferentialReport
        ``suspect=True`` iff any compared sample diverged bit-for-bit.

    Raises
    ------
    ValueError
        If the sequences are not aligned 1:1.
    """
    if len(sample) != len(production):
        msg = (
            f"differential_spot_check: sample ({len(sample)}) and production "
            f"({len(production)}) must align 1:1"
        )
        raise ValueError(msg)
    if policy_skipped is not None and len(policy_skipped) != len(sample):
        msg = (
            f"differential_spot_check: policy_skipped ({len(policy_skipped)}) must "
            f"align 1:1 with sample ({len(sample)})"
        )
        raise ValueError(msg)

    mismatches: list[str] = []
    n_checked = 0
    n_skipped = 0
    for i, (signal, produced) in enumerate(zip(sample, production, strict=True)):
        if policy_skipped is not None and policy_skipped[i]:
            n_skipped += 1
            continue
        naive = naive_reference_futures_pnl(signal, resolver=resolver, source=source, n_lots=n_lots)
        n_checked += 1
        if naive != produced:
            mismatches.append(
                f"sample[{i}] {signal.instrument} entry={signal.entry_ts.isoformat()}: "
                f"production={produced!r} naive={naive!r}"
            )
    return DifferentialReport(
        mismatches=tuple(mismatches), n_checked=n_checked, n_skipped_policy=n_skipped
    )
