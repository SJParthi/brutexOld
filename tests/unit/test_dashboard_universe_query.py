"""Unit tests for :mod:`brutex.dashboard.universe_query`.

Pure-Python — no FastAPI, no DuckDB, no network. Builds
:class:`~brutex.models.Instrument` fixtures and a lightweight fake
snapshot (any iterable of instruments) and asserts the classification +
summary + listing logic at 100% line + branch coverage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.universe_query import (
    EXPLORER_KINDS,
    KIND_INDEX,
    KIND_STOCK,
    classify_instrument,
    list_underlyings,
    universe_summary,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import build_fno_eligible_underlyings

# ────────────────────────────────────────────────────────────────────────
# Fixture builders
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _full_scope_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration law).

    This suite pins the pre-#1934 admission MECHANICS over stock/sectoral
    fixtures; under ``BRUTEX_FEED_SCOPE=full`` those legacy rules are
    byte-identical (the parity half of the §6 widen-path pin — running this
    suite full-arm IS that pin's permanent net). The shipped index4-default
    guard arms live in ``test_universe_index_only_scope.py`` + the
    scope-gate batteries (migration Law 1)."""
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")


def _index(
    symbol: str, *, exchange: Exchange = Exchange.NSE, name: str | None = None
) -> Instrument:
    """Build an IDX instrument fixture."""
    return Instrument(
        exch_id=exchange,
        security_id=1,
        symbol_name=symbol,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
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


def _equity(symbol: str, *, name: str | None = None, security_id: int = 2) -> Instrument:
    """Build an NSE EQ instrument fixture."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=name,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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


def _option(
    underlying: str, *, strike: str = "100", side: InstrumentType = InstrumentType.CE
) -> Instrument:
    """Build an NSE option leg fixture naming *underlying*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=3,
        symbol_name=f"{underlying}25JAN{strike}{side}",
        display_name=None,
        instrument=side,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=2,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal(strike),
        asm_gsm_flag=False,
    )


# ────────────────────────────────────────────────────────────────────────
# classify_instrument
# ────────────────────────────────────────────────────────────────────────


class TestClassifyInstrument:
    def test_index_is_index(self) -> None:
        eligible = frozenset()
        assert classify_instrument(_index("NIFTY"), eligible) == KIND_INDEX

    def test_non_fno_sectoral_index_is_index(self) -> None:
        # A non-F&O / non-brute sectoral index (NIFTY IT) is still an IDX
        # instrument → classifies as "index". Locks the all-NSE-indices
        # behaviour (Refinement B): the Index tab is NOT limited to the 3
        # F&O / brute-force indices.
        eligible = frozenset()
        assert classify_instrument(_index("NIFTYIT"), eligible) == KIND_INDEX

    def test_fno_eligible_equity_is_stock(self) -> None:
        instruments = [_equity("RELIANCE"), _option("RELIANCE")]
        eligible = build_fno_eligible_underlyings(instruments)
        assert classify_instrument(_equity("RELIANCE"), eligible) == KIND_STOCK

    def test_non_fno_equity_is_none(self) -> None:
        # No derivative names WIPRO → not F&O-eligible → unclassified.
        eligible = build_fno_eligible_underlyings([_equity("WIPRO")])
        assert classify_instrument(_equity("WIPRO"), eligible) is None

    def test_derivative_leg_is_none(self) -> None:
        instruments = [_equity("RELIANCE"), _option("RELIANCE")]
        eligible = build_fno_eligible_underlyings(instruments)
        assert classify_instrument(_option("RELIANCE"), eligible) is None

    def test_bse_non_sensex_index_is_none(self) -> None:
        # Now that classify delegates to is_universe_underlying
        # (DEC-INSTRUMENT-UNIVERSE-FAST-001), a BSE non-SENSEX index is NOT a
        # universe underlying → unclassified (it is never persisted anyway).
        eligible = frozenset()
        assert classify_instrument(_index("BSE500", exchange=Exchange.BSE), eligible) is None

    def test_bse_sensex_index_is_index(self) -> None:
        eligible = frozenset()
        assert classify_instrument(_index("SENSEX", exchange=Exchange.BSE), eligible) == KIND_INDEX


# ────────────────────────────────────────────────────────────────────────
# universe_summary
# ────────────────────────────────────────────────────────────────────────


