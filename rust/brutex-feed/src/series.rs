//! The sweep-slice loader's DECISION arithmetic — `DEC-FEED-RUST-MATERIALIZE-002`.
//!
//! Pure-integer twins of the three decision laws deleted from
//! `brutex/feed/series_load.py`'s Python bodies:
//!
//! * [`expected_bar_instant`] — the "can this window hold ANY expectable
//!   stored bar instant?" verdict (`_expected_bar_time_in_window`'s
//!   geometric core). Python keeps the CALENDAR I/O (it walks the IST
//!   days, resolves each real NSE session via `get_session_checked`, and
//!   owns the two ABSTAIN arms — out-of-coverage dates and unknown
//!   interval labels — which return `True` Python-side exactly as
//!   before); Rust owns the grid arithmetic over the session instants
//!   Python hands it as exact epoch-microsecond integers.
//! * [`ownership_cut`] — the exact IST-date ownership cut over a
//!   contract segment's bar timestamps (`bar.timestamp.astimezone(IST)
//!   .date()` bounded to `[own_start, own_end]`), as epoch-day ordinals
//!   under the fixed +05:30 law. ABSTAINS (`None`) when any timestamp
//!   lies outside `[0, MAX_SAFE_TS_US)` — the envelope where the fixed
//!   offset provably equals `ZoneInfo("Asia/Kolkata")`; the
//!   wheel-required Python seam raises a LOUD refusal on abstain (real
//!   lake bars are post-2020 by the `GROWW_HISTORY_FLOOR` law, so only
//!   planted/out-of-band data can abstain).
//! * [`boundary_monotonic_break`] — the disjoint-ownership monotonicity
//!   verdict between consecutive stitched segments.
//!
//! All arithmetic is exact `i64` microseconds / epoch-day ordinals —
//! Python's `datetime`/`timedelta` carry microsecond resolution, so the
//! integer transcription is lossless by construction (no float ever
//! enters).

use crate::scan::{DAY_US, IST_OFFSET_US, MAX_SAFE_TS_US};

/// One resolvable NSE session day, as exact epoch-microsecond instants:
/// `(stamp_us, open_us, close_us)` — `stamp_us` is the derived-`1day`
/// bar's midnight-IST stamp for the day (consulted only on the 1day arm),
/// `open_us` / `close_us` the session open/close instants.
pub type SessionDay = (i64, i64, i64);

/// `True` iff `[seg_start, seg_end]` can contain an expectable stored bar
/// instant — the verbatim transcription of the deleted Python geometric
/// core (`series_load.py::_expected_bar_time_in_window`, review rounds
/// 1/3/6/8 semantics preserved line by line):
///
/// * a degenerate window (`seg_end < seg_start`) holds no instants —
///   `false` (checked FIRST, before any session is consulted);
/// * `step_us == 0` marks the derived FNO `1day` arm: expectable iff the
///   window holds the midnight stamp (`seg_start <= stamp <= seg_end`)
///   AND the bar can already exist (`seg_end >= close` — the window
///   reaches the session close the bar summarises — OR `close <= now` —
///   deep history, the post-session prebuild owes the bar);
/// * `step_us > 0` is the intraday grid arm: the session overlaps
///   (`seg_end >= open && seg_start < close`) and the FIRST grid label
///   at/after `seg_start` (`open + k·step`, exact ceil via Euclidean
///   floor division on the negated numerator — Python's `timedelta`
///   floordiv law) lies inside both the fragment and the session
///   (`first_label <= seg_end && first_label < close`).
///
/// Sessions are consulted in the caller's (chronological) order and the
/// first qualifying day wins — matching the deleted walk's early return.
#[must_use]
pub fn expected_bar_instant(
    seg_start_us: i64,
    seg_end_us: i64,
    step_us: i64,
    now_us: i64,
    sessions: &[SessionDay],
) -> bool {
    if seg_end_us < seg_start_us {
        return false;
    }
    for &(stamp_us, open_us, close_us) in sessions {
        if step_us == 0 {
            // The derived FNO 1day bar — midnight-IST stamp.
            if seg_start_us <= stamp_us
                && stamp_us <= seg_end_us
                && (seg_end_us >= close_us || close_us <= now_us)
            {
                return true;
            }
        } else if seg_end_us >= open_us && seg_start_us < close_us {
            let first_label = if seg_start_us <= open_us {
                open_us
            } else {
                // Exact ceil-division transcription: Python's
                // `open_dt + -((open_dt - seg_start) // step) * step` with
                // timedelta floordiv (floor semantics) — `div_euclid` is
                // floor for the positive divisor, so the negated-numerator
                // trick is bit-identical.
                open_us + -((open_us - seg_start_us).div_euclid(step_us)) * step_us
            };
            if first_label <= seg_end_us && first_label < close_us {
                return true;
            }
        }
    }
    false
}

/// The exact IST-date ownership cut: keep bar `i` iff
/// `own_start_days <= ist_day_ordinal(ts_us[i]) <= own_end_days`, where
/// the ordinal is the fixed `+05:30` civil-day ordinal (epoch days) —
/// provably `astimezone(IST).date()` for every `ts` in
/// `[0, MAX_SAFE_TS_US)` (the [`crate::scan`] envelope law). Returns the
/// kept indices ascending, or `None` (ABSTAIN) the moment any timestamp
/// leaves the envelope — the caller raises a loud refusal (never a
/// silently wrong civil date).
#[must_use]
pub fn ownership_cut(ts_us: &[i64], own_start_days: i64, own_end_days: i64) -> Option<Vec<usize>> {
    let mut kept: Vec<usize> = Vec::new();
    for (i, &t) in ts_us.iter().enumerate() {
        if !(0..MAX_SAFE_TS_US).contains(&t) {
            return None;
        }
        let ordinal = (t + IST_OFFSET_US).div_euclid(DAY_US);
        if own_start_days <= ordinal && ordinal <= own_end_days {
            kept.push(i);
        }
    }
    Some(kept)
}

/// `True` iff stitching breaks the disjoint-IST-date-ownership
/// monotonicity law: the next segment's first bar is NOT strictly after
/// the previous segment's last bar (`first <= prev_last` — the verbatim
/// deleted comparison; the caller raises `FuturesSeriesError`).
#[must_use]
pub fn boundary_monotonic_break(prev_last_us: i64, first_us: i64) -> bool {
    first_us <= prev_last_us
}
