"""Core dataclasses + enums for the BruteX strategy engine.

Intentionally pure — no ``polars`` / ``pyarrow`` imports — so this
module can participate in the unit-test suite on a fresh checkout
without any Rust toolchain present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any


class Side(StrEnum):
    """Signal direction.

    ``HOLD`` is explicitly modeled (not represented as "no signal")
    because strategies in the vectorised mode need a dense side
    array; a "no signal" is expressed by not emitting a
    :class:`Signal` at all.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class FibAnchorKind(Enum):
    """The Fibonacci anchor distinguishing the three Fib strategy families (ADR-0012 D2).

    The anchor is a SWEPT STRATEGY PARAMETER (D4), never a vocabulary bit — the
    three are mutually exclusive per strategy, so modelling them as bits would
    triple the 11-bit Fib group for zero analytical gain (the D4/D66 leanness
    anti-pattern). It lives HERE (the pure leaf ``strategies.types`` module,
    already imported by ``brutex.engine.*``) rather than in
    ``strategies.fib_definitions`` so the brute-force engine can thread it as a
    grid axis without pulling ``fib_definitions``' ``dashboard.combo_decoder``
    import into the engine's runtime graph (the strict per-module
    ``--cov=brutex.engine`` gate keeps that wheel out). ``fib_definitions``
    re-exports this same enum (COMMON — one source of truth; CLAUDE.md §11).
    """

    #: Prev-day High/Low range (``prev_day_ohlc.lookup_prev_day_ohlc``).
    PREV_DAY = "prev_day"
    #: Trailing 5-session High/Low range (``weekly_anchors.rolling_weekly_anchor``).
    ROLLING_WEEKLY = "rolling_weekly"
    #: Last completed ISO week's High/Low (``weekly_anchors.weekly_static_anchor``).
    WEEKLY_STATIC = "weekly_static"


class PriceSource(Enum):
    """The per-bar price scalar feeding VWAP / EMA (DEC-NIFTY-PRICE-SOURCE-AXIS-001).

    A SWEPT STRATEGY PARAMETER (like :class:`FibAnchorKind`), never a vocabulary
    bit: the Stage-1A ``bar_bits`` for the VWAP-position bits (52-53) and the
    EMA-vs-close bits (0-5) depend on WHICH price scalar the running VWAP /
    EMA accumulators are fed each bar, so two sources are two computations and
    MUST get two run-identities via the params axis (the exact ``fib_anchor``
    precedent, no ``indicator_version`` bump — the shared-Rust accumulator math
    is byte-unchanged, only the pre-FFI scalar selection moves). It lives HERE
    (the pure leaf ``strategies.types`` module) so the engine can thread it as a
    grid axis without pulling any heavy import into the engine runtime graph.

    ``hl2`` / ``ohlc4`` are DELIBERATELY absent: on liquid 1-minute index /
    futures bars they are near-clones of ``HLC3`` (correlated Sharpe draws that
    worsen DSR effective-N), AND their ``/2`` / ``/4`` reductions can land on a
    ½-paisa rounding tie that the ``/3`` path provably never hits (the
    ``(H+L+C)`` paisa-sum remainder is ``{0,1,2}`` so ``r/3 ∈ {0, ⅓, ⅔}`` never
    equals ½). Re-adding either needs a clones + rounding-tie review + a golden.
    """

    #: Bar open (``bar.open``).
    OPEN = "open"
    #: Bar high (``bar.high``) — a DEFERRED variant (fixed-``close``-comparison
    #: bias risk); shipped in the enum for completeness, not in the OFAT lattice.
    HIGH = "high"
    #: Bar low (``bar.low``) — a DEFERRED variant (see ``HIGH``).
    LOW = "low"
    #: Bar close (``bar.close``) — today's hardcoded EMA source.
    CLOSE = "close"
    #: Typical price ``(H+L+C)/3`` — today's hardcoded VWAP source.
    HLC3 = "hlc3"


@dataclass(frozen=True, slots=True)
class PriceSourceCell:
    """One cell of the VWAP/EMA price-source grid axis (DEC-NIFTY-PRICE-SOURCE-AXIS-001).

    A ``(vwap_source, ema_source)`` pair, decomposed into two run-identity params
    keys downstream. The default ``(HLC3, CLOSE)`` reproduces today's hardcoded
    behaviour bit-for-bit, and — per the default-omit convention (Override 1) —
    serialises ABSENT from every canonical params / store-address / cache key so
    existing run-identities stay byte-identical. Frozen + slotted so it is
    hashable (an ``itertools.product`` grid axis + a ``dict.fromkeys`` de-dup key).
    """

    vwap_source: PriceSource = PriceSource.HLC3
    ema_source: PriceSource = PriceSource.CLOSE


