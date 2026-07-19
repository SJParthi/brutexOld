//! # brutex-lake Wave-D conversion modules (ADR-0020 §6 — `BRUTEX_LAKE_WAVED_RUST`)
//!
//! The Wave-D lake conversion re-hosts the heal/scan/read surface of the
//! Python lake layer (`timeframe_materialize` → `lake_heal` →
//! `catalog_reconcile` → `lake_invariants`, in that ruled order) behind the
//! ONE default-OFF flag `BRUTEX_LAKE_WAVED_RUST`, whose facade is
//! `brutex/data/lake_rust_waved.py`. Each conversion step lands as its OWN
//! module file under `src/waved/` so the crate-hub diffs (`lib.rs`
//! `#[pymodule]` registration, this `mod` list) stay one-line adds — the
//! §4.1 concurrent-sibling additive-registration merge law.
//!
//! Honesty banner (ADR-0020 §0, carried verbatim): **expected payoff ≈ 0 —
//! uniformity, not speed**. The heal/scan surface is anomaly-gated and
//! IO-bound; nothing here claims a speedup.
//!
//! This `mod.rs` carries only the SHARED scaffolding the per-step modules
//! reuse: the events-JSON serializer (structured verdicts cross the FFI as
//! ONE JSON string — the `stats_shard.rs` idiom, ints/strings/bools only)
//! and the epoch-day → ISO civil-date conversion (the IST session-date law
//! is fixed +05:30 integer arithmetic — the `resample.rs` precedent).
//!
//! ## Registered steps
//!
//! * `read` (W-1, this PR): the `timeframe_materialize.make_lake_read_1m`
//!   read core — enumeration + collect + ts-range gate + price-sanity gate
//!   and bad-tick screen — plus the `has_stored_1m_bars` and
//!   `month_partition_files` drift-pinned twins.
//! * `heal` (W-2) / `reconcile` (W-3) / `scan` (W-4): future sibling
//!   modules per the Wave-D design §H; they register ADDITIVELY below the
//!   W-1 functions in `lib.rs`, never in place of them.

pub mod heal;
pub mod read;

/// One per-session bad-tick WARN record the Python shell replays through
/// `brutex.data.bad_tick.emit_bad_tick_warn` (the SINGLE emission seam —
/// the `rust/brutex-feed` `scan_bars` precedent): the crate returns warn
/// COORDINATES, Python owns structlog (Wave-D house law i — structlog
/// stays Python).
pub struct BadTickEvent {
    /// The poisoned session's IST calendar date, ISO `YYYY-MM-DD`.
    pub session_iso: String,
    /// How many bad-tick rows the session skipped (>= 1 by construction —
    /// only sessions with a skip emit an event).
    pub skipped: u64,
    /// The bounded multiple the screen judged against (the shell replays
    /// the event's OWN ratio so the two sides can never fork silently).
    pub max_ratio: f64,
    /// The session's FIRST offender: the last kept good close it was
    /// judged against …
    pub first_prev_close: f64,
    /// … and the offending close itself.
    pub first_close: f64,
}

/// Serialize the bad-tick events as ONE JSON array string (the FFI events
/// channel — `stats_shard.rs` idiom: hand-rolled, no serde dependency).
///
/// Field values are ints + floats + the ISO session string; the session
/// string comes ONLY from [`civil_date_iso`] (ASCII digits + hyphens by
/// construction), so no JSON string escaping is required — asserted in
/// debug builds. Floats render via `{:?}` (shortest round-trip — the bits
/// survive `json.loads` exactly, and `4.0` stays `"4.0"`, never the
/// int-punned `"4"`).
pub fn bad_tick_events_json(events: &[BadTickEvent]) -> String {
    let mut out = String::from("[");
    for (i, event) in events.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        debug_assert!(
            event
                .session_iso
                .chars()
                .all(|c| c.is_ascii_digit() || c == '-'),
            "session_iso must be the civil_date_iso ASCII shape"
        );
        out.push_str("{\"session\":\"");
        out.push_str(&event.session_iso);
        out.push_str("\",\"skipped\":");
        out.push_str(&event.skipped.to_string());
        out.push_str(",\"max_ratio\":");
        out.push_str(&format!("{:?}", event.max_ratio));
        out.push_str(",\"first_prev_close\":");
        out.push_str(&format!("{:?}", event.first_prev_close));
        out.push_str(",\"first_close\":");
        out.push_str(&format!("{:?}", event.first_close));
        out.push('}');
    }
    out.push(']');
    out
}

