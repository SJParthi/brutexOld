"""Unit tests for the gainers/losers screener (DEC-MOVERS-GAINERS-001).

Every branch of :mod:`brutex.screeners.gainers_losers` is exercised for the
strict 100% line+branch gate (the ``--cov=brutex.screeners`` per-module gate
runs ONLY this file): the sign-split ranking, the deterministic tiebreak, the
top-N cap, the empty / flat / skipped edges, the ``_build_mover`` reduction
(empty window + zero-base skip), and both ``ValueError`` guards.
"""

# Whitebox: ``_build_mover`` is imported directly to pin the per-instrument
# reduction (empty / zero-base / normal) byte-exact for the mutation gate.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.screeners.gainers_losers import (
    InstrumentRef,
    LakeReader,
    Mover,
    ScreenResult,
    _build_mover,
    rank_movers,
    screen_gainers_losers,
)

from tests.fakes.screener_lake import T0, FakeLakeReader, make_bar


def _ref(symbol: str, *, exchange: str = "NSE", segment: str = "INDEX") -> InstrumentRef:
    """Build an :class:`InstrumentRef` with sensible defaults."""
    return InstrumentRef(symbol=symbol, exchange=exchange, segment=segment)


def _mover(
    symbol: str,
    pct: str,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
) -> Mover:
    """Build a :class:`Mover` whose ``pct_change`` is fixed for ranking tests."""
    return Mover(
        ref=_ref(symbol, exchange=exchange, segment=segment),
        base_price=Decimal("100.00"),
        last_price=Decimal("100.00"),
        pct_change=Decimal(pct),
        bar_count=2,
    )


# ────────────────────────────────────────────────────────────────────────
# InstrumentRef
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentRef:
    def test_sort_key_is_the_triple(self) -> None:
        assert _ref("NIFTY").sort_key == ("NIFTY", "NSE", "INDEX")

    def test_is_frozen(self) -> None:
        ref = _ref("NIFTY")
        with pytest.raises(AttributeError):
            ref.symbol = "X"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# LakeReader protocol (runtime_checkable)
# ────────────────────────────────────────────────────────────────────────


class TestLakeReaderProtocol:
    def test_fake_satisfies_protocol(self) -> None:
        reader = FakeLakeReader(data={})
        assert isinstance(reader, LakeReader)

    def test_incomplete_object_is_not_a_lakereader(self) -> None:
        class _Partial:
            def list_instruments(self) -> list[InstrumentRef]:
                return []

        # Missing read_window → runtime_checkable rejects it.
        assert not isinstance(_Partial(), LakeReader)


# ────────────────────────────────────────────────────────────────────────
# _build_mover — the O(1) per-instrument reduction
# ────────────────────────────────────────────────────────────────────────


class TestBuildMover:
    def test_empty_window_is_skipped(self) -> None:
        assert _build_mover(_ref("NIFTY"), []) is None

    def test_zero_base_price_is_skipped(self) -> None:
        # A sub-paisa open snaps to 0.00 → % change undefined → None.
        bar = make_bar(minute=0, open_="0.004", close="0.004", high="0.005", low="0.001")
        assert _build_mover(_ref("PENNY"), [bar]) is None

    def test_single_bar_uses_open_to_close(self) -> None:
        bar = make_bar(minute=0, open_="100.00", close="110.00")
        mover = _build_mover(_ref("NIFTY"), [bar])
        assert mover is not None
        assert mover.base_price == Decimal("100.00")
        assert mover.last_price == Decimal("110.00")
        assert mover.pct_change == Decimal("10")
        assert mover.bar_count == 1

    def test_multi_bar_uses_first_open_and_last_close(self) -> None:
        bars = [
            make_bar(minute=0, open_="100.00", close="105.00"),
            make_bar(minute=1, open_="105.00", close="120.00"),
        ]
        mover = _build_mover(_ref("NIFTY"), bars)
        assert mover is not None
        assert mover.base_price == Decimal("100.00")  # first OPEN
        assert mover.last_price == Decimal("120.00")  # last CLOSE
        assert mover.pct_change == Decimal("20")
        assert mover.bar_count == 2

    def test_prices_are_snapped_to_two_decimals(self) -> None:
        # 100.005 snaps half-up to 100.01; 100.004 snaps to 100.00.
        bar = make_bar(minute=0, open_="100.004", close="100.005", high="101", low="100")
        mover = _build_mover(_ref("NIFTY"), [bar])
        assert mover is not None
        assert mover.base_price == Decimal("100.00")
        assert mover.last_price == Decimal("100.01")


