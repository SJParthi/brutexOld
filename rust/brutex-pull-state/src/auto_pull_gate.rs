//! The `--auto` pull-gate decision kernels — pure, clockless, calendar-free.
//!
//! Wave 2D of `DEC-RUST-PULL-STATE-001`: the DECISION plane of
//! `brutex/calendars/auto_pull_gate.py` (DEC-PULL-SCOPED-ALL-ONECLICK-001,
//! MIGRATION.md #1779 + DEC-SETTLE-EVIDENCE-ADOPTION-001, #1793) — three
//! kernels, codes/bools out, every human string composed in Python:
//!
//! * [`auto_gate_decision`] — the `auto_pull_allowed` gate LADDER over the
//!   caller-resolved calendar predicates (trading-day / holiday-coverage /
//!   muhurat-listed — `brutex/calendars/nse.py` stays the ONE calendar
//!   source of truth; flags cross the FFI, never dates) + the
//!   microsecond-of-day instant/settle pair. The market-hours BELT arm
//!   reuses THIS crate's [`crate::market_hours`] core — the one-constant
//!   law: `BLOCK_END` lives in exactly one place per language side.
//! * [`muhurat_window_active`] — the margined Muhurat session-window
//!   interval law (`open − pre_margin <= t < close + post_margin`); the
//!   session LOOKUP (`MUHURAT_SESSIONS`) stays Python.
//! * [`settle_token_verdict`] — the strict `HH:MM` settle-token parse
//!   verdict + the `BLOCK_END` floor check. Python OWNS the fail-loud env
//!   posture: the three refusal `ValueError` texts are composed (and
//!   raised) by the Python consumer (`brutex/calendars/auto_pull_gate.py`)
//!   FROM this kernel's verdict code — the kernel only ever serves the OK
//!   arm's value; a non-OK verdict makes the consumer raise the deleted
//!   oracle's byte-identical refusal message, preserved Python-side.
//!
//! Times cross as MICROSECOND-OF-DAY (`i64` in `[0, DAY_US)`) — the crate's
//! established discipline: order-isomorphic to Python `datetime.time`
//! comparison, so every boundary (the settle strict-`<`, the margined
//! window's inclusive-start/exclusive-end) is bit-identical. Verdicts are
//! pinned by the committed pre-conversion golden
//! (`tests/golden/fixtures/auto_pull_gate_verdicts.tsv` — replayed by
//! `tests/auto_pull_gate_golden.rs`).

use crate::market_hours::{self, DAY_US};

/// Gate reason: the pull may proceed.
pub const GATE_OK: u8 = 0;
/// Gate reason: not an NSE trading day (weekend / listed holiday — every
/// LISTED Muhurat date is a holiday, so it defers HERE; pinned).
pub const GATE_NON_TRADING_DAY: u8 = 1;
/// Gate reason: the date has a listed Muhurat session (the belt for a
/// future TRADING-day Muhurat listing — structurally unreachable while
/// every listed Muhurat date is a holiday; the arm is kept + pinned).
pub const GATE_MUHURAT_DEFERRAL: u8 = 2;
/// Gate reason: before the resolved post-close settle instant (strict `<`).
pub const GATE_BEFORE_SETTLE: u8 = 3;
/// Gate reason: inside the market-hours pull window (a BELT — structurally
/// unreachable while the settle floor is `>= BLOCK_END`, since every
/// in-window instant is already `before_settle`; kept + pinned reachable
/// so a future settle-law change can never open a hole).
pub const GATE_MARKET_HOURS_BLOCKED: u8 = 4;

/// Settle-token verdict: a valid `HH:MM` at/after the floor.
pub const SETTLE_OK: u8 = 0;
/// Settle-token verdict: not the strict two-ASCII-digits `HH:MM` shape
/// (Python's `re.ASCII` law — Unicode digits refuse).
pub const SETTLE_BAD_SHAPE: u8 = 1;
/// Settle-token verdict: shaped but not a real wall clock (hour > 23 or
/// minute > 59).
pub const SETTLE_NOT_WALL_CLOCK: u8 = 2;
/// Settle-token verdict: a real wall clock BEFORE the market-hours guard's
/// `BLOCK_END` floor (exactly the floor is ALLOWED — boundary pinned).
pub const SETTLE_BELOW_FLOOR: u8 = 3;

/// The non-OK [`settle_token_verdict`] value slot (no instant to carry).
pub const SETTLE_NO_VALUE: i64 = -1;

fn check_us_of_day(name: &str, us: i64) -> Result<(), String> {
    if (0..DAY_US).contains(&us) {
        Ok(())
    } else {
        Err(format!(
            "{name} {us} out of range [0, {DAY_US}) — not a valid wall-clock instant"
        ))
    }
}

