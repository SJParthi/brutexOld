"""Fill-sensitivity annotation — the worst-case-vs-open-anchored haircut fold.

DEC-FILL-EXEC-GRID-1M-001 (the W4 #9 annotation half, R13). The sweep prices
every trade under the ONE hardcoded worst-case fill law on the 1-minute
execution grid (DEC-FILL-WORST-CASE-ONLY-001 + the 1m grid). This leaf folds
one finalist's re-simulated trades into an ADDITIVE per-entry ``fill_sensitivity``
object that discloses how much of the P&L the worst-case pricing itself costs
versus an INDEPENDENT open-anchored re-price of the SAME legs — the exact
reference law the frozen floor property
(``tests/property/test_trade_sim_worst_case_floor.py``) audits the walker
against, promoted here into ONE production home so the test oracle and the
shipped annotation can never fork:

* entry = the entry 1m bar's OPEN;
* ``take_profit`` / ``stop_loss`` = the level anchored on that OPEN entry
  (pnl exactly ``+tp`` / ``-sl``);
* ``signal`` / ``forced_1520`` = the exit 1m bar's OPEN;
* ``end_of_data`` / ``session_gap`` = the exit_ts bar's CLOSE (the B3
  terminal carve-out rides the same close-priced reference — no exemption
  needed, see the property module docstring).

Pure arithmetic over the SAME 1m bars the worst-case trade filled on — no
simulation, so trigger timing cannot diverge from the trade under audit. By
the per-trade dominance law (worst ≤ open-anchored, property-pinned) the
folded ``haircut_paisa`` is ``>= 0`` by construction.

R13: the annotation NEVER re-ranks or hides an entry — a sign-flipped rank-1
stays rank-1, flagged by the display layer. Degrades are NAMED (``not_priced``
with a reason), never a fabricated verdict (Golden Rule #1).

O() honesty (Golden Rule #10): one O(trades) fold per annotated entry over an
O(exec bars) keep-first index built once per call — run-end display work,
never on the mask hot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from brutex.money import rupees_to_paisa
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from brutex.data.models import CandleBar
    from brutex.engine.trade_capture import CapturedTrade

__all__ = [
    "FILL_REFERENCE_BASIS",
    "FILL_SENSITIVITY_NOT_PRICED_REASONS",
    "FILL_SENSITIVITY_STATUS_NOT_PRICED",
    "FILL_SENSITIVITY_STATUS_PRICED",
    "fill_sensitivity_not_priced",
    "fill_sensitivity_verdict",
    "open_anchored_trade_pnl",
]

#: The ONE reference basis token the object discloses — the retired precise-era
#: open-anchored pricing (the floor property's reference law).
FILL_REFERENCE_BASIS: Final[str] = "open_anchored"

#: Status vocabulary (the ``options_exec`` object's two-state law).
FILL_SENSITIVITY_STATUS_PRICED: Final[str] = "priced"
FILL_SENSITIVITY_STATUS_NOT_PRICED: Final[str] = "not_priced"

#: The NAMED not-priced reasons a wired provider may emit — an absent 1m
#: stream, a failed / parity-diverged oracle re-sim, a trade referencing a
#: bar the exec stream does not hold, or an exit reason outside the priced
#: vocabulary (each an honest disclosure, never a fabricated verdict).
FILL_SENSITIVITY_NOT_PRICED_REASONS: Final[tuple[str, ...]] = (
    "no_1m_data",
    "resim_failed",
    "resim_parity_mismatch",
    "bar_missing",
    "unknown_exit_reason",
    "no_replay_context",
)

#: Exit classes priced at the exit bar's OPEN under the reference law.
_OPEN_ANCHORED_EXITS: Final[frozenset[str]] = frozenset({"signal", "forced_1520"})

#: Terminal exit classes priced at the exit_ts bar's CLOSE under the reference.
_CLOSE_PRICED_EXITS: Final[frozenset[str]] = frozenset({"end_of_data", "session_gap"})


class _ReferenceUnpriceable(Exception):
    """Internal control flow: one trade's reference leg cannot be priced.

    Carries the NAMED ``not_priced`` reason — the verdict builder converts it
    into the honest degrade object (never a crash, never a fabricated number).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def fill_sensitivity_not_priced(reason: str) -> dict[str, object]:
    """The NAMED degrade object of a wired-but-unpriceable entry.

    Raises
    ------
    ValueError
        On a reason outside :data:`FILL_SENSITIVITY_NOT_PRICED_REASONS` — the
        vocabulary is the wire contract (the reader validates it), so a typo'd
        reason must fail loud at the writer, never ship an unknown token.
    """
    if reason not in FILL_SENSITIVITY_NOT_PRICED_REASONS:
        raise ValueError(
            f"fill_sensitivity_not_priced: unknown reason {reason!r}; expected one of "
            f"{FILL_SENSITIVITY_NOT_PRICED_REASONS}"
        )
    return {"status": FILL_SENSITIVITY_STATUS_NOT_PRICED, "reason": reason}


