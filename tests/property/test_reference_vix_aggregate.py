"""Property tests for :func:`brutex.engine.reference_wire.aggregate_trade_vix`.

Hypothesis invariants over arbitrary stamped/unstamped trade sets
(DEC-VIX-STAMP-WIRE-001):

* **Order law** — whenever any stamp exists: ``min <= mean <= max``.
* **None-propagation** — the trio is all-``None`` IFF no non-``None`` stamp
  exists (empty trades / all-``None`` stamps → ``(None, None, None)``).
* **Never fabricates** (Golden Rule #1) — ``min`` and ``max`` are ALWAYS
  members of the supplied stamp set; the mean is exactly the
  :func:`math.fsum` mean of the supplied stamps clamped into ``[min, max]``
  (the documented ≤ 1-ulp division-artefact snap) — no value is invented,
  imputed, or carried forward from a ``None``.
* **Permutation invariance** — the trio is independent of trade order.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.engine.reference_wire import aggregate_trade_vix
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_T0 = datetime(2024, 6, 3, 3, 45, tzinfo=UTC)

# Finite positive Decimals in a realistic VIX band (ClosedTrade rejects
# non-finite stamps; the band keeps float conversion well-conditioned).
_vix_levels = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("500"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)
_stamp = st.one_of(st.none(), _vix_levels)
_stamp_pairs = st.lists(st.tuples(_stamp, _stamp), max_size=25)


def _trades(pairs: list[tuple[Decimal | None, Decimal | None]]) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            pnl_paisa=0,
            mfe_paisa=0,
            mae_paisa=0,
            holding_bars=1,
            entry_ts=_T0,
            exit_ts=_T0 + timedelta(minutes=1),
            direction=Side.BUY,
            vix_at_entry=entry,
            vix_at_exit=exit_,
        )
        for entry, exit_ in pairs
    ]


def _stamps(pairs: list[tuple[Decimal | None, Decimal | None]]) -> list[float]:
    return [float(s) for pair in pairs for s in pair if s is not None]


@given(_stamp_pairs)
def test_none_propagation_and_order_law(
    pairs: list[tuple[Decimal | None, Decimal | None]],
) -> None:
    mean, low, high = aggregate_trade_vix(_trades(pairs))
    stamps = _stamps(pairs)
    if not stamps:
        # Empty / all-None input → the honest unknown trio, never a fabricated 0.
        assert (mean, low, high) == (None, None, None)
    else:
        assert mean is not None and low is not None and high is not None
        assert low <= mean <= high


@given(_stamp_pairs)
def test_never_fabricates_a_level(
    pairs: list[tuple[Decimal | None, Decimal | None]],
) -> None:
    """min/max come FROM the stamps; the mean is exactly the clamped fsum mean."""
    mean, low, high = aggregate_trade_vix(_trades(pairs))
    stamps = _stamps(pairs)
    if stamps:
        assert low == min(stamps)
        assert high == max(stamps)
        assert low in stamps
        assert high in stamps
        fsum_mean = math.fsum(stamps) / len(stamps)
        assert mean == min(max(fsum_mean, low), high)
        # The clamp can only ever absorb a ≤ 1-ulp division artefact.
        assert mean is not None
        assert abs(mean - fsum_mean) <= math.ulp(fsum_mean)
    else:
        assert (mean, low, high) == (None, None, None)


@given(_stamp_pairs, st.randoms())
def test_permutation_invariance(
    pairs: list[tuple[Decimal | None, Decimal | None]],
    rng: object,
) -> None:
    """The trio is a set-level summary — trade order never changes it."""
    import random

    assert isinstance(rng, random.Random)
    shuffled = list(pairs)
    rng.shuffle(shuffled)
    assert aggregate_trade_vix(_trades(shuffled)) == aggregate_trade_vix(_trades(pairs))
