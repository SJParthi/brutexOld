//! Options charge stack (DEC-EXEC-RUST-CORE-001, MIGRATION.md #1979) — the
//! exact-integer twin of `brutex.costs.calculator._charge_stack_py`
//! (CALCULATOR_SPEC §4 Steps 1, 3–13 in EXACT order).
//!
//! Steps 0/2 (outcome guard, lot lookup) and every rate/regime resolution —
//! including the `UnverifiedCostRegimeError` refusal raise — stay in Python;
//! rates arrive here as arguments (rate encoding `rate_decimal × 10^7`).

use crate::exec::fills::apply_slippage;
use crate::exec::money::{floor_rate_scale, gst_paisa, round_to_paisa, round_to_rupee};

/// The 17 integer outputs of one round-trip charge-stack computation —
/// field-for-field the `brutex.costs.calculator.ChargeStack` NamedTuple
/// (order PINNED; both project 1:1 onto the numeric fields of
/// `TradeCostBreakdown` / `FuturesCostBreakdown`). All values i64 paisa.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChargeStack {
    pub buy_fill_paisa: i64,
    pub sell_fill_paisa: i64,
    pub n_shares: i64,
    pub buy_notional_paisa: i64,
    pub sell_notional_paisa: i64,
    pub brokerage_paisa: i64,
    pub stt_paisa: i64,
    pub exch_paisa: i64,
    pub sebi_paisa: i64,
    pub ipft_paisa: i64,
    pub stamp_paisa: i64,
    pub gst_paisa: i64,
    pub slippage_paisa: i64,
    pub total_charges_paisa: i64,
    pub gross_pnl_paisa: i64,
    pub net_pnl_paisa: i64,
    pub realized_slip_per_share: i64,
}

impl ChargeStack {
    /// Internal-consistency law check (mutation teeth on field wiring):
    /// `net == gross − total` AND `total == Σ(the 7 charge components)`.
    /// A violating construction is a logic bug — refused as `Err`, never
    /// silently adopted (the Python `__post_init__` re-validates again at
    /// the FFI boundary, defence-in-depth).
    pub fn validated(self) -> Result<Self, String> {
        let component_sum = i128::from(self.brokerage_paisa)
            + i128::from(self.stt_paisa)
            + i128::from(self.exch_paisa)
            + i128::from(self.sebi_paisa)
            + i128::from(self.ipft_paisa)
            + i128::from(self.stamp_paisa)
            + i128::from(self.gst_paisa);
        if i128::from(self.total_charges_paisa) != component_sum {
            return Err(format!(
                "ChargeStack: total {} != component sum {component_sum}",
                self.total_charges_paisa
            ));
        }
        if i128::from(self.net_pnl_paisa)
            != i128::from(self.gross_pnl_paisa) - i128::from(self.total_charges_paisa)
        {
            return Err(format!(
                "ChargeStack: net {} != gross {} - charges {}",
                self.net_pnl_paisa, self.gross_pnl_paisa, self.total_charges_paisa
            ));
        }
        Ok(self)
    }

    /// The pinned 17-field wire order (mirrors the Python NamedTuple) —
    /// the FFI crossing shape.
    pub fn to_wire(self) -> [i64; 17] {
        [
            self.buy_fill_paisa,
            self.sell_fill_paisa,
            self.n_shares,
            self.buy_notional_paisa,
            self.sell_notional_paisa,
            self.brokerage_paisa,
            self.stt_paisa,
            self.exch_paisa,
            self.sebi_paisa,
            self.ipft_paisa,
            self.stamp_paisa,
            self.gst_paisa,
            self.slippage_paisa,
            self.total_charges_paisa,
            self.gross_pnl_paisa,
            self.net_pnl_paisa,
            self.realized_slip_per_share,
        ]
    }
}

/// Checked i64 conversion with a named context (every arithmetic exit is
/// guarded — panic=abort crate law).
pub(crate) fn to_i64(value: i128, what: &str) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| format!("{what} {value} overflows i64"))
}

