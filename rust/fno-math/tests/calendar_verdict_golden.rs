//! Native replay of the frozen 2,557-day calendar verdict-table golden
//! (DEC-CALENDAR-VERDICT-RUST-001, ADR-0019 / MIGRATION.md #1727).
//!
//! Replays `tests/golden/fixtures/calendar_verdict_table_golden.json` —
//! the full 2020-2026 per-date verdict table, the two out-of-coverage
//! strips, and the engine-relevant scalar families — through the PURE Rust
//! [`fno_math::calendar_verdict::CalendarEngine`], with the engine CONFIG
//! **derived from the fixture itself** (holiday ordinals = weekday records
//! whose `td` is false with no special/Muhurat session; Muhurat +
//! special-weekend sessions = the records that carry them; extended
//! sessions = trading weekdays whose bounds differ from the regular bounds
//! — DEC-EXTENDED-SESSION-OUTAGE-001, 2021-02-24; coverage span =
//! the fixture meta) — so NO calendar data is duplicated into Rust
//! (ADR-0012 D8: the data stays in `brutex.calendars`; the Python-side
//! flag-ON golden proves the real nse.py-built config, THIS test proves the
//! verdict LOGIC reproduces the frozen table).
//!
//! Weekend-dated holidays are invisible to this reconstruction BY
//! CONSTRUCTION — and provably verdict-invisible: every fixture field
//! (`td`/`tdc`/`s`/`sc`/`m`/`w`/`p`) resolves the weekend arm before any
//! holiday probe.
//!
//! Date arithmetic here is the Hinnant pair (test-local, no chrono),
//! self-pinned below against Python-computed ordinals.

use std::collections::HashSet;

use fno_math::calendar_verdict::{
    CalendarEngine, SESSION_MUHURAT, SESSION_REGULAR, VERDICT_RAISE, VERDICT_TRUE,
    expected_bars_for_tf, forced_exit_bar_verdict, forced_exit_seconds_of_close, last_bar_complete,
    weekday_of_ordinal,
};

// ── date/time helpers (test-local) ──────────────────────────────────────

/// Python `date(y, m, d).toordinal()` — Hinnant `days_from_civil` + 719_163.
fn ordinal_from_ymd(y: i32, m: u32, d: u32) -> i32 {
    let yy = i64::from(if m <= 2 { y - 1 } else { y });
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let mp = (i64::from(m) + 9) % 12;
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    (era * 146_097 + doe - 719_468 + 719_163) as i32
}

/// Inverse of [`ordinal_from_ymd`] — Hinnant `civil_from_days`.
fn ymd_from_ordinal(ordinal: i32) -> (i32, u32, u32) {
    let z = i64::from(ordinal) - 719_163 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    ((y + i64::from(m <= 2)) as i32, m, d)
}

fn parse_iso_date(s: &str) -> i32 {
    let y: i32 = s[0..4].parse().expect("year");
    let m: u32 = s[5..7].parse().expect("month");
    let d: u32 = s[8..10].parse().expect("day");
    ordinal_from_ymd(y, m, d)
}

fn iso_date(ordinal: i32) -> String {
    let (y, m, d) = ymd_from_ordinal(ordinal);
    format!("{y:04}-{m:02}-{d:02}")
}

/// `"HH:MM:SS"` (or `"HH:MM"`) → seconds-of-day.
fn parse_iso_time(s: &str) -> u32 {
    let h: u32 = s[0..2].parse().expect("hour");
    let m: u32 = s[3..5].parse().expect("minute");
    let sec: u32 = if s.len() >= 8 {
        s[6..8].parse().expect("second")
    } else {
        0
    };
    h * 3600 + m * 60 + sec
}

fn iso_time(seconds: u32) -> String {
    format!(
        "{:02}:{:02}:{:02}",
        seconds / 3600,
        (seconds / 60) % 60,
        seconds % 60
    )
}

#[test]
fn hinnant_pair_self_pins() {
    // Python-computed anchors (date.toordinal()).
    assert_eq!(ordinal_from_ymd(2026, 7, 6), 739_803);
    assert_eq!(ordinal_from_ymd(2020, 1, 1), 737_425);
    assert_eq!(ordinal_from_ymd(2024, 2, 29), 738_945);
    assert_eq!(ordinal_from_ymd(1970, 1, 1), 719_163);
    assert_eq!(ordinal_from_ymd(1, 1, 1), 1);
    for ordinal in [1, 719_163, 737_425, 738_945, 739_803, 739_982] {
        let (y, m, d) = ymd_from_ordinal(ordinal);
        assert_eq!(ordinal_from_ymd(y, m, d), ordinal, "round trip {ordinal}");
    }
}

