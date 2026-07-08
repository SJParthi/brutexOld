"""Shared WP4 twin-detector test fixtures (DEC-SWEEP-RESULTS-DEPTH-001).

One factory for building valid :class:`~brutex.engine.trade_capture.CapturedTrade`
instances with only the behavior-identity fields varying, plus a deterministic
valid-fingerprint generator for hand-built :class:`TwinCandidate` pools. Used
by the WP4 unit / property / golden suites — a NEW uniquely-named fake module
per the parallel-build discipline (no edits to existing fakes).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import blake3
from brutex.calendars.nse import IST
from brutex.engine.minute_path import PathStatus
from brutex.engine.trade_capture import CapturedTrade
from brutex.strategies.types import Side

__all__ = ["BASE_TS_UTC", "fingerprint_seed", "ist_ts", "make_captured_trade"]

_DAY = date(2024, 1, 2)  # a regular NSE Tuesday

#: 2024-01-02 09:16 IST expressed in UTC — the canonical fixture anchor
#: (mirrors the WP1 test fixtures' choice of day + minute grid).
BASE_TS_UTC = datetime(_DAY.year, _DAY.month, _DAY.day, 9, 16, tzinfo=IST).astimezone(UTC)


def ist_ts(hh: int, mm: int) -> datetime:
    """The fixture day's ``hh:mm`` IST as a tz-aware UTC datetime."""
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def make_captured_trade(
    *,
    entry_ts: datetime | None = None,
    exit_ts: datetime | None = None,
    entry_minute: int = 0,
    hold_minutes: int = 5,
    direction: Side = Side.BUY,
    pnl_paisa: int = 100,
    entry_px: Decimal = Decimal("100.00"),
    exit_px: Decimal = Decimal("101.00"),
    exit_reason: str = "signal",
    mae_paisa: int = 0,
    mfe_paisa: int = 0,
    vix_entry: Decimal | None = None,
    vix_exit: Decimal | None = None,
) -> CapturedTrade:
    """Build a valid CapturedTrade, defaulting every non-identity field.

    ``entry_ts`` / ``exit_ts`` win when given; otherwise the trade sits at
    ``BASE_TS_UTC + entry_minute`` and holds ``hold_minutes``. The 1-minute
    path fields are pinned to the honest ``NO_1M_DATA`` shape (both ``None``)
    so the factory never fabricates path data the fixture does not have.
    """
    entry = BASE_TS_UTC + timedelta(minutes=entry_minute) if entry_ts is None else entry_ts
    exit_ = entry + timedelta(minutes=hold_minutes) if exit_ts is None else exit_ts
    return CapturedTrade(
        entry_ts=entry,
        entry_px=entry_px,
        exit_ts=exit_,
        exit_px=exit_px,
        direction=direction,
        lots=1,
        exit_reason=exit_reason,
        pnl_paisa=pnl_paisa,
        costs_paisa=0,
        mae_paisa=mae_paisa,
        mfe_paisa=mfe_paisa,
        mae_1m_paisa=None,
        mfe_1m_paisa=None,
        path_1m_status=PathStatus.NO_1M_DATA,
        vix_entry=vix_entry,
        vix_exit=vix_exit,
    )


def fingerprint_seed(seed: int) -> str:
    """A deterministic VALID 64-hex fingerprint for hand-built candidates.

    Distinct seeds yield distinct digests (blake3 of the seed's own
    serialization), so tests can force tier-1 hits/misses without building a
    whole trade list per candidate.
    """
    return str(blake3.blake3(f"wp4-fixture-seed|{seed}".encode()).hexdigest())
