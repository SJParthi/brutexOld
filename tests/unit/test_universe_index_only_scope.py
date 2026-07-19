"""Admission narrowing — DEC-UNIVERSE-INDEX-ONLY-001 (design §3.1 A1–A4 + §6).

The fails-before regressions (RELIANCE EQ + NIFTYIT IDX admitted at
merge-base → refused at head), the directive-E pins (VIX admission survives;
index-F&O pullability), the widen-path pin (``BRUTEX_FEED_SCOPE=full``
reproduces merge-base admission on the IDX/EQ arms byte-identically; the
FUT/CE/PE arm pins the CONSCIOUS BSE correction instead), and the classify
mirror. Executed manually against a merge-base worktree as the fails-before
proof (recorded in the PR body); these tests are the permanent net.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.config.feed_scope import FEED_SCOPE_ENV
from brutex.dashboard.universe_query import classify_instrument
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import is_in_fetch_scope, is_universe_underlying


def _idx(symbol: str, exchange: Exchange = Exchange.NSE, security_id: int = 0) -> Instrument:
    return Instrument(
        exch_id=exchange,
        security_id=security_id or abs(hash((symbol, exchange))) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _eq(symbol: str, security_id: int = 0) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id or abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _leg(
    underlying: str,
    instrument_type: InstrumentType = InstrumentType.FUT,
    exchange: Exchange = Exchange.NSE,
) -> Instrument:
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash((underlying, instrument_type, exchange))) % 100_000,
        symbol_name=f"{underlying}FUT",
        display_name=underlying,
        instrument=instrument_type,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=None,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2026, 9, 24),
        strike_price=Decimal("100") if instrument_type.value in ("CE", "PE") else None,
        asm_gsm_flag=False,
    )


_FNO_ELIGIBLE = frozenset({(Exchange.NSE, "RELIANCE"), (Exchange.NSE, "BAJAJ-AUTO")})


class TestIndex4AdmissionDefault:
    """The shipped index4 default (the conftest-forced env) — fails-before net."""

    def test_fno_eligible_stock_refused(self) -> None:
        """[FB] RELIANCE EQ: admitted at merge-base → refused at head."""
        assert is_universe_underlying(_eq("RELIANCE"), _FNO_ELIGIBLE) is False

    def test_sectoral_index_refused(self) -> None:
        """[FB] NIFTYIT IDX: admitted at merge-base → refused at head."""
        assert is_universe_underlying(_idx("NIFTYIT"), _FNO_ELIGIBLE) is False

    def test_reference_and_token_join_ids_refused(self) -> None:
        """[FB] The NTM reference-id + token-join EQ arms are dormant under index4."""
        row = _eq("SOME LONG COMPANY NAME", security_id=777)
        assert (
            is_universe_underlying(
                row,
                frozenset(),
                reference_security_ids=frozenset({777}),
            )
            is False
        )
        assert (
            is_universe_underlying(
                row,
                frozenset(),
                fno_eligible_security_ids=frozenset({777}),
            )
            is False
        )

    @pytest.mark.parametrize(
        ("symbol", "exchange"),
        [
            ("NIFTY", Exchange.NSE),
            ("BANKNIFTY", Exchange.NSE),
            ("SENSEX", Exchange.BSE),
            ("INDIAVIX", Exchange.NSE),
        ],
    )
    def test_the_four_admitted(self, symbol: str, exchange: Exchange) -> None:
        """Directive E pin (i): all 4 V1 pairs admitted — INDIAVIX included

        (the C8 VIX-killer guard: an engine-trio-keyed arm would drop it)."""
        assert is_universe_underlying(_idx(symbol, exchange), _FNO_ELIGIBLE) is True

    def test_wrong_exchange_pair_refused(self) -> None:
        """The admitted set is PAIR-keyed: (BSE, NIFTY) / (NSE, SENSEX) refused."""
        assert is_universe_underlying(_idx("NIFTY", Exchange.BSE), _FNO_ELIGIBLE) is False
        assert is_universe_underlying(_idx("SENSEX", Exchange.NSE), _FNO_ELIGIBLE) is False

    def test_derivative_legs_never_universe_rows(self) -> None:
        """The lean-ingest law is scope-independent: legs never persist."""
        assert is_universe_underlying(_leg("NIFTY"), _FNO_ELIGIBLE) is False


class TestFetchScopeIndex4:
    """A2 under the shipped default — the F&O admission arms."""

    @pytest.mark.parametrize(
        ("underlying", "exchange", "admitted"),
        [
            ("NIFTY", Exchange.NSE, True),
            ("BANKNIFTY", Exchange.NSE, True),
            ("SENSEX", Exchange.BSE, True),  # the BSE chain — directive E pin (ii)
            ("SENSEX", Exchange.NSE, False),
            ("RELIANCE", Exchange.NSE, False),  # [FB] stock legs refused
            ("INDIAVIX", Exchange.NSE, False),  # VIX has NO chain
        ],
    )
    def test_derivative_arm(self, underlying: str, exchange: Exchange, admitted: bool) -> None:
        leg = _leg(underlying, InstrumentType.FUT, exchange)
        assert is_in_fetch_scope(leg, _FNO_ELIGIBLE) is admitted

    def test_eq_arm_refused(self) -> None:
        assert is_in_fetch_scope(_eq("RELIANCE"), _FNO_ELIGIBLE) is False

    def test_idx_arm_narrowed(self) -> None:
        assert is_in_fetch_scope(_idx("NIFTY"), _FNO_ELIGIBLE) is True
        assert is_in_fetch_scope(_idx("NIFTYIT"), _FNO_ELIGIBLE) is False

    def test_explicit_scope_kwarg_bypasses_env_resolution(self) -> None:
        """The §2.4 seam: an EXPLICIT ``scope=`` is honored verbatim (the
        ``scope is None`` env-resolution arm is never entered) — the caller's
        injected scope decides, independent of ``BRUTEX_FEED_SCOPE``."""
        from brutex.config.feed_scope import SCOPE_FULL, SCOPE_INDEX4, _scope_for_token

        index4 = _scope_for_token("groww", SCOPE_INDEX4)
        full = _scope_for_token("groww", SCOPE_FULL)
        stock_leg = _leg("RELIANCE", InstrumentType.FUT, Exchange.NSE)
        assert is_in_fetch_scope(stock_leg, _FNO_ELIGIBLE, scope=index4) is False
        assert is_in_fetch_scope(stock_leg, _FNO_ELIGIBLE, scope=full) is True
        assert is_universe_underlying(_idx("NIFTYIT"), _FNO_ELIGIBLE, scope=index4) is False
        assert is_universe_underlying(_idx("NIFTYIT"), _FNO_ELIGIBLE, scope=full) is True


class TestClassifyMirror:
    """A4 — classify_instrument threads the SAME scope law."""

    def test_stock_unclassified_under_default(self) -> None:
        """[FB] classify("RELIANCE") == "stock" at merge-base → None at head."""
        assert classify_instrument(_eq("RELIANCE"), _FNO_ELIGIBLE) is None

    def test_sectoral_index_unclassified_under_default(self) -> None:
        assert classify_instrument(_idx("NIFTYIT"), _FNO_ELIGIBLE) is None

    def test_vix_classifies_index(self) -> None:
        """Directive E pin (i): INDIAVIX stays an admitted, classified index."""
        assert classify_instrument(_idx("INDIAVIX"), _FNO_ELIGIBLE) == "index"

    def test_the_four_classify_index(self) -> None:
        for symbol, exchange in (
            ("NIFTY", Exchange.NSE),
            ("BANKNIFTY", Exchange.NSE),
            ("SENSEX", Exchange.BSE),
            ("INDIAVIX", Exchange.NSE),
        ):
            assert classify_instrument(_idx(symbol, exchange), _FNO_ELIGIBLE) == "index"


class TestWidenPathPin:
    """``BRUTEX_FEED_SCOPE=full`` reproduces merge-base admission (the §6 pin)."""

    @pytest.fixture(autouse=True)
    def _full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")

    def test_idx_arm_legacy(self) -> None:
        """IDX under full: every NSE index; from BSE only SENSEX — byte-identical."""
        assert is_universe_underlying(_idx("NIFTYIT"), _FNO_ELIGIBLE) is True
        assert is_universe_underlying(_idx("NIFTY100"), frozenset()) is True
        assert is_universe_underlying(_idx("SENSEX", Exchange.BSE), frozenset()) is True
        assert is_universe_underlying(_idx("BSEIDX", Exchange.BSE), frozenset()) is False

    def test_eq_arm_legacy(self) -> None:
        """EQ under full: F&O-ticker OR reference-id OR token-join — byte-identical."""
        assert is_universe_underlying(_eq("RELIANCE"), _FNO_ELIGIBLE) is True
        assert is_universe_underlying(_eq("NOTFNO"), _FNO_ELIGIBLE) is False
        row = _eq("SOME LONG NAME", security_id=42)
        assert (
            is_universe_underlying(row, frozenset(), reference_security_ids=frozenset({42})) is True
        )
        assert (
            is_universe_underlying(row, frozenset(), fno_eligible_security_ids=frozenset({42}))
            is True
        )

    def test_classify_legacy(self) -> None:
        assert classify_instrument(_eq("RELIANCE"), _FNO_ELIGIBLE) == "stock"
        assert classify_instrument(_idx("NIFTYIT"), _FNO_ELIGIBLE) == "index"

    def test_fetch_scope_bse_derivative_correction(self) -> None:
        """The A2 CONSCIOUS correction: `full` admits BSE legs too (the pinned

        deviation from the verbatim legacy "no BSE derivatives" clause —
        without it `full` would NOT be a superset of `index4`, which admits
        the (BSE, SENSEX) legs; zero production consumers of the old clause
        outside universe.py, verified in the design)."""
        assert is_in_fetch_scope(_leg("SENSEX", InstrumentType.FUT, Exchange.BSE), frozenset())
        assert is_in_fetch_scope(_leg("ANYTHING", InstrumentType.CE, Exchange.BSE), frozenset())
        # NSE legs stay admitted (verbatim legacy).
        assert is_in_fetch_scope(_leg("RELIANCE", InstrumentType.FUT), frozenset())

    def test_fetch_scope_eq_arm_legacy(self) -> None:
        assert is_in_fetch_scope(_eq("RELIANCE"), _FNO_ELIGIBLE) is True
        assert is_in_fetch_scope(_eq("NOTFNO"), _FNO_ELIGIBLE) is False


class TestPartitionReferenceOnlyPin:
    """Directive E pin (ii): the 4 admitted rows partition tradable=3 + VIX skipped."""

    def test_fno_fanout_partition(self) -> None:
        from brutex.dashboard.composite_pull import (
            _partition_reference_only,  # pyright: ignore[reportPrivateUsage]
        )

        rows = [
            _idx("NIFTY", Exchange.NSE, 1),
            _idx("BANKNIFTY", Exchange.NSE, 2),
            _idx("SENSEX", Exchange.BSE, 3),
            _idx("INDIAVIX", Exchange.NSE, 4),
        ]
        tradable, skipped = _partition_reference_only(rows, frozenset({4}))
        assert [inst.symbol_name for inst in tradable] == ["NIFTY", "BANKNIFTY", "SENSEX"]
        assert (Exchange.BSE, "SENSEX") in {(inst.exch_id, inst.symbol_name) for inst in tradable}
        assert skipped == ["INDIAVIX"]
