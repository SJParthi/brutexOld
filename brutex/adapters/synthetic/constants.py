"""Synthetic adapter constants — the single pure-data lookup surface.

This module holds ONLY pure data (no I/O, no mutable state, no logic) for the
SYNTHETIC, offline, deterministic market adapter (DEC-SYNTHETIC-ADAPTER-001).
Every value is an honest, self-describing constant: the ``market_id`` /
``display_name`` make it unmistakable that the bars are GENERATED, never real
market data (Golden Rule #1/#10).

It mirrors the canonical adapter constants layout
(:mod:`brutex.adapters.groww.constants`) so the synthetic leaf reads the same
as the live one. The synthetic universe is a tiny, fixed table — the three
in-scope brute-force indices (NIFTY / BANKNIFTY / SENSEX) plus one clearly
labelled synthetic equity that exercises the non-index (traded-volume,
positive lot/tick) code path. No CSV, no network: the :class:`Instrument`
objects are built directly from this table by :mod:`.instruments`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from brutex.data.timeframes import trading_minutes_for_lake_interval
from brutex.models import Exchange, InstrumentType

# ── Identity (honesty-critical — Golden Rule #1/#10) ─────────────────────────
#: Stable market id stamped into partition paths / audit logs / dashboard
#: labels. Deliberately distinct from the existing dry-run adapter's ``"spot"``
#: coverage namespace so the two never collide.
MARKET_ID: Final[str] = "synthetic"
#: Human-readable label — states plainly that the bars are NOT real.
DISPLAY_NAME: Final[str] = "SYNTHETIC (offline, deterministic — NOT real market data)"
#: The wall clock the synthetic session grid uses (NSE/BSE), fixed +05:30.
NATIVE_TIMEZONE: Final[str] = "Asia/Kolkata"
#: Earliest date the OFFLINE generator will serve. Bounded to the verified
#: NSE/BSE trading-calendar coverage (2021-2026) so every emitted dry-run bar
#: sits on a real session — this is the synthetic adapter's own constraint and is
#: deliberately INDEPENDENT of the live pull floor (``GROWW_HISTORY_FLOOR`` is
#: 2020-01-01 per DEC-PULL-FLOOR-2020-001; the live vendor depth reaches 2020, the
#: offline calendar's verified holiday set still starts 2021).
EARLIEST_HISTORY: Final[date] = date(2021, 1, 1)
#: Nominal rate ceiling. Offline generation has no real per-second limit, but
#: :class:`~brutex.adapters.protocol.MarketCapabilities` requires ``> 0``; a
#: high value reflects "no network bottleneck" honestly.
RATE_LIMIT_RPS: Final[float] = 1000.0
#: Default seed for the deterministic generator when none is injected. Any
#: ``(seed, symbol, interval, range)`` reproduces bit-identical bars.
DEFAULT_SEED: Final[int] = 20210101

# ── Generator tuning (pure data; consumed by :mod:`.generator`) ──────────────
#: Absolute time anchor for the per-bar minute ordinal. A bar's ordinal is
#: ``minutes since EPOCH`` — an O(1) function of its timestamp alone.
EPOCH: Final[datetime] = datetime(2020, 1, 1, tzinfo=UTC)
#: Minutes per noise "anchor unit" (one regular session = 375 min). The price
#: noise is sampled at ``ordinal / ANCHOR_MINUTES`` so adjacent sessions move
#: by roughly one anchor unit.
ANCHOR_MINUTES: Final[int] = 375
#: Number of value-noise octaves summed per price sample. A FIXED constant, so
#: per-bar generation is O(1) (constant work, independent of the bar index or
#: the range length).
NOISE_OCTAVES: Final[int] = 5

#: The intervals this SYNTHETIC adapter serves — a vendor-scoped SUBSET of the
#: canonical registry labels (the selection deliberately stays local).
_SYNTHETIC_INTERVAL_LABELS: Final[tuple[str, ...]] = (
    "1minute",
    "5minute",
    "15minute",
    "1hour",
    "1day",
)

#: BruteX canonical interval → minutes-per-bar — DERIVED from the canonical
#: registry (DEC-TF-REGISTRY-DYNAMIC-001; values byte-identical to the old
#: literals). ``1day`` maps to the 375-minute session length (NOT 1440
#: wall-clock minutes) so a day bar's open→close span samples one full
#: session ahead — matching ``SESSION_DURATION_MINUTES``.
INTERVAL_TO_MINUTES: Final[Mapping[str, int]] = {
    lake_label: trading_minutes_for_lake_interval(lake_label)
    for lake_label in _SYNTHETIC_INTERVAL_LABELS
}
#: The intervals this adapter declares it can serve.
SUPPORTED_INTERVALS: Final[frozenset[str]] = frozenset(INTERVAL_TO_MINUTES)


@dataclass(frozen=True, slots=True)
class SyntheticSymbolSpec:
    """One synthetic instrument's identity + deterministic price parameters.

    The single source of truth for a synthetic symbol: :mod:`.instruments`
    builds its :class:`~brutex.models.Instrument` from the identity fields and
    :mod:`.generator` reads the price/volume parameters — so the universe and
    the bar maths can never drift apart (CLAUDE.md §11 COMMON).
    """

    symbol: str
    exchange: Exchange
    instrument_type: InstrumentType
    display_name: str
    base_price: Decimal
    vol_frac: float
    wick_frac: float
    base_volume: int
    lot_size: int
    tick_size: Decimal
    series: str | None

    @property
    def is_index(self) -> bool:
        """True for an index spec (``IDX``) — index bars carry zero volume."""
        return self.instrument_type == InstrumentType.IDX

    @property
    def market_symbol(self) -> str:
        """The ``EXCHANGE-SYMBOL`` key (e.g. ``NSE-NIFTY``) used for O(1) lookup."""
        return f"{self.exchange.value}-{self.symbol}"


#: The fixed synthetic universe. Indices mirror the in-scope brute-force
#: instruments (so the engine can run offline against the same symbols); the
#: lone ``SYNTH`` equity exercises the traded-volume / positive lot+tick path.
SYMBOL_SPECS: Final[tuple[SyntheticSymbolSpec, ...]] = (
    SyntheticSymbolSpec(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        instrument_type=InstrumentType.IDX,
        display_name="SYNTHETIC NIFTY 50 (generated)",
        base_price=Decimal("22000"),
        vol_frac=0.030,
        wick_frac=0.006,
        base_volume=0,
        lot_size=0,
        tick_size=Decimal("0.05"),
        series=None,
    ),
    SyntheticSymbolSpec(
        symbol="BANKNIFTY",
        exchange=Exchange.NSE,
        instrument_type=InstrumentType.IDX,
        display_name="SYNTHETIC NIFTY BANK (generated)",
        base_price=Decimal("48000"),
        vol_frac=0.040,
        wick_frac=0.008,
        base_volume=0,
        lot_size=0,
        tick_size=Decimal("0.05"),
        series=None,
    ),
    SyntheticSymbolSpec(
        symbol="SENSEX",
        exchange=Exchange.BSE,
        instrument_type=InstrumentType.IDX,
        display_name="SYNTHETIC BSE SENSEX (generated)",
        base_price=Decimal("72000"),
        vol_frac=0.028,
        wick_frac=0.006,
        base_volume=0,
        lot_size=0,
        tick_size=Decimal("0.05"),
        series=None,
    ),
    SyntheticSymbolSpec(
        symbol="SYNTH",
        exchange=Exchange.NSE,
        instrument_type=InstrumentType.EQ,
        display_name="SYNTHETIC TEST EQUITY (generated)",
        base_price=Decimal("2500"),
        vol_frac=0.050,
        wick_frac=0.010,
        base_volume=1200,
        lot_size=1,
        tick_size=Decimal("0.05"),
        series="EQ",
    ),
)


__all__ = [
    "ANCHOR_MINUTES",
    "DEFAULT_SEED",
    "DISPLAY_NAME",
    "EARLIEST_HISTORY",
    "EPOCH",
    "INTERVAL_TO_MINUTES",
    "MARKET_ID",
    "NATIVE_TIMEZONE",
    "NOISE_OCTAVES",
    "RATE_LIMIT_RPS",
    "SUPPORTED_INTERVALS",
    "SYMBOL_SPECS",
    "SyntheticSymbolSpec",
]
