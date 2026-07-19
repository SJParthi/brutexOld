//! Edge matrix for the pure `\x1f` first-offender scan
//! (DEC-STRATEGIES-RUST-COMBO-001 design §2.4).

use brutex_strategies::scan_sep::first_unit_separator;

fn v(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_string()).collect()
}

#[test]
fn empty_vec_is_none() {
    assert_eq!(first_unit_separator(&[]), None);
}

#[test]
fn no_hit_is_none() {
    assert_eq!(
        first_unit_separator(&v(&["mask:0000ffff", "long", "NSE-NIFTY", ""])),
        None
    );
}

#[test]
fn first_of_several_offenders_wins() {
    // The law is FIRST offender by VALUE index — never the last, never all.
    assert_eq!(
        first_unit_separator(&v(&["clean", "bad\u{1f}one", "bad\u{1f}two"])),
        Some(1)
    );
}

#[test]
fn offender_at_index_zero() {
    assert_eq!(first_unit_separator(&v(&["\u{1f}lead", "clean"])), Some(0));
}

#[test]
fn separator_at_string_start_middle_end() {
    assert_eq!(first_unit_separator(&v(&["\u{1f}abc"])), Some(0));
    assert_eq!(first_unit_separator(&v(&["ab\u{1f}cd"])), Some(0));
    assert_eq!(first_unit_separator(&v(&["abc\u{1f}"])), Some(0));
}

#[test]
fn bare_separator_only_string_hits() {
    assert_eq!(first_unit_separator(&v(&["clean", "\u{1f}"])), Some(1));
}

#[test]
fn multibyte_neighbors_no_false_positive() {
    // 0x1F can never appear inside a multi-byte UTF-8 sequence
    // (continuation bytes are 0x80..=0xBF) — non-ASCII neighbors must not
    // trip the scan, and a separator BETWEEN multi-byte chars must.
    assert_eq!(first_unit_separator(&v(&["é₹😀", "٢٠٢٣"])), None);
    assert_eq!(first_unit_separator(&v(&["😀\u{1f}₹"])), Some(0));
}

#[test]
fn nearby_controls_are_not_the_separator() {
    // \x1e (record separator) and \x20 (space) bracket \x1f — neither hits.
    assert_eq!(first_unit_separator(&v(&["a\u{1e}b", "c d"])), None);
}
