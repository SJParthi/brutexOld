#![no_main]
//! Fuzz target for the eval-path metadata derivation + the near-month roll
//! decision (`src/indicators/metadata.rs`, ADR-0012 D8/D11) — the calendar
//! arithmetic BOTH repos stamp on every bar.
//!
//! ## Domain (honest — matches the crate's documented contract)
//!
//! `seconds_of_day` is documented total over VALID civil components
//! (`hour <= 23`, `minute <= 59`, `second <= 59`; range validation for
//! untrusted input lives at the PyO3 boundary), so the fuzzer maps bytes into
//! that domain. Ordinals are documented over the proleptic-Gregorian range;
//! they are bounded to `±2^40` (astronomically past any calendar date, far
//! inside the `i64` overflow bound the crate's release `overflow-checks`
//! would otherwise trip on `ordinal - 1`).
//!
//! Asserted contracts (all from the module's own docs/tests):
//!
//! 1. **Never panics** over the full documented domain.
//! 2. `tod_seconds == h·3600 + m·60 + s` and is `< 86_400`.
//! 3. `weekday <= 6` always, and advances by exactly 1 (mod 7) per day.
//! 4. `minute_of_session` is monotone non-decreasing in the time-of-day,
//!    is 0 at the open, and floors (a pre-open bar is honestly NEGATIVE,
//!    never clamped).
//! 5. `is_expiry_day` iff `day_ordinal == expiry_ordinal`.
//! 6. `resolve_near_month` returns the candidate iff it has not expired
//!    (`candidate >= entry`), else the next month — never anything else.

use libfuzzer_sys::fuzz_target;
use trading_core::{
    derive_bar_metadata, minute_of_session, resolve_near_month, seconds_of_day,
    weekday_from_ordinal,
};

/// Far past any real calendar, far inside the i64 arithmetic bound.
const MAX_ORDINAL: i64 = 1 << 40;

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

/// Map an arbitrary u64 into `[-MAX_ORDINAL, MAX_ORDINAL]`.
fn ordinal(raw: u64) -> i64 {
    (raw % (2 * MAX_ORDINAL as u64 + 1)) as i64 - MAX_ORDINAL
}

fuzz_target!(|data: &[u8]| {
    let hour = (read_u64(data, 0) % 24) as u32;
    let minute = (read_u64(data, 8) % 60) as u32;
    let second = (read_u64(data, 16) % 60) as u32;
    let day_ordinal = ordinal(read_u64(data, 24));
    let expiry_ordinal = ordinal(read_u64(data, 32));
    let session_open = (read_u64(data, 40) % 86_401) as i64;

    // ── 1+2. derive never panics; tod is the exact civil arithmetic ──
    let m = derive_bar_metadata(
        day_ordinal,
        hour,
        minute,
        second,
        session_open,
        expiry_ordinal,
    );
    assert_eq!(
        m.tod_seconds,
        hour * 3600 + minute * 60 + second,
        "tod_seconds must be the plain civil arithmetic"
    );
    assert!(m.tod_seconds < 86_400, "tod_seconds must fit one day");
    assert_eq!(
        m.tod_seconds,
        seconds_of_day(hour, minute, second),
        "derive must delegate to seconds_of_day"
    );

    // ── 3. weekday in range + advances by one per day ──
    assert!(m.weekday <= 6, "weekday must be 0..=6");
    assert_eq!(m.weekday, weekday_from_ordinal(day_ordinal));
    assert_eq!(
        weekday_from_ordinal(day_ordinal + 1),
        (m.weekday + 1) % 7,
        "the weekday must advance by 1 (mod 7) per day"
    );

    // ── 4. minute-of-session: anchored, floored, monotone ──
    assert_eq!(
        minute_of_session(session_open, session_open),
        0,
        "the open bar is minute 0"
    );
    let tod = i64::from(m.tod_seconds);
    assert_eq!(m.minute_of_session, minute_of_session(tod, session_open));
    if tod < session_open {
        assert!(
            m.minute_of_session < 0 || (session_open - tod) < 60,
            "a pre-open bar must floor to a negative minute, never clamp to 0"
        );
    }
    // Monotone: one second later is never an earlier minute.
    let later = minute_of_session(tod + 1, session_open);
    assert!(
        later >= m.minute_of_session,
        "minute_of_session must be monotone in the time-of-day"
    );

    // ── 5. is_expiry_day iff the ordinals match ──
    assert_eq!(
        m.is_expiry_day,
        day_ordinal == expiry_ordinal,
        "is_expiry_day must be exact ordinal equality"
    );

    // ── 6. the near-month roll decision ──
    let entry = ordinal(read_u64(data, 48));
    let candidate = ordinal(read_u64(data, 56));
    let next_month = ordinal(read_u64(data, 64));
    let got = resolve_near_month(entry, candidate, next_month);
    if candidate >= entry {
        assert_eq!(got, candidate, "an unexpired candidate is the near month");
    } else {
        assert_eq!(
            got, next_month,
            "an expired candidate must roll to next month"
        );
    }
});