class TestUniverseSummary:
    def test_counts_index_and_stock(self) -> None:
        snapshot = [
            _index("NIFTY"),
            _index("BANKNIFTY"),
            _equity("RELIANCE"),
            _option("RELIANCE"),  # derivative — not counted
            _equity("WIPRO"),  # no derivative → not F&O-eligible, not counted
        ]
        summary = universe_summary(snapshot)
        assert summary == {
            "index_count": 2,
            "stock_count": 1,
            "total": 3,
            "reference_stock_count": 0,
            "stock_count_total": 1,
        }

    def test_empty_snapshot(self) -> None:
        assert universe_summary([]) == {
            "index_count": 0,
            "stock_count": 0,
            "total": 0,
            "reference_stock_count": 0,
            "stock_count_total": 0,
        }

    def test_precomputed_fno_eligible_candidates_only(self) -> None:
        # Fast path: candidates-only (IDX + EQ, no legs) + precomputed eligible
        # set still tallies the stock correctly (DEC-INSTRUMENT-UNIVERSE-FAST-001).
        candidates = [_index("NIFTY"), _index("BANKNIFTY"), _equity("RELIANCE")]
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        assert universe_summary(candidates, fno_eligible=eligible) == {
            "index_count": 2,
            "stock_count": 1,
            "total": 3,
            "reference_stock_count": 0,
            "stock_count_total": 1,
        }

    # ── DEC-LABEL-TRUTH-001: the additive reference/total stock split ──

    def test_reference_split_additive_keys_legs_shape(self) -> None:
        """A reference-only EQ (id-admitted, ticker NOT eligible) splits honestly.

        FAILS BEFORE DEC-LABEL-TRUTH-001: ``universe_summary`` had no
        ``reference_only_ids`` parameter and the payload carried neither
        ``reference_stock_count`` nor ``stock_count_total``.
        """
        candidates = [
            _index("NIFTY"),
            _equity("RELIANCE", security_id=2),
            # An admitted NTM REFERENCE_ONLY constituent: symbol_name is the long
            # company name (never in the ticker-keyed eligible set); admitted by id.
            _equity("Some Long Company Name", security_id=7),
        ]
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        summary = universe_summary(
            candidates, fno_eligible=eligible, reference_only_ids=frozenset({7})
        )
        assert summary == {
            "index_count": 1,
            "stock_count": 1,  # legacy semantics — F&O ticker arm only, unchanged
            "total": 2,  # legacy semantics — index_count + stock_count, unchanged
            "reference_stock_count": 1,
            "stock_count_total": 2,  # the Stocks-tab population (F&O + reference)
        }

    def test_reference_split_lean_store_shape_membership_not_classify_diff(self) -> None:
        """The split is by REFERENCE-ID MEMBERSHIP, honest on the LEAN store too.

        On the lean store the SQL-derived eligible set already carries every
        persisted NSE EQ symbol INCLUDING the reference rows
        (``InstrumentStore.fno_eligible_underlyings`` arm (b), default
        ``tradable_only=False``) — so a reference row classifies "stock" by
        TICKER even without the id arm, and a classify-with-vs-without-ids
        DIFF would read a dishonest ``reference_stock_count == 0``. The
        id-membership split still reports the true reference subset.
        """
        candidates = [
            _index("NIFTY"),
            _equity("RELIANCE", security_id=2),
            _equity("Some Long Company Name", security_id=7),
        ]
        # The lean-store shape: BOTH equities' symbols are in the eligible set.
        eligible = frozenset({(Exchange.NSE, "RELIANCE"), (Exchange.NSE, "Some Long Company Name")})
        summary = universe_summary(
            candidates, fno_eligible=eligible, reference_only_ids=frozenset({7})
        )
        assert summary == {
            "index_count": 1,
            "stock_count": 2,  # legacy value on this store shape — byte-identical
            "total": 3,
            "reference_stock_count": 1,  # membership split — NOT the classify diff's 0
            "stock_count_total": 2,
        }

    def test_reference_index_id_never_counts_as_reference_stock(self) -> None:
        """A reference-flagged INDEX (India VIX) never lands in the STOCK split."""
        vix = _index("INDIAVIX")  # security_id=1 per the fixture builder
        summary = universe_summary(
            [vix], fno_eligible=frozenset(), reference_only_ids=frozenset({1})
        )
        assert summary == {
            "index_count": 1,
            "stock_count": 0,
            "total": 1,
            "reference_stock_count": 0,
            "stock_count_total": 0,
        }


# ────────────────────────────────────────────────────────────────────────
# list_underlyings
# ────────────────────────────────────────────────────────────────────────


