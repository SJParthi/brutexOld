"""Golden contract for the NSE/BSE 1-day cross-check report (DEC-NSE-1DAY-VALIDATION-001).

Byte-pins :func:`~brutex.data.validation.nse_oneday.render_report` over a frozen
synthetic scenario covering all three verdict statuses (MATCHED / MISMATCHED /
UNAVAILABLE) so the operator-facing report format, the 2-dp snap, the IST-date
bar selection, and the signed-paisa delta rendering cannot silently drift. The
fixtures are HAND-AUTHORED (NOT real OHLC) — Golden Rule #1: this harness never
hardcodes real market prices.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars import IST
from brutex.data.models import CandleBar
from brutex.data.validation.nse_oneday import (
    IndexSource,
    render_report,
    validate_brute_force_indices,
)

pytestmark = pytest.mark.golden

_DATE = date(2025, 9, 24)

# Synthetic official CSV (default NSE column labels) — NIFTY + BANKNIFTY rows.
_OFFICIAL_CSV = (
    "Index Name,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
    "Nifty 50,25000.10,25100.20,24900.05,25050.75\n"
    "Nifty Bank,54000.50,54250.90,53800.10,54100.25\n"
)

# Synthetic Groww OHLC: NIFTY matches official exactly; BANKNIFTY's close is +0.10.
_GROWW: dict[str, tuple[str, str, str, str]] = {
    "NIFTY": ("25000.10", "25100.20", "24900.05", "25050.75"),
    "BANKNIFTY": ("54000.50", "54250.90", "53800.10", "54100.35"),
}

_EXPECTED = """NSE/BSE 1-day OHLC cross-check — 2025-09-24 IST
  matched=1 mismatched=1 unavailable=1

[MATCHED] NIFTY
    open  groww=25000.10 official=25000.10 Δ=0.00 (+0p) [ok]
    high  groww=25100.20 official=25100.20 Δ=0.00 (+0p) [ok]
    low   groww=24900.05 official=24900.05 Δ=0.00 (+0p) [ok]
    close groww=25050.75 official=25050.75 Δ=0.00 (+0p) [ok]
[MISMATCHED] BANKNIFTY
    open  groww=54000.50 official=54000.50 Δ=0.00 (+0p) [ok]
    high  groww=54250.90 official=54250.90 Δ=0.00 (+0p) [ok]
    low   groww=53800.10 official=53800.10 Δ=0.00 (+0p) [ok]
    close groww=54100.35 official=54100.25 Δ=0.10 (+10p) [DIFF]
    note: 1 field(s) [close] exceed tolerance ₹0.01; max |Δ|=10 paisa (tolerance is UNVERIFIED — see module docs)
[UNAVAILABLE] SENSEX
    note: no official source URL for BSE-SENSEX — set BRUTEX_NSE_ONEDAY_SOURCES (e.g. BSE-SENSEX needs an operator-verified URL)"""


def _bar(open_: str, high: str, low: str, close: str) -> CandleBar:
    ts = datetime(2025, 9, 24, 9, 15, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


def _fake_groww(source: IndexSource, _trade_date: date) -> list[CandleBar]:
    if source.engine_symbol not in _GROWW:
        return []
    return [_bar(*_GROWW[source.engine_symbol])]


def test_render_report_matches_golden() -> None:
    report = validate_brute_force_indices(
        _DATE, groww_fetcher=_fake_groww, official_opener=lambda _url: _OFFICIAL_CSV
    )
    assert render_report(report) == _EXPECTED
