"""Per-trade reject guards for the options executor (R2-OC-4/5 + R2-OC-7).

Covers the two NEW :class:`RejectReason` members:

- ``LOT_SIZE_UNKNOWN`` — a signal dated BEFORE the lot-size history floor
  (2021-01-01; the data pull floor is 2020-01-01) rejects THAT trade only
  instead of crashing the whole sim with the ``get_lot_size`` ValueError.
- ``STALE_BAR`` — an entry OR exit bar with ``volume == 0`` is a stale/
  carried price no real order could fill at; rejected by default, opt-out
  via ``reject_stale_bars=False``.

Both are structlog-logged and surfaced as dedicated counters on
:class:`OptionsRuleResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import structlog.testing
from brutex.data.models import OptionBar
from brutex.options.executor import (
    MaterializedTrade,
    OptionBarPair,
    OptionsRuleResult,
    RejectedTrade,
    RejectReason,
    SpotSignal,
    execute_options_sim,
    materialize_trade,
    run_rule_over_signals,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

_IST = ZoneInfo("Asia/Kolkata")

#: A 2020 trade date — IN scope per the data floor (GROWW_HISTORY_FLOOR =
#: 2020-01-01) but BEFORE the lot-size history floor (2021-01-01).
_ENTRY_2020 = datetime(2020, 6, 15, 9, 16, tzinfo=_IST)
_EXIT_2020 = datetime(2020, 6, 15, 15, 20, tzinfo=_IST)
_EXPIRY_2020 = date(2020, 6, 18)

#: A healthy in-history trade date (lot size recorded).
_ENTRY_2024 = datetime(2024, 5, 2, 9, 16, tzinfo=_IST)
_EXIT_2024 = datetime(2024, 5, 2, 15, 20, tzinfo=_IST)
_EXPIRY_2024 = date(2024, 5, 2)


def _bar_with_volume(side: str, open_rupees: str, volume: int) -> OptionBar:
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY_2024,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=volume,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=Decimal("0.15"),
    )


@dataclass(frozen=True)
class _VolumeSource:
    """Bar source with configurable entry/exit volumes (default healthy)."""

    entry_volume: int = 10
    exit_volume: int = 10
    returns_none: bool = False

    def load_pair(
        self,
        *,
        instrument: OptionsUnderlying,
        expiry_date: date,
        strike: int,
        option_type: str,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> OptionBarPair | None:
        del instrument, expiry_date, strike, entry_ts, exit_ts
        if self.returns_none:
            return None
        return OptionBarPair(
            entry_bar=_bar_with_volume(option_type, "120.00", self.entry_volume),
            exit_bar=_bar_with_volume(option_type, "150.00", self.exit_volume),
        )


def _signal_2024() -> SpotSignal:
    return SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_2024,
        exit_ts=_EXIT_2024,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY_2024,
    )


def _signal_2020() -> SpotSignal:
    """A 2020-dated signal (constructed directly; SpotSignal only requires
    expiry >= entry date, so no expiry-calendar lookup is needed)."""
    return SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_2020,
        exit_ts=_EXIT_2020,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY_2020,
    )


# ── LOT_SIZE_UNKNOWN (R2-OC-4/5) ─────────────────────────────────────────────


def test_2020_signal_rejects_lot_size_unknown_not_crash() -> None:
    out = materialize_trade(_signal_2020(), RULES_BY_NAME["atm"], source=_VolumeSource())
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.LOT_SIZE_UNKNOWN


def test_lot_size_unknown_is_logged() -> None:
    with structlog.testing.capture_logs() as logs:
        materialize_trade(_signal_2020(), RULES_BY_NAME["atm"], source=_VolumeSource())
    events = [e for e in logs if e["event"] == "options.reject.lot_size_unknown"]
    assert len(events) == 1
    assert events[0]["instrument"] == "NIFTY"
    assert events[0]["entry_date"] == "2020-06-15"


def test_lot_size_guard_precedes_source_probe() -> None:
    """Deterministic precedence: the lot guard is a pure date precondition
    checked BEFORE any lake I/O — a 2020 signal with NO bars still reads
    LOT_SIZE_UNKNOWN (not NO_DATA)."""
    out = materialize_trade(
        _signal_2020(), RULES_BY_NAME["atm"], source=_VolumeSource(returns_none=True)
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.LOT_SIZE_UNKNOWN


def test_batch_with_one_2020_trade_completes_with_one_reject() -> None:
    """THE R2-OC-4/5 regression: one 2020-dated signal used to abort the
    ENTIRE options sim via an uncaught ValueError from get_lot_size. Now it
    rejects that trade only; the other trades price normally."""
    signals = [_signal_2024(), _signal_2020(), _signal_2024()]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_VolumeSource())
    assert res.n_signals == 3
    assert res.n_accepted == 2
    assert res.n_rejected_lot_size_unknown == 1
    assert res.n_rejected == 1
    assert res.n_winners == 2  # both 2024 trades are winners (120 -> 150)


def test_other_value_errors_still_propagate() -> None:
    """The catch is NARROW — a ValueError from a DIFFERENT layer (here the
    delta-target rule's missing chain) still propagates, never silently
    swallowed into a reject."""
    with pytest.raises(ValueError, match="needs a delta_chain"):
        materialize_trade(_signal_2024(), RULES_BY_NAME["delta_target_050"], source=_VolumeSource())


# ── STALE_BAR (R2-OC-7) ──────────────────────────────────────────────────────


def test_zero_volume_entry_bar_rejects_stale() -> None:
    out = materialize_trade(
        _signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource(entry_volume=0)
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.STALE_BAR


def test_zero_volume_exit_bar_rejects_stale() -> None:
    out = materialize_trade(
        _signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource(exit_volume=0)
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.STALE_BAR


def test_stale_bar_is_logged() -> None:
    with structlog.testing.capture_logs() as logs:
        materialize_trade(
            _signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource(entry_volume=0)
        )
    events = [e for e in logs if e["event"] == "options.reject.stale_bar"]
    assert len(events) == 1
    assert events[0]["entry_volume"] == 0
    assert events[0]["exit_volume"] == 10


def test_stale_guard_opt_out_accepts_zero_volume_bars() -> None:
    out = materialize_trade(
        _signal_2024(),
        RULES_BY_NAME["atm"],
        source=_VolumeSource(entry_volume=0, exit_volume=0),
        reject_stale_bars=False,
    )
    assert isinstance(out, MaterializedTrade)  # priced exactly as before the guard


def test_normal_volume_bars_unaffected_by_default_guard() -> None:
    out = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource())
    assert isinstance(out, MaterializedTrade)


def test_run_rule_counts_stale_bar_rejects() -> None:
    signals = [_signal_2024(), _signal_2024()]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_VolumeSource(exit_volume=0))
    assert res.n_rejected_stale_bar == 2
    assert res.n_accepted == 0
    assert res.n_rejected == 2


def test_run_rule_opt_out_threads_through() -> None:
    res = run_rule_over_signals(
        [_signal_2024()],
        RULES_BY_NAME["atm"],
        source=_VolumeSource(entry_volume=0),
        reject_stale_bars=False,
    )
    assert res.n_accepted == 1
    assert res.n_rejected_stale_bar == 0


def test_execute_options_sim_opt_out_available_at_api() -> None:
    strict = execute_options_sim(
        [_signal_2024()], source=_VolumeSource(entry_volume=0), rules=[RULES_BY_NAME["atm"]]
    )
    relaxed = execute_options_sim(
        [_signal_2024()],
        source=_VolumeSource(entry_volume=0),
        rules=[RULES_BY_NAME["atm"]],
        reject_stale_bars=False,
    )
    assert strict[0].n_rejected_stale_bar == 1
    assert strict[0].n_accepted == 0
    assert relaxed[0].n_rejected_stale_bar == 0
    assert relaxed[0].n_accepted == 1


# ── OptionsRuleResult new-counter validation ─────────────────────────────────


def _result(**kw: int) -> OptionsRuleResult:
    base: dict[str, object] = {
        "rule_name": "atm",
        "n_signals": 1,
        "n_accepted": 1,
        "n_rejected_no_data": 0,
        "n_rejected_tick_floor": 0,
        "gross_pnl_paisa": 100,
        "total_charges_paisa": 10,
        "net_pnl_paisa": 90,
        "n_winners": 1,
        "n_losers": 0,
    }
    base.update(kw)
    return OptionsRuleResult(**base)  # type: ignore[arg-type]


def test_negative_lot_size_unknown_count_rejected() -> None:
    with pytest.raises(ValueError, match="n_rejected_lot_size_unknown must be >= 0"):
        _result(n_rejected_lot_size_unknown=-1)


def test_negative_stale_bar_count_rejected() -> None:
    with pytest.raises(ValueError, match="n_rejected_stale_bar must be >= 0"):
        _result(n_rejected_stale_bar=-1)


def test_n_rejected_sums_all_four_reasons() -> None:
    res = _result(
        n_signals=5,
        n_accepted=1,
        n_rejected_no_data=1,
        n_rejected_tick_floor=1,
        n_rejected_lot_size_unknown=1,
        n_rejected_stale_bar=1,
    )
    assert res.n_rejected == 4


def test_new_counters_default_to_zero_for_back_compat() -> None:
    res = _result()
    assert res.n_rejected_lot_size_unknown == 0
    assert res.n_rejected_stale_bar == 0


def test_selection_bias_note_documented_on_result() -> None:
    """R2-OC-11 traceability: the liquidity-selection-bias honesty note is
    part of the result surface's documented contract."""
    doc = OptionsRuleResult.__doc__ or ""
    assert "SELECTION BIAS" in doc
    assert "adverse conditions" in doc


def test_mixed_reject_reasons_all_counted() -> None:
    """One batch mixing all four reject reasons + one accept — every counter
    lands in its own bucket and the count invariant holds. (The 2020 signal
    never reaches load_pair — its lot guard fires first — so the dispatcher
    serves only the four in-history signals.)"""
    behaviours = iter(["accept", "no_data", "stale", "tick_floor"])

    class _Dispatch:
        def load_pair(self, **kw: object) -> OptionBarPair | None:
            kind = next(behaviours)
            if kind == "no_data":
                return None
            if kind == "stale":
                return OptionBarPair(
                    entry_bar=_bar_with_volume("CE", "120.00", 0),
                    exit_bar=_bar_with_volume("CE", "150.00", 10),
                )
            if kind == "tick_floor":
                return OptionBarPair(
                    entry_bar=_bar_with_volume("CE", "0.25", 10),
                    exit_bar=_bar_with_volume("CE", "1.00", 10),
                )
            return OptionBarPair(
                entry_bar=_bar_with_volume("CE", "120.00", 10),
                exit_bar=_bar_with_volume("CE", "150.00", 10),
            )

    signals = [
        _signal_2024(),
        _signal_2024(),
        _signal_2024(),
        _signal_2024(),
        _signal_2020(),
    ]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_Dispatch())
    assert res.n_signals == 5
    assert res.n_accepted == 1
    assert res.n_rejected_no_data == 1
    assert res.n_rejected_stale_bar == 1
    assert res.n_rejected_tick_floor == 1
    assert res.n_rejected_lot_size_unknown == 1
    assert res.n_accepted + res.n_rejected == res.n_signals
