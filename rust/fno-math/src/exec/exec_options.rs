//! Options post-I/O materialize tail (DEC-EXEC-RUST-CORE-001, MIGRATION.md
//! #1724) — the verdict core of `brutex.options.executor.materialize_trade`
//! AFTER `source.load_pair` returned a bar pair.
//!
//! Gates 1–3 of the PINNED reject order (lot_size → cost_regime → no_data)
//! stay in Python — they are lazy date gates / a `None` probe with
//! narrow-except + monkeypatch semantics a Rust table cannot see. THIS core
//! owns gates 4–5 in the pinned order: STALE_BAR (volume == 0 on either bar,
//! iff `reject_stale` — staleness makes the price itself meaningless, so it
//! is checked BEFORE the tick floor) then TICK_FLOOR (entry ≤ 25 paisa =
//! `TICK_FLOOR_MULTIPLE × TICK_PAISA`, or exit < one tick), then the charge
//! stack. Structlog WARNs are emitted by the Python executor from the
//! returned discriminant (identical event names / cardinality).

use crate::exec::costs_options::{ChargeStack, option_charge_stack};
use crate::exec::fills::TICK_PAISA;

/// §7.8 entry-quality floor multiple — mirrors
/// `brutex.options.executor.TICK_FLOOR_MULTIPLE` (executor.py:127).
pub const TICK_FLOOR_MULTIPLE: i64 = 5;

/// The options post-I/O verdict: a named counted reject, or the priced stack.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BarVerdict {
    /// volume == 0 on the entry or exit bar (rejected iff `reject_stale`).
    StaleBar,
    /// entry open ≤ `TICK_FLOOR_MULTIPLE × TICK_PAISA` or exit open < 1 tick.
    TickFloor,
    /// Priced — the full charge stack.
    Trade(ChargeStack),
}