/// The whole `auto_pull_allowed` decision ladder — `(reason, coverage_warn)`.
///
/// Decision order (the Python oracle's, token-for-token): non-trading →
/// (coverage WARN latched, fired by PYTHON) → muhurat-listed →
/// before-settle (strict `<`) → the market-hours belt (via the crate's ONE
/// [`market_hours::pull_allowed_us`] core — `trading_day` is `true` on that
/// arm by construction) → ok. `coverage_warn` is `true` iff the date IS a
/// trading day whose holiday coverage is exhausted — Python fires the
/// `auto_pull_gate.calendar_coverage_exhausted` WARN itself (ruling 10);
/// it is `false` on the non-trading arm (the oracle returns before the
/// coverage probe).
///
/// # Errors
///
/// An out-of-range `now_us_of_day` / `settle_us_of_day` is a caller defect
/// and is REFUSED with a message (validated FIRST — total over the input
/// domain; the Python facade re-raises it as the typed
/// `PullStateKernelError`, fail-closed).
pub fn auto_gate_decision(
    trading_day: bool,
    coverage_available: bool,
    muhurat_listed: bool,
    now_us_of_day: i64,
    settle_us_of_day: i64,
) -> Result<(u8, bool), String> {
    check_us_of_day("now_us_of_day", now_us_of_day)?;
    check_us_of_day("settle_us_of_day", settle_us_of_day)?;
    if !trading_day {
        return Ok((GATE_NON_TRADING_DAY, false));
    }
    let coverage_warn = !coverage_available;
    if muhurat_listed {
        return Ok((GATE_MUHURAT_DEFERRAL, coverage_warn));
    }
    if now_us_of_day < settle_us_of_day {
        return Ok((GATE_BEFORE_SETTLE, coverage_warn));
    }
    // The belt reuses the crate's ONE market-hours core (the one-constant
    // law — never a second BLOCK_END). This arm runs only on a trading day.
    if !market_hours::pull_allowed_us(now_us_of_day, true)? {
        return Ok((GATE_MARKET_HOURS_BLOCKED, coverage_warn));
    }
    Ok((GATE_OK, coverage_warn))
}

/// The margined Muhurat session-window interval law.
///
/// `open − pre_margin <= now < close + post_margin`, all in
/// microsecond-of-day on the session's OWN date — plain `i64` arithmetic
/// reproduces the Python datetime comparison exactly for an instant on
/// that date (a start underflowing midnight or an end overflowing it
/// simply makes its bound vacuous over `[0, DAY_US)`, exactly as the
/// datetime form behaves).
///
/// # Errors
///
/// Out-of-range instants (`now`/`open`/`close` outside `[0, DAY_US)`) and
/// out-of-envelope margins (outside `[0, DAY_US)` — a negative or
/// beyond-a-day margin is a caller defect) are REFUSED with a message; the
/// Python facade re-raises it as the typed `PullStateKernelError`
/// (fail-closed).
pub fn muhurat_window_active(
    now_us_of_day: i64,
    open_us_of_day: i64,
    close_us_of_day: i64,
    pre_margin_us: i64,
    post_margin_us: i64,
) -> Result<bool, String> {
    check_us_of_day("now_us_of_day", now_us_of_day)?;
    check_us_of_day("open_us_of_day", open_us_of_day)?;
    check_us_of_day("close_us_of_day", close_us_of_day)?;
    for (name, margin) in [
        ("pre_margin_us", pre_margin_us),
        ("post_margin_us", post_margin_us),
    ] {
        if !(0..DAY_US).contains(&margin) {
            return Err(format!(
                "{name} {margin} out of range [0, {DAY_US}) — not a valid session margin"
            ));
        }
    }
    let start = open_us_of_day - pre_margin_us;
    let end = close_us_of_day + post_margin_us;
    Ok(start <= now_us_of_day && now_us_of_day < end)
}

