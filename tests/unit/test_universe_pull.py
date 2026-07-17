"""Tests for `brutex.dashboard.universe_pull.select_pull_instruments`.

100% line+branch: index vs stock selection (reusing the COMMON classifier),
the derive-vs-injected `fno_eligible` paths, the stable `trading_symbol` sort,
exclusion of derivative legs + non-F&O equities, and the bad-kind guard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.models import Exchange, Instrument, InstrumentType, Segment


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


def _index(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """An NSE/BSE spot index (IDX → classified ``index``)."""
    return Instrument(
        exch_id=exchange,
        security_id=hash(symbol) % 100000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _equity(symbol: str) -> Instrument:
    """An NSE cash equity (EQ → classified ``stock`` iff F&O-eligible)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=f"INE{abs(hash(symbol)) % 10000:04d}A01010",
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=550,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _option_leg(underlying: str) -> Instrument:
    """A CE leg on *underlying* — makes the equity F&O-eligible; itself excluded."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(underlying + "CE") % 100000,
        symbol_name=f"{underlying}25MAR1000CE",
        display_name=None,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=hash(underlying) % 100000,
        lot_size=550,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=7200,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("1000"),
        asm_gsm_flag=False,
    )


def _full_snapshot() -> list[Instrument]:
    """Two indices + one F&O-eligible equity (+ its leg) + one non-F&O equity."""
    return [
        _index("NIFTY"),
        _index("BANKNIFTY"),
        _equity("HDFCBANK"),
        _option_leg("HDFCBANK"),  # makes HDFCBANK F&O-eligible
        _equity("NONFNOCO"),  # no derivative → not eligible → excluded
    ]


def test_selects_only_indices() -> None:
    selected = select_pull_instruments(_full_snapshot(), kind="index")
    assert [i.symbol_name for i in selected] == ["BANKNIFTY", "NIFTY"]  # sorted
    assert all(i.instrument is InstrumentType.IDX for i in selected)


def test_selects_only_fno_eligible_stocks() -> None:
    selected = select_pull_instruments(_full_snapshot(), kind="stock")
    # Only HDFCBANK (has a leg); NONFNOCO is excluded (no derivative).
    assert [i.symbol_name for i in selected] == ["HDFCBANK"]
    assert selected[0].instrument is InstrumentType.EQ


def test_injected_fno_eligible_skips_derivation() -> None:
    # Candidates-only snapshot (no legs) + an injected eligible set — the
    # DEC-INSTRUMENT-UNIVERSE-FAST-001 fast path.
    candidates = [_index("NIFTY"), _equity("HDFCBANK")]
    eligible = frozenset({(Exchange.NSE, "HDFCBANK")})
    selected = select_pull_instruments(candidates, kind="stock", fno_eligible=eligible)
    assert [i.symbol_name for i in selected] == ["HDFCBANK"]


def test_sort_is_stable_alphabetical() -> None:
    snapshot = [_index("SENSEX", exchange=Exchange.BSE), _index("AXISIDX"), _index("NIFTY")]
    selected = select_pull_instruments(snapshot, kind="index")
    assert [i.symbol_name for i in selected] == ["AXISIDX", "NIFTY", "SENSEX"]


def test_empty_snapshot_is_empty() -> None:
    assert select_pull_instruments([], kind="index") == []


def test_bad_kind_raises() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        select_pull_instruments(_full_snapshot(), kind="options")