# ────────────────────────────────────────────────────────────────────────
# rank_movers — the pure ranking core
# ────────────────────────────────────────────────────────────────────────


class TestRankMovers:
    def test_top_n_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            rank_movers([], top_n=0)

    def test_empty_input(self) -> None:
        gainers, losers = rank_movers([], top_n=3)
        assert gainers == ()
        assert losers == ()

    def test_gainers_descending_losers_ascending(self) -> None:
        movers = [
            _mover("A", "1"),
            _mover("B", "5"),
            _mover("C", "-2"),
            _mover("D", "-8"),
        ]
        gainers, losers = rank_movers(movers, top_n=5)
        assert [m.ref.symbol for m in gainers] == ["B", "A"]  # +5 then +1
        assert [m.ref.symbol for m in losers] == ["D", "C"]  # -8 then -2

    def test_flat_movers_excluded_from_both(self) -> None:
        gainers, losers = rank_movers([_mover("FLAT", "0")], top_n=3)
        assert gainers == ()
        assert losers == ()

    def test_top_n_caps_each_side_independently(self) -> None:
        movers = [
            _mover("G1", "3"),
            _mover("G2", "2"),
            _mover("G3", "1"),
            _mover("L1", "-3"),
            _mover("L2", "-2"),
        ]
        gainers, losers = rank_movers(movers, top_n=2)
        assert [m.ref.symbol for m in gainers] == ["G1", "G2"]
        assert [m.ref.symbol for m in losers] == ["L1", "L2"]

    def test_equal_pct_change_breaks_tie_by_symbol(self) -> None:
        movers = [_mover("ZED", "4"), _mover("ACE", "4")]
        gainers, _ = rank_movers(movers, top_n=5)
        assert [m.ref.symbol for m in gainers] == ["ACE", "ZED"]

    def test_tiebreak_falls_through_to_exchange_then_segment(self) -> None:
        # Same symbol + pct → exchange decides; same symbol+exchange → segment.
        movers = [
            _mover("DUP", "4", exchange="NSE", segment="FNO"),
            _mover("DUP", "4", exchange="NSE", segment="CASH"),
            _mover("DUP", "4", exchange="BSE", segment="INDEX"),
        ]
        gainers, _ = rank_movers(movers, top_n=5)
        assert [(m.ref.exchange, m.ref.segment) for m in gainers] == [
            ("BSE", "INDEX"),  # exchange BSE < NSE
            ("NSE", "CASH"),  # within NSE, segment CASH < FNO
            ("NSE", "FNO"),
        ]

    def test_loser_tiebreak_by_symbol(self) -> None:
        movers = [_mover("ZED", "-4"), _mover("ACE", "-4")]
        _, losers = rank_movers(movers, top_n=5)
        assert [m.ref.symbol for m in losers] == ["ACE", "ZED"]

    def test_accepts_any_iterable(self) -> None:
        gainers, losers = rank_movers(iter([_mover("A", "1")]), top_n=1)
        assert [m.ref.symbol for m in gainers] == ["A"]
        assert losers == ()


# ────────────────────────────────────────────────────────────────────────
# screen_gainers_losers — the injected-reader driver
# ────────────────────────────────────────────────────────────────────────

