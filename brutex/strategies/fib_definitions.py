"""The 3 Fibonacci strategies as ONE-LINE DATA declarations (strategy = data).

PR-4b sub-PR 3 (DEC-FIB-STRATEGY-REGISTRY-001, ADR-0012 D2/D4/D5). The operator
decree is **strategy-as-DATA**: adding a strategy is ONE ``register(...)`` line, no
rewrite; symbol-agnostic; readable; no illusion (every field is USED — by the
anchor resolution, the spec/decoder bridge, or the sweep candidate scope).

## What a "strategy" is here (ADR-0012 D2/D4)

ADR-0012 D2 defines exactly **THREE** Fib strategies distinguished ONLY by the
Fibonacci ANCHOR — (a) prev-day OHLC, (b) rolling-weekly (trailing-5-session H/L),
(c) weekly-static (last completed ISO week's H/L). Everything else is identical so
the three are directly comparable. Per **D4** the anchor is a SWEPT STRATEGY
PARAMETER, not a set of vocabulary bits (tripling the 11-bit Fib group for zero
analytical gain is the leanness anti-pattern D4/D66 reject). **Direction (Long /
Short) is likewise a SWEPT DIMENSION INSIDE each strategy — NOT a separate
strategy.** The brute-force sweep tries BOTH directions on each anchor, exactly as
it sweeps the condition mask + timeframe. So a Fib strategy is a *declaration* of
(anchor, symbols, the swept ``directions`` set, timeframes, exit, candidate
condition scope), and one declaration EXPANDS at sweep time into concrete Rust
``StrategySpec``\\ s over ``{directions} × {condition masks} × {timeframes}`` — the
brute-force MASK + direction within that grid is what PR-5's sweep searches.

## Wrap, NOT extend the frozen Rust `StrategySpec`

The shared Rust ``trading_core.StrategySpec`` (DEC-STRATEGYSPEC-CORE-001, #1027) is
FROZEN and carries only ``(condition_mask, direction, proximity, prior_n,
symbol_id)`` — ``fib_anchor`` / ``timeframe`` / ``exit`` were DEFERRED from
``spec.rs`` (they are NOT bits and do not change the per-bar ``evaluate_atoms``
arithmetic). The Rust spec keeps ONE ``direction`` field per CONCRETE spec; only
the Python DEFINITION carries the SWEPT ``directions`` SET. Rather than touch the
frozen crate (which would risk the byte-identical 400-case golden), this module
WRAPS it: a :class:`StrategyDefinition` adds the anchor + the swept directions +
timeframes + exit + symbol context, and :meth:`StrategyDefinition.spec_args`
projects ONE concrete ``(condition_mask, direction)`` from the strategy's swept grid
back onto the Rust spec's ``(condition_mask_hex, direction)`` ABI — so the SAME
frozen evaluator/decoder consume it unchanged. The wrap adds context the kernel
never needed; it never re-implements the kernel.

## Symbol-agnostic

``symbols`` is a free tuple of opaque identifiers (config, not a hardcoded index).
The SAME declaration runs for a different symbol set by changing that tuple —
:func:`for_symbols` returns a re-symboled copy, no rewrite. The brute-force ENGINE
scope (NIFTY/BANKNIFTY/SENSEX, CLAUDE.md §3) is unchanged; this module is symbol
machinery, it admits no symbol to the engine.

## Runnable, not a stub (anchor resolution)

:func:`resolve_fib_levels` proves a declaration is RUNNABLE: given a definition + a
date + the daily candles it resolves the family's anchor via the REAL primitive
(``prev_day_ohlc.lookup_prev_day_ohlc`` / ``weekly_anchors.rolling_weekly_anchor`` /
``weekly_anchors.weekly_static_anchor``) and feeds the ``(high, low)`` into the SAME
``fib_levels.compute_fib_levels``. A cold-start (rolling-weekly < 5 prior sessions,
weekly-static no completed prior week, prev-day no prior daily candle) returns
``None`` (combo INELIGIBLE per ADR-0012 §4.5 #6), never a crash or a fabricated
level (Golden Rule #1).

## Honest envelope (Golden Rule #10)

This is the strategy DECLARATION + anchor-resolution + decoder bridge layer. The
brute-force mask SWEEP over each family's candidate scope, the frictionless
trade-sim, and the SL/TP grid live in PR-5 (sweep) / PR-4 sim — the
:class:`ExitConfig` here is the declared exit POLICY (15:20 forced + optional
SL/TP bps), consumed by that later sim, not executed here. ``resolve_fib_levels``
produces the per-day Fib LEVELS the sweep evaluates conditions against; it does not
itself fire trades.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, time
from enum import Enum
from typing import Final

from brutex.dashboard.combo_decoder import ComboExplanation, decode_combo
from brutex.indicators.fib_levels import FibLevels, compute_fib_levels
from brutex.indicators.prev_day_ohlc import DailyCandle, lookup_prev_day_ohlc
from brutex.indicators.weekly_anchors import (
    FibAnchor,
    rolling_weekly_anchor,
    weekly_static_anchor,
)
from brutex.strategies.errors import StrategyRegistrationError

# ``FibAnchorKind`` lives in the pure leaf ``strategies.types`` (already imported
# by ``brutex.engine.*``) so the engine can thread it as a grid axis WITHOUT
# pulling this module's ``dashboard.combo_decoder`` import into the engine's
# runtime import graph (DEC-NIFTY-FIB-AXIS-001). It is re-exported here (COMMON —
# one enum, CLAUDE.md §11) so every existing ``fib_definitions.FibAnchorKind``
# reference is unchanged.
from brutex.strategies.types import FibAnchorKind
from brutex.vocabulary.bits import IMPLEMENTED_MASK, NAME_TO_BIT, RETIRED_MASK

#: NSE/BSE intraday forced-exit clock (ADR-0012 D5; CLAUDE.md §7 "15:20 IST").
FORCED_EXIT_IST: Final[time] = time(15, 20)

#: The trailing-session window for the rolling-weekly anchor (one trading week).
ROLLING_WEEKLY_SESSIONS: Final[int] = 5


class Direction(Enum):
    """Trade direction — a strategy parameter, not a bit (ADR-0012 D4).

    The string values map 1:1 to the decoder's accepted tokens
    (``decode_combo(direction=...)`` → "Buy"/"Sell") and to the Rust
    ``StrategySpec`` discriminant via :meth:`as_u8` (``Long = 0``, ``Short = 1``,
    matching ``trading_core`` ``spec.rs``).
    """

    LONG = "long"
    SHORT = "short"

    def as_u8(self) -> int:
        """Return the Rust ``StrategySpec`` direction discriminant (Long=0/Short=1)."""
        return 0 if self is Direction.LONG else 1


@dataclass(frozen=True, slots=True)
class ExitConfig:
    """The declared intraday exit POLICY (ADR-0012 D5).

    Attributes
    ----------
    forced_exit_ist
        The hard forced-exit clock — 15:20 IST, no overrides ever (D5). Nothing
        is held overnight.
    stop_loss_bps
        Optional stop-loss distance in basis points, or ``None`` for Mode B
        (forced-exit-only). Consumed by the PR-4/PR-5 trade-sim's SL/TP grid; not
        executed in this declaration layer.
    take_profit_bps
        Optional take-profit distance in basis points, or ``None``.
    """

    forced_exit_ist: time = FORCED_EXIT_IST
    stop_loss_bps: int | None = None
    take_profit_bps: int | None = None

    def __post_init__(self) -> None:
        if self.stop_loss_bps is not None and self.stop_loss_bps <= 0:
            raise ValueError(f"stop_loss_bps must be positive when set, got {self.stop_loss_bps}")
        if self.take_profit_bps is not None and self.take_profit_bps <= 0:
            raise ValueError(
                f"take_profit_bps must be positive when set, got {self.take_profit_bps}"
            )

    @property
    def has_sl_tp(self) -> bool:
        """``True`` iff this is a Mode-A (SL and/or TP) exit, ``False`` for Mode B."""
        return self.stop_loss_bps is not None or self.take_profit_bps is not None


@dataclass(frozen=True, slots=True)
class ConcreteSpec:
    """One concrete (strategy, direction, mask) point in the brute-force grid.

    Produced by :meth:`StrategyDefinition.expand` — a single swept-out combination
    carrying the FROZEN Rust ``StrategySpec`` ABI pair (``condition_mask_hex`` +
    ``direction_u8``) that ``trading_core.evaluate_spec`` consumes. A definition
    expands into one of these PER swept direction (Long AND Short by default).

    Attributes
    ----------
    strategy_name
        The originating :attr:`StrategyDefinition.name`.
    direction
        The concrete swept :class:`Direction` for this point.
    condition_mask_hex
        The 32-nibble hex condition mask (the Rust spec ABI form).
    direction_u8
        The Rust ``StrategySpec`` direction discriminant (Long=0/Short=1).
    """

    strategy_name: str
    direction: Direction
    condition_mask_hex: str
    direction_u8: int


#: The directions the brute-force sweep explores by default on EVERY strategy —
#: BOTH Long and Short. Direction is a SWEPT DIMENSION inside a strategy (like the
#: condition mask + timeframe), not a separate strategy (ADR-0012 D2/D4).
_BOTH_DIRECTIONS: Final[tuple[Direction, ...]] = (Direction.LONG, Direction.SHORT)


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """ONE Fib strategy as DATA — a declaration, wrapping the frozen `StrategySpec`.

    There are exactly THREE Fib strategies (one per ANCHOR); adding one is ONE
    :func:`register` line. Direction is NOT pinned on a strategy — it is a SWEPT
    DIMENSION (:attr:`directions`) the brute-force sweep explores INSIDE each
    strategy, exactly like the condition mask + timeframe (ADR-0012 D2/D4). So one
    declaration EXPANDS at sweep time into concrete Rust ``StrategySpec``\\ s over
    ``{directions} × {condition masks} × {timeframes}`` — each concrete spec keeps
    its single ``direction`` field; only the DEFINITION carries the swept set.

    Every field is USED: ``anchor`` selects the resolution primitive,
    ``directions`` is the swept direction set fed (one at a time) to the
    spec/decoder, ``symbols`` is the symbol-agnostic target set, ``timeframes`` the
    TFs the sweep evaluates, ``exit`` the declared exit policy, and
    ``condition_scope`` the candidate vocabulary the brute-force mask searches
    within.

    Attributes
    ----------
    name
        Short globally-unique strategy id (e.g. ``"fib_prev_day"``).
    anchor
        The :class:`FibAnchorKind` distinguishing this strategy (the ONLY axis of
        difference across the three per ADR-0012 D2).
    symbols
        Opaque, symbol-agnostic identifiers this declaration targets. A free
        tuple (config), NOT a hardcoded index; :func:`for_symbols` re-symbols a
        copy without a rewrite.
    directions
        The directions the sweep explores for this strategy — BOTH (Long, Short)
        by default. Direction is a swept dimension, not a fixed strategy property
        (D4). Order-preserving + de-duplicated; never empty.
    timeframes
        The timeframe labels the sweep evaluates (e.g. ``("1minute",)`` or all 21
        per D5). Default is the 1-minute base clock.
    exit
        The declared :class:`ExitConfig` (15:20 forced + optional SL/TP).
    condition_scope
        The candidate vocabulary condition NAMES (``bits.py`` ``NAME_TO_BIT`` keys)
        the brute-force sweep searches masks WITHIN. ``frozenset()`` means "any
        implemented condition" — the full vocabulary is the candidate set. Every
        named condition must itself be IMPLEMENTED: a RETIRED tombstone (the
        VWAP bits 52/53) or a declared-only LAYOUT bit (the SuperTrend bits
        64/65) is refused at construction, keeping :attr:`candidate_mask` a
        true subset of ``IMPLEMENTED_MASK``.
    description
        Optional human-readable one-liner.
    """

    name: str
    anchor: FibAnchorKind
    symbols: tuple[str, ...]
    directions: tuple[Direction, ...] = _BOTH_DIRECTIONS
    timeframes: tuple[str, ...] = ("1minute",)
    exit: ExitConfig = field(default_factory=ExitConfig)
    condition_scope: frozenset[str] = frozenset()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("StrategyDefinition.name must be non-empty")
        if not self.symbols:
            raise ValueError(
                f"StrategyDefinition.symbols must list at least one symbol (strategy {self.name!r})"
            )
        if not self.directions:
            raise ValueError(
                f"StrategyDefinition.directions must list at least one direction "
                f"(strategy {self.name!r})"
            )
        # Order-preserving de-dup so a sweep never explores the same direction
        # twice (and the swept set is canonical for run-identity).
        deduped = tuple(dict.fromkeys(self.directions))
        if deduped != self.directions:
            object.__setattr__(self, "directions", deduped)
        if not self.timeframes:
            raise ValueError(
                f"StrategyDefinition.timeframes must list at least one timeframe "
                f"(strategy {self.name!r})"
            )
        unknown = self.condition_scope - set(NAME_TO_BIT)
        if unknown:
            raise ValueError(
                f"condition_scope has unknown vocabulary condition(s) {sorted(unknown)!r} "
                f"(strategy {self.name!r}); valid names are the bits.py NAME_TO_BIT keys"
            )
        # A KNOWN name whose bit is outside IMPLEMENTED_MASK would break the
        # candidate_mask "always a subset of IMPLEMENTED_MASK" invariant and
        # sweep a bit the evaluator emits 0 for forever — refuse loudly, naming
        # each entry + its status (the parse_condition_ids wording family).
        dead = sorted(
            name for name in self.condition_scope if not (IMPLEMENTED_MASK >> NAME_TO_BIT[name]) & 1
        )
        if dead:
            statuses = "; ".join(
                f"{name!r} (bit {NAME_TO_BIT[name]}) is "
                + (
                    "RETIRED — a tombstone whose math was deleted"
                    if (RETIRED_MASK >> NAME_TO_BIT[name]) & 1
                    else "declared but NOT YET implemented (LAYOUT-ONLY)"
                )
                for name in dead
            )
            raise ValueError(
                f"condition_scope names unimplemented vocabulary condition(s): {statuses} "
                f"(strategy {self.name!r}). The evaluator emits 0 for such bits, so "
                "sweeping them is dead compute; drop them from the scope — only "
                "IMPLEMENTED_MASK bits are candidates"
            )

    @property
    def candidate_mask(self) -> int:
        """The u128 superset of bits the sweep MAY require for this family.

        For an explicit :attr:`condition_scope` it is the OR of those bits; for the
        empty scope it is the full :data:`IMPLEMENTED_MASK` (every implemented
        condition is a candidate). Always a subset of ``IMPLEMENTED_MASK`` (a spec
        can never require a reserved/un-evaluated bit — the same invariant the Rust
        ``StrategySpec::new`` enforces).
        """
        if not self.condition_scope:
            return IMPLEMENTED_MASK
        mask = 0
        for name in self.condition_scope:
            mask |= 1 << NAME_TO_BIT[name]
        return mask

    def spec_args(self, condition_mask: int, direction: Direction) -> tuple[str, int]:
        """Project one concrete ``(condition_mask, direction)`` onto the Rust spec ABI.

        Direction is a SWEPT DIMENSION, so the concrete direction the sweep picked
        is a PARAMETER here (not read off the definition). Returns
        ``(condition_mask_hex, direction_u8)`` — the exact pair
        ``trading_core.evaluate_spec`` (and this module's decoder bridge) consume,
        so a definition's swept mask + direction flow into the FROZEN
        evaluator/decoder unchanged. The mask must be a subset of this strategy's
        :attr:`candidate_mask` (so the sweep cannot require a bit outside the
        declared scope); the direction must be one this strategy actually sweeps.

        Raises
        ------
        ValueError
            If ``condition_mask`` requires a bit outside :attr:`candidate_mask`,
            or ``direction`` is not in this strategy's :attr:`directions`.
        """
        out_of_scope = condition_mask & ~self.candidate_mask
        if out_of_scope:
            raise ValueError(
                f"condition_mask {condition_mask:#x} requires bit(s) {out_of_scope:#x} "
                f"outside strategy {self.name!r} candidate scope {self.candidate_mask:#x}"
            )
        if direction not in self.directions:
            raise ValueError(
                f"direction {direction.value!r} is not swept by strategy {self.name!r} "
                f"(swept directions: {[d.value for d in self.directions]})"
            )
        return f"{condition_mask:032x}", direction.as_u8()

    def expand(self, condition_mask: int) -> tuple[ConcreteSpec, ...]:
        """Expand *condition_mask* across EVERY swept direction → concrete specs.

        The direction-sweep proof: one :class:`StrategyDefinition` yields one
        concrete spec PER swept direction (Long AND Short by default) for the given
        mask — each a frozen :class:`ConcreteSpec` carrying the Rust spec ABI pair.
        The actual brute-force sweep additionally crosses ``{condition masks} ×
        {timeframes}``; this helper is the per-mask direction slice of that grid.

        Raises
        ------
        ValueError
            If ``condition_mask`` requires a bit outside :attr:`candidate_mask`.
        """
        specs: list[ConcreteSpec] = []
        for direction in self.directions:
            mask_hex, direction_u8 = self.spec_args(condition_mask, direction)
            specs.append(
                ConcreteSpec(
                    strategy_name=self.name,
                    direction=direction,
                    condition_mask_hex=mask_hex,
                    direction_u8=direction_u8,
                )
            )
        return tuple(specs)


def for_symbols(definition: StrategyDefinition, symbols: Iterable[str]) -> StrategyDefinition:
    """Return a copy of *definition* targeting a DIFFERENT symbol set.

    The symbol-agnostic proof: the SAME declaration runs for any symbols by
    swapping the tuple — no rewrite, no per-symbol subclass. ``name`` is suffixed
    with the new symbols so the copy keeps a unique registry id.

    Raises
    ------
    ValueError
        If *symbols* is empty (an unusable definition).
    """
    syms = tuple(symbols)
    if not syms:
        raise ValueError(f"for_symbols requires at least one symbol (strategy {definition.name!r})")
    return replace(definition, name=f"{definition.name}@{'+'.join(syms)}", symbols=syms)


class FibStrategyRegistry:
    """In-memory registry of :class:`StrategyDefinition`s, by ``name``.

    Independent per instance (use :data:`fib_registry` for the module-wide scope,
    a fresh instance inside tests for isolation), mirroring
    :class:`brutex.strategies.registry.StrategyRegistry`.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, StrategyDefinition] = {}

    def register(self, definition: StrategyDefinition) -> StrategyDefinition:
        """Add *definition*. ONE call = one strategy added (the operator's core ask).

        Returns the same definition so it can be assigned inline.

        Raises
        ------
        StrategyRegistrationError
            If another definition already registered the same ``name``.
        """
        if definition.name in self._definitions:
            raise StrategyRegistrationError(
                f"strategy name {definition.name!r} is already registered; cannot re-register"
            )
        self._definitions[definition.name] = definition
        return definition

    def get(self, name: str) -> StrategyDefinition:
        """Return the definition registered under *name*.

        Raises
        ------
        StrategyRegistrationError
            If no definition is registered under *name*.
        """
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise StrategyRegistrationError(
                f"no strategy registered under name {name!r}; "
                f"known names: {sorted(self._definitions)}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Every registered ``name`` in insertion order."""
        return tuple(self._definitions)

    def all(self) -> tuple[StrategyDefinition, ...]:
        """Every registered definition in insertion order."""
        return tuple(self._definitions.values())

    def clear(self) -> None:
        """Drop every registration (used by tests to reset between cases)."""
        self._definitions.clear()


#: Module-wide default Fib-strategy registry.
fib_registry: Final[FibStrategyRegistry] = FibStrategyRegistry()


def register(definition: StrategyDefinition) -> StrategyDefinition:
    """Register *definition* in the module-wide :data:`fib_registry` (one-line add)."""
    return fib_registry.register(definition)


# ─────────────────────────────────────────────────────────────────────────────
# The 3 Fib strategies — ONE data declaration each, one per ANCHOR. Direction is a
# SWEPT DIMENSION (BOTH Long+Short by default), NOT a separate strategy.
#
# Symbol-agnostic: ``NSE-NIFTY`` is the ADR-0012 D1 reference symbol, supplied
# here as plain config — re-symbol any of these via ``for_symbols(...)`` with no
# rewrite. Adding a 4th anchor-strategy is ONE more ``register(StrategyDefinition(...))``.
# ─────────────────────────────────────────────────────────────────────────────
_REFERENCE_SYMBOLS: Final[tuple[str, ...]] = ("NSE-NIFTY",)


def _fib_definition(name: str, anchor: FibAnchorKind, description: str) -> StrategyDefinition:
    """Build one Fib strategy (everything but the anchor is identical).

    Direction is NOT passed — it defaults to the BOTH-directions swept set
    (:data:`_BOTH_DIRECTIONS`), so the sweep explores Long AND Short inside the one
    strategy.
    """
    return StrategyDefinition(
        name=name,
        anchor=anchor,
        symbols=_REFERENCE_SYMBOLS,
        timeframes=("1minute",),
        exit=ExitConfig(),
        description=description,
    )


PREV_DAY = register(
    _fib_definition(
        "fib_prev_day",
        FibAnchorKind.PREV_DAY,
        "Prev-day-anchored Fib levels (both directions swept); 15:20 forced exit.",
    )
)
ROLLING_WEEKLY = register(
    _fib_definition(
        "fib_rolling_weekly",
        FibAnchorKind.ROLLING_WEEKLY,
        "Trailing-5-session Fib levels (both directions swept); 15:20 forced exit.",
    )
)
WEEKLY_STATIC = register(
    _fib_definition(
        "fib_weekly_static",
        FibAnchorKind.WEEKLY_STATIC,
        "Last-completed-week Fib levels (both directions swept); 15:20 forced exit.",
    )
)


def resolve_fib_anchor_levels(
    anchor: FibAnchorKind,
    today: date,
    daily_candles_by_date: Mapping[date, DailyCandle],
) -> FibLevels | None:
    """Resolve *anchor* → Fib levels for *today* (the ONE anchor dispatcher).

    The COMMON single source of the anchor → ``(high, low)`` → Fib dispatch
    (CLAUDE.md §11): dispatches on the :class:`FibAnchorKind` directly to the REAL
    anchor primitive (``lookup_prev_day_ohlc`` / ``rolling_weekly_anchor`` /
    ``weekly_static_anchor``) then feeds the resolved ``(high, low)`` into the SAME
    :func:`compute_fib_levels`. Consumed by BOTH :func:`resolve_fib_levels` (the
    strategy-definition entry point) AND the brute-force engine's Stage-1A builder
    (:mod:`brutex.engine.stage1a`, DEC-NIFTY-FIB-AXIS-001) — so the sweep evaluates
    the SAME anchor levels the strategy declaration would, never a re-implementation.

    This takes the enum directly (not a whole :class:`StrategyDefinition`) so the
    engine can dispatch on the swept ``fib_anchor`` axis without constructing a
    definition per bar.

    Returns
    -------
    FibLevels | None
        The 21 Fib levels for *today*, or ``None`` on a cold-start (no prior
        daily candle for prev-day; fewer than 5 prior sessions for rolling-weekly;
        no completed prior ISO week for weekly-static — combo INELIGIBLE per
        ADR-0012 §4.5 #6, never a crash or a fabricated level).

    Raises
    ------
    ValueError
        If *today* is not a trading day (propagated from the primitive) or the
        anchor produces a corrupt ``high < low`` range.
    """
    daily = dict(daily_candles_by_date)
    if anchor is FibAnchorKind.PREV_DAY:
        prev = lookup_prev_day_ohlc(today, daily)
        if prev is None:
            return None
        return compute_fib_levels(today, prev.pdh_paisa, prev.pdl_paisa)

    resolved: FibAnchor | None
    if anchor is FibAnchorKind.ROLLING_WEEKLY:
        resolved = rolling_weekly_anchor(today, daily, sessions=ROLLING_WEEKLY_SESSIONS)
    else:  # FibAnchorKind.WEEKLY_STATIC — the enum is exhaustive (3 members).
        resolved = weekly_static_anchor(today, daily)
    if resolved is None:
        return None
    return compute_fib_levels(today, resolved.high_paisa, resolved.low_paisa)


def resolve_fib_levels(
    definition: StrategyDefinition,
    today: date,
    daily_candles_by_date: Mapping[date, DailyCandle],
) -> FibLevels | None:
    """Resolve *definition*'s anchor → Fib levels for *today* (runnable, not a stub).

    Thin wrapper over :func:`resolve_fib_anchor_levels` that reads the anchor off
    the strategy :attr:`StrategyDefinition.anchor`. Direction does NOT affect the
    anchor — the Fib LEVELS are direction-agnostic (direction is a swept dimension,
    ADR-0012 D2/D4), so one strategy's Long and Short sweeps share one set of levels
    per day.

    Returns
    -------
    FibLevels | None
        The 21 Fib levels for *today*, or ``None`` on a cold-start (delegated to
        :func:`resolve_fib_anchor_levels`).

    Raises
    ------
    ValueError
        If *today* is not a trading day (propagated from the primitive) or the
        anchor produces a corrupt ``high < low`` range.
    """
    return resolve_fib_anchor_levels(definition.anchor, today, daily_candles_by_date)


def render_definition(
    definition: StrategyDefinition,
    condition_mask: int,
    direction: Direction,
    *,
    timeframe: str | None = None,
) -> ComboExplanation:
    """Render *definition* + a swept ``(condition_mask, direction)`` into sentences.

    The decoder bridge: projects the mask + the CHOSEN swept direction onto the
    Rust spec ABI (:meth:`StrategyDefinition.spec_args`, which validates scope AND
    that the direction is swept) then feeds ``(direction, timeframe)`` into the
    FROZEN :func:`brutex.dashboard.combo_decoder.decode_combo`, so a Fib strategy
    reads exactly like any other winning combo (no bespoke decoder). Direction is a
    PARAMETER (a swept dimension), not read off the definition.

    Parameters
    ----------
    definition
        The strategy declaration.
    condition_mask
        A concrete u128 mask within the strategy's :attr:`candidate_mask`.
    direction
        The concrete swept :class:`Direction` to render (must be in
        :attr:`StrategyDefinition.directions`).
    timeframe
        Override label for the sentence; defaults to the definition's first
        declared timeframe.

    Returns
    -------
    ComboExplanation
        The dual-layer (Simple + Pro) explanation.
    """
    mask_hex, _direction_u8 = definition.spec_args(condition_mask, direction)
    tf = timeframe if timeframe is not None else definition.timeframes[0]
    return decode_combo(int(mask_hex, 16), direction.value, tf)


__all__ = [
    "FORCED_EXIT_IST",
    "PREV_DAY",
    "ROLLING_WEEKLY",
    "ROLLING_WEEKLY_SESSIONS",
    "WEEKLY_STATIC",
    "ConcreteSpec",
    "Direction",
    "ExitConfig",
    "FibAnchorKind",
    "FibStrategyRegistry",
    "StrategyDefinition",
    "fib_registry",
    "for_symbols",
    "register",
    "render_definition",
    "resolve_fib_anchor_levels",
    "resolve_fib_levels",
]
