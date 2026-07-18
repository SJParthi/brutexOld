//! Proptest differential — each law vs a NAIVE in-crate reference
//! (DEC-NOTIFY-RUST-CORE-001; the brutex-feed proptest shape).
//!
//! The references are deliberately DIFFERENT implementations (per-char
//! `match` arms, index-filtered collection, `partial_cmp`) so a shared bug
//! cannot hide; the cross-LANGUAGE differential vs the Python oracle lives
//! in `tests/unit/test_notify_rust.py` (hypothesis, real wheel).

use brutex_notify::{count_or_question, dedupe_should_drop, escape_markdown_v2, truncate_id_12};
use proptest::prelude::*;

/// Naive escape reference: explicit match arms (no set membership).
fn naive_escape(text: &str) -> String {
    let mut out = String::new();
    for ch in text.chars() {
        let special = matches!(
            ch,
            '_' | '*'
                | '['
                | ']'
                | '('
                | ')'
                | '~'
                | '`'
                | '>'
                | '#'
                | '+'
                | '-'
                | '='
                | '|'
                | '{'
                | '}'
                | '.'
                | '!'
        );
        if special {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

/// Naive truncation reference: index-filtered collection.
fn naive_truncate_12(text: &str) -> String {
    text.chars()
        .enumerate()
        .filter(|(i, _)| *i < 12)
        .map(|(_, c)| c)
        .collect()
}

/// Naive dedupe reference: `partial_cmp` instead of the `<` operator.
fn naive_dedupe(window_bits: u64, last_seen_bits: Option<u64>, now_bits: u64) -> bool {
    let Some(bits) = last_seen_bits else {
        return false;
    };
    let diff = f64::from_bits(now_bits) - f64::from_bits(bits);
    matches!(
        diff.partial_cmp(&f64::from_bits(window_bits)),
        Some(std::cmp::Ordering::Less)
    )
}

proptest! {
    #[test]
    fn escape_matches_naive_reference(text in "\\PC*") {
        prop_assert_eq!(escape_markdown_v2(&text), naive_escape(&text));
    }

    #[test]
    fn escape_matches_naive_on_special_heavy_strings(
        text in proptest::collection::vec(
            proptest::char::ranges(vec![
                '\u{0}'..='\u{7f}',          // ASCII incl. every special + controls
                '\u{1F300}'..='\u{1FAFF}',   // astral emoji planes
                '\u{FE00}'..='\u{FE0F}',     // variation selectors
            ].into()),
            0..256,
        )
    ) {
        let s: String = text.into_iter().collect();
        prop_assert_eq!(escape_markdown_v2(&s), naive_escape(&s));
    }

    #[test]
    fn escape_never_changes_non_special_count(text in "\\PC*") {
        // Structure law: output length (in chars) = input + number of specials.
        let specials = text
            .chars()
            .filter(|c| brutex_notify::MDV2_SPECIALS.contains(*c))
            .count();
        prop_assert_eq!(
            escape_markdown_v2(&text).chars().count(),
            text.chars().count() + specials
        );
    }

    #[test]
    fn count_matches_decimal_grammar(value in proptest::option::of(any::<i64>())) {
        let rendered = count_or_question(value);
        match value {
            None => prop_assert_eq!(rendered, "?"),
            Some(v) => {
                // Round-trips through Rust's own parser and matches the
                // canonical decimal grammar (CPython str(int) over i64).
                prop_assert_eq!(rendered.parse::<i64>().expect("decimal round-trip"), v);
                prop_assert_eq!(&rendered, &v.to_string());
            }
        }
    }

    #[test]
    fn dedupe_matches_naive_reference(
        window in any::<u64>(),
        last_seen in proptest::option::of(any::<u64>()),
        now in any::<u64>(),
    ) {
        // Full u64 bit-pattern domain: covers NaN payloads, infinities,
        // subnormals, signed zeros — the entire f64 plane.
        prop_assert_eq!(
            dedupe_should_drop(window, last_seen, now),
            naive_dedupe(window, last_seen, now)
        );
    }

    #[test]
    fn truncate_matches_naive_reference(text in "\\PC*") {
        prop_assert_eq!(truncate_id_12(&text), naive_truncate_12(&text));
    }

    #[test]
    fn truncate_is_a_prefix_of_at_most_12_chars(text in "\\PC*") {
        let out = truncate_id_12(&text);
        prop_assert!(out.chars().count() <= 12);
        prop_assert!(text.starts_with(&out));
    }
}
