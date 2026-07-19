"""Instrument-scaled Stage-3 SL/TP axes — median-daily-range grid derivation.

DEC-STAGE3-SLTP-INSTRUMENT-SCALE-001 (LANE W3 of the 2026-07-18 operator
approve-all ruling — item 7). The spec's locked Mode-A constants (SL ∈
[5, 200] paisa / TP ∈ [10, 500] paisa, ``TRACK1_BACKTEST_SPEC.md`` §Stage 3,
"constants, never data-derived") are CONSCIOUSLY SUPERSEDED-when-armed by
that ruling: on SENSEX the Mode-A ceiling is a 200-paisa (₹2) stop —
~0.002% of the index, an absurd risk grid — so the axes may now be
PARAMETERIZED PER INSTRUMENT, scaled by the instrument's own MEDIAN DAILY
RANGE (computed deterministically from the sweep's ALREADY-LOADED bars —
zero extra vendor I/O), while keeping the SAME 40 × 50 = 2,000 cell count
(:data:`~brutex.engine.stage3_grid.MODE_A_GRID_CELLS` shape invariant).
DEFAULT-OFF: absent config ⇒ the literal Mode-A constants, byte-identical
(the spec text stands for the default; the amendment note lives beside it).

The exact scaling law (pure integer arithmetic — deterministic, golden-pinned)
-------------------------------------------------------------------------------
Let ``r`` = the instrument's median daily range in paisa
(:func:`median_daily_range_paisa` — per-IST-session ``max(high) - min(low)``,
LOWER median over the sessions, exact ints end-to-end). Then for scale
fraction ``num/den`` (defaults: SL span = r/2, TP span = 5r/4):

* ``sl_tick = max(1, ceil(r · sl_num / (sl_den · 40)))``;
  the SL axis is ``[sl_tick, 40·sl_tick]`` on the ``sl_tick`` grid —
  EXACTLY 40 steps (the Mode-A shape ``lo == tick``, ``hi == steps·tick``).
* ``tp_tick = max(1, ceil(r · tp_num / (tp_den · 50)))``;
  the TP axis is ``[tp_tick, 50·tp_tick]`` — EXACTLY 50 steps.

``ceil`` is exact integer ``-(-a // b)``; NO floats anywhere, so identical
bars give identical axes on any platform (Golden Rule #4). The step counts
are structurally invariant (property-pinned over the whole ``r`` domain).

Run-identity law (Golden Rule #4)
---------------------------------
Every evaluated (SL, TP) is an int paisa pair that ALREADY enters
``canonical_trade_params`` (``sl_paisa``/``tp_paisa``) and the 9-field combo
hash — scaled axes therefore produce DISTINCT run_ids through the EXISTING
identity fields (sanctioned: greenfield, no stored results to alias).
:func:`instrument_axis_params_fragment` additionally provides the
default-OMITTED axis-provenance fragment (the ``price_source_params_fragment``
precedent): Mode-A axes ⇒ ``{}`` so every baseline run-identity stays
BYTE-IDENTICAL; scaled axes ⇒ the ``stage3_sl_axis``/``stage3_tp_axis``
int-triples. Merging the fragment into
:func:`~brutex.engine.sweep_run.canonical_trade_params` is DEFERRED to the
club fold (that file is triply contended — in-flight PR #1479 + lane W2 +
the B3-2 conversion carve; MIGRATION.md #1978 records the one-line merge),
which is provably identity-neutral at default because the default fragment
is ``{}`` (test-pinned).

Coarse per-instrument default sweep axis (default-OFF)
------------------------------------------------------
:data:`COARSE_SCALE_LADDER` is the ~5-point log-spaced scale ladder
(×¼ ×½ ×1 ×2 ×4); :func:`coarse_sltp_cells` derives the 5 (SL, TP) int
cells for an instrument (base SL = r/4 per cell scale, TP = 2·SL — the 2:1
reward:risk W3-default, disclosed in MIGRATION.md #1978). The nightly key
list consumes them via ``nightly_sweep_kwargs(..., sltp_scale_cells=…)``
(``brutex/nightly/sweep_binding.py``) behind the
:data:`NIGHTLY_SCALE_ENV` knob — default unset = OFF = the forwarded
``sl``/``tp`` args byte-identical. HONEST (Golden Rule #10): both of this
module's knob resolvers are fail-loud ONCE BOUND, but NO production code
reads either knob until the deferred club-fold binding lands — setting them
today is a clean silent no-op. Per-instrument staging follows the
DEC-NIFTY-PRICE-SOURCE-AXIS-001 staged-invocation precedent: one armed
invocation per instrument (a single ``cmd_sweep_all`` call has one sl/tp
list for all instruments), each ×5 cells — the compute disclosure.

Honest envelope (Golden Rule #10)
---------------------------------
Everything here is O(bars) once per launch (the median) + O(1) per
derivation — config-time work, never the per-mask hot path. The derivation
law deliberately lives in pure Python config code: the Stage-3 grid math it
feeds (``stage3_grid``) is owned by the LIVE B2-2 ``rust/brutex-refine``
conversion lane, so a parallel Rust mirror here would collide with that
lane's crate skeleton — the Rust mirror rides B2-2 (disclosed deviation from
the born-Rust default, recorded in MIGRATION.md #1978; the O(1)-per-instrument
config derivation is not a hot path).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS, GridAxis

if TYPE_CHECKING:
    from brutex.data.models import CandleBar

__all__ = [
    "COARSE_SCALE_LADDER",
    "INSTRUMENT_SCALE_ENV",
    "NIGHTLY_SCALE_ENV",
    "coarse_sltp_cells",
    "derive_scaled_axes",
    "instrument_axis_params_fragment",
    "is_mode_a_axes",
    "median_daily_range_paisa",
    "resolve_instrument_scale_env",
    "resolve_nightly_scale_env",
    "sltp_cells_csv",
]

#: Fail-loud arming knob for the per-instrument Stage-3 axis derivation.
#: Unset/blank/``0``/``false`` = OFF (the literal Mode-A constants);
#: ``1``/``true`` = ON; junk = a loud ``ValueError``.
INSTRUMENT_SCALE_ENV: Final[str] = "BRUTEX_STAGE3_INSTRUMENT_SCALE"

#: Fail-loud arming knob for the nightly coarse SL/TP scale axis (same
#: vocabulary law as :data:`INSTRUMENT_SCALE_ENV`).
NIGHTLY_SCALE_ENV: Final[str] = "BRUTEX_NIGHTLY_SLTP_SCALE_AXIS"

#: Default SL-axis span as a fraction of the median daily range (r/2).
_SL_SPAN_FRACTION: Final[tuple[int, int]] = (1, 2)

#: Default TP-axis span as a fraction of the median daily range (5r/4).
_TP_SPAN_FRACTION: Final[tuple[int, int]] = (5, 4)

#: The ~5-point log-spaced coarse scale ladder (×¼ ×½ ×1 ×2 ×4) — exact
#: integer fractions, base-2 log-spacing (item 7: "a COARSE per-instrument
#: SL/TP default sweep axis: ~5 log-spaced scale values").
COARSE_SCALE_LADDER: Final[tuple[tuple[int, int], ...]] = (
    (1, 4),
    (1, 2),
    (1, 1),
    (2, 1),
    (4, 1),
)

#: The coarse cells' base SL as a fraction of the median daily range (r/4 at
#: ladder scale ×1) — a W3-default constant, disclosed in MIGRATION.md #1978.
_COARSE_SL_BASE_FRACTION: Final[tuple[int, int]] = (1, 4)

#: Coarse cells' TP:SL reward:risk multiple (2:1 — W3-default, disclosed).
_COARSE_TP_PER_SL: Final[int] = 2

_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"1", "true"})
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"0", "false"})


def _ceil_div(numerator: int, denominator: int) -> int:
    """Exact integer ceiling division (no floats — Golden Rule #4)."""
    return -(-numerator // denominator)


def _resolve_flag(env_name: str, value: str | None) -> bool:
    """The ONE fail-loud ON/OFF vocabulary for this module's two knobs."""
    raw = os.environ.get(env_name) if value is None else value
    if raw is None or not raw.strip():
        return False
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"{env_name} must be one of 1/0/true/false (or unset = off), got {raw!r}")


def resolve_instrument_scale_env(value: str | None = None) -> bool:
    """Resolve :data:`INSTRUMENT_SCALE_ENV` — fail-loud once bound, default OFF.

    ``value=None`` (default) reads ``os.environ`` at call time; the sweep
    resolves ONCE per launch. Junk raises ``ValueError`` (never silent).
    Honest: no production code calls this resolver until the club-fold
    binding lands (module docstring) — arming the knob today is a no-op.
    """
    return _resolve_flag(INSTRUMENT_SCALE_ENV, value)


def resolve_nightly_scale_env(value: str | None = None) -> bool:
    """Resolve :data:`NIGHTLY_SCALE_ENV` — fail-loud once bound, default OFF.

    Honest: no production code calls this resolver until the club-fold
    binding lands (module docstring) — arming the knob today is a no-op.
    """
    return _resolve_flag(NIGHTLY_SCALE_ENV, value)


def median_daily_range_paisa(bars: Sequence[CandleBar]) -> int:
    """The instrument's MEDIAN daily high-low range in int paisa — exact.

    Groups *bars* by their IST session date (the engine's session axis),
    takes each session's ``max(high) - min(low)`` on the 2-dp price grid
    (Decimal rupees × 100 — exact ints, DEC-SPOT-PRICE-2DP-001), and returns
    the LOWER median of the sorted session ranges (a deterministic int for
    every parity of the session count — never a float midpoint). O(bars)
    once per launch; zero extra vendor I/O (the sweep's own loaded bars).

    Raises
    ------
    ValueError
        If *bars* is empty, or every session's range is zero (a degenerate
        flat fixture cannot scale a grid — refused loudly, never a zero-tick
        axis).
    """
    from brutex.calendars.nse import IST

    if not bars:
        raise ValueError("median_daily_range_paisa: bars must be non-empty")
    highs: dict[object, int] = {}
    lows: dict[object, int] = {}
    for bar in bars:
        day = bar.timestamp.astimezone(IST).date()
        high_paisa = int(bar.high * 100)
        low_paisa = int(bar.low * 100)
        if day not in highs:
            highs[day] = high_paisa
            lows[day] = low_paisa
        else:
            highs[day] = max(highs[day], high_paisa)
            lows[day] = min(lows[day], low_paisa)
    ranges = sorted(highs[day] - lows[day] for day in highs)
    median = ranges[(len(ranges) - 1) // 2]  # LOWER median — deterministic int.
    if median < 1:
        raise ValueError(
            "median_daily_range_paisa: the median session range is 0 paisa — "
            "a flat/degenerate series cannot derive a scaled grid"
        )
    return median


def derive_scaled_axes(
    median_range_paisa: int,
    *,
    sl_span: tuple[int, int] = _SL_SPAN_FRACTION,
    tp_span: tuple[int, int] = _TP_SPAN_FRACTION,
) -> tuple[GridAxis, GridAxis]:
    """Derive the instrument-scaled (SL, TP) axes — SAME 40 × 50 grid shape.

    The exact law of the module docstring: each axis keeps the Mode-A step
    count (40 / 50) with its tick derived from the median daily range via
    exact integer ceiling division. Pure O(1) per call; every output axis is
    a self-validating :class:`~brutex.engine.stage3_grid.GridAxis` (the
    bijection invariants hold by construction).

    Parameters
    ----------
    median_range_paisa
        The instrument's median daily range (:func:`median_daily_range_paisa`),
        ``>= 1``.
    sl_span, tp_span
        The axis spans as exact ``(num, den)`` fractions of the range
        (defaults r/2 and 5r/4). Both parts must be ``>= 1``.

    Raises
    ------
    ValueError
        On a non-positive range or fraction part.
    """
    if median_range_paisa < 1:
        raise ValueError(f"median_range_paisa must be >= 1, got {median_range_paisa}")
    for name, (num, den) in (("sl_span", sl_span), ("tp_span", tp_span)):
        if num < 1 or den < 1:
            raise ValueError(f"{name} parts must be >= 1, got ({num}, {den})")
    sl_steps = MODE_A_SL_AXIS.steps  # 40 — the locked Mode-A shape.
    tp_steps = MODE_A_TP_AXIS.steps  # 50
    sl_tick = max(1, _ceil_div(median_range_paisa * sl_span[0], sl_span[1] * sl_steps))
    tp_tick = max(1, _ceil_div(median_range_paisa * tp_span[0], tp_span[1] * tp_steps))
    sl_axis = GridAxis(
        name="sl_paisa", lo_paisa=sl_tick, hi_paisa=sl_steps * sl_tick, tick_paisa=sl_tick
    )
    tp_axis = GridAxis(
        name="tp_paisa", lo_paisa=tp_tick, hi_paisa=tp_steps * tp_tick, tick_paisa=tp_tick
    )
    # Shape invariant — STRUCTURAL: an axis [t, S·t] on tick t has exactly
    # (S·t - t)/t + 1 == S steps for every t >= 1, so the cell count is the
    # Mode-A 40 × 50 for EVERY input (property-pinned across the r domain in
    # tests/property/test_stage3_scale_steps.py — no runtime re-check needed).
    return sl_axis, tp_axis


def is_mode_a_axes(sl_axis: GridAxis, tp_axis: GridAxis) -> bool:
    """Whether the pair IS the literal Mode-A grid (the default-omit predicate).

    The ONE predicate (CLAUDE.md §11 COMMON) the params fragment consults —
    a pair equal to :data:`~brutex.engine.stage3_grid.MODE_A_SL_AXIS` /
    :data:`~brutex.engine.stage3_grid.MODE_A_TP_AXIS` field-for-field is the
    baseline and serialises ABSENT.
    """
    return sl_axis == MODE_A_SL_AXIS and tp_axis == MODE_A_TP_AXIS


def instrument_axis_params_fragment(sl_axis: GridAxis, tp_axis: GridAxis) -> dict[str, list[int]]:
    """The default-OMITTED axis-provenance params fragment (ints only).

    The ``price_source_params_fragment`` precedent
    (DEC-NIFTY-PRICE-SOURCE-AXIS-001 Override 1): Mode-A axes ⇒ ``{}`` so a
    baseline run-identity is BYTE-IDENTICAL to the pre-axis form; scaled
    axes ⇒ ``{"stage3_sl_axis": [lo, hi, tick], "stage3_tp_axis": [...]}`` —
    JSON-canonical int triples, deterministic. The canonicaliser merge line
    is the club-fold step (module docstring — ``sweep_run.py`` contention).
    """
    if is_mode_a_axes(sl_axis, tp_axis):
        return {}
    return {
        "stage3_sl_axis": [sl_axis.lo_paisa, sl_axis.hi_paisa, sl_axis.tick_paisa],
        "stage3_tp_axis": [tp_axis.lo_paisa, tp_axis.hi_paisa, tp_axis.tick_paisa],
    }


def coarse_sltp_cells(median_range_paisa: int) -> tuple[tuple[int, int], ...]:
    """The ~5 log-spaced coarse (SL, TP) cells for one instrument — exact ints.

    Per ladder scale ``num/den``: ``sl = max(1, ceil(r·num / (den·4)))``
    (base SL = quarter-range at ×1) and ``tp = 2·sl`` (the 2:1 W3-default).
    Duplicates (a tiny ``r`` collapsing neighbouring ladder rungs onto the
    same cell) are dropped order-preserving — the axis is honestly "~5",
    never a padded fake.

    Raises
    ------
    ValueError
        On a non-positive *median_range_paisa*.
    """
    if median_range_paisa < 1:
        raise ValueError(f"median_range_paisa must be >= 1, got {median_range_paisa}")
    base_num, base_den = _COARSE_SL_BASE_FRACTION
    cells: list[tuple[int, int]] = []
    for num, den in COARSE_SCALE_LADDER:
        sl = max(1, _ceil_div(median_range_paisa * base_num * num, base_den * den))
        cell = (sl, _COARSE_TP_PER_SL * sl)
        if cell not in cells:
            cells.append(cell)
    return tuple(cells)


def sltp_cells_csv(cells: Sequence[tuple[int, int]]) -> tuple[str, str]:
    """Render coarse cells as the ``--sl`` / ``--tp`` CSV token pair.

    The nightly conduit (``nightly_sweep_kwargs(..., sltp_scale_cells=…)``):
    the returned strings are exactly the ``_parse_paisa_list`` vocabulary the
    sweep CLI already parses. Raises ``ValueError`` on an empty sequence or a
    non-positive paisa value (a junk plan must fail loudly at build time).
    """
    if not cells:
        raise ValueError("sltp_cells_csv: cells must be non-empty")
    for sl, tp in cells:
        if sl < 1 or tp < 1:
            raise ValueError(f"sltp_cells_csv: cells must be positive paisa, got ({sl}, {tp})")
    return (
        ",".join(str(sl) for sl, _ in cells),
        ",".join(str(tp) for _, tp in cells),
    )
