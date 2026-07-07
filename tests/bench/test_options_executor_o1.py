"""O(1)-per-trade bench for the options materializer (DEC-OPTIONS-EXECUTOR-001).

:func:`brutex.options.executor.materialize_trade` does a fixed amount of work per
call — a constant strike ALU, one bar-source probe, and one O(1) cost compute —
so its per-call cost must be **independent of how many trades precede it**
(CLAUDE.md §5 / Golden Rule #5). Pricing the 1,000,000th signal must cost the
same as the 1,000th; there is no per-call state that grows.

Measured as the mean per-call ``materialize_trade`` latency at two tiers
(``N_small`` vs ``N_big``); the ratio must stay under the CI ceiling — ``5×`` on
the shared GHA runner, tightened to the decreed strict ``1.1×`` on the
self-hosted M4-Pro tier (``tests/bench/_o1_tiers.py::ratio_ceiling``,
DEC-O1-STRICT-TIER-001). Each tier is timed as the best (minimum) of N repeats —
the noise-robust true-cost estimator.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import OptionBar
from brutex.options.executor import MaterializedTrade, OptionBarPair, SpotSignal, materialize_trade
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

from tests.bench._o1_tiers import ratio_ceiling

_IST = ZoneInfo("Asia/Kolkata")
# Canonical priced-path date: post-2024-10-01 (the verified ETC window —
# DEC-ETC-REGIME-HISTORY-001 made pre-boundary dates reject) and clear of
# the 2024-11-20 NIFTY lot transition.
_ENTRY = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_EXIT = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_RULE = RULES_BY_NAME["atm_plus_2_in_dir"]

_N_SMALL = 2_000
_N_BIG = 200_000
_REPEATS = 5


def _bar(side: str, open_rupees: str) -> OptionBar:
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=10,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=Decimal("0.15"),
    )


class _Source:
    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        return OptionBarPair(
            entry_bar=_bar(option_type, "120.00"), exit_bar=_bar(option_type, "150.00")
        )


_SIGNAL = SpotSignal(
    instrument=OptionsUnderlying.NIFTY,
    direction=TradeDirection.BULL,
    entry_ts=_ENTRY,
    exit_ts=_EXIT,
    spot_at_entry=Decimal("25012"),
    expiry_date=date(2024, 10, 31),
)


def _per_call_seconds(n: int) -> float:
    source = _Source()
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        for _i in range(n):
            out = materialize_trade(_SIGNAL, _RULE, source=source)
            assert isinstance(out, MaterializedTrade)
        best = min(best, (time.perf_counter() - t0) / n)
    return best


@pytest.mark.slow
@pytest.mark.bench
def test_materialize_per_call_cost_is_flat() -> None:
    """Per-call materialize cost at ``N_big`` must be within the CI ceiling of ``N_small``."""
    _per_call_seconds(_N_SMALL)  # warm the allocator / import the lazy costs deps
    per_small = _per_call_seconds(_N_SMALL)
    per_big = _per_call_seconds(_N_BIG)
    ratio = per_big / max(per_small, 1e-12)

    ceiling = ratio_ceiling(5.0)
    print(
        f"\nOptions materialize per-call O(1) bench (DEC-OPTIONS-EXECUTOR-001) "
        f"— best of {_REPEATS} runs:\n"
        f"  N_small = {_N_SMALL:,} | {per_small * 1e9:.1f} ns/call\n"
        f"  N_big   = {_N_BIG:,} | {per_big * 1e9:.1f} ns/call\n"
        f"  ratio   = {ratio:.2f}× (ceiling {ceiling}×)\n"
    )
    assert ratio < ceiling, (
        f"materialize_trade per-call cost ratio {ratio:.2f}× (N_big vs N_small) exceeds "
        f"the {ceiling}× CI ceiling — per-trade work may have regressed to super-linear."
    )
