"""Direct unit coverage of the pure ``cli._options_signals_from_trades`` seam.

The options-conversion half split out of ``_replay_spot_signals_for_row`` at
the DEC-OPTIONS-EXEC-FEEDBACK-001 change (MIGRATION.md #1990). Its own
docstring states "no lake I/O, directly unit-testable" and it carries NO
``# pragma: no cover`` — yet its only two callers
(``_replay_spot_signals_for_row`` / ``_maybe_run_exec_sims_on_winners``) are
both lake-I/O ``pragma: no cover`` functions, so the function body (cli.py
lines 10320-10327) was uncovered under the whole-package ``--cov=brutex
--cov-fail-under=100`` gate (empty omit list, DEC-COV-CLI-HARNESS-001) AND
the ``test_cli*`` auto-discovery gate. This file honours the docstring's
directly-unit-testable claim: it drives the pure conversion with plain
namespaces + hand-derived spot signals, covering BOTH the spot happy path
(BULL and BEAR ternary arms) AND the futures/non-index ``OptionsUnderlying``
``ValueError`` arm (cli.py:10324).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from brutex.calendars.nse import IST
from brutex.cli import _options_signals_from_trades
from brutex.options.executor import ExpiryKind, SpotSignal
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import TradeDirection

# A regular NSE trading Monday (NIFTY has a live monthly expiry in June 2024).
_ENTRY_IST = datetime(2024, 6, 3, 9, 20, tzinfo=IST)
_EXIT_IST = datetime(2024, 6, 3, 15, 20, tzinfo=IST)
# The trades carry UTC timestamps (bars store UTC); the conversion converts to
# IST via ``.astimezone(IST)`` and looks up the spot via the raw UTC key.
_ENTRY_UTC = _ENTRY_IST.astimezone(UTC)
_EXIT_UTC = _EXIT_IST.astimezone(UTC)


def _trade() -> SimpleNamespace:
    """One replayed spot round-trip — only ``entry_ts`` / ``exit_ts`` are read."""
    return SimpleNamespace(entry_ts=_ENTRY_UTC, exit_ts=_EXIT_UTC)


class TestOptionsSignalsFromTrades:
    def test_bull_index_happy_path(self) -> None:
        # HAND-DERIVED: instrument "NIFTY" -> OptionsUnderlying.NIFTY; direction
        # "bull" (in {bull,buy,long}) -> TradeDirection.BULL; the UTC trade
        # stamps convert to IST 09:20 / 15:20; spot_at_entry is the raw-UTC-key
        # lookup 23000; the expiry is a live NIFTY expiry >= the entry date.
        row = SimpleNamespace(instrument="NIFTY", direction="bull")
        open_by_ts = {_ENTRY_UTC: Decimal("23000")}
        signals = _options_signals_from_trades(
            row, [_trade()], open_by_ts, expiry_kind=ExpiryKind.MONTHLY
        )
        assert len(signals) == 1
        sig = signals[0]
        assert isinstance(sig, SpotSignal)
        assert sig.instrument is OptionsUnderlying.NIFTY
        assert sig.direction is TradeDirection.BULL
        assert sig.entry_ts == _ENTRY_IST
        assert sig.exit_ts == _EXIT_IST
        assert sig.spot_at_entry == Decimal("23000")
        # The conversion threads through resolve_current_expiry — a live expiry
        # on/after the entry date (the SpotSignal invariant), never fabricated.
        assert isinstance(sig.expiry_date, date)
        assert sig.expiry_date >= _ENTRY_IST.date()

    def test_bear_index_direction_arm(self) -> None:
        # The ELSE arm of the ``is_bull`` ternary (cli.py:10326): a non-bull
        # direction ("bear") -> TradeDirection.BEAR.
        row = SimpleNamespace(instrument="BANKNIFTY", direction="bear")
        open_by_ts = {_ENTRY_UTC: Decimal("48000")}
        signals = _options_signals_from_trades(
            row, [_trade()], open_by_ts, expiry_kind=ExpiryKind.MONTHLY
        )
        assert len(signals) == 1
        assert signals[0].instrument is OptionsUnderlying.BANKNIFTY
        assert signals[0].direction is TradeDirection.BEAR

    def test_futures_instrument_raises_before_any_conversion(self) -> None:
        # cli.py:10324 — a futures-series / non-index winner row fails at the
        # ``OptionsUnderlying(row.instrument)`` construction (an unknown enum
        # value), BEFORE any trade iteration. The callers catch + honestly SKIP
        # this (ADR-0012 PR-6 named follow-up).
        row = SimpleNamespace(instrument="NSE-NIFTY-FUT", direction="bull")
        with pytest.raises(ValueError):
            _options_signals_from_trades(row, [], {}, expiry_kind=ExpiryKind.MONTHLY)
