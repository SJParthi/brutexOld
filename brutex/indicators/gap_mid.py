"""Session-edge gap-midpoint level — thin delegator to the shared Rust core.

``DEC-GAP-MIDPOINT-CONDITIONS-001`` (Group 15, bits 66-68): after a gap
open, price tends to revert intraday toward the **midpoint of the opening
gap measured between the two session-edge candle extremes** —

* ``X1`` — the HIGH/LOW of the *last completed TF bar of the PRIOR session*;
* ``X2`` — the HIGH/LOW of the *first completed TF bar of TODAY's session*;
* gap-UP day (``open_of_day > pdc``): level = round-half-even midpoint of
  the two HIGHs; gap-DOWN day: the two LOWs; zero gap ⇒ **no level**.

The arithmetic is delegated to the shared Rust ``trading_core`` crate
(``DEC-SHARED-CORE-001`` — the **single source of truth**, exactly like
:func:`brutex.indicators.pivots.compute_pivot_levels`), so BruteX and
TickVault compute identical levels by construction. Python NEVER
re-implements the midpoint / direction / rounding math.

The stateful X1/X2 *capture* (the per-day session-edge tracking, the Muhurat
skip law, the arm-from-bar-2 ordinal) lives in
:class:`brutex.engine.stage1a.Stage1AMatrixBuilder` — this module is the pure
level function only.
"""

from __future__ import annotations

import trading_core

__all__ = ["compute_gap_mid"]


def compute_gap_mid(
    x1_hi_paisa: int,
    x1_lo_paisa: int,
    x2_hi_paisa: int,
    x2_lo_paisa: int,
    open_of_day_paisa: int,
    pdc_paisa: int,
) -> tuple[int, int] | None:
    """Compute the session-edge gap-midpoint level for one trading day.

    Delegates to the shared Rust ``trading_core.compute_gap_mid``
    (``DEC-SHARED-CORE-001`` — one math source; the ``pivots.py`` delegation
    pattern). O(1).

    Parameters
    ----------
    x1_hi_paisa, x1_lo_paisa
        The prior session's LAST completed TF bar high/low (int64 paisa).
    x2_hi_paisa, x2_lo_paisa
        Today's FIRST completed TF bar high/low (int64 paisa).
    open_of_day_paisa
        Today's session open (the first bar's open, int64 paisa).
    pdc_paisa
        The prior completed session's close (int64 paisa) — the gap
        direction is the exact sign of ``open_of_day - pdc`` (deliberately
        NOT the 0.5% threshold of bits 48/49 — the design's no-threshold
        law; masks compose the thresholded variant by AND).

    Returns
    -------
    tuple[int, int] | None
        ``(gap_mid_paisa, direction)`` with ``direction`` ``1`` for a gap-UP
        day (the level is the round-half-even midpoint of the two HIGHs) and
        ``-1`` for gap-DOWN (the two LOWs) — or ``None`` on a zero gap
        (``open_of_day == pdc``): no level exists and the Group-15 bits
        stay 0 all day.

    Raises
    ------
    ValueError
        If either candle has ``high < low`` (data corruption) or any input
        magnitude exceeds the kernel's overflow-safe bar envelope — the Rust
        boundary's own clean refusal, never a panic.
    """
    result: tuple[int, int] | None = trading_core.compute_gap_mid(
        x1_hi_paisa,
        x1_lo_paisa,
        x2_hi_paisa,
        x2_lo_paisa,
        open_of_day_paisa,
        pdc_paisa,
    )
    return result