def open_anchored_trade_pnl(
    trade: CapturedTrade,
    bars_by_ts: Mapping[datetime, CandleBar],
    *,
    sl_paisa: int | None,
    tp_paisa: int | None,
) -> int:
    """Re-price ONE captured trade's legs under the open-anchored reference law.

    The floor property's ``_open_anchored_pnl`` restated over the captured-trade
    record (module docstring — one law, one home). *bars_by_ts* is the
    keep-first 1m EXECUTION-bar index the trade filled on.

    Raises
    ------
    _ReferenceUnpriceable
        (internal) when a referenced bar is absent from *bars_by_ts*, the exit
        reason is outside the priced vocabulary, or a level exit lacks its
        level — the verdict builder converts it into the named degrade.
    """
    entry_bar = bars_by_ts.get(trade.entry_ts)
    if entry_bar is None:
        raise _ReferenceUnpriceable("bar_missing")
    sign = 1 if trade.direction is Side.BUY else -1
    if trade.exit_reason == "take_profit":
        if tp_paisa is None:
            raise _ReferenceUnpriceable("unknown_exit_reason")
        return tp_paisa
    if trade.exit_reason == "stop_loss":
        if sl_paisa is None:
            raise _ReferenceUnpriceable("unknown_exit_reason")
        return -sl_paisa
    exit_bar = bars_by_ts.get(trade.exit_ts)
    if exit_bar is None:
        raise _ReferenceUnpriceable("bar_missing")
    entry_ref = rupees_to_paisa(entry_bar.open)
    if trade.exit_reason in _CLOSE_PRICED_EXITS:
        return sign * (rupees_to_paisa(exit_bar.close) - entry_ref)
    if trade.exit_reason in _OPEN_ANCHORED_EXITS:
        return sign * (rupees_to_paisa(exit_bar.open) - entry_ref)
    raise _ReferenceUnpriceable("unknown_exit_reason")


def fill_sensitivity_verdict(
    trades: Sequence[CapturedTrade],
    exec_bars: Sequence[CandleBar],
    *,
    sl_paisa: int | None,
    tp_paisa: int | None,
) -> dict[str, object]:
    """Fold one finalist's captured trades into the ``fill_sensitivity`` object.

    Priced shape (all paisa fields exact int64; nothing is ever rounded)::

        {"status": "priced", "reference_basis": "open_anchored",
         "n_trades": N,
         "worst_case_net_pnl_paisa": …,   # Σ trade.pnl_paisa (the shipped law)
         "reference_net_pnl_paisa": …,    # Σ open-anchored re-price
         "haircut_paisa": …,              # reference − worst_case (>= 0 by the
                                          # per-trade dominance law)
         "sign_flip": …}                  # reference > 0 AND worst_case <= 0;
                                          # null when n_trades == 0 (no verdict)

    A trade whose reference leg cannot be priced degrades the WHOLE verdict to
    the named ``not_priced`` object — a partially-referenced sum would compare
    unlike sets (Golden Rule #1: correct-or-absent, never a mixed fold).
    """
    bars_by_ts: dict[datetime, CandleBar] = {}
    for bar in exec_bars:
        # Keep-FIRST on a duplicated timestamp (DEC-LAKE-READ-UNIQUE-001 —
        # the catalog gate upstream already collapses; belt-and-braces).
        bars_by_ts.setdefault(bar.timestamp, bar)
    worst_net = 0
    reference_net = 0
    for trade in trades:
        try:
            reference_net += open_anchored_trade_pnl(
                trade, bars_by_ts, sl_paisa=sl_paisa, tp_paisa=tp_paisa
            )
        except _ReferenceUnpriceable as exc:
            return fill_sensitivity_not_priced(exc.reason)
        worst_net += trade.pnl_paisa
    # null when no trades were folded — no verdict, never a fabricated False.
    sign_flip: bool | None = (reference_net > 0 and worst_net <= 0) if trades else None
    return {
        "status": FILL_SENSITIVITY_STATUS_PRICED,
        "reference_basis": FILL_REFERENCE_BASIS,
        "n_trades": len(trades),
        "worst_case_net_pnl_paisa": worst_net,
        "reference_net_pnl_paisa": reference_net,
        "haircut_paisa": reference_net - worst_net,
        "sign_flip": sign_flip,
    }