#: The one baseline cell — today's hardcoded ``(HLC3, CLOSE)`` behaviour. The
#: default price-source axis for every grid; a grid carrying only this cell is
#: cardinality-1 and byte-identical to the pre-axis sweep (dormant axis).
DEFAULT_PRICE_SOURCE_CELL: PriceSourceCell = PriceSourceCell()

#: The exact number of ``:``-separated parts in one ``<vwap>:<ema>`` cell token.
_CELL_TOKEN_PARTS: int = 2


def parse_price_source_cells(spec: str) -> list[PriceSourceCell]:
    """Parse a CSV of ``vwap:ema`` cell tokens into :class:`PriceSourceCell`\\ s.

    Grammar: comma-separated cells, each ``"<vwap_source>:<ema_source>"`` using
    the :class:`PriceSource` string values (e.g. ``"hlc3:close,open:close"``).
    Whitespace around tokens is ignored; blank tokens are skipped. Duplicate
    cells are collapsed (first-seen order preserved) so a repeated cell never
    yields a duplicate grid point. An empty / all-blank spec yields the single
    baseline cell ``(HLC3, CLOSE)`` so an unset flag is byte-identical to today.

    The 5-cell OFAT (one-factor-at-a-time) lattice is
    ``"hlc3:close,open:close,close:close,hlc3:open,hlc3:hlc3"`` — the baseline
    plus 2 VWAP-perturb + 2 EMA-perturb cells (cardinality 5, linear).

    Parameters
    ----------
    spec
        The CSV cell spec.

    Returns
    -------
    list[PriceSourceCell]
        The de-duplicated cells, in first-seen order (never empty).

    Raises
    ------
    ValueError
        On a malformed cell (missing / extra ``:`` separator) or an unknown
        ``PriceSource`` token.
    """
    cells: list[PriceSourceCell] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) != _CELL_TOKEN_PARTS:
            raise ValueError(
                f"price-source cell {token!r} must be '<vwap>:<ema>' (exactly one ':' separator)"
            )
        vwap_tok, ema_tok = parts[0].strip(), parts[1].strip()
        cell = PriceSourceCell(
            vwap_source=PriceSource(vwap_tok),
            ema_source=PriceSource(ema_tok),
        )
        if cell not in cells:
            cells.append(cell)
    if not cells:
        cells.append(DEFAULT_PRICE_SOURCE_CELL)
    return cells


@dataclass(frozen=True, slots=True)
class Signal:
    """One trade signal emitted by a :class:`~brutex.strategies.Strategy`.

    Attributes
    ----------
    symbol
        Canonical (vendor) trading symbol the signal applies to. Must be one
        of the strategy's declared :attr:`StrategyMeta.symbols`.
    side
        :class:`Side` — ``BUY``, ``SELL``, or ``HOLD``.
    timestamp
        The bar timestamp the signal was generated on. Must be
        timezone-aware and must not be strictly after the
        :attr:`~brutex.feed.ReplayFeed.current_ts` of the driving
        feed (``FR-INT-004``).
    strength
        Free-form numeric strength in ``[0.0, 1.0]``. The engine does
        not interpret this — it is passed through to sizing /
        execution layers.
    reason
        Short human-readable justification string (e.g. ``"rsi<30"``).
        Used in audit trails and the dashboard trade blotter.
    """

    symbol: str
    side: Side
    timestamp: datetime
    strength: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                f"Signal.timestamp must be timezone-aware, got naive datetime {self.timestamp!r}"
            )
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Signal.strength must be in [0.0, 1.0], got {self.strength}")


@dataclass(slots=True)
class StrategyState:
    """Engine-managed mutable state carried across ``on_event`` calls.

    Per ``FR-STR-003``, concrete strategies **must** be stateless
    except for fields they store inside this container. The runner
    resets the state at the start of every replay, which is what
    makes ``replay(A) == replay(A)`` hold.

    Attributes
    ----------
    last_prices
        Per-symbol last-seen close, keyed by symbol. Updated by the
        runner before each ``on_event`` call so strategies don't
        need to re-derive it.
    bar_count
        Total number of bars observed so far. Strategies use this
        to wait out their declared ``lookback_bars`` before emitting
        signals.
    scratch
        Strategy-private scratchpad. The engine treats it as opaque
        — anything a concrete strategy wants to persist across
        events (EMA running totals, flag state, …) lives here. Kept
        as ``dict[str, Any]`` so tests can assert on specific keys.
    """

    last_prices: dict[str, Decimal] = field(default_factory=dict)
    bar_count: int = 0
    scratch: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        """Restore the state to its just-constructed form.

        Called by :class:`~brutex.strategies.runner.StrategyRunner`
        before every replay so two back-to-back runs over the same
        feed yield bit-identical results.
        """
        self.last_prices.clear()
        self.bar_count = 0
        self.scratch.clear()
