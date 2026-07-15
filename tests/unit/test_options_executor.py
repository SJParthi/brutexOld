"""Unit tests for the options executor / materializer (DEC-OPTIONS-EXECUTOR-001).

Targets 100% line + branch coverage of :mod:`brutex.options.executor`: signal
validation, strike-leg selection (offset + delta-target), bar-pair pricing,
the no-data / tick-floor rejections, per-rule aggregation, the 10-rule sweep
with its audit seam, expiry resolution (incl. the weekly→monthly fallback), and
the spot-trade → signal replay seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.costs.constants import TICK_PAISA
from brutex.data.models import OptionBar
from brutex.options.executor import (
    TICK_FLOOR_MULTIPLE,
    ExpiryKind,
    MaterializedTrade,
    OptionBarPair,
    OptionLeg,
    OptionsRuleResult,
    RejectedTrade,
    RejectReason,
    SpotSignal,
    execute_options_sim,
    materialize_trade,
    resolve_current_expiry,
    run_rule_over_signals,
    select_option_leg,
    signals_from_spot_trades,
)
from brutex.options.expiry_calendar import next_monthly_expiry, next_weekly_expiry
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

_IST = ZoneInfo("Asia/Kolkata")
# Canonical priced-path date: post-2024-10-01 (the verified ETC window —
# DEC-ETC-REGIME-HISTORY-001 made pre-boundary dates reject) and clear of
# the 2024-11-20 NIFTY lot transition.
_ENTRY = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_EXIT = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_EXPIRY = date(2024, 10, 3)


# ── builders ────────────────────────────────────────────────────────────────


def _bar(side: str, open_rupees: str, *, iv: Decimal | None = Decimal("0.15")) -> OptionBar:
    """A flat (limit-locked) option bar at *open_rupees* for *side*."""
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=10,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=iv,
    )


@dataclass(frozen=True)
class _Source:
    """Configurable :class:`OptionBarSource` fake."""

    entry_open: str = "120.00"
    exit_open: str = "150.00"
    iv: Decimal | None = Decimal("0.15")
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
            entry_bar=_bar(option_type, self.entry_open, iv=self.iv),
            exit_bar=_bar(option_type, self.exit_open, iv=self.iv),
        )


class _Audit:
    """Audit sink fake — records every appended row."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        self.rows.append((action, subject, payload))
        return object()


def _signal(
    *,
    direction: TradeDirection = TradeDirection.BULL,
    spot: str = "25012",
    instrument: OptionsUnderlying = OptionsUnderlying.NIFTY,
) -> SpotSignal:
    return SpotSignal(
        instrument=instrument,
        direction=direction,
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
        spot_at_entry=Decimal(spot),
        expiry_date=_EXPIRY,
    )


# ── SpotSignal validation ─────────────────────────────────────────────────────


def test_spot_signal_valid() -> None:
    sig = _signal()
    assert sig.instrument is OptionsUnderlying.NIFTY
    assert sig.spot_at_entry == Decimal("25012")


def test_spot_signal_naive_entry_rejected() -> None:
    with pytest.raises(ValueError, match="entry_ts must be timezone-aware"):
        SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL,
            entry_ts=datetime(2024, 10, 3, 9, 16),
            exit_ts=_EXIT,
            spot_at_entry=Decimal("25012"),
            expiry_date=_EXPIRY,
        )


def test_spot_signal_naive_exit_rejected() -> None:
    with pytest.raises(ValueError, match="exit_ts must be timezone-aware"):
        SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL,
            entry_ts=_ENTRY,
            exit_ts=datetime(2024, 10, 3, 15, 20),
            spot_at_entry=Decimal("25012"),
            expiry_date=_EXPIRY,
        )


def test_spot_signal_exit_before_entry_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= entry_ts"):
        SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL,
            entry_ts=_EXIT,
            exit_ts=_ENTRY,
            spot_at_entry=Decimal("25012"),
            expiry_date=_EXPIRY,
        )


def test_spot_signal_nonpositive_spot_rejected() -> None:
    with pytest.raises(ValueError, match="spot_at_entry must be > 0"):
        SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL,
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
            spot_at_entry=Decimal("0"),
            expiry_date=_EXPIRY,
        )