/// The strict `HH:MM` settle-token parse verdict — `(verdict, us_of_day)`.
///
/// The token must arrive PRE-STRIPPED (Python owns `str.strip` — its
/// whitespace set is CPython's `isspace` law, not Unicode `White_Space`,
/// so the strip never crosses the FFI). Verdict order mirrors the oracle:
/// shape (exactly `DD:DD`, ASCII digits — the `re.ASCII` law) → real wall
/// clock (hour ≤ 23, minute ≤ 59) → the `block_end_us` floor (strict `<`
/// refuses; exactly the floor is ALLOWED). The value slot carries the
/// parsed microsecond-of-day on [`SETTLE_OK`], else [`SETTLE_NO_VALUE`] —
/// Python composes (and raises) every refusal text itself.
///
/// # Errors
///
/// An out-of-range `block_end_us` is a caller defect and is REFUSED with a
/// message (the floor is derived from the guard's own `BLOCK_END`).
pub fn settle_token_verdict(token: &str, block_end_us: i64) -> Result<(u8, i64), String> {
    check_us_of_day("block_end_us", block_end_us)?;
    let b = token.as_bytes();
    let shaped = b.len() == 5
        && b[2] == b':'
        && b[0].is_ascii_digit()
        && b[1].is_ascii_digit()
        && b[3].is_ascii_digit()
        && b[4].is_ascii_digit();
    if !shaped {
        return Ok((SETTLE_BAD_SHAPE, SETTLE_NO_VALUE));
    }
    let hour = i64::from(b[0] - b'0') * 10 + i64::from(b[1] - b'0');
    let minute = i64::from(b[3] - b'0') * 10 + i64::from(b[4] - b'0');
    if hour > 23 || minute > 59 {
        return Ok((SETTLE_NOT_WALL_CLOCK, SETTLE_NO_VALUE));
    }
    let us = (hour * 60 + minute) * 60 * 1_000_000;
    if us < block_end_us {
        return Ok((SETTLE_BELOW_FLOOR, SETTLE_NO_VALUE));
    }
    Ok((SETTLE_OK, us))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::market_hours::{BLOCK_END_US, BLOCK_START_US};

    const NOON_US: i64 = 12 * 3600 * 1_000_000;
    const SETTLE_1730_US: i64 = (17 * 60 + 30) * 60 * 1_000_000;

    #[test]
    fn ladder_order_non_trading_beats_everything() {
        // Non-trading returns FIRST — muhurat/settle/coverage all moot, and
        // the coverage WARN is suppressed (the oracle returns before it).
        assert_eq!(
            auto_gate_decision(false, false, true, NOON_US, SETTLE_1730_US),
            Ok((GATE_NON_TRADING_DAY, false))
        );
    }

    #[test]
    fn ladder_order_muhurat_beats_settle() {
        assert_eq!(
            auto_gate_decision(true, true, true, 0, SETTLE_1730_US),
            Ok((GATE_MUHURAT_DEFERRAL, false))
        );
    }

    #[test]
    fn settle_boundary_is_strict_less_than() {
        // One microsecond before the settle → deferred; exactly at it → ok.
        assert_eq!(
            auto_gate_decision(true, true, false, SETTLE_1730_US - 1, SETTLE_1730_US),
            Ok((GATE_BEFORE_SETTLE, false))
        );
        assert_eq!(
            auto_gate_decision(true, true, false, SETTLE_1730_US, SETTLE_1730_US),
            Ok((GATE_OK, false))
        );
    }

    #[test]
    fn market_hours_belt_stays_reachable() {
        // With a (contract-violating) pre-window settle the belt ARM fires —
        // the ladder keeps it live so a future settle-law change can never
        // open a hole (the design's keep-the-arm pin).
        assert_eq!(
            auto_gate_decision(true, true, false, NOON_US, 0),
            Ok((GATE_MARKET_HOURS_BLOCKED, false))
        );
        // At the floor-exact 15:35 settle the belt is structurally
        // unreachable: 15:35:00 is both post-settle AND post-window.
        assert_eq!(
            auto_gate_decision(true, true, false, BLOCK_END_US, BLOCK_END_US),
            Ok((GATE_OK, false))
        );
        assert_eq!(
            auto_gate_decision(true, true, false, BLOCK_END_US - 1, BLOCK_END_US),
            Ok((GATE_BEFORE_SETTLE, false))
        );
        // The belt consults the crate's ONE market-hours core: the window
        // start boundary is inclusive there.
        assert_eq!(
            auto_gate_decision(true, true, false, BLOCK_START_US, 0),
            Ok((GATE_MARKET_HOURS_BLOCKED, false))
        );
        assert_eq!(
            auto_gate_decision(true, true, false, BLOCK_START_US - 1, 0),
            Ok((GATE_OK, false))
        );
    }

    #[test]
    fn coverage_warn_rides_every_trading_arm() {
        for (now, settle, reason) in [
            (0, SETTLE_1730_US, GATE_BEFORE_SETTLE),
            (SETTLE_1730_US, SETTLE_1730_US, GATE_OK),
            (NOON_US, 0, GATE_MARKET_HOURS_BLOCKED),
        ] {
            assert_eq!(
                auto_gate_decision(true, false, false, now, settle),
                Ok((reason, true)),
                "now={now} settle={settle}"
            );
        }
        assert_eq!(
            auto_gate_decision(true, false, true, NOON_US, SETTLE_1730_US),
            Ok((GATE_MUHURAT_DEFERRAL, true))
        );
    }

    #[test]
    fn gate_out_of_range_inputs_refused_never_clamped() {
        for bad in [-1, DAY_US, i64::MIN, i64::MAX] {
            assert!(auto_gate_decision(true, true, false, bad, 0).is_err());
            assert!(auto_gate_decision(true, true, false, 0, bad).is_err());
            // Validation runs FIRST — even on the non-trading short-circuit.
            assert!(auto_gate_decision(false, true, false, bad, 0).is_err());
        }
    }

    #[test]
    fn muhurat_window_boundaries_exact() {
        // The frozen 2024 session shape: open 18:00, close 19:00 → window
        // [17:30, 19:05) with the 30/5-minute margins.
        let open = 18 * 3600 * 1_000_000;
        let close = 19 * 3600 * 1_000_000;
        let pre = 30 * 60 * 1_000_000;
        let post = 5 * 60 * 1_000_000;
        let start = open - pre;
        let end = close + post;
        assert_eq!(
            muhurat_window_active(start - 1, open, close, pre, post),
            Ok(false)
        );
        assert_eq!(
            muhurat_window_active(start, open, close, pre, post),
            Ok(true)
        );
        assert_eq!(
            muhurat_window_active(end - 1, open, close, pre, post),
            Ok(true)
        );
        assert_eq!(
            muhurat_window_active(end, open, close, pre, post),
            Ok(false)
        );
    }

    #[test]
    fn muhurat_window_midnight_underflow_and_overflow_are_vacuous_bounds() {
        // An open inside the pre-margin → the start bound underflows
        // midnight → vacuous over [0, DAY_US) (exactly the datetime law for
        // an instant on the session's own date).
        let pre = 30 * 60 * 1_000_000;
        let post = 5 * 60 * 1_000_000;
        let open = 10 * 60 * 1_000_000; // 00:10
        assert_eq!(
            muhurat_window_active(0, open, open + 3_600_000_000, pre, post),
            Ok(true)
        );
        // A close at 23:59 → the end bound overflows midnight → vacuous.
        let close = DAY_US - 60 * 1_000_000;
        assert_eq!(
            muhurat_window_active(DAY_US - 1, close - 3_600_000_000, close, pre, post),
            Ok(true)
        );
    }

    #[test]
    fn muhurat_window_refuses_bad_inputs() {
        let pre = 30 * 60 * 1_000_000;
        for bad in [-1, DAY_US] {
            assert!(muhurat_window_active(bad, 0, 0, pre, pre).is_err());
            assert!(muhurat_window_active(0, bad, 0, pre, pre).is_err());
            assert!(muhurat_window_active(0, 0, bad, pre, pre).is_err());
            assert!(muhurat_window_active(0, 0, 0, bad, pre).is_err());
            assert!(muhurat_window_active(0, 0, 0, pre, bad).is_err());
        }
    }

    #[test]
    fn settle_token_verdicts_mirror_the_oracle() {
        let floor = BLOCK_END_US;
        // ok — incl. the floor-exact boundary.
        assert_eq!(
            settle_token_verdict("17:30", floor),
            Ok((SETTLE_OK, SETTLE_1730_US))
        );
        assert_eq!(
            settle_token_verdict("15:35", floor),
            Ok((SETTLE_OK, BLOCK_END_US))
        );
        assert_eq!(
            settle_token_verdict("23:59", floor),
            Ok((SETTLE_OK, ((23 * 60 + 59) * 60) * 1_000_000))
        );
        // below the floor (strict `<`).
        for tok in ["15:34", "08:45", "00:00"] {
            assert_eq!(
                settle_token_verdict(tok, floor),
                Ok((SETTLE_BELOW_FLOOR, SETTLE_NO_VALUE)),
                "{tok}"
            );
        }
        // shaped but not a real wall clock.
        for tok in ["24:00", "17:60", "99:99"] {
            assert_eq!(
                settle_token_verdict(tok, floor),
                Ok((SETTLE_NOT_WALL_CLOCK, SETTLE_NO_VALUE)),
                "{tok}"
            );
        }
        // shape refusals — incl. the Unicode-digit re.ASCII law + the
        // unstripped shape (the caller strips; a raw-space token refuses).
        for tok in [
            "7:30",
            "17:3",
            "1730",
            "17-30",
            "0x:30",
            "+7:30",
            "١٧:٣٠",
            "17:30 IST",
            " 17:30 ",
            "",
        ] {
            assert_eq!(
                settle_token_verdict(tok, floor),
                Ok((SETTLE_BAD_SHAPE, SETTLE_NO_VALUE)),
                "{tok:?}"
            );
        }
    }

    #[test]
    fn settle_token_refuses_bad_floor() {
        for bad in [-1, DAY_US, i64::MIN] {
            assert!(settle_token_verdict("17:30", bad).is_err());
        }
    }
}
