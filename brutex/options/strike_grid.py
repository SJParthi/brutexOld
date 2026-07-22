"""Strike grid + ATM rounding per underlying.

Per `docs/track2/TRACK2_OPTIONS_SPEC.md` §5 — strike steps are
exchange-published (NSE / BSE F&O master) and verified quarterly.

| Underlying | Strike step | Source |
|---|---|---|
| NIFTY 50   | 50 pts  | NSE FNO master |
| BANKNIFTY  | 100 pts | NSE FNO master |
| SENSEX     | 100 pts | BSE FNO master |

Strike steps are CONSTANTS for the 5-year backfill window — no
SEBI changes to step sizes in 2021-2026 per the spec. If a step
change is later announced (rare; usually means lot resize compensates),
add a per-date table similar to lot_size_history.
"""

from __future__ import annotations

from typing import Final

from brutex import optcost_rust
from brutex.options.lot_size_history import OptionsUnderlying

# Strike step in price units (rupees). All instruments use integer
# strike values; step is integer too.
STRIKE_STEP: Final[dict[OptionsUnderlying, int]] = {
    OptionsUnderlying.NIFTY: 50,
    OptionsUnderlying.BANKNIFTY: 100,
    OptionsUnderlying.SENSEX: 100,
}


def get_strike_step(instrument: OptionsUnderlying) -> int:
    """Return the strike-grid step for `instrument` in rupee units."""
    return STRIKE_STEP[instrument]


def round_to_atm(spot_price: float, instrument: OptionsUnderlying) -> int:
    """Round `spot_price` to the nearest strike on the grid (= ATM).

    Args:
        spot_price: Underlying spot value (float rupees).
        instrument: NIFTY / BANKNIFTY / SENSEX.

    Returns:
        Nearest strike (integer rupees). Half-strike ties round away
        from zero (i.e., 24,025 with step=50 rounds to 24,050 because
        Python round() uses banker's rounding but we use explicit floor
        division here to be deterministic).

    Example:
        round_to_atm(24_012.0, NIFTY) -> 24_000  (step=50, closer to 24_000 than 24_050)
        round_to_atm(24_025.0, NIFTY) -> 24_050  (half-step ties up)
        round_to_atm(50_075.0, BANKNIFTY) -> 50_100  (closer to 50_100 than 50_000)

    DEC-OPTCOST-TAILS-RUST-001: with ``BRUTEX_OPTCOST_TAILS_RUST`` truthy the
    rounding runs on the ``fno_math`` ``optcost_round_to_atm`` kernel (the
    CPython float floor-division law transcribed with proof — outside its
    provable envelope the kernel abstains and this Python arm serves); flag
    OFF — the default — is byte-identical.
    """
    step = STRIKE_STEP[instrument]
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.round_atm(
            spot_price, step, python=lambda: _round_to_atm_python(spot_price, step)
        )
    return _round_to_atm_python(spot_price, step)


def _round_to_atm_python(spot_price: float, step: int) -> int:
    """The verbatim Python arm of :func:`round_to_atm` — the live oracle."""
    # Add half-step then floor-divide for deterministic half-up rounding
    # (banker's rounding doesn't apply because we're rounding to a grid step,
    # not to a fixed precision)
    return int((spot_price + step / 2) // step) * step


def strike_offset_from_atm(
    atm_strike: int,
    offset_steps: int,
    instrument: OptionsUnderlying,
) -> int:
    """Compute the strike at `offset_steps` grid steps from ATM.

    Args:
        atm_strike: ATM strike (must be on the grid).
        offset_steps: Number of steps (positive = higher strike, negative = lower).
        instrument: Underlying.

    Returns:
        Resolved strike. Caller responsible for staying within the
        21-strike chain (ATM ± 10) — this function doesn't bounds-check.

    Example:
        strike_offset_from_atm(24_000, 3, NIFTY) -> 24_150
        strike_offset_from_atm(50_000, -2, BANKNIFTY) -> 49_800

    DEC-OPTCOST-TAILS-RUST-001: flag-gated onto the ``fno_math``
    ``optcost_strike_offset_from_atm`` kernel; flag OFF is byte-identical.
    """
    step = STRIKE_STEP[instrument]
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.strike_offset(
            atm_strike,
            offset_steps,
            step,
            python=lambda: atm_strike + offset_steps * step,
        )
    return atm_strike + offset_steps * step


def grid_around_atm(
    atm_strike: int,
    instrument: OptionsUnderlying,
    half_width: int = 10,
) -> list[int]:
    """Return the 21-strike chain centered on ATM (ATM-10..ATM..ATM+10).

    Args:
        atm_strike: ATM strike (on grid).
        instrument: Underlying.
        half_width: Number of strikes each side of ATM (default 10 → 21 total).

    Returns:
        Sorted list of strikes from ATM - half_width × step to ATM + half_width × step.

    DEC-OPTCOST-TAILS-RUST-001: flag-gated onto the ``fno_math``
    ``optcost_grid_around_atm`` kernel; flag OFF is byte-identical.
    """
    step = STRIKE_STEP[instrument]
    if optcost_rust.optcost_rust_enabled():
        return optcost_rust.grid_around(
            atm_strike,
            step,
            half_width,
            python=lambda: [atm_strike + i * step for i in range(-half_width, half_width + 1)],
        )
    return [atm_strike + i * step for i in range(-half_width, half_width + 1)]
