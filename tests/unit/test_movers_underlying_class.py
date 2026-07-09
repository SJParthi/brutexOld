"""Unit tests for the ``/movers`` underlying-class filter (DEC-MOVERS-UNDERLYING-CLASS-001).

Covers the pure COMMON module
:mod:`brutex.dashboard.movers_underlying_class` (normalisation, segment/kind
resolution, the stored-master kind map, the predicate + rank-time filter, and the
note/echo helpers) AND the ``build_*_payload`` rank-time application in
:mod:`~brutex.dashboard.movers_snapshot` /
:mod:`~brutex.dashboard.movers_futures` /
:mod:`~brutex.dashboard.movers_options` — including the hard invariant that the
DEFAULT (``all`` / omitted) class is BYTE-IDENTICAL to the pre-change payload.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from brutex.dashboard.movers_underlying_class import (
    CLASS_ALL,
    CLASS_INDICES,
    CLASS_STOCKS,
    KIND_INDEX,
    KIND_STOCK,
    KIND_UNKNOWN,
    build_underlying_kind_map,
    candidate_passes_class,
    class_echo,
    class_note_suffix,
    filter_by_underlying_class,
    fno_kind_from_underlying,
    normalize_underlying_class,
    spot_kind_from_segment,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _inst(symbol: str, *, kind: InstrumentType, segment: Segment) -> Instrument:
    """A minimal index / equity master row for the kind-map builder."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


# ── normalize_underlying_class ────────────────────────────────────────────


class TestNormalizeUnderlyingClass:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, CLASS_ALL),
            ("", CLASS_ALL),
            ("All", CLASS_ALL),
            ("all", CLASS_ALL),
            ("Indices", CLASS_INDICES),
            ("INDICES", CLASS_INDICES),
            ("Stocks", CLASS_STOCKS),
            ("junk", CLASS_ALL),
        ],
    )
    def test_normalises(self, raw: str | None, expected: str) -> None:
        assert normalize_underlying_class(raw) == expected


# ── spot_kind_from_segment ────────────────────────────────────────────────


class TestSpotKindFromSegment:
    def test_index_segment(self) -> None:
        assert spot_kind_from_segment("INDEX") == KIND_INDEX

    def test_cash_segment(self) -> None:
        assert spot_kind_from_segment("CASH") == KIND_STOCK

    def test_other_segment_unknown(self) -> None:
        assert spot_kind_from_segment("FNO") == KIND_UNKNOWN
        assert spot_kind_from_segment("") == KIND_UNKNOWN


# ── build_underlying_kind_map ─────────────────────────────────────────────


class TestBuildUnderlyingKindMap:
    def test_index_and_stock(self) -> None:
        kinds = build_underlying_kind_map(
            [
                _inst("NIFTY", kind=InstrumentType.IDX, segment=Segment.CASH),
                _inst("RELIANCE", kind=InstrumentType.EQ, segment=Segment.CASH),
            ]
        )
        assert kinds["NIFTY"] == KIND_INDEX
        assert kinds["RELIANCE"] == KIND_STOCK

    def test_hyphenated_stock_keys_on_primary_form(self) -> None:
        # primary_fno_underlying("BAJAJ-AUTO") == "BAJAJAUTO" — the map key must be
        # the F&O form the contract symbol carries (never the raw spot spelling).
        kinds = build_underlying_kind_map(
            [_inst("BAJAJ-AUTO", kind=InstrumentType.EQ, segment=Segment.CASH)]
        )
        assert kinds == {"BAJAJAUTO": KIND_STOCK}

    def test_first_wins_on_key_collision(self) -> None:
        # Two spot spellings collapsing to one F&O primary — first-wins, never a
        # later overwrite (mirrors the pull-side de-dup).
        kinds = build_underlying_kind_map(
            [
                _inst("NIFTY", kind=InstrumentType.IDX, segment=Segment.CASH),
                _inst("NIFTY", kind=InstrumentType.EQ, segment=Segment.CASH),
            ]
        )
        assert kinds == {"NIFTY": KIND_INDEX}

    def test_empty(self) -> None:
        assert build_underlying_kind_map([]) == {}


# ── fno_kind_from_underlying ──────────────────────────────────────────────


class TestFnoKindFromUnderlying:
    def test_none_map_is_unknown(self) -> None:
        assert fno_kind_from_underlying("NIFTY", None) == KIND_UNKNOWN

    def test_hit(self) -> None:
        assert fno_kind_from_underlying("NIFTY", {"NIFTY": KIND_INDEX}) == KIND_INDEX

    def test_miss_is_unknown(self) -> None:
        assert fno_kind_from_underlying("FINNIFTY", {"NIFTY": KIND_INDEX}) == KIND_UNKNOWN


# ── candidate_passes_class ────────────────────────────────────────────────


