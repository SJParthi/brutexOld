//! Fixture-driven golden parity for the `pull_scope` note-surgery laws.
//!
//! Consumes the COMMITTED pre-conversion oracle capture
//! `tests/golden/fixtures/pull_scope_note_surgery_rust.tsv` (the flat twin of
//! the JSON fixture — ONE generator, cross-format-pinned by the Python golden
//! test) via `include_str!`, dependency-free (the crate has no JSON parser;
//! Cargo.toml deps are hub-frozen). Every scope-token verdict, market-hours
//! rewrite and surgery output must be BYTE-identical to the Python oracle's
//! frozen bytes — the whole contract of a string-surgery port.

use brutex_pull_state::pull_scope::{
    FNO_ONLY_PREFIX, FNO_SKIPPED_CLAUSE, FULL_PREFIX, GREEKS_NOT_RUN_NOTE,
    MARKET_HOURS_PHASES_CLAUSE, MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE,
    MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE, MARKET_HOURS_RESUME_CLAUSE,
    MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE, MARKET_HOURS_WINDOW_CLAUSE,
    MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE, NOOP_FNO_CLAUSE, NOOP_SPOT_CLAUSE, SCOPE_FNO_ONLY,
    SCOPE_FULL, SCOPE_SPOT_ONLY, SPOT_ONLY_PREFIX, SPOT_SKIPPED_CLAUSE, STOP_FNO_CLAUSE,
    STOP_FNO_SPOT_ONLY_CLAUSE, fno_only_note_surgery, market_hours_note_rewrite, scope_token,
    spot_only_note_surgery,
};

/// The committed golden fixture (repo-relative; compile-time include).
const FIXTURE: &str =
    include_str!("../../../tests/golden/fixtures/pull_scope_note_surgery_rust.tsv");

/// Unescape one TSV field (the exact mirror of the generator's
/// `escape_field`): `\\` `\t` `\n` `\r` + `\u{XXXX}` for C0/C1 controls.
fn unescape(field: &str) -> String {
    let mut out = String::with_capacity(field.len());
    let mut chars = field.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('\\') => out.push('\\'),
            Some('t') => out.push('\t'),
            Some('n') => out.push('\n'),
            Some('r') => out.push('\r'),
            Some('u') => {
                assert_eq!(chars.next(), Some('{'), "malformed \\u escape in fixture");
                let hex: String = chars.by_ref().take_while(|&h| h != '}').collect();
                let code = u32::from_str_radix(&hex, 16).expect("hex codepoint");
                out.push(char::from_u32(code).expect("valid codepoint"));
            }
            other => panic!("malformed escape in fixture: \\{other:?}"),
        }
    }
    out
}

/// The frozen anchor literal for a Python constant NAME (the cross-language
/// drift pin: fixture bytes == these consts == the Python module's).
fn anchor_by_name(name: &str) -> &'static str {
    match name {
        "SCOPE_FULL" => SCOPE_FULL,
        "SCOPE_SPOT_ONLY" => SCOPE_SPOT_ONLY,
        "SCOPE_FNO_ONLY" => SCOPE_FNO_ONLY,
        "GREEKS_NOT_RUN_NOTE" => GREEKS_NOT_RUN_NOTE,
        "FNO_SKIPPED_CLAUSE" => FNO_SKIPPED_CLAUSE,
        "SPOT_SKIPPED_CLAUSE" => SPOT_SKIPPED_CLAUSE,
        "_NOOP_SPOT_CLAUSE" => NOOP_SPOT_CLAUSE,
        "_NOOP_FNO_CLAUSE" => NOOP_FNO_CLAUSE,
        "_STOP_FNO_CLAUSE" => STOP_FNO_CLAUSE,
        "_STOP_FNO_SPOT_ONLY_CLAUSE" => STOP_FNO_SPOT_ONLY_CLAUSE,
        "_FULL_PREFIX" => FULL_PREFIX,
        "_SPOT_ONLY_PREFIX" => SPOT_ONLY_PREFIX,
        "_FNO_ONLY_PREFIX" => FNO_ONLY_PREFIX,
        "_MARKET_HOURS_WINDOW_CLAUSE" => MARKET_HOURS_WINDOW_CLAUSE,
        "_MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE" => MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE,
        "_MARKET_HOURS_PHASES_CLAUSE" => MARKET_HOURS_PHASES_CLAUSE,
        "_MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE" => MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE,
        "_MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE" => MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE,
        "_MARKET_HOURS_RESUME_CLAUSE" => MARKET_HOURS_RESUME_CLAUSE,
        "_MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE" => MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE,
        other => panic!("fixture names an anchor this crate does not carry: {other}"),
    }
}

