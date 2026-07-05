"""Unit tests for the synthetic offline adapter (DEC-SYNTHETIC-ADAPTER-001).

Covers the adapter surface, the seeded generator maths, the instrument index,
and the constants table — fully offline, no creds, no network. Targets 100%
line + branch coverage of ``brutex.adapters.synthetic``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.protocol import MarketAdapter
from brutex.adapters.synthetic import (
    DEFAULT_SEED,
    SUPPORTED_INTERVALS,
    SYMBOL_SPECS,
    SyntheticMarketAdapter,
    build_synthetic_index,
)
from brutex.adapters.synthetic.adapter import SYNTHETIC_CAPABILITIES
from brutex.adapters.synthetic.constants import (
    EPOCH,
    SyntheticSymbolSpec,
)
from brutex.adapters.synthetic.generator import (
    bar_timestamps,
    generate_bar,
    generate_candles,
    minute_ordinal,
)
from brutex.adapters.synthetic.instruments import instrument_for_spec
from brutex.calendars import get_session
from brutex.data.models import CandleBar
from brutex.models import Exchange, InstrumentType, OutOfScopeError, Segment

# A regular Monday trading day and the following days (Jan 2021 has only one
# holiday — Jan 26 — so Jan 4-8 are all regular sessions; Jan 9/10 = weekend).
_MON = date(2021, 1, 4)
_TUE = date(2021, 1, 5)
_SAT = date(2021, 1, 9)
_NEXT_MON = date(2021, 1, 11)


def _adapter(seed: int = DEFAULT_SEED) -> SyntheticMarketAdapter:
    return SyntheticMarketAdapter(seed=seed)


def _nifty_day_1m() -> list[CandleBar]:
    return _adapter().fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )


# ── Protocol satisfaction + capabilities ─────────────────────────────────────


def test_is_a_market_adapter() -> None:
    assert isinstance(_adapter(), MarketAdapter)


def test_capabilities_are_honest_and_spot_only() -> None:
    caps = _adapter().capabilities
    assert caps is SYNTHETIC_CAPABILITIES
    assert caps.market_id == "synthetic"
    assert "/" not in caps.market_id
    assert caps.rate_limit_rps > 0
    assert caps.has_derivatives is False
    assert caps.has_expiries is False
    assert caps.has_option_chains is False
    assert caps.supports_intraday is True
    assert caps.exchanges == frozenset({Exchange.NSE, Exchange.BSE})
    assert caps.supported_intervals == SUPPORTED_INTERVALS
    # Honesty (Golden Rule #1/#10): the label must say it is NOT real.
    assert "SYNTHETIC" in caps.display_name
    assert "NOT real" in caps.display_name


def test_earliest_history_within_calendar_coverage() -> None:
    assert _adapter().capabilities.earliest_history == date(2021, 1, 1)


# ── list_instruments / list_derivatives ──────────────────────────────────────


def test_list_instruments_one_page_then_exhausted() -> None:
    adapter = _adapter()
    page = adapter.list_instruments()
    assert len(page.instruments) == len(SYMBOL_SPECS)
    assert page.next_cursor is None
    # Any non-None cursor is past the single page.
    assert adapter.list_instruments(cursor="anything").instruments == ()


def test_list_instruments_surfaces_index_and_equity() -> None:
    names = {i.symbol_name for i in _adapter().list_instruments().instruments}
    assert names == {"NIFTY", "BANKNIFTY", "SENSEX", "SYNTH"}


def test_list_derivatives_is_empty() -> None:
    adapter = _adapter()
    assert list(adapter.list_derivatives(underlying_symbol="NIFTY", exchange=Exchange.NSE)) == []


# ── fetch_ohlcv — happy path + invariants ────────────────────────────────────


def test_fetch_index_day_has_full_session_zero_volume() -> None:
    bars = _nifty_day_1m()
    assert len(bars) == 375  # 09:15-15:30 regular session at 1m
    assert all(b.volume == 0 for b in bars)  # index → no traded volume
    assert all(b.open_interest is None for b in bars)  # spot → no OI
    first = bars[0]
    assert first.timestamp == datetime(2021, 1, 4, 3, 45, tzinfo=UTC)  # 09:15 IST


def test_fetch_bars_satisfy_ohlc_and_grid_invariants() -> None:
    bars = _nifty_day_1m()
    for b in bars:
        assert b.timestamp.tzinfo is not None  # tz-aware
        assert b.timestamp.utcoffset() == UTC.utcoffset(None)  # stored UTC
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.high >= b.low
        for px in (b.open, b.high, b.low, b.close):
            assert px == px.quantize(Decimal("0.01"))  # snapped to 2dp grid


def test_fetch_bars_are_strictly_ascending() -> None:
    bars = _nifty_day_1m()
    assert all(bars[i].timestamp < bars[i + 1].timestamp for i in range(len(bars) - 1))


def test_fetch_bars_sit_inside_their_session_window() -> None:
    from brutex.calendars import IST

    for b in _nifty_day_1m():
        ist = b.timestamp.astimezone(IST)
        session = get_session(ist.date())
        assert session is not None
        assert session.open_time_ist <= ist.time() < session.close_time_ist


def test_fetch_has_both_up_and_down_bars() -> None:
    # Exercises BOTH ternary branches of the high/low body selection.
    bars = _nifty_day_1m()
    assert any(b.close > b.open for b in bars)
    assert any(b.close < b.open for b in bars)


def test_fetch_bare_and_prefixed_symbol_agree() -> None:
    adapter = _adapter()
    bare = adapter.fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )
    prefixed = adapter.fetch_ohlcv(
        symbol="NSE-NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )
    assert bare == prefixed


def test_fetch_equity_has_positive_volume() -> None:
    bars = _adapter().fetch_ohlcv(
        symbol="SYNTH", exchange=Exchange.NSE, interval="5minute", start=_MON, end=_TUE
    )
    assert len(bars) == 75  # 375 / 5
    assert all(b.volume > 0 for b in bars)  # non-index → traded volume
    # Pin an exact deterministic volume (kills volume-formula constant /
    # interval-scaling mutants that a `> 0` assertion would miss).
    assert bars[0].volume == 8614


def test_fetch_daily_one_bar_per_trading_day() -> None:
    bars = _adapter().fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1day", start=_MON, end=date(2021, 1, 9)
    )
    assert len(bars) == 5  # Mon-Fri, no holiday


@pytest.mark.parametrize("interval", sorted(SUPPORTED_INTERVALS))
def test_every_supported_interval_produces_valid_bars(interval: str) -> None:
    bars = _adapter().fetch_ohlcv(
        symbol="BANKNIFTY", exchange=Exchange.NSE, interval=interval, start=_MON, end=_TUE
    )
    assert bars  # at least one bar on a regular day
    assert all(b.low <= b.open <= b.high and b.low <= b.close <= b.high for b in bars)


# ── fetch_ohlcv — edge cases ─────────────────────────────────────────────────


def test_fetch_weekend_only_range_is_empty() -> None:
    bars = _adapter().fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_SAT, end=_NEXT_MON
    )
    assert bars == []  # exercises the `session is None` branch


def test_fetch_empty_range_when_start_not_before_end() -> None:
    adapter = _adapter()
    assert (
        adapter.fetch_ohlcv(
            symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_TUE, end=_MON
        )
        == []
    )
    assert (
        adapter.fetch_ohlcv(
            symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_MON
        )
        == []
    )


def test_fetch_unknown_symbol_raises_out_of_scope() -> None:
    with pytest.raises(OutOfScopeError, match="not in synthetic universe"):
        _adapter().fetch_ohlcv(
            symbol="ZZZ", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
        )


def test_fetch_unsupported_interval_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported synthetic interval"):
        _adapter().fetch_ohlcv(
            symbol="NIFTY", exchange=Exchange.NSE, interval="2minute", start=_MON, end=_TUE
        )


# ── determinism / idempotence ────────────────────────────────────────────────


def test_same_inputs_are_bit_identical() -> None:
    assert _nifty_day_1m() == _nifty_day_1m()


def test_different_seed_changes_the_bars() -> None:
    base = _nifty_day_1m()
    other = _adapter(seed=DEFAULT_SEED + 1).fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )
    assert base != other


def test_different_symbol_changes_the_bars() -> None:
    adapter = _adapter()
    nifty = adapter.fetch_ohlcv(
        symbol="NIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )
    bank = adapter.fetch_ohlcv(
        symbol="BANKNIFTY", exchange=Exchange.NSE, interval="1minute", start=_MON, end=_TUE
    )
    assert [b.open for b in nifty] != [b.open for b in bank]


# ── generator internals ──────────────────────────────────────────────────────


def test_minute_ordinal_counts_minutes_since_epoch() -> None:
    assert minute_ordinal(EPOCH) == 0
    assert minute_ordinal(datetime(2020, 1, 1, 0, 1, tzinfo=UTC)) == 1
    assert minute_ordinal(datetime(2020, 1, 2, tzinfo=UTC)) == 1440


def test_bar_timestamps_bad_interval_raises() -> None:
    with pytest.raises(ValueError, match="unsupported synthetic interval"):
        list(bar_timestamps("7minute", _MON, _TUE))


def test_bar_timestamps_out_of_coverage_range_warns_once_but_still_generates() -> None:
    # Calendar-correctness pack: the generator DELIBERATELY stays unguarded (it
    # fabricates data by design), but an out-of-coverage span is no longer
    # SILENT — one synthetic.calendar_coverage_exceeded warning per call, and
    # the out-of-coverage weekday still yields a full regular session of bars.
    from structlog.testing import capture_logs

    out_mon, out_tue = date(2027, 1, 4), date(2027, 1, 5)  # above the 2026 ceiling
    with capture_logs() as logs:
        stamps = list(bar_timestamps("1day", out_mon, out_tue))
    assert len(stamps) == 1  # the weekday is generated as a regular session
    events = [log for log in logs if log["event"] == "synthetic.calendar_coverage_exceeded"]
    assert len(events) == 1
    assert events[0]["start"] == "2027-01-04"


def test_bar_timestamps_in_coverage_range_does_not_warn() -> None:
    # In-coverage behaviour byte-identical: no warning, same bars as before.
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        stamps = list(bar_timestamps("1day", _MON, _TUE))
    assert len(stamps) == 1
    assert not [log for log in logs if log["event"] == "synthetic.calendar_coverage_exceeded"]


def test_bar_timestamps_empty_range_does_not_warn() -> None:
    # start == end (no days at all) short-circuits the endpoint check — no
    # warning even for an out-of-coverage anchor (nothing is generated).
    from structlog.testing import capture_logs

    far = date(2027, 1, 4)
    with capture_logs() as logs:
        assert list(bar_timestamps("1day", far, far)) == []
    assert not [log for log in logs if log["event"] == "synthetic.calendar_coverage_exceeded"]


def test_generate_candles_matches_adapter_path() -> None:
    spec = next(s for s in SYMBOL_SPECS if s.symbol == "NIFTY")
    direct = generate_candles(
        seed=DEFAULT_SEED, spec=spec, interval="1minute", start=_MON, end=_TUE
    )
    assert direct == _nifty_day_1m()


def test_generate_bar_is_pure() -> None:
    spec = next(s for s in SYMBOL_SPECS if s.symbol == "SYNTH")
    ts = datetime(2021, 1, 4, 4, 0, tzinfo=UTC)
    a = generate_bar(seed=7, spec=spec, interval="1minute", timestamp=ts)
    b = generate_bar(seed=7, spec=spec, interval="1minute", timestamp=ts)
    assert a == b
    assert isinstance(a, CandleBar)


# ── instrument index ─────────────────────────────────────────────────────────


def test_build_index_resolves_every_spec_o1() -> None:
    index = build_synthetic_index()
    assert len(index.universe) == len(SYMBOL_SPECS)
    for spec in SYMBOL_SPECS:
        assert index.spec_by_market_symbol[spec.market_symbol] is spec


def test_build_index_rejects_duplicate_symbol() -> None:
    spec = SYMBOL_SPECS[0]
    with pytest.raises(ValueError, match="duplicate synthetic symbol"):
        build_synthetic_index([spec, spec])


def test_instrument_for_index_spec_uses_index_segment() -> None:
    spec = next(s for s in SYMBOL_SPECS if s.symbol == "NIFTY")
    inst = instrument_for_spec(spec)
    assert inst.segment is Segment.INDEX
    assert inst.instrument is InstrumentType.IDX
    assert inst.is_index
    assert inst.lot_size == 0


def test_instrument_for_equity_spec_uses_cash_segment() -> None:
    spec = next(s for s in SYMBOL_SPECS if s.symbol == "SYNTH")
    inst = instrument_for_spec(spec)
    assert inst.segment is Segment.CASH
    assert inst.instrument is InstrumentType.EQ
    assert inst.lot_size == 1
    assert inst.tick_size == Decimal("0.05")


def test_surrogate_security_ids_are_stable_and_unique() -> None:
    universe = build_synthetic_index().universe
    ids = [i.security_id for i in universe]
    assert len(set(ids)) == len(ids)  # unique
    assert build_synthetic_index().universe[0].security_id == universe[0].security_id  # stable


# ── constants / spec ─────────────────────────────────────────────────────────


def test_symbol_spec_properties() -> None:
    index_spec = SyntheticSymbolSpec(
        symbol="NIFTY",
        exchange=Exchange.NSE,
        instrument_type=InstrumentType.IDX,
        display_name="SYNTHETIC NIFTY",
        base_price=Decimal("100"),
        vol_frac=0.01,
        wick_frac=0.01,
        base_volume=0,
        lot_size=0,
        tick_size=Decimal("0.05"),
        series=None,
    )
    equity_spec = SyntheticSymbolSpec(
        symbol="SYNTH",
        exchange=Exchange.BSE,
        instrument_type=InstrumentType.EQ,
        display_name="SYNTHETIC EQ",
        base_price=Decimal("100"),
        vol_frac=0.01,
        wick_frac=0.01,
        base_volume=10,
        lot_size=1,
        tick_size=Decimal("0.05"),
        series="EQ",
    )
    assert index_spec.is_index is True
    assert equity_spec.is_index is False
    assert index_spec.market_symbol == "NSE-NIFTY"
    assert equity_spec.market_symbol == "BSE-SYNTH"


def test_spec_table_has_three_indices_and_one_equity() -> None:
    indices = [s for s in SYMBOL_SPECS if s.is_index]
    equities = [s for s in SYMBOL_SPECS if not s.is_index]
    assert len(indices) == 3
    assert len(equities) == 1
