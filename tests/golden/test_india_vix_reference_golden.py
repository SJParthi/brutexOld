"""Golden corpus #4 — INDIA VIX, one week (reference-only data, no signal bits).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #4 —
"INDIA VIX — 1 week / reference-only data; no signal bits").

India VIX is **REFERENCE ONLY** (CLAUDE.md §1/§3): it is fetched + stored and
stamped as ``vix_at_entry`` / ``vix_at_exit`` on every trade, but is **NEVER**
part of the brute-force pattern combinations or the 60-condition vocabulary.
This corpus locks all three halves of that contract against REAL components:

* **reference data path** — VIX one-week 1-minute bars resample through the
  canonical :func:`brutex.data.tf_resampler.resample_1m_to_tf` to the daily
  (1d) timeframe exactly like any spot series (the lake stores the reference
  series too). The two daily bars are hand-derived OHLC aggregations.
* **per-trade stamp (DEC-VIX-TRADE-STAMP-001)** — a constructed trade run
  through the real
  :class:`~brutex.engine.reference_series.TradeReferenceStamper` over this VIX
  week carries the ACTUAL VIX close at its entry + exit instants, and ``None``
  (never a fabricated number) when no VIX bar exists at that timestamp.
* **no signal bits** — the universe scope filters
  (:mod:`brutex.universe`) classify INDIA VIX as ``reference_only`` and
  **exclude** it from the brute-force engine, so no VIX bar is ever fed to the
  60-condition evaluator. The ``is_reference_only`` / ``is_in_brute_force_scope``
  mutual-exclusivity is the engine-isolation guarantee
  (DEC-ENGINE-ISOLATION-INVARIANT-001).

Hand-derived daily aggregation (left-closed, anchored at the 09:15 IST open):

* **2024-01-02** — open 15.00, high max(15.20, 15.30, 15.40, 15.50) = 15.50,
  low min(14.90, 15.05, 15.20, 15.30) = 14.90, close 15.45.
* **2024-01-03** — open 16.00, high 16.40, low 15.90, close 16.35.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.tf_resampler import resample_1m_to_tf
from brutex.data.timeframes import DAILY_TF_MINUTES
from brutex.engine.reference_series import TradeReferenceStamper
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.strategies.types import Side
from brutex.universe import (
    BRUTE_FORCE_INDEX_UNDERLYINGS,
    BRUTE_FORCE_INDICES,
    REFERENCE_ONLY_INDEX_UNDERLYINGS,
    REFERENCE_ONLY_INDICES,
    is_in_brute_force_scope,
    is_reference_only,
)

#: 09:15 IST == 03:45 UTC — the daily-bucket anchor used by the resampler.
_DAY1_OPEN_UTC = datetime(2024, 1, 2, 3, 45)
_DAY2_OPEN_UTC = datetime(2024, 1, 3, 3, 45)

#: One week of VIX 1-minute reference bars (two trading days × 4 bars), as
#: ``(open, high, low, close)`` in index points (VIX is ~15 in early 2024).
_DAY1_BARS = [
    (15.00, 15.20, 14.90, 15.10),
    (15.10, 15.30, 15.05, 15.25),
    (15.25, 15.40, 15.20, 15.35),
    (15.35, 15.50, 15.30, 15.45),
]
_DAY2_BARS = [
    (16.00, 16.10, 15.90, 16.05),
    (16.05, 16.20, 16.00, 16.15),
    (16.15, 16.30, 16.10, 16.25),
    (16.25, 16.40, 16.20, 16.35),
]


def _vix_1m() -> pl.DataFrame:
    """Build the VIX 1-minute reference DataFrame (sorted, resampler schema)."""
    rows: list[tuple[datetime, float, float, float, float, int]] = []
    for base, day in ((_DAY1_OPEN_UTC, _DAY1_BARS), (_DAY2_OPEN_UTC, _DAY2_BARS)):
        for j, (o, h, lo, c) in enumerate(day):
            rows.append((base + timedelta(minutes=j), o, h, lo, c, 0))
    return pl.DataFrame(
        rows,
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
        orient="row",
    )


def _expected_daily() -> pl.DataFrame:
    """The hand-derived 2-row daily aggregation of the VIX week."""
    return pl.DataFrame(
        {
            "ts_utc": [_DAY1_OPEN_UTC, _DAY2_OPEN_UTC],
            "open": [15.00, 16.00],
            "high": [15.50, 16.40],
            "low": [14.90, 15.90],
            "close": [15.45, 16.35],
            "volume": [0, 0],
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


def _india_vix_instrument() -> Instrument:
    """A minimal INDIA VIX IDX row (symbol per brutex.universe: ``INDIAVIX``)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=99,
        symbol_name="INDIAVIX",
        display_name="India VIX",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol="INDIAVIX",
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def test_vix_week_resamples_to_hand_derived_daily() -> None:
    """The reference VIX week aggregates to the two hand-derived daily bars."""
    daily = resample_1m_to_tf(_vix_1m(), DAILY_TF_MINUTES)
    assert daily.equals(_expected_daily())


