"""Unit tests for the per-entry ``options_exec`` verdict fold.

DEC-OPTIONS-EXEC-FEEDBACK-001 (W4 item #10): the ATM headline selection, the
×100 ``priced_trade_pct`` law, the null-semantics honesty core
(``sign_flip`` / ``drag_per_trade_paisa`` null on zero accepted;
``priced_trade_pct`` null on zero signals), the named not-priced degrade
vocabulary, and the DERIVED (never hardcoded) ``etc_verified_from`` boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from brutex.options.exec_verdict import (
    OPTIONS_EXEC_HEADLINE_RULE,
    OPTIONS_EXEC_NOT_PRICED_REASONS,
    OPTIONS_EXEC_STATUS_NOT_PRICED,
    OPTIONS_EXEC_STATUS_PRICED,
    etc_verified_from_token,
    options_exec_not_priced,
    options_exec_verdict,
)
from brutex.options.executor import OptionsRuleResult


def _result(**kw: Any) -> OptionsRuleResult:
    """The test_options_executor construction idiom (same base shape)."""
    base: dict[str, Any] = {
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
    return OptionsRuleResult(**base)


# ── Vocabulary constants ──────────────────────────────────────────────────────


class TestVocabulary:
    def test_headline_rule_is_atm(self) -> None:
        # The module-docstring decision: ATM (offset 0), never best-of-7.
        assert OPTIONS_EXEC_HEADLINE_RULE == "atm"

    def test_status_tokens(self) -> None:
        assert OPTIONS_EXEC_STATUS_PRICED == "priced"
        assert OPTIONS_EXEC_STATUS_NOT_PRICED == "not_priced"

    def test_not_priced_reason_vocabulary_frozen(self) -> None:
        # The wire contract — an addition/rename must consciously edit this pin.
        assert OPTIONS_EXEC_NOT_PRICED_REASONS == (
            "not_in_replay_set",
            "replay_failed",
            "no_signals",
        )


# ── options_exec_not_priced ───────────────────────────────────────────────────


class TestNotPriced:
    @pytest.mark.parametrize("reason", OPTIONS_EXEC_NOT_PRICED_REASONS)
    def test_every_named_reason(self, reason: str) -> None:
        assert options_exec_not_priced(reason) == {
            "status": OPTIONS_EXEC_STATUS_NOT_PRICED,
            "reason": reason,
        }

    def test_unknown_reason_refused_loud(self) -> None:
        # The exact source behavior: ValueError naming the bad token + the
        # allowed vocabulary — a typo'd reason fails at the writer, never
        # ships an unknown token.
        with pytest.raises(
            ValueError,
            match=r"options_exec_not_priced: unknown reason 'typo'; expected one of",
        ):
            options_exec_not_priced("typo")


# ── etc_verified_from_token ───────────────────────────────────────────────────


class TestEtcVerifiedFromToken:
    def test_golden_literal(self) -> None:
        # The current regime-table truth (DEC-ETC-REGIME-HISTORY-001): both
        # exchanges' ETC rates verify from 2024-10-01. A verified pre-boundary
        # row landing later legitimately moves this golden.
        assert etc_verified_from_token() == "2024-10-01"

    def test_derivation_law_recomputed_independently(self) -> None:
        # The max-refusal-window-end law, recomputed with a DIFFERENT fold
        # than the source (which flattens every closed end across exchanges
        # into one list and takes max): here we fold PER-EXCHANGE first
        # (latest closed refusal end per exchange), then take the
        # cross-exchange max — a genuinely independent oracle of the same law.
        from brutex.costs.constants import etc_unverified_windows

        per_exchange: dict[str, date] = {}
        for exch, spans in etc_unverified_windows().items():
            closed_ends = [end for _start, end in spans if end is not None]
            if closed_ends:
                per_exchange[exch] = max(closed_ends)
        assert per_exchange, "regime tables must carry at least one closed refusal window"
        expected = max(per_exchange.values()).isoformat()
        assert etc_verified_from_token() == expected

    def test_none_when_no_closed_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The honest-None arm: no closed refusal window anywhere → None,
        # never a fabricated date. The source lazily imports the accessor at
        # call time, so patching the costs module attribute reaches it.
        import brutex.costs.constants as costs_constants

        monkeypatch.setattr(costs_constants, "etc_unverified_windows", dict)
        assert etc_verified_from_token() is None

    def test_open_ended_windows_filtered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An OPEN (end=None) trailing refusal window is not a verified-from
        # boundary — filtered by the `if end` guard → None overall.
        import brutex.costs.constants as costs_constants

        monkeypatch.setattr(
            costs_constants,
            "etc_unverified_windows",
            lambda: {"NSE": ((date(2020, 1, 1), None),)},
        )
        assert etc_verified_from_token() is None


# ── options_exec_verdict ──────────────────────────────────────────────────────


class TestVerdict:
    def test_full_priced_object_hand_derived(self) -> None:
        # Hand-derived arithmetic:
        #   n_signals = 8, n_accepted = 5
        #   rejects: no_data 1 + tick_floor 0 + lot 1 + stale 0 + cost 1 = 3
        #   5 + 3 = 8 (post_init consistency holds)
        #   gross = 100_000 paisa, charges = 25_000 → net = 75_000
        #   priced_trade_pct = 5 / 8 * 100.0 = 62.5 exactly (×100 law)
        #   drag_per_trade_paisa = (100_000 − 75_000) / 5 = 5_000.0
        #   sign_flip = (100_000 > 0 and 75_000 <= 0) = False
        result = _result(
            n_signals=8,
            n_accepted=5,
            n_rejected_no_data=1,
            n_rejected_lot_size_unknown=1,
            n_rejected_cost_regime_unverified=1,
            gross_pnl_paisa=100_000,
            total_charges_paisa=25_000,
            net_pnl_paisa=75_000,
            n_winners=3,
            n_losers=2,
        )
        verdict = options_exec_verdict(
            [result],
            expiry_kind="monthly",
            broker="groww",
            n_lots=2,
            replay_start="2024-01-01",
            replay_end="2025-01-01",
        )
        assert verdict == {
            "status": OPTIONS_EXEC_STATUS_PRICED,
            "rule_name": "atm",
            "expiry_kind": "monthly",
            "broker": "groww",
            "n_lots": 2,
            "n_signals": 8,
            "n_accepted": 5,
            "n_rejected": 3,
            "rejects": {
                "no_data": 1,
                "tick_floor": 0,
                "lot_size_unknown": 1,
                "stale_bar": 0,
                "cost_regime_unverified": 1,
            },
            "gross_pnl_paisa": 100_000,
            "total_charges_paisa": 25_000,
            "net_after_cost_paisa": 75_000,
            "n_winners": 3,
            "n_losers": 2,
            "priced_trade_pct": 62.5,
            "drag_per_trade_paisa": 5_000.0,
            "sign_flip": False,
            "window": {
                "replay_start": "2024-01-01",
                "replay_end": "2025-01-01",
                "etc_verified_from": "2024-10-01",
            },
        }

    def test_n_lots_defaults_to_one(self) -> None:
        # Ruling 18 — the lots=1 sizing hypothetical is the default.
        verdict = options_exec_verdict(
            [_result()],
            expiry_kind="weekly",
            broker="zerodha",
            replay_start="a",
            replay_end="b",
        )
        assert verdict["n_lots"] == 1
        assert verdict["broker"] == "zerodha"
        assert verdict["expiry_kind"] == "weekly"

    def test_atm_headline_selected_among_multiple_rules(self) -> None:
        # The ATM row wins by rule_name regardless of position or a "better"
        # net elsewhere (deliberately NOT best-of-7 — selection-bias law).
        otm = _result(rule_name="otm_2", gross_pnl_paisa=999, net_pnl_paisa=989)
        atm = _result(gross_pnl_paisa=100, total_charges_paisa=10, net_pnl_paisa=90)
        itm = _result(rule_name="itm_1", gross_pnl_paisa=500, net_pnl_paisa=490)
        verdict = options_exec_verdict(
            [otm, atm, itm],
            expiry_kind="monthly",
            broker="groww",
            replay_start="s",
            replay_end="e",
        )
        assert verdict["rule_name"] == "atm"
        assert verdict["gross_pnl_paisa"] == 100
        assert verdict["net_after_cost_paisa"] == 90

    def test_no_atm_row_fails_loud(self) -> None:
        # A code defect at the composing hook — never silently annotated with
        # a different rule.
        with pytest.raises(ValueError, match=r"no 'atm' rule row among \['otm_2', 'itm_1'\]"):
            options_exec_verdict(
                [_result(rule_name="otm_2"), _result(rule_name="itm_1")],
                expiry_kind="monthly",
                broker="groww",
                replay_start="s",
                replay_end="e",
            )

    def test_empty_results_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="no 'atm' rule row"):
            options_exec_verdict(
                [], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
            )

    def test_sign_flip_true_gross_profit_cost_flipped(self) -> None:
        # gross 50_000 > 0 but net −10_000 <= 0 (charges 60_000) → the cost
        # stack flipped a gross-profitable leg. drag = (50_000 − (−10_000))/2
        # = 30_000.0 per accepted trade.
        result = _result(
            n_signals=2,
            n_accepted=2,
            gross_pnl_paisa=50_000,
            total_charges_paisa=60_000,
            net_pnl_paisa=-10_000,
            n_winners=0,
            n_losers=2,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["sign_flip"] is True
        assert verdict["drag_per_trade_paisa"] == 30_000.0
        assert verdict["priced_trade_pct"] == 100.0

    def test_sign_flip_true_exact_zero_net(self) -> None:
        # The boundary: net == 0 counts as flipped (`net <= 0`).
        result = _result(
            gross_pnl_paisa=10, total_charges_paisa=10, net_pnl_paisa=0, n_winners=0, n_losers=1
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["sign_flip"] is True

    def test_sign_flip_false_gross_loss(self) -> None:
        # gross <= 0 → nothing to flip (the `gross > 0` conjunct fails).
        result = _result(
            gross_pnl_paisa=-100,
            total_charges_paisa=100,
            net_pnl_paisa=-200,
            n_winners=0,
            n_losers=1,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["sign_flip"] is False

    def test_sign_flip_none_on_zero_accepted(self) -> None:
        # 0 accepted → drag + sign_flip are honest nulls, never fabricated
        # 0/false; priced_trade_pct = 0/2*100 = 0.0 (signals existed).
        result = _result(
            n_signals=2,
            n_accepted=0,
            n_rejected_no_data=2,
            gross_pnl_paisa=0,
            total_charges_paisa=0,
            net_pnl_paisa=0,
            n_winners=0,
            n_losers=0,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["sign_flip"] is None
        assert verdict["drag_per_trade_paisa"] is None
        assert verdict["priced_trade_pct"] == 0.0

    def test_priced_trade_pct_none_on_zero_signals(self) -> None:
        # 0 signals → the pct denominator is honest null too.
        result = _result(
            n_signals=0,
            n_accepted=0,
            gross_pnl_paisa=0,
            total_charges_paisa=0,
            net_pnl_paisa=0,
            n_winners=0,
            n_losers=0,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["priced_trade_pct"] is None
        assert verdict["sign_flip"] is None
        assert verdict["drag_per_trade_paisa"] is None
        assert verdict["status"] == OPTIONS_EXEC_STATUS_PRICED

    def test_priced_trade_pct_times_100_exactness(self) -> None:
        # The ×100 law is the EXACT float projection n_accepted/n_signals*100
        # — pinned against the independently-computed expression.
        result = _result(
            n_signals=3,
            n_accepted=1,
            n_rejected_no_data=2,
            n_winners=1,
            n_losers=0,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["priced_trade_pct"] == 1 / 3 * 100.0

    def test_rejects_dict_passthrough(self) -> None:
        # Every named reject counter projects 1:1 into the rejects dict.
        result = _result(
            n_signals=15,
            n_accepted=0,
            n_rejected_no_data=1,
            n_rejected_tick_floor=2,
            n_rejected_lot_size_unknown=3,
            n_rejected_stale_bar=4,
            n_rejected_cost_regime_unverified=5,
            gross_pnl_paisa=0,
            total_charges_paisa=0,
            net_pnl_paisa=0,
            n_winners=0,
            n_losers=0,
        )
        verdict = options_exec_verdict(
            [result], expiry_kind="monthly", broker="groww", replay_start="s", replay_end="e"
        )
        assert verdict["rejects"] == {
            "no_data": 1,
            "tick_floor": 2,
            "lot_size_unknown": 3,
            "stale_bar": 4,
            "cost_regime_unverified": 5,
        }
        assert verdict["n_rejected"] == 15