class TestCandidatePassesClass:
    @pytest.mark.parametrize(
        ("kind", "wanted", "expected"),
        [
            (KIND_INDEX, CLASS_ALL, True),
            (KIND_STOCK, CLASS_ALL, True),
            (KIND_UNKNOWN, CLASS_ALL, True),
            (KIND_INDEX, CLASS_INDICES, True),
            (KIND_STOCK, CLASS_INDICES, False),
            (KIND_UNKNOWN, CLASS_INDICES, False),
            (KIND_STOCK, CLASS_STOCKS, True),
            (KIND_INDEX, CLASS_STOCKS, False),
            (KIND_UNKNOWN, CLASS_STOCKS, False),
        ],
    )
    def test_predicate(self, kind: str, wanted: str, expected: bool) -> None:
        assert candidate_passes_class(kind, wanted) is expected


# ── filter_by_underlying_class ────────────────────────────────────────────


class _Cand:
    """A tiny candidate carrying only ``underlying_kind`` for the filter test."""

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.underlying_kind = kind


class TestFilterByUnderlyingClass:
    def _items(self) -> list[_Cand]:
        return [
            _Cand("NIFTY", KIND_INDEX),
            _Cand("RELIANCE", KIND_STOCK),
            _Cand("FINNIFTY", KIND_UNKNOWN),
        ]

    def test_all_returns_unchanged_zero_excluded(self) -> None:
        items = self._items()
        kept, excluded = filter_by_underlying_class(items, CLASS_ALL)
        assert [c.name for c in kept] == ["NIFTY", "RELIANCE", "FINNIFTY"]
        assert excluded == 0

    def test_indices_filters_and_counts_unclassified(self) -> None:
        kept, excluded = filter_by_underlying_class(self._items(), CLASS_INDICES)
        assert [c.name for c in kept] == ["NIFTY"]
        assert excluded == 1  # the KIND_UNKNOWN FINNIFTY was excluded + counted

    def test_stocks_filters_and_counts_unclassified(self) -> None:
        kept, excluded = filter_by_underlying_class(self._items(), CLASS_STOCKS)
        assert [c.name for c in kept] == ["RELIANCE"]
        assert excluded == 1


# ── class_note_suffix / class_echo ────────────────────────────────────────


class TestClassNoteAndEcho:
    def test_note_all_empty(self) -> None:
        assert class_note_suffix(CLASS_ALL, 0) == ""

    def test_note_indices_no_excluded(self) -> None:
        assert class_note_suffix(CLASS_INDICES, 0) == " Class: Indices."

    def test_note_stocks_with_excluded(self) -> None:
        assert class_note_suffix(CLASS_STOCKS, 3) == " Class: Stocks (3 unclassified excluded)."

    def test_echo_all_empty(self) -> None:
        assert class_echo(CLASS_ALL, 0) == {}

    def test_echo_non_all(self) -> None:
        assert class_echo(CLASS_INDICES, 2) == {
            "underlying_class": CLASS_INDICES,
            "excluded_unclassified": 2,
        }


# ── build_*_payload rank-time filter (default byte-identity + filtering) ───


def _spot_snaps() -> tuple:
    from brutex.dashboard.movers_snapshot import MoverSnapshot, ScreenInstrument

    def _snap(symbol: str, segment: str, kind: str, pct: str) -> MoverSnapshot:
        return MoverSnapshot(
            instrument=ScreenInstrument(
                symbol=symbol, exchange="NSE", segment=segment, display_name=symbol
            ),
            ltp=Decimal("100"),
            prev_close=Decimal("90"),
            change=Decimal("10"),
            pct_change=Decimal(pct),
            volume=100,
            value_paisa=1_000,
            bar_count=1,
            underlying_kind=kind,
        )

    return (
        _snap("NIFTY", "INDEX", KIND_INDEX, "5"),
        _snap("RELIANCE", "CASH", KIND_STOCK, "3"),
        _snap("FINNIFTY", "INDEX", KIND_UNKNOWN, "4"),
    )