def test_spot_signal_expiry_before_entry_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= entry date"):
        SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL,
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
            spot_at_entry=Decimal("25012"),
            expiry_date=date(2024, 10, 2),
        )


# ── select_option_leg ─────────────────────────────────────────────────────────


def test_select_leg_bull_atm() -> None:
    leg = select_option_leg(_signal(), RULES_BY_NAME["atm"])
    assert leg.option_type == "CE"
    assert leg.side == "long_call"
    assert leg.atm_strike == 25000
    assert leg.strike == 25000
    assert leg.strike_offset == 0


def test_select_leg_bull_offset_otm() -> None:
    leg = select_option_leg(_signal(), RULES_BY_NAME["atm_plus_2_in_dir"])
    assert leg.strike == 25100  # ATM 25000 + 2 * step(50)
    assert leg.strike_offset == 2


def test_select_leg_bear_is_put_with_inverted_offset() -> None:
    leg = select_option_leg(
        _signal(direction=TradeDirection.BEAR), RULES_BY_NAME["atm_plus_1_in_dir"]
    )
    assert leg.option_type == "PE"
    assert leg.side == "long_put"
    # BEAR PE "further OTM" means a LOWER strike → 25000 - 50.
    assert leg.strike == 24950
    assert leg.strike_offset == -1


def test_select_leg_delta_target_uses_chain() -> None:
    chain = [(24900, 0.62), (25000, 0.51), (25100, 0.39)]
    leg = select_option_leg(_signal(), RULES_BY_NAME["delta_target_050"], delta_chain=chain)
    assert leg.strike == 25000  # |delta| closest to 0.50


def test_select_leg_delta_target_without_chain_raises() -> None:
    with pytest.raises(ValueError, match="needs a delta_chain"):
        select_option_leg(_signal(), RULES_BY_NAME["delta_target_030"])


def test_option_leg_nonpositive_strike_rejected() -> None:
    with pytest.raises(ValueError, match="strike must be > 0"):
        OptionLeg(
            rule_name="x",
            option_type="CE",
            side="long_call",
            atm_strike=25000,
            strike=0,
            strike_offset=0,
        )


# ── OptionBarPair ─────────────────────────────────────────────────────────────


def test_bar_pair_side_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="sides must match"):
        OptionBarPair(entry_bar=_bar("CE", "100"), exit_bar=_bar("PE", "100"))


# ── materialize_trade ─────────────────────────────────────────────────────────


def test_materialize_winner() -> None:
    out = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=_Source())
    assert isinstance(out, MaterializedTrade)
    assert out.net_pnl_paisa > 0
    assert out.gross_pnl_paisa == out.breakdown.gross_pnl_paisa
    assert out.total_charges_paisa == out.breakdown.total_charges_paisa
    assert out.entry_open_paisa == 12000
    assert out.exit_open_paisa == 15000
    assert out.iv_at_entry == Decimal("0.15")
    assert out.expiry_date == _EXPIRY
    assert out.leg.strike == 25000


def test_materialize_loser_when_premium_falls() -> None:
    out = materialize_trade(
        _signal(), RULES_BY_NAME["atm"], source=_Source(entry_open="150.00", exit_open="100.00")
    )
    assert isinstance(out, MaterializedTrade)
    assert out.net_pnl_paisa < 0


def test_materialize_no_data_rejected() -> None:
    out = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=_Source(returns_none=True))
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.NO_DATA
    assert out.leg.strike == 25000


def test_materialize_tick_floor_on_low_entry() -> None:
    # Entry premium == TICK_FLOOR_MULTIPLE * tick (₹0.25) → at the floor → reject.
    floor_rupees = f"{TICK_FLOOR_MULTIPLE * TICK_PAISA / 100:.2f}"
    out = materialize_trade(
        _signal(), RULES_BY_NAME["atm"], source=_Source(entry_open=floor_rupees, exit_open="1.00")
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.TICK_FLOOR


def test_materialize_tick_floor_on_subtick_exit() -> None:
    # Entry healthy (> floor) but exit below one tick → defensive reject.
    out = materialize_trade(
        _signal(), RULES_BY_NAME["atm"], source=_Source(entry_open="100.00", exit_open="0.03")
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.TICK_FLOOR


def test_materialize_carries_none_iv() -> None:
    out = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=_Source(iv=None))
    assert isinstance(out, MaterializedTrade)
    assert out.iv_at_entry is None


def test_materialize_delta_rule_with_chain() -> None:
    out = materialize_trade(
        _signal(),
        RULES_BY_NAME["delta_target_050"],
        source=_Source(),
        delta_chain=[(25000, 0.5)],
    )
    assert isinstance(out, MaterializedTrade)


def test_materialize_sensex_uses_bse_costs() -> None:
    # Exercises the non-NSE instrument-literal mapping + BSE charge branch.
    sig = _signal(spot="81012", instrument=OptionsUnderlying.SENSEX)
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source())
    assert isinstance(out, MaterializedTrade)
    assert out.leg.atm_strike == 81000  # SENSEX step 100


