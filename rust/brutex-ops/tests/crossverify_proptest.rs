//! Property tests over the pure crossverify cores (dependency-free rlib).
//!
//! Invariants: the assembler never panics on arbitrary cell strings (the
//! unification's unwind release profile still treats panic-freedom as law); an
//! accepted render is exactly `header LF (join(cells, ',') LF)*`; every
//! accepted cell survives byte-verbatim; the key-safe charset law is total.

use brutex_ops::{
    CSV_CELLS_PER_ROW, canonical_identity_fields, render_csv_bytes, s3_key_assemble,
    symbol_key_safe,
};
use proptest::prelude::*;

/// Cells drawn from the SAFE alphabet (no metachar, no control char) — the
/// accepted-input domain.
fn safe_cell() -> impl Strategy<Value = String> {
    proptest::string::string_regex("[A-Z0-9&.%€ _;:=+@-]{0,12}").expect("valid regex")
}

/// Fixed-width strictly-increasing timestamp cells for the ordering law.
fn ts_cell(minute: usize) -> String {
    format!("2026-07-10 09:{:02}:00", 15 + (minute % 44))
}

proptest! {
    #[test]
    fn assembler_never_panics_on_arbitrary_cells(
        header in ".*",
        rows in proptest::collection::vec(
            proptest::collection::vec(".*", 0..10),
            0..8,
        ),
    ) {
        // Outcome irrelevant — the law is "no panic on ANY input".
        let _ = render_csv_bytes(&header, &rows);
    }

    #[test]
    fn accepted_render_is_the_exact_join_law(
        cells_rows in proptest::collection::vec(
            proptest::collection::vec(safe_cell(), CSV_CELLS_PER_ROW..=CSV_CELLS_PER_ROW),
            1..6,
        ),
    ) {
        let header = "symbol,timestamp_ist,open,high,low,close,volume,oi";
        // Force the ordering law: overwrite the ts cell with an increasing grid.
        let mut rows = cells_rows;
        for (i, row) in rows.iter_mut().enumerate() {
            row[1] = ts_cell(i);
        }
        match render_csv_bytes(header, &rows) {
            Ok(bytes) => {
                let mut expected = String::from(header);
                expected.push('\n');
                for row in &rows {
                    expected.push_str(&row.join(","));
                    expected.push('\n');
                }
                prop_assert_eq!(bytes, expected.into_bytes());
            }
            Err(refusal) => {
                // Only the string-law belts may refuse a safe-alphabet row
                // set: some drawn cell must genuinely trip one (control
                // chars/metachars are outside the alphabet, arity is fixed,
                // ordering is forced — so refusal is impossible here).
                prop_assert!(false, "safe rows refused: {refusal:?}");
            }
        }
    }

    #[test]
    fn key_safe_law_is_total_and_charset_exact(symbol in ".*") {
        let verdict = symbol_key_safe(&symbol);
        let charset_ok = !symbol.is_empty()
            && symbol != "."
            && symbol != ".."
            && symbol.chars().all(|ch| {
                ch.is_ascii_uppercase() || ch.is_ascii_digit() || matches!(ch, '&' | '-' | '.')
            });
        prop_assert_eq!(verdict, charset_ok);
    }

    #[test]
    fn canonical_mapper_never_panics(
        exchange in ".{0,8}",
        segment in ".{0,8}",
        symbol in ".{0,16}",
    ) {
        let _ = canonical_identity_fields(&exchange, &segment, &symbol, None, None, false, None);
        let _ = canonical_identity_fields(
            &exchange,
            &segment,
            &symbol,
            Some("NIFTY"),
            Some("2026-07-30"),
            true,
            Some("NSE"),
        );
    }

    #[test]
    fn s3_key_is_the_exact_format_law(
        prefix in "[a-z/]{1,12}",
        symbol in "[A-Z&.-]{1,8}",
    ) {
        let key = s3_key_assemble(&prefix, "2026-07-10", "NSE_EQ", &symbol);
        prop_assert_eq!(key, format!("{prefix}/2026-07-10/NSE_EQ/{symbol}.csv"));
    }
}