/// The options materialize tail: stale → tick floor → charge stack, in the
/// pinned order (`materialize_trade` gates 4–5 + the cost compute).
///
/// # Errors
///
/// `Err` on arithmetic overflow inside the charge stack (out-of-envelope
/// inputs — the Python seam routes them to the Python arm).
#[allow(clippy::too_many_arguments)]
pub fn options_materialize_core(
    entry_open_paisa: i64,
    exit_open_paisa: i64,
    entry_volume: i64,
    exit_volume: i64,
    is_long: bool,
    reject_stale: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> Result<BarVerdict, String> {
    // Gate 4 — STALE_BAR (before the tick floor: staleness makes the price
    // itself meaningless; executor.py:542-556).
    if reject_stale && (entry_volume == 0 || exit_volume == 0) {
        return Ok(BarVerdict::StaleBar);
    }
    // Gate 5 — TICK_FLOOR: `<=` on the entry (₹0.25 itself rejects), a
    // defensive sub-tick `<` on the exit (executor.py:558-563).
    if entry_open_paisa <= TICK_FLOOR_MULTIPLE * TICK_PAISA || exit_open_paisa < TICK_PAISA {
        return Ok(BarVerdict::TickFloor);
    }
    let stack = option_charge_stack(
        entry_open_paisa,
        exit_open_paisa,
        is_long,
        lot_size,
        n_lots,
        brokerage_round_trip_paisa,
        stt_rate,
        etc_rate,
        sebi_rate,
        ipft_rate,
        stamp_rate,
        gst_rate,
    )?;
    Ok(BarVerdict::Trade(stack))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn core(
        entry: i64,
        exit: i64,
        entry_vol: i64,
        exit_vol: i64,
        reject_stale: bool,
    ) -> BarVerdict {
        options_materialize_core(
            entry,
            exit,
            entry_vol,
            exit_vol,
            true,
            reject_stale,
            25,
            1,
            4_000,
            10_000,
            3_503,
            10,
            50,
            300,
            1_800_000,
        )
        .unwrap()
    }

    #[test]
    fn stale_bar_precedes_tick_floor() {
        // A bar that is BOTH zero-volume AND ≤ 25 paisa reads STALE (the
        // documented code-order law — preserved even though no Python test
        // pins this exact combination; r1 §1.2 note).
        assert_eq!(core(10, 10, 0, 100, true), BarVerdict::StaleBar);
        assert_eq!(core(12_000, 15_000, 100, 0, true), BarVerdict::StaleBar);
    }

    #[test]
    fn stale_bar_opt_out_fills() {
        // reject_stale=False accepts zero-volume bars (the opt-out arm).
        assert!(matches!(
            core(12_000, 15_000, 0, 0, false),
            BarVerdict::Trade(_)
        ));
    }

    #[test]
    fn tick_floor_boundaries_exact() {
        // entry ≤ 25 rejects (the `<=` law: ₹0.25 itself rejects)…
        assert_eq!(core(25, 15_000, 100, 100, true), BarVerdict::TickFloor);
        // …26 does not.
        assert!(matches!(
            core(26, 15_000, 100, 100, true),
            BarVerdict::Trade(_)
        ));
        // exit < 5 rejects; exactly 5 does not.
        assert_eq!(core(12_000, 4, 100, 100, true), BarVerdict::TickFloor);
        assert!(matches!(
            core(12_000, 5, 100, 100, true),
            BarVerdict::Trade(_)
        ));
    }

    #[test]
    fn golden_fixture_a_nifty_bull_atm() {
        // tests/golden/test_options_executor_golden.py Fixture A: NIFTY BULL
        // atm 2024-10-03, ₹120 → ₹150, lot 25 — STT 0.10% (the 2024-10-01+
        // regime), NSE ETC 3_503, IPFT 50.
        let v = options_materialize_core(
            12_000, 15_000, 100, 100, true, true, 25, 1, 4_000, 10_000, 3_503, 10, 50, 300,
            1_800_000,
        )
        .unwrap();
        let BarVerdict::Trade(s) = v else {
            panic!("expected Trade, got {v:?}");
        };
        assert_eq!(s.buy_fill_paisa, 12_005);
        assert_eq!(s.sell_fill_paisa, 14_995);
        assert_eq!(s.n_shares, 25);
        assert_eq!(s.brokerage_paisa, 4_000);
        assert_eq!(s.stt_paisa, 400);
        assert_eq!(s.exch_paisa, 236); // 105 + 131 @ NSE 3_503
        assert_eq!(s.sebi_paisa, 0);
        assert_eq!(s.ipft_paisa, 4);
        assert_eq!(s.stamp_paisa, 0);
        assert_eq!(s.gst_paisa, 800);
        assert_eq!(s.total_charges_paisa, 5_440);
        assert_eq!(s.gross_pnl_paisa, 74_750);
        assert_eq!(s.net_pnl_paisa, 69_310);
    }

    #[test]
    fn golden_fixture_b_sensex_bear() {
        // Fixture B: SENSEX BEAR atm_plus_1 (PE), ₹200 → ₹50, lot 10 — BSE
        // ETC 3_250, IPFT 0; a long_put is still is_long=true.
        let v = options_materialize_core(
            20_000, 5_000, 100, 100, true, true, 10, 1, 4_000, 10_000, 3_250, 10, 0, 300, 1_800_000,
        )
        .unwrap();
        let BarVerdict::Trade(s) = v else {
            panic!("expected Trade, got {v:?}");
        };
        assert_eq!(s.buy_fill_paisa, 20_005);
        assert_eq!(s.sell_fill_paisa, 4_995);
        assert_eq!(s.brokerage_paisa, 4_000);
        assert_eq!(s.stt_paisa, 0);
        assert_eq!(s.exch_paisa, 81); // 65 + 16 @ BSE 3_250
        assert_eq!(s.ipft_paisa, 0);
        assert_eq!(s.gst_paisa, 700);
        assert_eq!(s.total_charges_paisa, 4_781);
        assert_eq!(s.gross_pnl_paisa, -150_100);
        assert_eq!(s.net_pnl_paisa, -154_881);
    }

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        proptest! {
            /// Verdict totality + order over the whole small-input envelope:
            /// zero-volume wins over tick-floor wins over trade; the trade
            /// arm always satisfies net == gross − total.
            #[test]
            fn verdict_order_total(
                entry in 1i64..100_000,
                exit in 1i64..100_000,
                entry_vol in 0i64..3,
                exit_vol in 0i64..3,
                reject_stale in proptest::bool::ANY,
            ) {
                let v = options_materialize_core(
                    entry, exit, entry_vol, exit_vol, true, reject_stale,
                    25, 1, 4_000, 10_000, 3_503, 10, 50, 300, 1_800_000,
                ).unwrap();
                let stale = reject_stale && (entry_vol == 0 || exit_vol == 0);
                let floored = entry <= 25 || exit < 5;
                match v {
                    BarVerdict::StaleBar => prop_assert!(stale),
                    BarVerdict::TickFloor => prop_assert!(!stale && floored),
                    BarVerdict::Trade(s) => {
                        prop_assert!(!stale && !floored);
                        prop_assert_eq!(
                            s.net_pnl_paisa,
                            s.gross_pnl_paisa - s.total_charges_paisa
                        );
                    }
                }
            }
        }
    }
}