# ── OptionsRuleResult validation + properties ─────────────────────────────────


def _result(**kw: int) -> OptionsRuleResult:
    base = {
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


def test_result_negative_count_rejected() -> None:
    with pytest.raises(ValueError, match="n_winners must be >= 0"):
        _result(n_winners=-1)


def test_result_inconsistent_signal_count_rejected() -> None:
    with pytest.raises(ValueError, match="must equal n_signals"):
        _result(n_signals=5)


def test_result_winners_plus_losers_exceeds_accepted_rejected() -> None:
    with pytest.raises(ValueError, match="cannot exceed n_accepted"):
        _result(n_signals=3, n_accepted=3, n_winners=2, n_losers=2)


def test_result_n_rejected_property() -> None:
    res = _result(
        n_signals=4, n_accepted=1, n_rejected_no_data=2, n_rejected_tick_floor=1, n_winners=1
    )
    assert res.n_rejected == 3


def test_result_win_rate() -> None:
    res = _result(n_signals=2, n_accepted=2, n_winners=1, n_losers=1)
    assert res.win_rate == 0.5


def test_result_win_rate_zero_when_none_accepted() -> None:
    res = _result(
        n_signals=1,
        n_accepted=0,
        n_rejected_no_data=1,
        gross_pnl_paisa=0,
        total_charges_paisa=0,
        net_pnl_paisa=0,
        n_winners=0,
    )
    assert res.win_rate == 0.0


# ── run_rule_over_signals ─────────────────────────────────────────────────────


def test_run_rule_aggregates_all_outcomes() -> None:
    floor_rupees = f"{TICK_FLOOR_MULTIPLE * TICK_PAISA / 100:.2f}"
    signals = [_signal(), _signal(), _signal(), _signal()]
    # Different sources per call: winner, loser, no-data, tick-floor. We achieve
    # the mix by running four single-signal rules and summing? Simpler: use a
    # source whose behaviour is fixed, so run two separate rules. Here we test
    # the winner + loser + reject branches by composing per-signal sources via
    # a dispatching source.
    behaviours = iter(
        [
            ("120.00", "150.00", False),  # winner
            ("150.00", "100.00", False),  # loser
            ("0.00", "0.00", True),  # no-data
            (floor_rupees, "1.00", False),  # tick-floor
        ]
    )

    class _Dispatch:
        def load_pair(self, **kw: object) -> OptionBarPair | None:
            entry, exit_, none = next(behaviours)
            if none:
                return None
            return OptionBarPair(
                entry_bar=_bar("CE", entry),
                exit_bar=_bar("CE", exit_),
            )

    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_Dispatch())
    assert res.n_signals == 4
    assert res.n_accepted == 2
    assert res.n_rejected_no_data == 1
    assert res.n_rejected_tick_floor == 1
    assert res.n_winners == 1
    assert res.n_losers == 1
    assert res.win_rate == 0.5


def test_run_rule_delta_without_provider_raises() -> None:
    with pytest.raises(ValueError, match="supply a delta_chain_provider"):
        run_rule_over_signals([_signal()], RULES_BY_NAME["delta_target_050"], source=_Source())


def test_run_rule_delta_with_provider() -> None:
    res = run_rule_over_signals(
        [_signal()],
        RULES_BY_NAME["delta_target_050"],
        source=_Source(),
        delta_chain_provider=lambda _s: [(25000, 0.5)],
    )
    assert res.n_accepted == 1


# ── execute_options_sim ───────────────────────────────────────────────────────