/// Checked i64 addition through i128 (sums of already-validated i64s).
pub(crate) fn add_i64(a: i64, b: i64, what: &str) -> Result<i64, String> {
    to_i64(i128::from(a) + i128::from(b), what)
}

/// `calculator.py` Steps 1, 3–13 in EXACT order (the `_charge_stack_py`
/// twin under the DEC-FILL-WORST-CASE-ONLY-001 law). Laws: fills anchor on
/// the worst-case bar EXTREMES ([`apply_slippage`]); STT/stamp/GST =
/// [`floor_rate_scale`] THEN the ADVERSE-ceiling [`round_to_rupee`];
/// ETC/SEBI/IPFT = the ADVERSE-ceiling [`round_to_paisa`] per LEG then sum
/// (never sum-then-round — COSTS_VERIFIED Ex.1's 502 = 228 + 274); GST base
/// = the already-rounded service components, STT + stamp excluded, ONE
/// rupee rounding (DEC-COST-001); STT reads the SELL notional only, stamp
/// the BUY notional only; gross is sign-branched (long: sell − buy; short:
/// buy − sell) — the options stack keeps the branch.
///
/// # Errors
///
/// `Err` on any i64/i128 overflow (out-of-envelope inputs — the Python seam
/// routes them to the Python-bigint arm) or an internal law violation.
#[allow(clippy::too_many_arguments)]
pub fn option_charge_stack(
    entry_high_paisa: i64,
    entry_low_paisa: i64,
    exit_high_paisa: i64,
    exit_low_paisa: i64,
    is_long: bool,
    lot_size: i64,
    n_lots: i64,
    brokerage_round_trip_paisa: i64,
    stt_rate: i64,
    etc_rate: i64,
    sebi_rate: i64,
    ipft_rate: i64,
    stamp_rate: i64,
    gst_rate: i64,
) -> Result<ChargeStack, String> {
    // ── Step 1: apply slippage (1-tick flat adverse, around the worst-case
    //    bar-extreme anchor — DEC-FILL-WORST-CASE-ONLY-001) ───────────────
    let (buy_fill_paisa, sell_fill_paisa, realized_slip_per_share) = apply_slippage(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long,
    )?;

    // ── Step 3: notionals (paise) — lot size resolved by the caller ─────
    let n_shares = to_i64(
        i128::from(n_lots) * i128::from(lot_size),
        "option_charge_stack: n_shares",
    )?;
    let buy_notional_paisa = to_i64(
        i128::from(buy_fill_paisa) * i128::from(n_shares),
        "option_charge_stack: buy notional",
    )?;
    let sell_notional_paisa = to_i64(
        i128::from(sell_fill_paisa) * i128::from(n_shares),
        "option_charge_stack: sell notional",
    )?;

    // ── Step 4: brokerage (flat round trip — caller-resolved) ───────────
    let brokerage_paisa = brokerage_round_trip_paisa;

    // ── Step 5: STT (sell-side premium only; floor THEN rupee-round) ────
    let stt_paisa = round_to_rupee(floor_rate_scale(
        i128::from(sell_notional_paisa),
        i128::from(stt_rate),
    )?)?;

    // ── Step 6: exchange transaction charge (per leg, adverse-ceil, sum) ─
    let exch_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(etc_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(etc_rate))?,
        "option_charge_stack: exch",
    )?;

    // ── Step 7: SEBI turnover fee (per leg, adverse-ceil, then sum) ─────
    let sebi_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(sebi_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(sebi_rate))?,
        "option_charge_stack: sebi",
    )?;

    // ── Step 8: IPFT (per leg, adverse-ceil, then sum) ──────────────────
    let ipft_paisa = add_i64(
        round_to_paisa(i128::from(buy_notional_paisa) * i128::from(ipft_rate))?,
        round_to_paisa(i128::from(sell_notional_paisa) * i128::from(ipft_rate))?,
        "option_charge_stack: ipft",
    )?;

    // ── Step 9: stamp duty (buy leg only; floor THEN rupee-round) ───────
    let stamp_paisa = round_to_rupee(floor_rate_scale(
        i128::from(buy_notional_paisa),
        i128::from(stamp_rate),
    )?)?;

    // ── Step 10: GST rounded ONCE on the services base (DEC-COST-001) ───
    let gst = gst_paisa(
        &[brokerage_paisa, exch_paisa, sebi_paisa, ipft_paisa],
        gst_rate,
    )?;

    // ── Step 11: total charges ──────────────────────────────────────────
    let total_charges_paisa = to_i64(
        i128::from(brokerage_paisa)
            + i128::from(stt_paisa)
            + i128::from(exch_paisa)
            + i128::from(sebi_paisa)
            + i128::from(ipft_paisa)
            + i128::from(stamp_paisa)
            + i128::from(gst),
        "option_charge_stack: total charges",
    )?;

    // ── Step 12: gross PnL (sign-branched — the options law) ────────────
    let gross_pnl_paisa = if is_long {
        sell_notional_paisa
            .checked_sub(buy_notional_paisa)
            .ok_or_else(|| "option_charge_stack: gross overflows i64".to_string())?
    } else {
        buy_notional_paisa
            .checked_sub(sell_notional_paisa)
            .ok_or_else(|| "option_charge_stack: gross overflows i64".to_string())?
    };

    // ── Step 13: net PnL + the informational slippage line ──────────────
    let net_pnl_paisa = gross_pnl_paisa
        .checked_sub(total_charges_paisa)
        .ok_or_else(|| "option_charge_stack: net overflows i64".to_string())?;
    let slippage_paisa = to_i64(
        i128::from(realized_slip_per_share) * i128::from(n_shares),
        "option_charge_stack: slippage",
    )?;

    ChargeStack {
        buy_fill_paisa,
        sell_fill_paisa,
        n_shares,
        buy_notional_paisa,
        sell_notional_paisa,
        brokerage_paisa,
        stt_paisa,
        exch_paisa,
        sebi_paisa,
        ipft_paisa,
        stamp_paisa,
        gst_paisa: gst,
        slippage_paisa,
        total_charges_paisa,
        gross_pnl_paisa,
        net_pnl_paisa,
        realized_slip_per_share,
    }
    .validated()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The VERIFIED production option rates as of 2026-05-15 entries
    /// (COSTS_VERIFIED §5): STT 0.15%, NSE ETC 0.03503%, SEBI 0.0001%,
    /// NSE IPFT 0.0005%, stamp 0.003%, GST 18%; Groww flat ₹40 round trip.
    /// Flat-bar helper (high == low == open) at the VERIFIED NSE rates —
    /// COSTS_VERIFIED's examples are open-quoted, so the flat-bar call IS
    /// the worst-case-law equivalent of each example.
    fn nse_2026(entry: i64, exit: i64, is_long: bool, lot: i64, n_lots: i64) -> ChargeStack {
        option_charge_stack(
            entry, entry, exit, exit, is_long, lot, n_lots, 4_000, 15_000, 3_503, 10, 50, 300,
            1_800_000,
        )
        .unwrap()
    }

    #[test]
    fn costs_verified_example_1_nifty_1_lot() {
        // COSTS_VERIFIED §5 Ex.1 (regenerated under the permanent adverse
        // CEIL law by DEC-FILL-WORST-CASE-ONLY-001 — total ₹67.12; every
        // field oracle-executed at the club-R1 reconcile): NIFTY long
        // 10_000→12_000 flat bars, lot 65.
        let s = nse_2026(10_000, 12_000, true, 65, 1);
        assert_eq!(s.buy_fill_paisa, 10_005);
        assert_eq!(s.sell_fill_paisa, 11_995);
        assert_eq!(s.n_shares, 65);
        assert_eq!(s.buy_notional_paisa, 650_325);
        assert_eq!(s.sell_notional_paisa, 779_675);
        assert_eq!(s.brokerage_paisa, 4_000);
        assert_eq!(s.stt_paisa, 1_200);
        assert_eq!(s.exch_paisa, 502); // 228 + 274 per-leg — never sum-then-round
        assert_eq!(s.sebi_paisa, 2);
        assert_eq!(s.ipft_paisa, 8);
        assert_eq!(s.stamp_paisa, 100);
        assert_eq!(s.gst_paisa, 900);
        assert_eq!(s.total_charges_paisa, 6_712);
        assert_eq!(s.gross_pnl_paisa, 129_350);
        assert_eq!(s.net_pnl_paisa, 122_638);
        assert_eq!(s.slippage_paisa, 650);
    }

    #[test]
    fn costs_verified_example_2_nifty_5_lots() {
        // Ex.2 regenerated (total ₹137.51 — DEC-FILL-WORST-CASE-ONLY-001).
        let s = nse_2026(10_000, 12_000, true, 65, 5);
        assert_eq!(s.buy_notional_paisa, 3_251_625);
        assert_eq!(s.sell_notional_paisa, 3_898_375);
        assert_eq!(s.brokerage_paisa, 4_000); // flat — never scales with lots
        assert_eq!(s.stt_paisa, 5_900);
        assert_eq!(s.exch_paisa, 2_506);
        assert_eq!(s.sebi_paisa, 8);
        assert_eq!(s.ipft_paisa, 37);
        assert_eq!(s.stamp_paisa, 100);
        assert_eq!(s.gst_paisa, 1_200);
        assert_eq!(s.total_charges_paisa, 13_751);
        assert_eq!(s.gross_pnl_paisa, 646_750);
        assert_eq!(s.net_pnl_paisa, 632_999);
    }

    #[test]
    fn costs_verified_example_3_banknifty_round_once_gst() {
        // Ex.3 regenerated (total ₹53.21): BANKNIFTY lot 30, 5_000→6_000 —
        // GST still rounded ONCE on the services base (DEC-COST-001), now
        // at the adverse rupee ceiling.
        let s = nse_2026(5_000, 6_000, true, 30, 1);
        assert_eq!(s.buy_notional_paisa, 150_150);
        assert_eq!(s.sell_notional_paisa, 179_850);
        assert_eq!(s.stt_paisa, 300);
        assert_eq!(s.exch_paisa, 117);
        assert_eq!(s.sebi_paisa, 2);
        assert_eq!(s.ipft_paisa, 2);
        assert_eq!(s.stamp_paisa, 100);
        assert_eq!(s.gst_paisa, 800);
        assert_eq!(s.total_charges_paisa, 5_321);
        assert_eq!(s.gross_pnl_paisa, 29_700);
        assert_eq!(s.net_pnl_paisa, 24_379);
    }

    #[test]
    fn costs_verified_example_4_sensex_bse_rates() {
        // Ex.4 regenerated (total ₹53.20): SENSEX lot 20, 8_000→10_000 —
        // BSE ETC 0.03250%, IPFT 0.
        let s = option_charge_stack(
            8_000, 8_000, 10_000, 10_000, true, 20, 1, 4_000, 15_000, 3_250, 10, 0, 300, 1_800_000,
        )
        .unwrap();
        assert_eq!(s.buy_notional_paisa, 160_100);
        assert_eq!(s.sell_notional_paisa, 199_900);
        assert_eq!(s.stt_paisa, 300);
        assert_eq!(s.exch_paisa, 118);
        assert_eq!(s.sebi_paisa, 2);
        assert_eq!(s.ipft_paisa, 0);
        assert_eq!(s.stamp_paisa, 100);
        assert_eq!(s.gst_paisa, 800);
        assert_eq!(s.total_charges_paisa, 5_320);
        assert_eq!(s.gross_pnl_paisa, 39_800);
        assert_eq!(s.net_pnl_paisa, 34_480);
    }

    #[test]
    fn short_side_inverts_gross_sign() {
        // The short branch (production-unreachable under DEC-EXEC-008 but
        // byte-pinned): short 10_000→12_000 fills (12_005, 9_995); gross =
        // buy − sell notionals.
        let s = nse_2026(10_000, 12_000, false, 65, 1);
        assert_eq!(s.buy_fill_paisa, 12_005);
        assert_eq!(s.sell_fill_paisa, 9_995);
        assert_eq!(
            s.gross_pnl_paisa,
            s.buy_notional_paisa - s.sell_notional_paisa
        );
        assert!(s.gross_pnl_paisa > 0);
    }

    #[test]
    fn overflow_inputs_err_never_panic() {
        assert!(
            option_charge_stack(
                i64::MAX / 2,
                i64::MAX / 2,
                i64::MAX / 2,
                i64::MAX / 2,
                true,
                i64::MAX,
                i64::MAX,
                4_000,
                15_000,
                3_503,
                10,
                50,
                300,
                1_800_000,
            )
            .is_err()
        );
    }

    #[test]
    fn wire_order_is_the_pinned_17() {
        let s = nse_2026(10_000, 12_000, true, 65, 1);
        let w = s.to_wire();
        assert_eq!(w[0], s.buy_fill_paisa);
        assert_eq!(w[2], s.n_shares);
        assert_eq!(w[13], s.total_charges_paisa);
        assert_eq!(w[15], s.net_pnl_paisa);
        assert_eq!(w[16], s.realized_slip_per_share);
        assert_eq!(w.len(), 17);
    }

    #[test]
    fn validated_refuses_wired_wrong_fields() {
        let good = nse_2026(10_000, 12_000, true, 65, 1);
        let broken_net = ChargeStack {
            net_pnl_paisa: good.gross_pnl_paisa, // net mis-wired to gross
            ..good
        };
        assert!(broken_net.validated().is_err());
        let broken_total = ChargeStack {
            total_charges_paisa: good.total_charges_paisa + 1,
            ..good
        };
        assert!(broken_total.validated().is_err());
    }

    mod props {
        use super::super::*;
        use proptest::prelude::*;

        proptest! {
            /// net == gross − total and count/notional identities over the
            /// production input envelope.
            #[test]
            fn stack_internal_laws(
                entry in 5i64..10_000_000,
                exit in 5i64..10_000_000,
                is_long in proptest::bool::ANY,
                lot in 1i64..1_000,
                n_lots in 1i64..10_000,
                stt in 0i64..2_000_000,
                etc in 0i64..2_000_000,
                sebi in 0i64..2_000_000,
                ipft in 0i64..2_000_000,
                stamp in 0i64..2_000_000,
            ) {
                let s = option_charge_stack(
                    entry, entry, exit, exit, is_long, lot, n_lots, 4_000,
                    stt, etc, sebi, ipft, stamp, 1_800_000,
                ).unwrap();
                prop_assert_eq!(s.net_pnl_paisa, s.gross_pnl_paisa - s.total_charges_paisa);
                prop_assert_eq!(s.n_shares, lot * n_lots);
                prop_assert_eq!(s.buy_notional_paisa, s.buy_fill_paisa * s.n_shares);
                prop_assert_eq!(s.sell_notional_paisa, s.sell_fill_paisa * s.n_shares);
                prop_assert_eq!(
                    s.total_charges_paisa,
                    s.brokerage_paisa + s.stt_paisa + s.exch_paisa + s.sebi_paisa
                        + s.ipft_paisa + s.stamp_paisa + s.gst_paisa
                );
                // Statutory levies land on whole rupees.
                prop_assert_eq!(s.stt_paisa % 100, 0);
                prop_assert_eq!(s.stamp_paisa % 100, 0);
                prop_assert_eq!(s.gst_paisa % 100, 0);
                prop_assert_eq!(s.slippage_paisa, s.realized_slip_per_share * s.n_shares);
            }
        }
    }
}