class TestBuildSnapshotPayloadClass:
    def _payload(self, underlying_class: str | None) -> dict:
        from brutex.dashboard.movers_snapshot import build_snapshot_payload

        return build_snapshot_payload(
            _spot_snaps(),
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            underlying_class=underlying_class,
        )

    def test_default_omits_class_keys(self) -> None:
        for arg in (None, "All"):
            payload = self._payload(arg)
            assert "underlying_class" not in payload
            assert "excluded_unclassified" not in payload
            assert "Class:" not in payload["note"]
            # All three underlyings ranked (nothing filtered).
            assert {r["symbol"] for r in payload["price_gainers"]} == {
                "NIFTY",
                "RELIANCE",
                "FINNIFTY",
            }

    def test_indices_filters_echoes_and_notes(self) -> None:
        payload = self._payload("Indices")
        assert payload["underlying_class"] == CLASS_INDICES
        assert payload["excluded_unclassified"] == 1  # FINNIFTY (unknown) excluded
        assert {r["symbol"] for r in payload["price_gainers"]} == {"NIFTY"}
        assert " Class: Indices (1 unclassified excluded)." in payload["note"]

    def test_stocks_filters(self) -> None:
        payload = self._payload("Stocks")
        assert {r["symbol"] for r in payload["price_gainers"]} == {"RELIANCE"}
        assert payload["underlying_class"] == CLASS_STOCKS

    def test_class_that_empties_set_keeps_lake_population_note(self) -> None:
        # Stocks over an all-index/unknown set → empty ranked, but the lake is NOT
        # empty, so the note must NOT read "no spot data in the lake yet".
        from brutex.dashboard.movers_snapshot import build_snapshot_payload

        snaps = tuple(s for s in _spot_snaps() if s.underlying_kind != KIND_STOCK)
        payload = build_snapshot_payload(
            snaps,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            underlying_class="Stocks",
        )
        assert "No spot data in the lake yet" not in payload["note"]
        assert all(payload[c] == [] for c in ("price_gainers", "volume", "value"))


def _future(underlying: str, kind: str, pct: str) -> object:
    from brutex.dashboard.movers_futures import FuturesMover

    return FuturesMover(
        underlying=underlying,
        expiry=date(2024, 6, 27),
        symbol=f"NSE-{underlying}-27Jun24-FUT",
        exchange="NSE",
        segment="FNO",
        ltp=Decimal("100"),
        spot=None,
        premium=None,
        pct_change=Decimal(pct),
        oi=10,
        oi_chg_pct=None,
        volume=100,
        value_paisa=1_000,
        underlying_kind=kind,
    )


class TestBuildFuturesPayloadClass:
    def _payload(self, underlying_class: str | None) -> dict:
        from brutex.dashboard.movers_futures import build_futures_payload

        movers = (
            _future("NIFTY", KIND_INDEX, "5"),
            _future("RELIANCE", KIND_STOCK, "3"),
        )
        return build_futures_payload(
            movers,  # type: ignore[arg-type]
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            underlying_class=underlying_class,
        )

    def test_default_omits_class_keys(self) -> None:
        payload = self._payload(None)
        assert "underlying_class" not in payload
        assert "Class:" not in payload["note"]
        assert {r["symbol"] for r in payload["price_gainers"]} == {
            "NSE-NIFTY-27Jun24-FUT",
            "NSE-RELIANCE-27Jun24-FUT",
        }

    def test_indices_filters(self) -> None:
        payload = self._payload("Indices")
        assert {r["symbol"] for r in payload["price_gainers"]} == {"NSE-NIFTY-27Jun24-FUT"}
        assert payload["underlying_class"] == CLASS_INDICES
        assert payload["excluded_unclassified"] == 0


def _option(underlying: str, kind: str, pct: str) -> object:
    from brutex.data.moneyness import Moneyness
    from brutex.dashboard.movers_options import OptionMover

    return OptionMover(
        underlying=underlying,
        expiry=date(2024, 6, 27),
        strike=Decimal("24000"),
        opt_type="CE",
        symbol=f"NSE-{underlying}-27Jun24-24000-CE",
        exchange="NSE",
        segment="FNO",
        ltp=Decimal("100"),
        spot=None,
        pct_change=Decimal(pct),
        oi=10,
        oi_chg=None,
        oi_chg_pct=None,
        volume=100,
        value_paisa=1_000,
        moneyness=Moneyness.UNKNOWN,
        atm_offset=None,
        moneyness_label="?",
        underlying_kind=kind,
    )


class TestBuildOptionsPayloadClass:
    def _payload(self, underlying_class: str | None) -> dict:
        from brutex.dashboard.movers_options import build_options_payload

        movers = (
            _option("NIFTY", KIND_INDEX, "5"),
            _option("RELIANCE", KIND_STOCK, "3"),
        )
        return build_options_payload(
            movers,  # type: ignore[arg-type]
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            underlying_class=underlying_class,
        )

    def test_default_omits_class_keys(self) -> None:
        payload = self._payload(None)
        assert "underlying_class" not in payload
        assert "Class:" not in payload["note"]

    def test_stocks_filters_echoes(self) -> None:
        payload = self._payload("Stocks")
        assert {r["symbol"] for r in payload["price_gainers"]} == {"NSE-RELIANCE-27Jun24-24000-CE"}
        assert payload["underlying_class"] == CLASS_STOCKS

    def test_class_empty_lake_note(self) -> None:
        # A truly-empty options lake with a class filter still reads the honest
        # "pull the F&O options first" note (keyed on the unfiltered population),
        # with the class suffix appended.
        from brutex.dashboard.movers_options import build_options_payload

        payload = build_options_payload(
            (),
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            underlying_class="Indices",
        )
        assert "pull the F&O" in payload["note"]
        assert " Class: Indices." in payload["note"]
