"""Integration — the ADR-0012 §4.5 roll-day boundary through the SWEEP path.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 F9: a trade can NEVER span a roll, and the
mechanism is the EXISTING engine contract, exercised here over a REAL
2-contract mini-lake stitched by the REAL alias loader:

* every roll boundary is a session-DATE boundary (ownership is IST-date
  granular), and the trade-sim walker **DROPS a pending entry whose fill bar
  lands on a later session date** — R2-OC-2, the feed-contract rule cited at
  ``brutex/engine/trade_sim.py:213-226`` ("a pending entry whose fill bar
  lands on a LATER session date … is DROPPED, never filled at the next
  session's open");
* a position still open on expiry day is closed by the session-aware forced
  exit (#1019) at session close − 10 min (15:20 IST on the regular expiry
  session) ON the expiring contract — entry AND exit inside its bars;
* the loader's IST-date ownership cut keeps every next-contract bar with an
  IST date ≤ the expiry OUT of the stitched slice (the ``>=`` law — the
  expiry day belongs to the EXPIRING contract);
* the first post-roll bar's mask carries NO prior-day-anchored bit (the B2
  hard reset makes the new contract anchor-cold — never a level compared
  across the basis gap).
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.calendars.nse import IST
from brutex.data.catalog import Catalog
from brutex.data.futures_series import resolve_futures_series
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine.stage1a import ANCHOR_DEPENDENT_MASK, build_bar_bits
from brutex.engine.trade_sim import simulate_trades
from brutex.feed.series_load import load_sweep_slice
from brutex.strategies.types import Side

_TZ = timezone(timedelta(hours=5, minutes=30))

_ALIAS = "NSE-NIFTY-FUT"
_JUN_CONTRACT = "NSE-NIFTY-26Jun25-FUT"
_JUL_CONTRACT = "NSE-NIFTY-31Jul25-FUT"
_EXPIRY = date(2025, 6, 26)  # the real NIFTY June-2025 monthly expiry (regular session)
_DAY_BEFORE = date(2025, 6, 25)
_POST_ROLL = date(2025, 6, 27)

#: Session-tail bars 15:10 → 15:29 IST — spanning the 15:20 forced-exit bar.
_TAIL_START_MINUTE = 15 * 60 + 10
_BARS_PER_DAY = 20

#: The Jun contract trades ~100; the Jul contract ~140 (the basis gap).
_JUN_BASE = 100
_JUL_BASE = 140


def _tail_bar(day: date, minute_offset: int, base: int) -> CandleBar:
    """One volume-bearing session-tail bar (15:10 IST + offset) on *day*."""
    total = _TAIL_START_MINUTE + minute_offset
    ts = datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=_TZ)
    o = base + minute_offset
    c = o + 1
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=Decimal(o),
        high=Decimal(c + 1),
        low=Decimal(o - 1),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _day(day: date, base: int) -> list[CandleBar]:
    return [_tail_bar(day, m, base) for m in range(_BARS_PER_DAY)]


@pytest.fixture(scope="module")
def stitched(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[list[CandleBar], tuple[int, ...]]:
    """The stitched slice across the Jun→Jul roll, loaded through the REAL chain.

    The Jul contract deliberately carries OVERLAP bars on 25-26 Jun at its own
    basis-gapped price plane — the loader's IST-date cut must drop every one.
    """
    root = tmp_path_factory.mktemp("expiry_boundary_lake")
    writer = ParquetWriter(lake_root=root)
    sink = ParquetBarSink(writer=writer)
    # Day-2 gap-up on the Jun contract (base 100 → 110) so its expiry-day bars
    # carry WARM prev-day anchors — the post-roll anchor-cold pin has teeth.
    jun_bars = _day(_DAY_BEFORE, _JUN_BASE) + _day(_EXPIRY, _JUN_BASE + 10)
    jul_bars = (
        _day(_DAY_BEFORE, _JUL_BASE)  # overlap — must be CUT
        + _day(_EXPIRY, _JUL_BASE)  # overlap on the expiry day itself — must be CUT
        + _day(_POST_ROLL, _JUL_BASE)
    )
    sink.append(
        exchange="NSE", segment="FNO", symbol=_JUN_CONTRACT, interval="1minute", bars=jun_bars
    )
    sink.append(
        exchange="NSE", segment="FNO", symbol=_JUL_CONTRACT, interval="1minute", bars=jul_bars
    )
    res = resolve_futures_series(root, start=_DAY_BEFORE, end=_POST_ROLL)
    with Catalog.open(root / "catalog.duckdb") as cat:
        bars = load_sweep_slice(
            cat=cat,
            lake_root=root,
            exchange="NSE",
            segment="FNO",
            symbol=_ALIAS,
            interval="1minute",
            start=datetime(2025, 6, 25, 0, 0, tzinfo=_TZ),
            end=datetime(2025, 6, 27, 23, 59, tzinfo=_TZ),
            resolution=res,
        )
    return bars, res.roll_schedule


def _ist_date(bar: CandleBar) -> date:
    return bar.timestamp.astimezone(IST).date()


def test_no_next_contract_bar_dated_at_or_before_the_expiry(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """The ``>=`` ownership law: expiry-day bars are the EXPIRING contract's only.

    The Jul contract's overlap bars (25-26 Jun, ~140 plane) exist in the lake
    but NOT in the stitched slice — every bar dated ≤ the expiry sits on the
    Jun contract's price plane, and the slice is strictly chronological.
    """
    bars, _ = stitched
    assert len(bars) == 3 * _BARS_PER_DAY  # 2 Jun-owned days + 1 Jul-owned day
    assert all(a.timestamp < b.timestamp for a, b in itertools.pairwise(bars))
    on_or_before_expiry = [b for b in bars if _ist_date(b) <= _EXPIRY]
    assert len(on_or_before_expiry) == 2 * _BARS_PER_DAY
    assert all(b.open < Decimal(_JUL_BASE) for b in on_or_before_expiry)
    post_roll = [b for b in bars if _ist_date(b) > _EXPIRY]
    assert all(b.open >= Decimal(_JUL_BASE) for b in post_roll)


def test_entry_armed_on_expiry_days_last_usable_bar_produces_no_trade(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """THE ADR-0012 §4.5 row-2 obligation, through the sweep path (R2-OC-2).

    An entry signal armed on the expiry day's LAST bar would fill at the next
    bar's open — but the next bar in the stitched stream is the NEXT session
    on the NEXT contract, so the walker DROPS the pending entry
    (``trade_sim.py:213-226``): no trade exists, and in particular no fill at
    the post-roll session's basis-gapped open.
    """
    bars, _ = stitched
    last_expiry_index = max(i for i, b in enumerate(bars) if _ist_date(b) == _EXPIRY)
    signals = [False] * len(bars)
    signals[last_expiry_index] = True
    trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
    assert trades == []


def test_same_day_control_arm_fills_the_entry(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """Non-vacuity control: the SAME signal shape mid-session DOES fill.

    Proves the empty trade list above comes from the session/roll boundary,
    not from a fixture that could never trade.
    """
    bars, _ = stitched
    first_expiry_index = min(i for i, b in enumerate(bars) if _ist_date(b) == _EXPIRY)
    signals = [False] * len(bars)
    signals[first_expiry_index] = True
    trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
    assert len(trades) == 1
    assert _ist_date_of(trades[0].entry_ts) == _EXPIRY


def _ist_date_of(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def test_position_open_on_expiry_day_force_exits_on_the_expiring_contract(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """#1019: the open position closes at session close − 10 min (15:20 IST)
    on the EXPIRING contract — entry AND exit timestamps inside the expiry
    session, exit price on the Jun plane, never a bar of the next contract."""
    bars, _ = stitched
    first_expiry_index = min(i for i, b in enumerate(bars) if _ist_date(b) == _EXPIRY)
    signals = [False] * len(bars)
    signals[first_expiry_index] = True  # fills at the NEXT bar's open (15:12 IST)
    trades = simulate_trades(bars, signals, direction=Side.BUY, tf_minutes=1)
    assert len(trades) == 1
    trade = trades[0]
    assert _ist_date_of(trade.entry_ts) == _EXPIRY
    assert _ist_date_of(trade.exit_ts) == _EXPIRY
    # The forced-exit bar: exactly 15:20 IST — NOT the day's last bar (15:29)
    # and NOT the next session's open (no sl/tp is configured, so only the
    # session-aware forced exit can close this position).
    exit_ist = trade.exit_ts.astimezone(IST)
    assert (exit_ist.hour, exit_ist.minute) == (15, 20)
    # holding_bars: filled 15:12, forced at 15:20 — strictly within the session.
    assert trade.holding_bars >= 1


def test_first_post_roll_bar_mask_carries_no_prior_day_anchor_bits(
    stitched: tuple[list[CandleBar], tuple[int, ...]],
) -> None:
    """B2 (DEC-STAGE1A-ROLL-RESET-001) at the boundary: the first post-roll
    bar is anchor-cold, while the expiry-day bars (day 2 of the Jun contract,
    gapped UP over day 1) genuinely carried warm anchor bits — the clear is
    the roll reset, not a fixture artefact."""
    bars, schedule = stitched
    masks = build_bar_bits(bars, roll_schedule=schedule)
    by_date = [(_ist_date(b), m) for b, m in zip(bars, masks, strict=True)]
    expiry_day_masks = [m for d, m in by_date if d == _EXPIRY]
    assert any(m & ANCHOR_DEPENDENT_MASK for m in expiry_day_masks)  # teeth
    post_roll_masks = [m for d, m in by_date if d == _POST_ROLL]
    assert post_roll_masks
    assert all((m & ANCHOR_DEPENDENT_MASK) == 0 for m in post_roll_masks)
