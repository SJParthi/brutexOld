//! Unit edge matrix for the four pure notification laws
//! (DEC-NOTIFY-RUST-CORE-001; the killing suite for the crate's mutants).
//!
//! Every expected literal below is hand-derived from the Python oracle
//! semantics (`sweep_alerts._MDV2_SPECIALS` per-code-point scan; `str(int)`;
//! the strict `<` float compare; the `text[:12]` code-point slice) — the
//! cross-language proof is the facade burn-in + the hypothesis differential
//! in `tests/unit/test_notify_rust.py`.

use brutex_notify::{
    ID_TRUNCATE_CODE_POINTS, MDV2_SPECIALS, count_or_question, dedupe_should_drop,
    escape_markdown_v2, truncate_id_12,
};

fn bits(v: f64) -> u64 {
    v.to_bits()
}

// ── escape_markdown_v2 ──────────────────────────────────────────────────

#[test]
fn escape_set_is_exactly_the_python_frozenset() {
    // The 18 chars of sweep_alerts._MDV2_SPECIALS = frozenset(r"_*[]()~`>#+-=|{}.!").
    let expected: Vec<char> = r"_*[]()~`>#+-=|{}.!".chars().collect();
    assert_eq!(expected.len(), 18);
    let actual: Vec<char> = MDV2_SPECIALS.chars().collect();
    assert_eq!(actual, expected);
}

#[test]
fn every_escaped_char_gets_a_backslash_prefix() {
    for ch in MDV2_SPECIALS.chars() {
        let input = ch.to_string();
        let escaped = escape_markdown_v2(&input);
        assert_eq!(escaped, format!("\\{ch}"), "char {ch:?} must escape");
    }
}

#[test]
fn backslash_is_deliberately_not_escaped() {
    // The pre-existing Python property preserved bit-exactly: `\` is NOT in
    // the escape set, so it passes through UNTOUCHED (a "more correct"
    // escaper would be a parity break).
    assert_eq!(escape_markdown_v2("\\"), "\\");
    assert_eq!(escape_markdown_v2("a\\b"), "a\\b");
    // A backslash BEFORE a special still yields backslash + escaped special.
    assert_eq!(escape_markdown_v2("\\."), "\\\\.");
}

#[test]
fn empty_string_escapes_to_empty() {
    assert_eq!(escape_markdown_v2(""), "");
}

#[test]
fn plain_ascii_passes_through() {
    assert_eq!(escape_markdown_v2("hello world 42"), "hello world 42");
}

#[test]
fn mixed_body_escapes_only_specials() {
    // A realistic alert body head: emoji + punctuation.
    assert_eq!(
        escape_markdown_v2("🛑 Sweep abc HALTED: disk_full. Fix + relaunch!"),
        "🛑 Sweep abc HALTED: disk\\_full\\. Fix \\+ relaunch\\!"
    );
}

#[test]
fn astral_emoji_and_variation_selectors_pass_through() {
    // U+1F6D1 (astral) + U+26A0 U+FE0F (variation selector) survive
    // byte-identically; per-CODE-POINT iteration, never per byte.
    let body = "🛑⚠️✅🌙🔁🧹⏳🔔🔑🔐⛔❌📦⏭💀";
    assert_eq!(escape_markdown_v2(body), body);
}

#[test]
fn control_chars_and_newlines_pass_through() {
    assert_eq!(escape_markdown_v2("a\nb\tc\u{1b}d\0e"), "a\nb\tc\u{1b}d\0e");
}

#[test]
fn long_string_scales_linearly_and_correctly() {
    // 10_000 copies of a mixed unit: each `.` escapes, the rest unchanged.
    let unit = "x.\u{1F6D1}";
    let input = unit.repeat(10_000);
    let expected = "x\\.\u{1F6D1}".repeat(10_000);
    assert_eq!(escape_markdown_v2(&input), expected);
}

#[test]
fn escape_is_idempotent_only_via_double_escaping() {
    // Escaping an ALREADY-escaped body escapes again (the Python law has no
    // memory) — the escape-ONCE discipline lives at the publish seam, not
    // in the law; pin the raw behaviour so nobody "fixes" it in Rust.
    assert_eq!(escape_markdown_v2("\\."), "\\\\.");
    assert_eq!(escape_markdown_v2(&escape_markdown_v2(".")), "\\\\.");
}

// ── count_or_question ───────────────────────────────────────────────────

#[test]
fn none_renders_question_mark_never_zero() {
    assert_eq!(count_or_question(None), "?");
}

#[test]
fn counts_render_python_str_semantics() {
    assert_eq!(count_or_question(Some(0)), "0");
    assert_eq!(count_or_question(Some(7)), "7");
    assert_eq!(count_or_question(Some(-1)), "-1");
    assert_eq!(count_or_question(Some(1_234_567_890)), "1234567890");
    assert_eq!(count_or_question(Some(i64::MAX)), "9223372036854775807");
    assert_eq!(count_or_question(Some(i64::MIN)), "-9223372036854775808");
}