class TestListUnderlyings:
    def _snapshot(self) -> list[Instrument]:
        return [
            _index("NIFTY", name="NIFTY 50"),
            _index("BANKNIFTY", name="Nifty Bank"),
            _equity("RELIANCE", name="Reliance Industries"),
            _equity("TCS", name="Tata Consultancy"),
            _option("RELIANCE"),
            _option("TCS"),
            _equity("WIPRO", name="Wipro"),  # not F&O-eligible
        ]

    def test_no_filter_returns_index_then_stock_sorted(self) -> None:
        rows = list_underlyings(self._snapshot())
        kinds = [r["kind"] for r in rows]
        # indices come first (EXPLORER_KINDS order), then stocks
        assert kinds == [KIND_INDEX, KIND_INDEX, KIND_STOCK, KIND_STOCK]
        # within a kind, alphabetical by symbol
        assert [r["symbol"] for r in rows] == ["BANKNIFTY", "NIFTY", "RELIANCE", "TCS"]

    def test_filter_index(self) -> None:
        rows = list_underlyings(self._snapshot(), kind=KIND_INDEX)
        assert {r["symbol"] for r in rows} == {"NIFTY", "BANKNIFTY"}
        assert all(r["kind"] == KIND_INDEX for r in rows)

    def test_non_fno_sectoral_index_listed_under_index_kind(self) -> None:
        # Refinement B: a non-F&O sectoral index (NIFTY IT) must appear in
        # the Index tab alongside the brute-force indices — the explorer
        # surfaces ALL NSE indices, not just the 3 F&O ones. NIFTY IT has
        # no derivatives in the snapshot (spot-only), proving non-F&O
        # indices are still listed.
        snapshot = [
            _index("NIFTY", name="NIFTY 50"),
            _index("NIFTYIT", name="NIFTY IT"),
            _equity("WIPRO"),  # non-F&O equity → excluded, not the index
        ]
        rows = list_underlyings(snapshot, kind=KIND_INDEX)
        assert {r["symbol"] for r in rows} == {"NIFTY", "NIFTYIT"}
        assert all(r["kind"] == KIND_INDEX for r in rows)

    def test_rows_carry_contract_economics(self) -> None:
        """Rows surface lot_size / tick_size (str) / freeze_quantity (DEC-DASHBOARD-ECON-COLUMNS-001).

        ``tick_size`` is a Decimal serialised to ``str`` (JSON-safe, exact);
        ``freeze_quantity`` is ``None`` on an index / non-F&O spot row. The values
        are whatever the (already leg-folded at ingest) instrument carries.
        """
        from dataclasses import replace

        # An F&O stock as it is STORED after the leg-economics fold: lot 500,
        # spot tick 0.10 (kept), freeze 15001. The option leg makes it eligible.
        rel = replace(
            _equity("RELIANCE"), lot_size=500, tick_size=Decimal("0.10"), sm_freeze_qty=15001
        )
        rows = list_underlyings([rel, _option("RELIANCE")], kind=KIND_STOCK)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "RELIANCE"
        assert row["lot_size"] == 500
        assert row["tick_size"] == "0.10"  # Decimal -> exact str, never a float
        assert row["freeze_quantity"] == 15001

        # An index surfaces its own (placeholder) economics; freeze is None.
        idx = list_underlyings([_index("NIFTY")], kind=KIND_INDEX)[0]
        assert idx["tick_size"] == "0.05"
        assert idx["freeze_quantity"] is None

    def test_filter_stock(self) -> None:
        rows = list_underlyings(self._snapshot(), kind=KIND_STOCK)
        assert {r["symbol"] for r in rows} == {"RELIANCE", "TCS"}
        assert all(r["kind"] == KIND_STOCK for r in rows)

    def test_name_falls_back_to_symbol(self) -> None:
        rows = list_underlyings([_index("NIFTY", name=None)])
        assert rows[0]["name"] == "NIFTY"

    def test_row_shape(self) -> None:
        inst = _index("NIFTY", name="NIFTY 50")
        rows = list_underlyings([inst])
        assert rows[0] == {
            "symbol": "NIFTY",
            "pull_symbol": inst.symbol_name,
            "name": "NIFTY 50",
            "kind": KIND_INDEX,
            "exchange": "NSE",
            # Contract economics (DEC-DASHBOARD-ECON-COLUMNS-001): the fixture's own
            # values; tick is a Decimal serialised to str, freeze is None on an index.
            "lot_size": 1,
            "tick_size": "0.05",
            "freeze_quantity": None,
            # NIFTY is not reference-only → option chain applies (DEC-OPTIONS-PULL-GUARD-001).
            "option_pullable": True,
        }

    def test_option_pullable_flag(self) -> None:
        # DEC-OPTIONS-PULL-GUARD-001: a reference-only index (id ∈ reference set,
        # not F&O) is NOT option-pullable; an F&O-eligible equity IS.
        ntm = _index("NIFTY TOTAL MKT")  # security_id=1
        rel = _equity("RELIANCE")  # security_id=2
        rows = list_underlyings(
            [ntm, rel],
            fno_eligible=frozenset({(Exchange.NSE, "RELIANCE")}),
            reference_only_ids=frozenset({ntm.security_id}),
        )
        flags = {r["symbol"]: r["option_pullable"] for r in rows}
        assert flags["NIFTY TOTAL MKT"] is False
        assert flags["RELIANCE"] is True

    def test_pull_symbol_is_bare_engine_symbol(self) -> None:
        # ADR-0003: the Dhan-native schema dropped the exchange-prefixed
        # vendor_symbol. ``pull_symbol`` is now the bare engine ``symbol_name``
        # (the spot pull resolves the numeric SECURITY_ID from the instrument's
        # own ``security_id``; the segment from ``exch_id`` + instrument type).
        snapshot = [
            _index("NIFTY", exchange=Exchange.NSE),
            _index("SENSEX", exchange=Exchange.BSE),
            _equity("RELIANCE"),
            _option("RELIANCE"),
        ]
        rows = list_underlyings(snapshot)
        assert rows  # non-empty
        for row in rows:
            # pull_symbol equals the bare display symbol — no exchange prefix.
            assert row["pull_symbol"] == row["symbol"], row
            assert not row["pull_symbol"].startswith(("NSE-", "BSE-")), row

    def test_precomputed_fno_eligible_candidates_only(self) -> None:
        # Fast path (DEC-INSTRUMENT-UNIVERSE-FAST-001): pass a candidates-only
        # snapshot (IDX + EQ, NO derivative legs) plus a precomputed eligible
        # set — RELIANCE must still classify as a stock even though no leg is
        # present to derive eligibility from.
        candidates = [_index("NIFTY", name="NIFTY 50"), _equity("RELIANCE")]
        eligible = frozenset({(Exchange.NSE, "RELIANCE")})
        rows = list_underlyings(candidates, fno_eligible=eligible)
        assert {(r["symbol"], r["kind"]) for r in rows} == {
            ("NIFTY", KIND_INDEX),
            ("RELIANCE", KIND_STOCK),
        }

    def test_precomputed_fno_eligible_empty_drops_equity(self) -> None:
        # With an explicit empty eligible set, an EQ candidate is NOT a stock
        # (the precomputed set is authoritative; no rebuild from the snapshot).
        candidates = [_index("NIFTY"), _equity("RELIANCE"), _option("RELIANCE")]
        rows = list_underlyings(candidates, fno_eligible=frozenset())
        assert {r["symbol"] for r in rows} == {"NIFTY"}

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must be one of"):
            list_underlyings([], kind="bogus")

    def test_explorer_kinds_constant(self) -> None:
        assert EXPLORER_KINDS == (KIND_INDEX, KIND_STOCK)