// ── fixture loading + config derivation ─────────────────────────────────

fn fixture_doc() -> serde_json::Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/fixtures/calendar_verdict_table_golden.json");
    let raw = std::fs::read_to_string(path).expect("calendar fixture readable");
    serde_json::from_str(&raw).expect("valid JSON")
}

/// Derive the engine config from the fixture's own day table (module docs).
fn engine_from_fixture(doc: &serde_json::Value) -> CalendarEngine {
    let days = doc["days"].as_array().expect("days");
    let mut holidays: Vec<i32> = Vec::new();
    let mut muhurat: Vec<(i32, u32, u32)> = Vec::new();
    let mut special: Vec<(i32, u32, u32)> = Vec::new();
    let mut extended: Vec<(i32, u32, u32)> = Vec::new();
    // Pass 1: the regular session bounds — the FIRST plain trading weekday
    // (2020-01-01 in the frozen span). Needed BEFORE classification so an
    // extended day (a trading weekday whose bounds differ from the regular
    // bounds — DEC-EXTENDED-SESSION-OUTAGE-001, 2021-02-24 close 17:00) can
    // be told apart from a regular record wherever it sits in the table.
    let mut session_bounds: Option<(u32, u32)> = None;
    for record in days {
        let td = record["td"].as_bool().expect("td");
        let m = record["m"].as_bool().expect("m");
        let w = record["w"].as_bool().expect("w");
        if td && !w && !m {
            let session = &record["s"];
            let o = parse_iso_time(session["o"].as_str().expect("regular open"));
            let c = parse_iso_time(session["c"].as_str().expect("regular close"));
            session_bounds = Some((o, c));
            break;
        }
    }
    let (open_s, close_s) = session_bounds.expect("a regular session exists in the span");
    // Pass 2: classify every record against the derived bounds.
    for record in days {
        let ordinal = parse_iso_date(record["d"].as_str().expect("d"));
        let td = record["td"].as_bool().expect("td");
        let m = record["m"].as_bool().expect("m");
        let w = record["w"].as_bool().expect("w");
        let session = &record["s"];
        if m {
            let o = parse_iso_time(session["o"].as_str().expect("muhurat open"));
            let c = parse_iso_time(session["c"].as_str().expect("muhurat close"));
            muhurat.push((ordinal, o, c));
        }
        if w {
            let o = parse_iso_time(session["o"].as_str().expect("special open"));
            let c = parse_iso_time(session["c"].as_str().expect("special close"));
            special.push((ordinal, o, c));
        }
        let weekday = weekday_of_ordinal(ordinal) < 5;
        if weekday && !w && !td {
            // A non-trading weekday with no special session is a listed
            // holiday (Muhurat weekdays are holidays in the real data too).
            holidays.push(ordinal);
        }
        if td && !w && !m {
            let o = parse_iso_time(session["o"].as_str().expect("trading open"));
            let c = parse_iso_time(session["c"].as_str().expect("trading close"));
            if (o, c) != (open_s, close_s) {
                // A trading weekday with non-regular bounds is an EXTENDED
                // session (2021-02-24, the NSE exchange-outage day).
                extended.push((ordinal, o, c));
            }
        }
    }
    let meta = &doc["meta"];
    CalendarEngine::new(
        holidays,
        vec![], // NSE_ONLY — empty by verified fact (the exchange-law tripwire)
        vec![], // BSE_ONLY — empty by verified fact
        muhurat,
        special,
        extended,
        meta["coverage_first_year"].as_i64().expect("first year") as i32,
        meta["coverage_last_year"].as_i64().expect("last year") as i32,
        open_s,
        close_s,
    )
}

// ── replay ───────────────────────────────────────────────────────────────

fn session_json(session: Option<(u8, u32, u32)>) -> serde_json::Value {
    match session {
        None => serde_json::Value::Null,
        Some((kind, open_s, close_s)) => {
            let t = if kind == SESSION_MUHURAT {
                "muhurat"
            } else {
                assert_eq!(kind, SESSION_REGULAR);
                "regular"
            };
            serde_json::json!({"t": t, "o": iso_time(open_s), "c": iso_time(close_s)})
        }
    }
}