def test_india_vix_is_reference_only_not_brute_force() -> None:
    """No signal bits: VIX is reference-only and never enters the engine."""
    vix = _india_vix_instrument()
    assert is_reference_only(vix) is True
    assert is_in_brute_force_scope(vix) is False


def test_reference_only_and_brute_force_partitions_are_disjoint() -> None:
    """The universe data structures place VIX in reference-only, not brute-force."""
    assert (Exchange.NSE, "INDIAVIX") in REFERENCE_ONLY_INDEX_UNDERLYINGS
    assert (Exchange.NSE, "INDIAVIX") not in BRUTE_FORCE_INDEX_UNDERLYINGS
    assert {s.underlying_symbol for s in REFERENCE_ONLY_INDICES} == {"INDIAVIX"}
    assert {s.underlying_symbol for s in BRUTE_FORCE_INDICES} == {"NIFTY", "BANKNIFTY", "SENSEX"}


def _vix_candlebars() -> list[CandleBar]:
    """The VIX week as tz-aware :class:`CandleBar` reference bars (DEC-VIX-TRADE-STAMP-001)."""
    bars: list[CandleBar] = []
    for base, day in ((_DAY1_OPEN_UTC, _DAY1_BARS), (_DAY2_OPEN_UTC, _DAY2_BARS)):
        for j, (o, h, lo, c) in enumerate(day):
            bars.append(
                CandleBar(
                    timestamp=(base + timedelta(minutes=j)).replace(tzinfo=UTC),
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(lo)),
                    close=Decimal(str(c)),
                    volume=0,
                    open_interest=None,
                )
            )
    return bars


def test_vix_stamps_real_close_on_a_constructed_trade() -> None:
    """A trade over this VIX week is stamped with the ACTUAL VIX close at entry+exit.

    Entry @ the day-1 first bar (close 15.10), exit @ the day-1 last bar
    (close 15.45) — the stamp must equal those hand-listed closes exactly
    (full Decimal precision, never money-rounded).
    """
    stamper = TradeReferenceStamper.from_vix_bars(_vix_candlebars())
    entry_ts = _DAY1_OPEN_UTC.replace(tzinfo=UTC)
    exit_ts = (_DAY1_OPEN_UTC + timedelta(minutes=3)).replace(tzinfo=UTC)
    vix_entry, vix_exit = stamper.stamp(entry_ts, exit_ts)
    assert vix_entry == Decimal("15.10")  # _DAY1_BARS[0] close
    assert vix_exit == Decimal("15.45")  # _DAY1_BARS[3] close


def test_vix_stamp_is_none_when_no_bar_at_instant() -> None:
    """A trade whose entry/exit instant has no VIX bar stamps ``None`` (no hallucination)."""
    stamper = TradeReferenceStamper.from_vix_bars(_vix_candlebars())
    # An instant inside the gap between the two trading days has no VIX bar.
    off_grid = (_DAY1_OPEN_UTC + timedelta(hours=2)).replace(tzinfo=UTC)
    vix_entry, vix_exit = stamper.stamp(off_grid, off_grid)
    assert vix_entry is None
    assert vix_exit is None


def test_constructed_closed_trade_carries_vix_stamp() -> None:
    """A real ``simulate_trades`` round-trip over this VIX week carries the stamp."""
    from brutex.engine.trade_sim import simulate_trades

    # A spot index feed aligned to the VIX day-1 grid: signal @bar0 → enter
    # @bar1 (VIX 15.25) → end-of-data exit @bar2 close (VIX 15.35).
    def _spot(minute: int, close: str) -> CandleBar:
        value = Decimal(close)
        return CandleBar(
            timestamp=(_DAY1_OPEN_UTC + timedelta(minutes=minute)).replace(tzinfo=UTC),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=0,
            open_interest=None,
        )

    spot = [_spot(0, "21000"), _spot(1, "21010"), _spot(2, "21020")]
    trades = simulate_trades(
        spot,
        [True, False, False],
        direction=Side.BUY,
        tf_minutes=1,
        # tf==1m fixed point (DEC-FILL-EXEC-GRID-1M-001): same list, both
        # streams — entry still fills on bar 1, end_of_data still exits on
        # bar 2 (not the entry bar, so no B3 carve-out), so the stamped
        # instants (and hence the golden VIX levels) are unchanged.
        exec_bars_1m=spot,
        reference_stamper=TradeReferenceStamper.from_vix_bars(_vix_candlebars()),
    )
    assert len(trades) == 1
    assert trades[0].vix_at_entry == Decimal("15.25")  # _DAY1_BARS[1] close
    assert trades[0].vix_at_exit == Decimal("15.35")  # _DAY1_BARS[2] close