/// Convert a days-since-epoch ordinal to the proleptic-Gregorian civil date
/// `(year, month, day)` — the Howard Hinnant `civil_from_days` algorithm,
/// the exact law behind Python's `date.fromordinal`/polars `dt.date()`.
///
/// Feeds the IST session-date ISO strings: the Wave-D read path derives the
/// session key as `(ts_physical + IST_OFFSET).div_euclid(per_day)` (the
/// `resample.rs` fixed-arithmetic law) and formats it here. Inputs are
/// bounded by the ts-range gate (year 1..=9999), so the `{:04}` year format
/// matches Python `date.isoformat()` byte-for-byte.
pub fn civil_from_days(days_since_epoch: i64) -> (i64, u32, u32) {
    let z = days_since_epoch + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097); // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month as u32, day as u32)
}

/// Format a days-since-epoch ordinal as the ISO `YYYY-MM-DD` civil date —
/// byte-identical to Python `date.isoformat()` for years 1..=9999 (the
/// ts-range-gated envelope).
pub fn civil_date_iso(days_since_epoch: i64) -> String {
    let (year, month, day) = civil_from_days(days_since_epoch);
    format!("{year:04}-{month:02}-{day:02}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_date_iso_matches_python_date_isoformat_anchors() {
        // Epoch day 0 = 1970-01-01; the resample suite's 2024-01-01 anchor;
        // leap-day + year boundaries.
        assert_eq!(civil_date_iso(0), "1970-01-01");
        assert_eq!(civil_date_iso(19_723), "2024-01-01");
        assert_eq!(civil_date_iso(19_782), "2024-02-29");
        assert_eq!(civil_date_iso(19_783), "2024-03-01");
        assert_eq!(civil_date_iso(-1), "1969-12-31");
        // datetime.min / datetime.max calendar dates (the gate's envelope).
        assert_eq!(civil_date_iso(-719_162), "0001-01-01");
        assert_eq!(civil_date_iso(2_932_896), "9999-12-31");
    }

    #[test]
    fn civil_from_days_round_trips_a_400_year_era() {
        // Exhaustive round-trip over one full 146,097-day Gregorian era:
        // days advance one at a time and every (y, m, d) is calendar-legal.
        let mut prev = civil_from_days(-100);
        for day in -99..146_000i64 {
            let cur = civil_from_days(day);
            assert!(cur > prev, "civil date must strictly advance: {cur:?}");
            assert!((1..=12).contains(&cur.1));
            assert!((1..=31).contains(&cur.2));
            prev = cur;
        }
    }

    #[test]
    fn events_json_shape_is_the_pinned_wire_contract() {
        let events = vec![
            BadTickEvent {
                session_iso: "2024-01-01".to_string(),
                skipped: 2,
                max_ratio: 4.0,
                first_prev_close: 100.0,
                first_close: 1_000.5,
            },
            BadTickEvent {
                session_iso: "2024-01-02".to_string(),
                skipped: 1,
                max_ratio: 4.0,
                first_prev_close: 99.95,
                first_close: 10.0,
            },
        ];
        assert_eq!(
            bad_tick_events_json(&events),
            "[{\"session\":\"2024-01-01\",\"skipped\":2,\"max_ratio\":4.0,\
             \"first_prev_close\":100.0,\"first_close\":1000.5},\
             {\"session\":\"2024-01-02\",\"skipped\":1,\"max_ratio\":4.0,\
             \"first_prev_close\":99.95,\"first_close\":10.0}]"
        );
        assert_eq!(bad_tick_events_json(&[]), "[]");
    }
}