// ── dedupe_should_drop ──────────────────────────────────────────────────

#[test]
fn no_last_seen_is_never_dropped() {
    assert!(!dedupe_should_drop(bits(60.0), None, bits(100.0)));
}

#[test]
fn inside_window_drops_outside_window_does_not() {
    let window = bits(60.0);
    // now - last_seen = 59.999… < 60 → drop.
    assert!(dedupe_should_drop(window, Some(bits(100.0)), bits(159.999)));
    // now - last_seen = 60.0 exactly → strict `<` → NOT dropped.
    assert!(!dedupe_should_drop(window, Some(bits(100.0)), bits(160.0)));
    // now - last_seen = 60.000…1 > 60 → NOT dropped.
    assert!(!dedupe_should_drop(
        window,
        Some(bits(100.0)),
        bits(160.001)
    ));
}

#[test]
fn zero_and_negative_windows_never_drop() {
    // The Python caller short-circuits window <= 0 before the predicate,
    // but the law itself must agree if fed one: diff >= 0 is never < 0/0.0.
    assert!(!dedupe_should_drop(
        bits(0.0),
        Some(bits(100.0)),
        bits(100.0)
    ));
    assert!(!dedupe_should_drop(
        bits(-5.0),
        Some(bits(100.0)),
        bits(100.0)
    ));
    // A CLOCK-REWOUND now (now < last_seen) yields a negative diff — that
    // IS < a positive window (drops), matching the Python float law.
    assert!(dedupe_should_drop(
        bits(60.0),
        Some(bits(100.0)),
        bits(50.0)
    ));
}

#[test]
fn nan_operands_never_drop() {
    // IEEE-754: any NaN comparison is false — identical in CPython.
    let nan = bits(f64::NAN);
    assert!(!dedupe_should_drop(nan, Some(bits(100.0)), bits(100.0)));
    assert!(!dedupe_should_drop(bits(60.0), Some(nan), bits(100.0)));
    assert!(!dedupe_should_drop(bits(60.0), Some(bits(100.0)), nan));
}

#[test]
fn infinities_follow_ieee_semantics() {
    let inf = bits(f64::INFINITY);
    // now - last = inf → not < any finite window.
    assert!(!dedupe_should_drop(bits(60.0), Some(bits(0.0)), inf));
    // inf window: any finite diff < inf → drop.
    assert!(dedupe_should_drop(inf, Some(bits(0.0)), bits(1e300)));
    // inf - inf = NaN → false → not dropped.
    assert!(!dedupe_should_drop(inf, Some(inf), inf));
}

#[test]
fn negative_zero_bit_pattern_is_preserved() {
    // -0.0 has a distinct bit pattern; from_bits must round-trip it so the
    // arithmetic matches CPython (0.0 - -0.0 = 0.0, not < 0.0).
    assert!(!dedupe_should_drop(bits(0.0), Some(bits(-0.0)), bits(0.0)));
    assert!(dedupe_should_drop(
        bits(1e-300),
        Some(bits(-0.0)),
        bits(-0.0)
    ));
}

// ── truncate_id_12 ──────────────────────────────────────────────────────

#[test]
fn truncation_boundary_matrix() {
    assert_eq!(ID_TRUNCATE_CODE_POINTS, 12);
    assert_eq!(truncate_id_12(""), "");
    assert_eq!(truncate_id_12("abc"), "abc"); // shorter → unchanged
    assert_eq!(truncate_id_12("0123456789a"), "0123456789a"); // 11 → unchanged
    assert_eq!(truncate_id_12("0123456789ab"), "0123456789ab"); // 12 → unchanged
    assert_eq!(truncate_id_12("0123456789abc"), "0123456789ab"); // 13 → cut
    // A real run-id shape (hex digest prefix).
    assert_eq!(
        truncate_id_12("5b020e892247deac0123456789abcdef"),
        "5b020e892247"
    );
}

#[test]
fn truncation_counts_code_points_not_bytes() {
    // 12 astral chars = 48 UTF-8 bytes; a byte-slicing port would split a
    // char (or keep only 3). Python's s[:12] keeps all 12 — so must Rust.
    let twelve_astral = "🛑".repeat(12);
    assert_eq!(truncate_id_12(&twelve_astral), twelve_astral);
    let thirteen_astral = "🛑".repeat(13);
    assert_eq!(truncate_id_12(&thirteen_astral), twelve_astral);
    // Astral char STRADDLING the boundary: 11 ASCII + astral at index 11
    // → kept whole (code-point law).
    let s = format!("{}🛑x", "a".repeat(11));
    assert_eq!(truncate_id_12(&s), format!("{}🛑", "a".repeat(11)));
}