fn replay_record(eng: &CalendarEngine, record: &serde_json::Value) {
    let d = record["d"].as_str().expect("d");
    let ordinal = parse_iso_date(d);
    let sel = 0u8; // exchange-agnostic default; NSE==BSE==ALL today (tripwire)
    assert_eq!(
        eng.is_trading_day(ordinal, sel),
        record["td"].as_bool().expect("td"),
        "td {d}"
    );
    let tdc = match eng.is_trading_day_checked(ordinal, sel) {
        VERDICT_RAISE => "raises",
        VERDICT_TRUE => "true",
        _ => "false",
    };
    assert_eq!(tdc, record["tdc"].as_str().expect("tdc"), "tdc {d}");
    assert_eq!(
        session_json(eng.get_session(ordinal, sel)),
        record["s"],
        "s {d}"
    );
    let sc = match eng.get_session_checked(ordinal, sel) {
        (VERDICT_RAISE, ..) => serde_json::Value::String("raises".to_string()),
        (VERDICT_TRUE, kind, open_s, close_s) => session_json(Some((kind, open_s, close_s))),
        _ => serde_json::Value::Null,
    };
    assert_eq!(sc, record["sc"], "sc {d}");
    assert_eq!(
        eng.has_muhurat(ordinal),
        record["m"].as_bool().expect("m"),
        "m {d}"
    );
    assert_eq!(
        eng.is_special_weekend(ordinal),
        record["w"].as_bool().expect("w"),
        "w {d}"
    );
    let prev = match eng.previous_trading_session(ordinal, sel, &HashSet::new()) {
        (VERDICT_TRUE, prev_ordinal) => serde_json::Value::String(iso_date(prev_ordinal)),
        _ => serde_json::Value::Null,
    };
    assert_eq!(prev, record["p"], "p {d}");
}

#[test]
fn day_table_replays_bit_for_bit() {
    let doc = fixture_doc();
    let eng = engine_from_fixture(&doc);
    let days = doc["days"].as_array().expect("days");
    assert_eq!(days.len(), 2557, "the frozen span is 2,557 records");
    for record in days {
        replay_record(&eng, record);
    }
}

#[test]
fn out_of_coverage_strips_replay() {
    let doc = fixture_doc();
    let eng = engine_from_fixture(&doc);
    let strips = doc["strips"].as_array().expect("strips");
    assert_eq!(strips.len(), 20);
    for record in strips {
        replay_record(&eng, record);
    }
}

#[test]
fn exchange_law_nse_bse_default_identical() {
    // NSE_ONLY / BSE_ONLY are empty by verified fact, so every verdict is
    // selector-invariant — the Rust twin of the Python exchange-law tripwire.
    let doc = fixture_doc();
    let eng = engine_from_fixture(&doc);
    for record in doc["days"].as_array().expect("days").iter().step_by(13) {
        let ordinal = parse_iso_date(record["d"].as_str().expect("d"));
        for sel in [1u8, 2u8] {
            assert_eq!(
                eng.is_trading_day(ordinal, sel),
                eng.is_trading_day(ordinal, 0)
            );
            assert_eq!(
                eng.is_trading_day_checked(ordinal, sel),
                eng.is_trading_day_checked(ordinal, 0)
            );
            assert_eq!(eng.get_session(ordinal, sel), eng.get_session(ordinal, 0));
        }
    }
}