struct Counts {
    anchors: usize,
    scope: usize,
    mh: usize,
    spot: usize,
    fno: usize,
}

fn drive() -> Counts {
    let mut counts = Counts {
        anchors: 0,
        scope: 0,
        mh: 0,
        spot: 0,
        fno: 0,
    };
    for (lineno, line) in FIXTURE.lines().enumerate() {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields: Vec<&str> = line.split('\t').collect();
        match fields[0] {
            "A" => {
                assert_eq!(fields.len(), 3, "line {lineno}");
                assert_eq!(
                    anchor_by_name(fields[1]),
                    unescape(fields[2]),
                    "anchor literal DRIFTED between Python and Rust: {} (line {lineno})",
                    fields[1],
                );
                counts.anchors += 1;
            }
            "S" => {
                assert_eq!(fields.len(), 3, "line {lineno}");
                let token_field = fields[1];
                let expected: u8 = fields[2].parse().expect("scope code");
                let got = if token_field == "N" {
                    scope_token(None)
                } else {
                    let stripped = token_field
                        .strip_prefix('T')
                        .unwrap_or_else(|| panic!("bad scope token marker (line {lineno})"));
                    scope_token(Some(&unescape(stripped)))
                };
                assert_eq!(got, expected, "scope verdict diverged (line {lineno})");
                counts.scope += 1;
            }
            "M" => {
                assert_eq!(fields.len(), 4, "line {lineno}");
                let note = unescape(fields[1]);
                let replacement = match fields[2] {
                    "SPOT" => MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE.to_string(),
                    "FNO" => MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE.to_string(),
                    custom => unescape(
                        custom
                            .strip_prefix("C:")
                            .unwrap_or_else(|| panic!("bad phases marker (line {lineno})")),
                    ),
                };
                assert_eq!(
                    market_hours_note_rewrite(&note, &replacement),
                    unescape(fields[3]),
                    "market-hours rewrite diverged (line {lineno})",
                );
                counts.mh += 1;
            }
            "P" => {
                assert_eq!(fields.len(), 5, "line {lineno}");
                let (note, prefix, clause) = spot_only_note_surgery(&unescape(fields[1]));
                assert_eq!(
                    note,
                    unescape(fields[2]),
                    "spot surgery diverged (line {lineno})"
                );
                assert_eq!(prefix, fields[3] == "1", "spot prefix flag (line {lineno})");
                assert_eq!(clause, fields[4] == "1", "spot clause flag (line {lineno})");
                counts.spot += 1;
            }
            "F" => {
                assert_eq!(fields.len(), 5, "line {lineno}");
                let (note, prefix, clause) = fno_only_note_surgery(&unescape(fields[1]));
                assert_eq!(
                    note,
                    unescape(fields[2]),
                    "fno surgery diverged (line {lineno})"
                );
                assert_eq!(prefix, fields[3] == "1", "fno prefix flag (line {lineno})");
                assert_eq!(clause, fields[4] == "1", "fno clause flag (line {lineno})");
                counts.fno += 1;
            }
            other => panic!("unknown fixture record kind {other:?} (line {lineno})"),
        }
    }
    counts
}

#[test]
fn golden_fixture_parity_byte_exact() {
    let counts = drive();
    // Anti-vacuous floors: the committed corpus genuinely covers every law
    // (a truncated / mis-parsed fixture must fail LOUDLY, never pass empty).
    assert_eq!(counts.anchors, 20, "anchor pin rows");
    assert!(counts.scope >= 20, "scope corpus shrank: {}", counts.scope);
    assert!(counts.mh >= 6, "market-hours corpus shrank: {}", counts.mh);
    assert!(
        counts.spot >= 9,
        "spot-surgery corpus shrank: {}",
        counts.spot
    );
    assert!(counts.fno >= 6, "fno-surgery corpus shrank: {}", counts.fno);
}

#[test]
fn unescape_roundtrip_controls() {
    assert_eq!(unescape("a\\tb\\nc\\\\d"), "a\tb\nc\\d");
    assert_eq!(unescape("x\\u{001c}y\\u{0000}z"), "x\u{1c}y\u{0}z");
    assert_eq!(unescape("em—dash raw"), "em—dash raw");
}
