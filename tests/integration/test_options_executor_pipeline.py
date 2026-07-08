"""Integration test for the options-sim orchestration (DEC-OPTIONS-EXECUTOR-001).

Exercises the exact pipeline the ``brutex options-sim`` CLI performs, minus the
lake/engine I/O: replayed spot round-trips → :func:`signals_from_spot_trades` →
:func:`execute_options_sim` → a REAL hash-chained
:class:`~brutex.orchestrator.audit.AuditWriter`, then verifies the audit chain
on disk (Golden Rule #8). This is the "real-run proof" of the wiring: the same
seams the CLI feeds the lake + engine into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from brutex.data.models import OptionBar
from brutex.options.executor import (
    ExpiryKind,
    OptionBarPair,
    execute_options_sim,
    signals_from_spot_trades,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import OFFSET_RULES, TradeDirection
from brutex.orchestrator.audit import AuditWriter, verify_chain

_IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class _Trade:
    entry_ts: datetime
    exit_ts: datetime


class _Source:
    """A lake-stand-in source: every leg gets the same winner bar pair."""

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        def _bar(open_rupees: str) -> OptionBar:
            px = Decimal(open_rupees)
            return OptionBar(
                timestamp=datetime(2024, 10, 3, 9, 16, tzinfo=_IST),
                open=px,
                high=px,
                low=px,
                close=px,
                volume=10,
                open_interest=100,
                side=option_type,
                strike=Decimal("25000"),
                spot=Decimal("25012"),
                iv=Decimal("0.15"),
            )

        return OptionBarPair(entry_bar=_bar("120.00"), exit_bar=_bar("150.00"))


def test_full_options_sim_pipeline_with_real_audit(tmp_path: Path) -> None:
    # Two replayed spot round-trips (entry/exit in UTC, as the lake stores
    # them). Post-2024-10-01 dates: the verified ETC window
    # (DEC-ETC-REGIME-HISTORY-001 made pre-boundary dates reject).
    trades = [
        _Trade(
            entry_ts=datetime(2024, 10, 3, 3, 46, tzinfo=UTC),
            exit_ts=datetime(2024, 10, 3, 9, 50, tzinfo=UTC),
        ),
        _Trade(
            entry_ts=datetime(2024, 10, 4, 3, 46, tzinfo=UTC),
            exit_ts=datetime(2024, 10, 4, 9, 50, tzinfo=UTC),
        ),
    ]
    spot_by_ts = {
        trades[0].entry_ts: Decimal("25012"),
        trades[1].entry_ts: Decimal("25040"),
    }

    signals = signals_from_spot_trades(
        trades,
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        spot_open_at=lambda ts: spot_by_ts[ts],
        expiry_kind=ExpiryKind.WEEKLY,
    )
    assert len(signals) == 2

    audit_path = tmp_path / "options_sim_audit.jsonl"
    with AuditWriter.open(audit_path) as writer:
        results = execute_options_sim(signals, source=_Source(), audit=writer)

    # One result per offset rule; both signals priced + winning for each.
    assert len(results) == len(OFFSET_RULES)
    for res in results:
        assert res.n_signals == 2
        assert res.n_accepted == 2
        assert res.n_winners == 2
        assert res.net_pnl_paisa > 0

    # The audit chain on disk is intact and has exactly one row per rule.
    assert verify_chain(audit_path) == len(OFFSET_RULES)