def test_execute_sweeps_seven_offset_rules() -> None:
    results = execute_options_sim([_signal()], source=_Source())
    assert len(results) == 7  # OFFSET_RULES
    assert all(r.n_accepted == 1 for r in results)


def test_execute_with_audit_appends_one_row_per_rule() -> None:
    audit = _Audit()
    results = execute_options_sim([_signal()], source=_Source(), audit=audit)
    assert len(audit.rows) == len(results)
    action, subject, payload = audit.rows[0]
    assert action == "OPTIONS_SIM_RULE"
    assert subject == results[0].rule_name
    assert payload["n_accepted"] == 1


def test_execute_custom_rule_subset() -> None:
    results = execute_options_sim([_signal()], source=_Source(), rules=[RULES_BY_NAME["atm"]])
    assert len(results) == 1
    assert results[0].rule_name == "atm"


# ── resolve_current_expiry ────────────────────────────────────────────────────


def test_resolve_weekly_returns_weekly() -> None:
    on = date(2024, 5, 1)
    got = resolve_current_expiry(OptionsUnderlying.NIFTY, on, ExpiryKind.WEEKLY)
    assert got == next_weekly_expiry(OptionsUnderlying.NIFTY, on)


def test_resolve_weekly_falls_back_to_monthly_when_no_weekly() -> None:
    # BANKNIFTY weekly was discontinued after 2024-11-13 → weekly is None.
    on = date(2025, 3, 1)
    assert next_weekly_expiry(OptionsUnderlying.BANKNIFTY, on) is None
    got = resolve_current_expiry(OptionsUnderlying.BANKNIFTY, on, ExpiryKind.WEEKLY)
    assert got == next_monthly_expiry(OptionsUnderlying.BANKNIFTY, on)


def test_resolve_monthly() -> None:
    on = date(2024, 5, 1)
    got = resolve_current_expiry(OptionsUnderlying.NIFTY, on, ExpiryKind.MONTHLY)
    assert got == next_monthly_expiry(OptionsUnderlying.NIFTY, on)


# ── signals_from_spot_trades ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _Trade:
    entry_ts: datetime
    exit_ts: datetime


def test_signals_from_spot_trades_converts_and_resolves() -> None:
    # Entry/exit supplied in UTC → must be converted to IST.
    entry_utc = datetime(2024, 10, 3, 3, 46, tzinfo=UTC)  # 09:16 IST
    exit_utc = datetime(2024, 10, 3, 9, 50, tzinfo=UTC)  # 15:20 IST
    trades = [_Trade(entry_ts=entry_utc, exit_ts=exit_utc)]
    sigs = signals_from_spot_trades(
        trades,
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        spot_open_at=lambda _ts: Decimal("25012"),
        expiry_kind=ExpiryKind.WEEKLY,
    )
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.entry_ts.utcoffset() == timedelta(hours=5, minutes=30)  # IST
    assert sig.entry_ts.hour == 9
    assert sig.entry_ts.minute == 16
    assert sig.spot_at_entry == Decimal("25012")
    assert sig.expiry_date == next_weekly_expiry(OptionsUnderlying.NIFTY, date(2024, 10, 3))


def test_signals_from_spot_trades_empty() -> None:
    sigs = signals_from_spot_trades(
        [],
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        spot_open_at=lambda _ts: Decimal("25012"),
        expiry_kind=ExpiryKind.MONTHLY,
    )
    assert sigs == []


# ─────────────────────────────────────────────────────────────────────────────
# DEC-EXEC-RUST-CORE-001 — the BRUTEX_EXEC_RUST dispatch tail (slice 7 / P8)
# ─────────────────────────────────────────────────────────────────────────────