_WINDOW_START = T0 - timedelta(days=1)
_WINDOW_END = T0 + timedelta(days=1)


def _screen(reader: FakeLakeReader, *, top_n: int = 3) -> ScreenResult:
    """Run the screen over the standard test window."""
    return screen_gainers_losers(
        reader,
        interval="1day",
        start=_WINDOW_START,
        end=_WINDOW_END,
        top_n=top_n,
    )


class TestScreenGainersLosers:
    def test_top_n_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="top_n must be >= 1"):
            _screen(FakeLakeReader(data={}), top_n=0)

    def test_start_after_end_raises(self) -> None:
        with pytest.raises(ValueError, match="must be <="):
            screen_gainers_losers(
                FakeLakeReader(data={}),
                interval="1day",
                start=_WINDOW_END,
                end=_WINDOW_START,
                top_n=3,
            )

    def test_ranks_gainers_and_losers_from_lake(self) -> None:
        reader = FakeLakeReader(
            data={
                _ref("UP"): [make_bar(minute=0, open_="100.00", close="115.00")],
                _ref("DOWN"): [make_bar(minute=0, open_="100.00", close="88.00")],
                _ref("FLAT"): [make_bar(minute=0, open_="100.00", close="100.00")],
            }
        )
        result = _screen(reader)
        assert [m.ref.symbol for m in result.gainers] == ["UP"]
        assert [m.ref.symbol for m in result.losers] == ["DOWN"]
        assert result.skipped == ()  # FLAT is ranked-out, not skipped

    def test_instruments_without_window_data_are_skipped(self) -> None:
        reader = FakeLakeReader(
            data={
                _ref("HAS"): [make_bar(minute=0, open_="100.00", close="110.00")],
                _ref("EMPTY"): [],
            }
        )
        result = _screen(reader)
        assert [m.ref.symbol for m in result.gainers] == ["HAS"]
        assert result.skipped == (_ref("EMPTY"),)

    def test_bars_outside_the_window_are_excluded(self) -> None:
        # The only bar sits a year past the window end → no usable data → skip.
        far = make_bar(minute=0, open_="100.00", close="200.00")
        reader = FakeLakeReader(data={_ref("FAR"): [far]})
        result = screen_gainers_losers(
            reader,
            interval="1day",
            start=datetime(2020, 1, 1, tzinfo=UTC),
            end=datetime(2020, 1, 2, tzinfo=UTC),
            top_n=3,
        )
        assert result.gainers == ()
        assert result.losers == ()
        assert result.skipped == (_ref("FAR"),)

    def test_skipped_is_sorted_by_ref_key(self) -> None:
        reader = FakeLakeReader(
            data={
                _ref("ZED"): [],
                _ref("ACE"): [],
                _ref("MID"): [],
            }
        )
        result = _screen(reader)
        assert [r.symbol for r in result.skipped] == ["ACE", "MID", "ZED"]

    def test_window_and_interval_are_propagated_to_the_reader(self) -> None:
        ref = _ref("NIFTY")
        reader = FakeLakeReader(data={ref: [make_bar(minute=0, open_="1.00", close="2.00")]})
        _screen(reader)
        assert reader.calls == [(ref, "1day", _WINDOW_START, _WINDOW_END)]

    def test_top_n_caps_the_result(self) -> None:
        reader = FakeLakeReader(
            data={
                _ref("G1"): [make_bar(minute=0, open_="100.00", close="130.00")],
                _ref("G2"): [make_bar(minute=0, open_="100.00", close="120.00")],
                _ref("G3"): [make_bar(minute=0, open_="100.00", close="110.00")],
            }
        )
        result = _screen(reader, top_n=2)
        assert [m.ref.symbol for m in result.gainers] == ["G1", "G2"]

    def test_empty_lake_yields_empty_result(self) -> None:
        result = _screen(FakeLakeReader(data={}))
        assert result == ScreenResult(gainers=(), losers=(), skipped=())