#[test]
fn scalar_families_replay() {
    let doc = fixture_doc();
    let eng = engine_from_fixture(&doc);
    let scalars = &doc["scalars"];

    // expected_bars_per_day_for_tf — ceiling law + the raise cells (-1).
    for (tf, expected) in scalars["expected_bars_per_day_for_tf"]
        .as_object()
        .expect("bars")
    {
        let verdict = expected_bars_for_tf(tf.parse::<i64>().expect("tf"));
        match expected {
            serde_json::Value::String(s) => {
                assert_eq!(s, "raises");
                assert_eq!(verdict, -1, "tf {tf}");
            }
            other => assert_eq!(verdict, other.as_i64().expect("bars"), "tf {tf}"),
        }
    }

    // is_forced_exit_bar — strict-> boundary under default + Muhurat bounds.
    const DEFAULT_BOUNDARY_S: u32 = 15 * 3600 + 20 * 60; // 15:20 IST
    for probe in scalars["is_forced_exit_bar"].as_array().expect("probes") {
        let open_s = parse_iso_time(probe["t"].as_str().expect("t"));
        let tf = probe["tf"].as_i64().expect("tf");
        let boundary = match &probe["forced_exit_seconds"] {
            serde_json::Value::Null => DEFAULT_BOUNDARY_S,
            v => v.as_u64().expect("boundary") as u32,
        };
        let expected = i8::from(probe["result"].as_bool().expect("result"));
        assert_eq!(
            forced_exit_bar_verdict(open_s, tf, boundary),
            expected,
            "{probe}"
        );
    }

    // daily_derive_cutoff — the strict-> at-close law (microsecond-exact).
    for probe in scalars["daily_derive_cutoff"].as_array().expect("cutoff") {
        let now = probe["now_ist"].as_str().expect("now_ist");
        let ordinal = parse_iso_date(&now[..10]);
        let now_us = u64::from(parse_iso_time(&now[11..])) * 1_000_000;
        let cutoff = eng.daily_derive_cutoff(ordinal, now_us, 0);
        assert_eq!(
            iso_date(cutoff),
            probe["cutoff"].as_str().expect("cutoff"),
            "{probe}"
        );
    }

    // session_last_bar_complete — the +1-minute >= law (same-date probes).
    for probe in scalars["session_last_bar_complete"]
        .as_array()
        .expect("last bar")
    {
        let session_ordinal = parse_iso_date(probe["session_date"].as_str().expect("date"));
        let (_, _, close_s) = eng
            .get_session(session_ordinal, 0)
            .expect("probe date trades");
        let last_bar = probe["last_bar_ist"].as_str().expect("last bar");
        assert_eq!(
            &last_bar[..10],
            probe["session_date"].as_str().expect("date")
        );
        let last_s = parse_iso_time(&last_bar[11..]);
        assert_eq!(
            last_bar_complete(close_s, last_s),
            probe["result"].as_bool().expect("result"),
            "{probe}"
        );
    }

    // previous_trading_session with the observed overlay (remove-only law).
    for probe in scalars["previous_session_observed"]
        .as_array()
        .expect("observed")
    {
        let ordinal = parse_iso_date(probe["d"].as_str().expect("d"));
        let observed: HashSet<i32> = probe["observed"]
            .as_array()
            .expect("observed list")
            .iter()
            .map(|v| parse_iso_date(v.as_str().expect("iso")))
            .collect();
        let got = match eng.previous_trading_session(ordinal, 0, &observed) {
            (VERDICT_TRUE, prev) => serde_json::Value::String(iso_date(prev)),
            _ => serde_json::Value::Null,
        };
        assert_eq!(got, probe["result"], "{probe}");
    }

    // Muhurat precedence: every Muhurat date is NOT a trading day AND its
    // session IS the Muhurat session.
    let rows = scalars["muhurat_precedence"].as_array().expect("muhurat");
    assert_eq!(rows.len(), 6);
    for row in rows {
        let ordinal = parse_iso_date(row["d"].as_str().expect("d"));
        assert!(!eng.is_trading_day(ordinal, 0));
        let (kind, ..) = eng.get_session(ordinal, 0).expect("muhurat session");
        assert_eq!(kind, SESSION_MUHURAT);
    }

    // forced_exit rows: session times + close − 10min, both representations.
    for row in scalars["forced_exit"].as_array().expect("forced exit") {
        let ordinal = parse_iso_date(row["d"].as_str().expect("d"));
        let (_, open_s, close_s) = eng.get_session(ordinal, 0).expect("probe date trades");
        assert_eq!(iso_time(open_s), row["o"].as_str().expect("o"), "{row}");
        assert_eq!(iso_time(close_s), row["c"].as_str().expect("c"), "{row}");
        let exit_seconds = forced_exit_seconds_of_close(close_s);
        assert_eq!(
            i64::from(exit_seconds),
            row["exit_seconds"].as_i64().expect("exit_seconds"),
            "{row}"
        );
        assert_eq!(
            iso_time(exit_seconds),
            row["exit_time"].as_str().expect("exit_time"),
            "{row}"
        );
    }
}