class TestExecRustDispatch:
    """ON-arm parity of ``_materialize_tail_rust`` through the REAL
    ``materialize_trade`` entry point.

    The flag-less default (every test above) is the Python tail; these flip
    ``BRUTEX_EXEC_RUST=1`` and prove: the kernel-served trade is WHOLE-OBJECT
    equal to the OFF arm (engagement-spied so it can never go vacuous), the
    tick-floor verdict maps onto the same ``RejectReason``, and EVERY
    fallback shape (tail ``None`` / unknown kind / kind-trade-without-stack /
    a prep-or-kernel raise) lands byte-identically on the Python tail.
    """

    def _off(self, monkeypatch: pytest.MonkeyPatch, source: _Source) -> object:
        from brutex import exec_rust

        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        return materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)

    def test_on_arm_trade_equals_off_arm_and_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import fno_math
        from brutex import exec_rust

        source = _Source()
        off = self._off(monkeypatch, source)

        calls: dict[str, int] = {}
        real = fno_math.exec_options_materialize

        def _spy(*a: Any) -> object:
            calls["exec_options_materialize"] = calls.get("exec_options_materialize", 0) + 1
            return real(*a)

        monkeypatch.setattr(
            exec_rust, "_rust", type("K", (), {"exec_options_materialize": staticmethod(_spy)})()
        )
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)
        assert calls == {"exec_options_materialize": 1}
        assert isinstance(on, MaterializedTrade)
        assert on == off

    def test_on_arm_tick_floor_matches_off_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import exec_rust

        for source in (_Source(entry_open="0.25"), _Source(exit_open="0.04")):
            off = self._off(monkeypatch, source)
            monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
            on = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)
            assert isinstance(on, RejectedTrade)
            assert on.reason is RejectReason.TICK_FLOOR
            assert on == off

    @pytest.mark.parametrize(
        "tail_result",
        [None, ("bogus_kind", None), ("trade", None)],
        ids=["tail-none", "unknown-kind", "trade-without-stack"],
    )
    def test_fallback_shapes_land_on_the_python_tail(
        self, monkeypatch: pytest.MonkeyPatch, tail_result: object
    ) -> None:
        from brutex import exec_rust

        source = _Source()
        off = self._off(monkeypatch, source)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "materialize_option_tail", lambda *a, **k: tail_result)
        on = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)
        assert isinstance(on, MaterializedTrade)
        assert on == off

    def test_prep_or_kernel_raise_falls_back_to_the_python_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex import exec_rust

        source = _Source()
        off = self._off(monkeypatch, source)

        def _boom(*a: object, **k: object) -> object:
            raise TypeError("kernel prep fault")

        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "materialize_option_tail", _boom)
        on = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)
        assert isinstance(on, MaterializedTrade)
        assert on == off

    @pytest.mark.parametrize(
        "wire",
        [
            list(range(16)),
            list(range(18)),
            list(range(20)),
            list("abcdefghijklmnopq"),  # the pinned length 17, junk element types
        ],
        ids=["len-16", "len-18", "len-20", "len-17-wrong-type"],
    )
    def test_malformed_kernel_wire_warns_and_serves_the_python_arm(
        self, monkeypatch: pytest.MonkeyPatch, wire: list[object]
    ) -> None:
        """The R3-F3 regression, reproduced at the EXACT reported path: a
        wrong-arity options wire from the KERNEL previously raised
        ``TypeError`` at ``ChargeStack(*wire)`` OUTSIDE the seam's try —
        no seam warn — and this executor's broad prep catch swallowed it
        with ZERO logs (a silent Python-arm fallback against the seam's
        documented loudness law). The seam now validates the wire and
        warn-onces ``exec_rust.kernel_fallback`` (``KernelWireFault``)
        BEFORE returning the fallback sentinel; the value is the
        byte-identical OFF arm."""
        import fno_math
        from brutex import exec_rust
        from structlog.testing import capture_logs

        source = _Source()
        off = self._off(monkeypatch, source)
        exec_rust.reset_warn_memo_for_tests()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        # Malformed ONLY at the tail symbol; every other kernel entry
        # delegates to the real wheel (the Python tail's own inner
        # charge-stack dispatch must not trip the stale-wheel guard).
        fake = SimpleNamespace(
            exec_options_materialize=lambda *_a: ("trade", wire),
            **{
                n: getattr(fno_math, n) for n in ("exec_option_charge_stack", "exec_apply_slippage")
            },
        )
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            on = materialize_trade(_signal(), RULES_BY_NAME["atm"], source=source)
        assert isinstance(on, MaterializedTrade)
        assert on == off
        events = [x for x in logs if x["event"] == "exec_rust.kernel_fallback"]
        assert len(events) == 1
        assert events[0]["site"] == "materialize_option_tail"
        assert events[0]["error_type"] == "KernelWireFault"
        exec_rust.reset_warn_memo_for_tests()