# ────────────────────────────────────────────────────────────────────────
# Reference-only constituents become tickable "stock" rows
# (DEC-INDEX-CONSTITUENTS-FULL-001)
# ────────────────────────────────────────────────────────────────────────
def test_classify_reference_only_equity_needs_reference_ids() -> None:
    """A non-F&O NTM constituent EQ classifies as "stock" ONLY with reference ids.

    Its security_id is in the reference set but it is NOT F&O-eligible (its
    symbol_name is the long company name, so the ticker key cannot match). Supplying
    ``reference_security_ids`` admits it as "stock"; omitting it preserves the prior
    F&O-only behaviour (dropped → None).
    """
    ref_eq = _equity("RELIANCE INDUSTRIES LTD")  # not in any fno_eligible set
    assert classify_instrument(ref_eq, frozenset()) is None
    assert classify_instrument(ref_eq, frozenset(), frozenset({ref_eq.security_id})) == KIND_STOCK


def test_list_underlyings_includes_reference_stocks_when_ids_passed() -> None:
    """``list_underlyings`` surfaces reference-only stocks (option_pullable False) with ids.

    Without ``reference_only_ids`` the non-F&O constituent is excluded (F&O-only);
    with it the explorer shows + can tick the FULL held universe
    (DEC-INDEX-CONSTITUENTS-FULL-001).
    """
    ref_eq = _equity("RELIANCE INDUSTRIES LTD")
    assert list_underlyings([ref_eq], kind=KIND_STOCK, fno_eligible=frozenset()) == []
    rows = list_underlyings(
        [ref_eq],
        kind=KIND_STOCK,
        fno_eligible=frozenset(),
        reference_only_ids=frozenset({ref_eq.security_id}),
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE INDUSTRIES LTD"
    assert rows[0]["kind"] == KIND_STOCK
    assert rows[0]["option_pullable"] is False
