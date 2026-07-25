"""Per-trade reject guards for the options executor (R2-OC-4/5 + R2-OC-7 +
DEC-ETC-REGIME-HISTORY-001).

Covers the three dedicated :class:`RejectReason` members:

- ``LOT_SIZE_UNKNOWN`` — a signal dated BEFORE the lot-size history floor
  (2021-01-01; the data pull floor is 2020-01-01) rejects THAT trade only
  instead of crashing the whole sim with the ``get_lot_size`` ValueError.
- ``STALE_BAR`` — an entry OR exit bar with ``volume == 0`` is a stale/
  carried price no real order could fill at; rejected UNCONDITIONALLY —
  the ``reject_stale_bars`` opt-out was DELETED by
  DEC-FILL-WORST-CASE-ONLY-001 (MIGRATION.md #1825).
- ``COST_REGIME_UNVERIFIED`` — a signal whose entry date falls in the
  pre-2024-10-01 UNVERIFIED ETC refusal window rejects per-trade instead
  of pricing at a fabricated rate (DEC-ETC-REGIME-HISTORY-001).

All are structlog-logged and surfaced as dedicated counters on
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

#: A healthy in-history trade date (lot size recorded AND inside the
#: verified post-2024-10-01 ETC window — relocated from 2024-05-02 by
#: DEC-ETC-REGIME-HISTORY-001, which made pre-boundary dates reject).
_ENTRY_2024 = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_EXIT_2024 = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_EXPIRY_2024 = date(2024, 10, 31)

#: A pre-boundary date: lot size KNOWN (post-2021) but the ETC regime is
#: the UNVERIFIED refusal window (pre-2024-10-01).
_ENTRY_PRE_BOUNDARY = datetime(2024, 5, 2, 9, 16, tzinfo=_IST)
_EXIT_PRE_BOUNDARY = datetime(2024, 5, 2, 15, 20, tzinfo=_IST)
_EXPIRY_PRE_BOUNDARY = date(2024, 5, 30)


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


def _signal_pre_boundary() -> SpotSignal:
    """A signal in the lot-known but ETC-UNVERIFIED window (2024-05-02)."""
    return SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_PRE_BOUNDARY,
        exit_ts=_EXIT_PRE_BOUNDARY,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY_PRE_BOUNDARY,
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


def test_stale_guard_is_unconditional_no_opt_out() -> None:
    # DEC-FILL-WORST-CASE-ONLY-001 (scope G): the reject_stale_bars opt-out
    # is DELETED — a zero-volume bar ALWAYS rejects (killed-token pin + the
    # uniform-reject law).
    import inspect

    for fn in (materialize_trade, run_rule_over_signals, execute_options_sim):
        assert "reject_stale_bars" not in inspect.signature(fn).parameters, fn.__name__
    out = materialize_trade(
        _signal_2024(),
        RULES_BY_NAME["atm"],
        source=_VolumeSource(entry_volume=0, exit_volume=0),
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.STALE_BAR


def test_normal_volume_bars_unaffected_by_default_guard() -> None:
    out = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource())
    assert isinstance(out, MaterializedTrade)


def test_run_rule_counts_stale_bar_rejects() -> None:
    signals = [_signal_2024(), _signal_2024()]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_VolumeSource(exit_volume=0))
    assert res.n_rejected_stale_bar == 2
    assert res.n_accepted == 0
    assert res.n_rejected == 2


def test_run_rule_counts_unconditional_stale_rejects() -> None:
    res = run_rule_over_signals(
        [_signal_2024()],
        RULES_BY_NAME["atm"],
        source=_VolumeSource(entry_volume=0),
    )
    assert res.n_accepted == 0
    assert res.n_rejected_stale_bar == 1


def test_execute_options_sim_rejects_zero_volume_always() -> None:
    out = execute_options_sim(
        [_signal_2024()], source=_VolumeSource(entry_volume=0), rules=[RULES_BY_NAME["atm"]]
    )
    assert out[0].n_rejected_stale_bar == 1
    assert out[0].n_accepted == 0


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
    """One batch mixing all five reject reasons + one accept — every counter
    lands in its own bucket and the count invariant holds. (The 2020 and
    pre-boundary signals never reach load_pair — their date guards fire
    first — so the dispatcher serves only the four in-window signals.)"""
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
        _signal_pre_boundary(),
    ]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_Dispatch())
    assert res.n_signals == 6
    assert res.n_accepted == 1
    assert res.n_rejected_no_data == 1
    assert res.n_rejected_stale_bar == 1
    assert res.n_rejected_tick_floor == 1
    assert res.n_rejected_lot_size_unknown == 1
    assert res.n_rejected_cost_regime_unverified == 1
    assert res.n_accepted + res.n_rejected == res.n_signals


# ── COST_REGIME_UNVERIFIED (DEC-ETC-REGIME-HISTORY-001) ──────────────────────


class _NeverCalledSource:
    """A bar source that MUST NOT be read — proves the cost-regime precheck
    fires BEFORE any lake I/O."""

    def load_pair(self, **_kw: object) -> OptionBarPair | None:
        raise AssertionError("load_pair must not be called for a pre-boundary signal")


def test_pre_boundary_signal_rejects_cost_regime_unverified() -> None:
    """A lot-known signal dated inside the pre-2024-10-01 ETC refusal window
    rejects COST_REGIME_UNVERIFIED — never priced at a fabricated rate,
    never a sim crash — and never touches the lake (precheck before I/O)."""
    out = materialize_trade(
        _signal_pre_boundary(), RULES_BY_NAME["atm"], source=_NeverCalledSource()
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.COST_REGIME_UNVERIFIED


def test_cost_regime_reject_is_logged() -> None:
    with structlog.testing.capture_logs() as logs:
        materialize_trade(_signal_pre_boundary(), RULES_BY_NAME["atm"], source=_VolumeSource())
    events = [e for e in logs if e["event"] == "options.reject.cost_regime_unverified"]
    assert len(events) == 1
    assert events[0]["rule"] == "atm"
    assert events[0]["instrument"] == "NIFTY"
    assert events[0]["entry_date"] == "2024-05-02"
    assert events[0]["window_start"] == date.min.isoformat()
    assert events[0]["verified_from"] == "2024-10-01"
    assert "UNVERIFIED" in str(events[0]["error"])


def test_reject_order_lot_size_wins() -> None:
    """PINNED deterministic reject order (lot_size → cost_regime → no_data →
    stale_bar → tick_floor): a 2020-06-15 signal is BOTH lot-unknown AND
    pre-boundary — it must read LOT_SIZE_UNKNOWN."""
    out = materialize_trade(_signal_2020(), RULES_BY_NAME["atm"], source=_NeverCalledSource())
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.LOT_SIZE_UNKNOWN


def test_reject_order_cost_regime_precedes_no_data() -> None:
    """A pre-boundary signal with NO bars still reads COST_REGIME_UNVERIFIED
    (not NO_DATA) — the date guard precedes the source probe."""
    out = materialize_trade(
        _signal_pre_boundary(), RULES_BY_NAME["atm"], source=_VolumeSource(returns_none=True)
    )
    assert isinstance(out, RejectedTrade)
    assert out.reason is RejectReason.COST_REGIME_UNVERIFIED


def test_run_rule_counts_cost_regime_rejects() -> None:
    """Mixed pre-boundary + post-boundary batch: the new counter fills, the
    count invariant holds, and the accepted set is IDENTICAL to a
    verified-window-only run (skip-and-count changes NOTHING for verified
    trades)."""
    mixed = [_signal_2024(), _signal_pre_boundary(), _signal_2024()]
    res = run_rule_over_signals(mixed, RULES_BY_NAME["atm"], source=_VolumeSource())
    assert res.n_signals == 3
    assert res.n_accepted == 2
    assert res.n_rejected_cost_regime_unverified == 1
    assert res.n_rejected == 1
    assert res.n_accepted + res.n_rejected == res.n_signals

    verified_only = run_rule_over_signals(
        [_signal_2024(), _signal_2024()], RULES_BY_NAME["atm"], source=_VolumeSource()
    )
    assert res.n_accepted == verified_only.n_accepted
    assert res.gross_pnl_paisa == verified_only.gross_pnl_paisa
    assert res.total_charges_paisa == verified_only.total_charges_paisa
    assert res.net_pnl_paisa == verified_only.net_pnl_paisa
    assert res.n_winners == verified_only.n_winners


def test_negative_cost_regime_count_rejected() -> None:
    with pytest.raises(ValueError, match="n_rejected_cost_regime_unverified must be >= 0"):
        _result(n_rejected_cost_regime_unverified=-1)


def test_cost_regime_counter_defaults_to_zero_for_back_compat() -> None:
    res = _result()
    assert res.n_rejected_cost_regime_unverified == 0


def test_n_rejected_sums_cost_regime_bucket() -> None:
    res = _result(
        n_signals=6,
        n_accepted=1,
        n_rejected_no_data=1,
        n_rejected_tick_floor=1,
        n_rejected_lot_size_unknown=1,
        n_rejected_stale_bar=1,
        n_rejected_cost_regime_unverified=1,
    )
    assert res.n_rejected == 5


def test_execute_options_sim_aggregate_warn_once() -> None:
    """A sim with pre-boundary rejections emits EXACTLY ONE aggregate
    ``options.sim.unverified_cost_window`` WARN (across MULTIPLE rules),
    carrying the total count + the DATA-derived window spans."""
    rules = [RULES_BY_NAME["atm"], RULES_BY_NAME["atm_plus_1_in_dir"]]
    with structlog.testing.capture_logs() as logs:
        execute_options_sim(
            [_signal_pre_boundary(), _signal_2024()],
            source=_VolumeSource(),
            rules=rules,
        )
    events = [e for e in logs if e["event"] == "options.sim.unverified_cost_window"]
    assert len(events) == 1
    assert events[0]["n_rejected"] == 2  # one pre-boundary signal × two rules
    windows = events[0]["windows"]
    assert set(windows) == {"NSE", "BSE"}
    assert windows["NSE"] == ["[history-start → 2024-10-01)"]
    assert windows["BSE"] == ["[history-start → 2024-10-01)"]


def test_execute_options_sim_no_aggregate_warn_when_clean() -> None:
    """A fully post-boundary sim emits NO aggregate WARN."""
    with structlog.testing.capture_logs() as logs:
        execute_options_sim([_signal_2024()], source=_VolumeSource(), rules=[RULES_BY_NAME["atm"]])
    events = [e for e in logs if e["event"] == "options.sim.unverified_cost_window"]
    assert events == []


def test_generic_valueerror_from_probe_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precheck's except is NARROW: a generic ValueError raised from the
    probe path (NOT UnverifiedCostRegimeError) propagates — never silently
    swallowed into a reject (narrow-except mutation killer)."""
    import brutex.costs.constants as costs_constants

    def _boom(instrument: str, on_date: date) -> int:
        raise ValueError("boom — a generic programmer error")

    monkeypatch.setattr(costs_constants, "exchange_charge_bps_x100", _boom)
    with pytest.raises(ValueError, match="boom — a generic programmer error"):
        materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=_VolumeSource())


# ── DEC-EXEC-RUST-CORE-001 — ON-arm stale verdict (slice 7 / P8) ─────────────


class TestExecRustStaleDispatch:
    """The kernel-served STALE_BAR verdict maps onto the SAME reject + the
    SAME single ``options.reject.stale_bar`` WARN (identical event name /
    keys / one-WARN-per-reject cardinality). The reject is UNCONDITIONAL on
    both arms — the ``reject_stale_bars`` opt-out was DELETED by
    DEC-FILL-WORST-CASE-ONLY-001 (MIGRATION.md #1825)."""

    @pytest.mark.parametrize(("entry_volume", "exit_volume"), [(0, 10), (10, 0)])
    def test_on_arm_stale_bar_matches_off_arm_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, entry_volume: int, exit_volume: int
    ) -> None:
        from brutex import exec_rust

        source = _VolumeSource(entry_volume=entry_volume, exit_volume=exit_volume)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        off = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=source)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        with structlog.testing.capture_logs() as logs:
            on = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=source)
        assert isinstance(on, RejectedTrade)
        assert on.reason is RejectReason.STALE_BAR
        assert on == off
        events = [x for x in logs if x["event"] == "options.reject.stale_bar"]
        assert len(events) == 1
        assert events[0]["entry_volume"] == source.entry_volume
        assert events[0]["exit_volume"] == source.exit_volume

    def test_both_volumes_zero_rejects_unconditionally_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both legs volume-0 rejects on BOTH arms — no opt-out exists
        (DEC-FILL-WORST-CASE-ONLY-001 deleted ``reject_stale_bars``)."""
        from brutex import exec_rust

        source = _VolumeSource(entry_volume=0, exit_volume=0)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        off = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=source)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_trade(_signal_2024(), RULES_BY_NAME["atm"], source=source)
        assert isinstance(off, RejectedTrade)
        assert off.reason is RejectReason.STALE_BAR
        assert isinstance(on, RejectedTrade)
        assert on.reason is RejectReason.STALE_BAR
        assert on == off
