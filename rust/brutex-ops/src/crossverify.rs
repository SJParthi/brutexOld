//! Cross-verify lane — pure string-law twins of the Python pinned laws.
//!
//! DEC-CROSSVERIFY-RUST-CORE (first cut). Two law families, both consumed by
//! the default-OFF facade `brutex/crossverify/rust_seam.py` with the verbatim
//! Python bodies retained as the LIVE parity oracles:
//!
//! 1. [`render_csv_bytes`] — the pinned CSV byte ASSEMBLY of
//!    `brutex/crossverify/csv_render.py::render_day_csv`: UTF-8, NO BOM,
//!    byte-exact header line, LF terminator with a trailing newline on the
//!    LAST row, comma-join with the no-quote-ever property. The
//!    Decimal/datetime/session REFUSAL laws (`_validate_row`) and the
//!    `csv_safe` formula-guard mutation check stay PYTHON-side (they operate
//!    on `Decimal`/`datetime`/the COMMON guard — none of which crosses the
//!    ints+strings FFI); this core re-refuses the STRING-level laws as a
//!    defense-in-depth belt (typed [`CsvRefusal`] codes): empty row set,
//!    wrong cell arity, non-increasing timestamp cells, CSV metacharacters,
//!    control characters.
//! 2. [`canonical_identity_fields`] + [`s3_key_assemble`] — the deny-by-
//!    default lake-coordinate → canonical-identity mapping and the ONE S3
//!    key law of `brutex/crossverify/canonical.py`. The F&O grammar
//!    (`parse_fno_symbol`) is NEVER re-implemented here — Python parses and
//!    passes the parsed fields (underlying / expiry-isoformat / is-future /
//!    exchange) across the FFI.
//!
//! ## Ordering-law honesty (documented equivalence, not an approximation)
//!
//! The Python ordering law compares `row.ts_utc` datetimes; this belt
//! compares the PRE-FORMATTED IST minute cells (`YYYY-MM-DD HH:MM:SS`,
//! fixed width) lexicographically. For any row set that passed the
//! Python-side `_validate_row` (same IST session date, second == 0,
//! microsecond == 0) string order equals instant order and string equality
//! equals instant equality, so the two laws agree on every input that can
//! reach this core through the facade; on hostile direct input the belt is
//! strictly a refusal belt, never a publisher.
//!
//! Structurally panic-free: no indexing without a bounds check, no
//! unwrap/expect on data (the release profile is `panic = "abort"` — a
//! panic would bypass the facade's belt in release wheels, so none may
//! exist).

/// Refusal codes carried across the FFI as plain ints (the typed-code law).
/// The facade maps a code onto the EXISTING `ExportContractError` semantics
/// by re-running the Python oracle (which raises the exact pinned message
/// bytes for genuinely-bad input) — never a new exception class.
pub const CSV_REFUSAL_EMPTY_ROWS: u8 = 1;
/// A row whose cell count is not [`CSV_CELLS_PER_ROW`] (defensive — the
/// Python caller always builds exactly 8 cells).
pub const CSV_REFUSAL_WRONG_ARITY: u8 = 2;
/// Timestamp cells not strictly increasing (duplicate/misordered minutes).
pub const CSV_REFUSAL_NON_INCREASING_TS: u8 = 3;
/// A cell carrying a CSV metacharacter (`,` `"` LF CR) — the no-quote-ever
/// property.
pub const CSV_REFUSAL_METACHAR: u8 = 4;
/// A cell carrying a control character (C0 block or DEL) — the canonical
/// symbol-allowlist mirror.
pub const CSV_REFUSAL_CONTROL_CHAR: u8 = 5;

/// Cells per data row: `symbol, timestamp_ist, open, high, low, close,
/// volume, oi` — the pinned 8-column contract.
pub const CSV_CELLS_PER_ROW: usize = 8;

/// The timestamp cell's index inside a row (the ordering-law column —
/// `timestamp_ist` is the second header column).
pub const CSV_TS_CELL_INDEX: usize = 1;

/// One typed refusal: `(code, row index, cell index)`. For
/// [`CSV_REFUSAL_EMPTY_ROWS`] both indices are 0; for
/// [`CSV_REFUSAL_WRONG_ARITY`] `cell` carries the OBSERVED arity; for
/// [`CSV_REFUSAL_NON_INCREASING_TS`] `cell` is [`CSV_TS_CELL_INDEX`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CsvRefusal {
    pub code: u8,
    pub row: usize,
    pub cell: usize,
}

/// `true` iff *cell* carries a CSV metacharacter (`,` `"` LF CR) — the
/// byte-for-byte twin of `csv_render._FORBIDDEN_CELL_CHARS`.
fn cell_has_metachar(cell: &str) -> bool {
    cell.chars().any(|ch| matches!(ch, ',' | '"' | '\n' | '\r'))
}

/// `true` iff *cell* carries a control character (C0 block `<= 0x1f` or DEL
/// `0x7f`) — the twin of `csv_render._CONTROL_CHAR_MAX` / `_DEL_CHAR`.
/// Checked AFTER the metachar law per cell (LF/CR are both — the metachar
/// refusal wins, mirroring the Python `_validate_cells` order).
fn cell_has_control_char(cell: &str) -> bool {
    cell.chars().any(|ch| {
        let code = ch as u32;
        code <= 0x1f || code == 0x7f
    })
}

/// Assemble one day file's pinned contract bytes, or refuse (typed code).
///
/// *header* is the byte-exact header LINE (no terminator — this core owns
/// every LF). *rows* are the PRE-VALIDATED, PRE-FORMATTED cell rows the
/// Python side built (prices already 2dp via `round_money`, timestamps
/// already IST-rendered). Laws, in the Python loop's order per row: arity
/// (defensive, first — the later laws index into the row), timestamp
/// ordering (before the cell scans, mirroring `render_day_csv` where the
/// ordering compare precedes `_validate_cells`), then per cell in order:
/// metachar, control char. Assembly: `header LF (cells joined by comma LF)*`
/// — QUOTE_MINIMAL never quotes a metachar-free cell, so the join is the
/// exact `csv.writer` byte law. O(total cell bytes); zero I/O.
pub fn render_csv_bytes(header: &str, rows: &[Vec<String>]) -> Result<Vec<u8>, CsvRefusal> {
    if rows.is_empty() {
        return Err(CsvRefusal {
            code: CSV_REFUSAL_EMPTY_ROWS,
            row: 0,
            cell: 0,
        });
    }
    // Capacity: header + per-row cells + separators (7 commas + 1 LF = 8).
    let cells_len: usize = rows
        .iter()
        .flat_map(|row| row.iter())
        .map(String::len)
        .sum();
    let mut out =
        String::with_capacity(header.len() + 1 + cells_len + rows.len() * CSV_CELLS_PER_ROW);
    out.push_str(header);
    out.push('\n');
    let mut prev_ts: Option<&str> = None;
    for (row_idx, cells) in rows.iter().enumerate() {
        if cells.len() != CSV_CELLS_PER_ROW {
            return Err(CsvRefusal {
                code: CSV_REFUSAL_WRONG_ARITY,
                row: row_idx,
                cell: cells.len(),
            });
        }
        // Arity checked above — the index is in bounds by construction.
        let ts = cells[CSV_TS_CELL_INDEX].as_str();
        if let Some(prev) = prev_ts
            && ts <= prev
        {
            return Err(CsvRefusal {
                code: CSV_REFUSAL_NON_INCREASING_TS,
                row: row_idx,
                cell: CSV_TS_CELL_INDEX,
            });
        }
        prev_ts = Some(ts);
        for (cell_idx, cell) in cells.iter().enumerate() {
            if cell_has_metachar(cell) {
                return Err(CsvRefusal {
                    code: CSV_REFUSAL_METACHAR,
                    row: row_idx,
                    cell: cell_idx,
                });
            }
            if cell_has_control_char(cell) {
                return Err(CsvRefusal {
                    code: CSV_REFUSAL_CONTROL_CHAR,
                    row: row_idx,
                    cell: cell_idx,
                });
            }
        }
        for (cell_idx, cell) in cells.iter().enumerate() {
            if cell_idx > 0 {
                out.push(',');
            }
            out.push_str(cell);
        }
        out.push('\n');
    }
    Ok(out.into_bytes())
}

// ───────────────────────── canonical-identity law ─────────────────────────

/// Segment tokens — the byte twins of `canonical.SEGMENT_*`.
pub const SEGMENT_IDX: &str = "IDX_I";
pub const SEGMENT_NSE_EQ: &str = "NSE_EQ";
pub const SEGMENT_BSE_EQ: &str = "BSE_EQ";
pub const SEGMENT_NSE_FNO: &str = "NSE_FNO";
pub const SEGMENT_BSE_FNO: &str = "BSE_FNO";

/// Instrument classes — the byte twins of `canonical.CLASS_*`.
pub const CLASS_INDEX_SPOT: &str = "index_spot";
pub const CLASS_CASH_SPOT: &str = "cash_spot";
pub const CLASS_FUT: &str = "fut";

/// From BSE only SENSEX — the twin of `canonical._BSE_INDEX_EXPORT_SYMBOLS`
/// (itself the mirror of the universe IDX fetch rule).
const BSE_INDEX_EXPORT_SYMBOLS: [&str; 1] = ["SENSEX"];

/// The STRUCTURAL FUT-arm allowlist — the twin of
/// `canonical.EXPORT_FUTURES_UNDERLYINGS`.
const EXPORT_FUTURES_UNDERLYINGS: [&str; 3] = ["NIFTY", "BANKNIFTY", "SENSEX"];

/// `true` iff *symbol* passes the positive S3-key/staging/cell allowlist —
/// the byte twin of `canonical.symbol_key_safe` (`A-Z 0-9 & - .`; empty and
/// the pure-dot path tokens `.` / `..` refused explicitly).
pub fn symbol_key_safe(symbol: &str) -> bool {
    if symbol.is_empty() || symbol == "." || symbol == ".." {
        return false;
    }
    symbol
        .chars()
        .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || matches!(ch, '&' | '-' | '.'))
}

fn eq_token_for(exchange: &str) -> Option<&'static str> {
    match exchange {
        "NSE" => Some(SEGMENT_NSE_EQ),
        "BSE" => Some(SEGMENT_BSE_EQ),
        _ => None,
    }
}

fn fno_token_for(exchange: &str) -> Option<&'static str> {
    match exchange {
        "NSE" => Some(SEGMENT_NSE_FNO),
        "BSE" => Some(SEGMENT_BSE_FNO),
        _ => None,
    }
}

/// The mapped identity's six fields, in `CanonicalIdentity` declaration
/// order: `(exchange, lake_segment, lake_symbol, canonical_symbol,
/// segment_token, instrument_class)`.
pub type IdentityFields = (String, String, String, String, String, String);

/// Map one lake coordinate to its canonical identity fields, or `None`
/// (deny-by-default — the exact decision twin of
/// `canonical.canonical_identity`).
///
/// The FNO arm consumes the PYTHON-parsed grammar fields (`parse_fno_symbol`
/// is never re-implemented): `fno_underlying` / `fno_expiry_iso` (the
/// expiry's `date.isoformat()`) / `fno_exchange` are `None` exactly when the
/// Python parse returned `None`, and `fno_is_future` is `true` only for a
/// parsed FUT leg. On non-FNO segments those arguments are ignored.
#[allow(clippy::too_many_arguments)] // Mirrors the Python law's inputs 1:1.
pub fn canonical_identity_fields(
    exchange: &str,
    segment: &str,
    lake_symbol: &str,
    fno_underlying: Option<&str>,
    fno_expiry_iso: Option<&str>,
    fno_is_future: bool,
    fno_exchange: Option<&str>,
) -> Option<IdentityFields> {
    match segment {
        "INDEX" => {
            if !symbol_key_safe(lake_symbol) {
                return None;
            }
            if exchange != "NSE"
                && !(exchange == "BSE" && BSE_INDEX_EXPORT_SYMBOLS.contains(&lake_symbol))
            {
                return None;
            }
            Some((
                exchange.to_owned(),
                "INDEX".to_owned(),
                lake_symbol.to_owned(),
                lake_symbol.to_owned(),
                SEGMENT_IDX.to_owned(),
                CLASS_INDEX_SPOT.to_owned(),
            ))
        }
        "CASH" => {
            let token = eq_token_for(exchange)?;
            if !symbol_key_safe(lake_symbol) {
                return None;
            }
            Some((
                exchange.to_owned(),
                "CASH".to_owned(),
                lake_symbol.to_owned(),
                lake_symbol.to_owned(),
                token.to_owned(),
                CLASS_CASH_SPOT.to_owned(),
            ))
        }
        "FNO" => {
            // `parts is None or parts.instrument_type != FUTURE_TYPE` — the
            // Python parse's outcome arrives as the fno_* argument trio.
            let underlying = fno_underlying?;
            let expiry_iso = fno_expiry_iso?;
            let parsed_exchange = fno_exchange?;
            if !fno_is_future {
                return None; // options / junk dirs are out of the contract
            }
            if !EXPORT_FUTURES_UNDERLYINGS.contains(&underlying) {
                return None;
            }
            if parsed_exchange != exchange {
                return None; // a dir filed under the wrong exchange is junk
            }
            let token = fno_token_for(exchange)?;
            Some((
                exchange.to_owned(),
                "FNO".to_owned(),
                lake_symbol.to_owned(),
                format!("{underlying}-{expiry_iso}-FUT"),
                token.to_owned(),
                CLASS_FUT.to_owned(),
            ))
        }
        _ => None,
    }
}

/// The ONE S3 key law: `<prefix>/<day_iso>/<segment_token>/<symbol>.csv` —
/// the byte twin of `canonical.s3_key` (RAW canonical symbol, `&` never
/// percent-encoded; *day_iso* is the Python-side `date.isoformat()`).
pub fn s3_key_assemble(prefix: &str, day_iso: &str, segment_token: &str, symbol: &str) -> String {
    format!("{prefix}/{day_iso}/{segment_token}/{symbol}.csv")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(cells: &[&str]) -> Vec<String> {
        cells.iter().map(|c| (*c).to_owned()).collect()
    }

    const HEADER: &str = "symbol,timestamp_ist,open,high,low,close,volume,oi";

    /// The frozen golden literal, mirrored BYTE-FOR-BYTE from the committed
    /// fixture `tests/golden/fixtures/crossverify/M&M.csv` (the SPOT oi=""
    /// arm; UTF-8, no BOM, LF everywhere incl. the trailing newline). A
    /// drift between this literal and the committed fixture is caught by
    /// the flag-ON golden suite (`tests/golden/test_crossverify_rust_golden.py`)
    /// re-driving the SAME fixture through the real wheel.
    const MANDM_GOLDEN: &str = "symbol,timestamp_ist,open,high,low,close,volume,oi\n\
M&M,2026-07-10 09:15:00,2900.00,2900.30,2899.75,2900.10,100,\n\
M&M,2026-07-10 09:16:00,2900.05,2900.35,2899.80,2900.15,101,\n\
M&M,2026-07-10 09:17:00,2900.10,2900.40,2899.85,2900.20,102,\n";

    #[test]
    fn golden_mandm_bytes_exact() {
        let rows = vec![
            row(&[
                "M&M",
                "2026-07-10 09:15:00",
                "2900.00",
                "2900.30",
                "2899.75",
                "2900.10",
                "100",
                "",
            ]),
            row(&[
                "M&M",
                "2026-07-10 09:16:00",
                "2900.05",
                "2900.35",
                "2899.80",
                "2900.15",
                "101",
                "",
            ]),
            row(&[
                "M&M",
                "2026-07-10 09:17:00",
                "2900.10",
                "2900.40",
                "2899.85",
                "2900.20",
                "102",
                "",
            ]),
        ];
        let bytes = render_csv_bytes(HEADER, &rows).expect("golden rows must render");
        assert_eq!(bytes, MANDM_GOLDEN.as_bytes());
        // No BOM, trailing LF present.
        assert!(!bytes.starts_with(&[0xEF, 0xBB, 0xBF]));
        assert_eq!(bytes.last(), Some(&b'\n'));
    }

    #[test]
    fn futures_oi_int_renders_verbatim() {
        let rows = vec![row(&[
            "NIFTY-2026-07-30-FUT",
            "2026-07-10 09:15:00",
            "24100.00",
            "24100.30",
            "24099.75",
            "24100.10",
            "100",
            "0",
        ])];
        let bytes = render_csv_bytes(HEADER, &rows).expect("must render");
        let text = String::from_utf8(bytes).expect("utf-8 by construction");
        assert!(text.ends_with(",100,0\n"));
    }

    #[test]
    fn empty_rows_refuse() {
        let err = render_csv_bytes(HEADER, &[]).expect_err("empty must refuse");
        assert_eq!(err.code, CSV_REFUSAL_EMPTY_ROWS);
    }

    #[test]
    fn wrong_arity_refuses_with_observed_len() {
        let rows = vec![row(&["A", "2026-07-10 09:15:00", "1.00"])];
        let err = render_csv_bytes(HEADER, &rows).expect_err("arity must refuse");
        assert_eq!(err.code, CSV_REFUSAL_WRONG_ARITY);
        assert_eq!((err.row, err.cell), (0, 3));
    }

    #[test]
    fn duplicate_minute_refuses() {
        let base = [
            "A",
            "2026-07-10 09:15:00",
            "1.00",
            "1.00",
            "1.00",
            "1.00",
            "1",
            "",
        ];
        let rows = vec![row(&base), row(&base)];
        let err = render_csv_bytes(HEADER, &rows).expect_err("dup minute must refuse");
        assert_eq!(err.code, CSV_REFUSAL_NON_INCREASING_TS);
        assert_eq!((err.row, err.cell), (1, CSV_TS_CELL_INDEX));
    }

    #[test]
    fn misordered_minute_refuses() {
        let rows = vec![
            row(&[
                "A",
                "2026-07-10 09:16:00",
                "1.00",
                "1.00",
                "1.00",
                "1.00",
                "1",
                "",
            ]),
            row(&[
                "A",
                "2026-07-10 09:15:00",
                "1.00",
                "1.00",
                "1.00",
                "1.00",
                "1",
                "",
            ]),
        ];
        let err = render_csv_bytes(HEADER, &rows).expect_err("misorder must refuse");
        assert_eq!(err.code, CSV_REFUSAL_NON_INCREASING_TS);
    }

    #[test]
    fn metachar_cell_refuses_before_control_char() {
        // LF is BOTH a metachar and a control char — the metachar law wins
        // (the Python `_validate_cells` order).
        let rows = vec![row(&[
            "A\nB",
            "2026-07-10 09:15:00",
            "1.00",
            "1.00",
            "1.00",
            "1.00",
            "1",
            "",
        ])];
        let err = render_csv_bytes(HEADER, &rows).expect_err("metachar must refuse");
        assert_eq!(err.code, CSV_REFUSAL_METACHAR);
        assert_eq!((err.row, err.cell), (0, 0));
    }

    #[test]
    fn control_char_cell_refuses() {
        let rows = vec![row(&[
            "A\u{1b}B",
            "2026-07-10 09:15:00",
            "1.00",
            "1.00",
            "1.00",
            "1.00",
            "1",
            "",
        ])];
        let err = render_csv_bytes(HEADER, &rows).expect_err("control char must refuse");
        assert_eq!(err.code, CSV_REFUSAL_CONTROL_CHAR);
        // DEL refuses too.
        let rows = vec![row(&[
            "A\u{7f}",
            "2026-07-10 09:15:00",
            "1.00",
            "1.00",
            "1.00",
            "1.00",
            "1",
            "",
        ])];
        let err = render_csv_bytes(HEADER, &rows).expect_err("DEL must refuse");
        assert_eq!(err.code, CSV_REFUSAL_CONTROL_CHAR);
    }

    #[test]
    fn symbol_key_safe_matrix() {
        for legal in [
            "M&M",
            "BAJAJ-AUTO",
            "L&TFH",
            "3MINDIA",
            "NAM-INDIA",
            "NIFTY",
        ] {
            assert!(symbol_key_safe(legal), "{legal} must pass");
        }
        for junk in ["", ".", "..", "m&m", "A B", "A/B", "A\u{1b}", "A,B", "Ā"] {
            assert!(!symbol_key_safe(junk), "{junk:?} must refuse");
        }
        // A dot inside a symbol is legal (legacy series dots).
        assert!(symbol_key_safe("A.B"));
    }

    #[test]
    fn canonical_index_arms() {
        let nse = canonical_identity_fields("NSE", "INDEX", "NIFTY", None, None, false, None)
            .expect("NSE index maps");
        assert_eq!(
            nse,
            (
                "NSE".into(),
                "INDEX".into(),
                "NIFTY".into(),
                "NIFTY".into(),
                SEGMENT_IDX.into(),
                CLASS_INDEX_SPOT.into(),
            )
        );
        // From BSE only SENSEX.
        assert!(
            canonical_identity_fields("BSE", "INDEX", "SENSEX", None, None, false, None).is_some()
        );
        assert!(
            canonical_identity_fields("BSE", "INDEX", "BSEIT", None, None, false, None).is_none()
        );
        // Key-unsafe index refuses; foreign exchange refuses.
        assert!(
            canonical_identity_fields("NSE", "INDEX", "bad sym", None, None, false, None).is_none()
        );
        assert!(
            canonical_identity_fields("MCX", "INDEX", "NIFTY", None, None, false, None).is_none()
        );
    }

    #[test]
    fn canonical_cash_arms() {
        let m_and_m = canonical_identity_fields("NSE", "CASH", "M&M", None, None, false, None)
            .expect("M&M maps verbatim");
        assert_eq!(m_and_m.3, "M&M");
        assert_eq!(m_and_m.4, SEGMENT_NSE_EQ);
        assert_eq!(m_and_m.5, CLASS_CASH_SPOT);
        let bse = canonical_identity_fields("BSE", "CASH", "RELIANCE", None, None, false, None)
            .expect("BSE cash maps");
        assert_eq!(bse.4, SEGMENT_BSE_EQ);
        assert!(
            canonical_identity_fields("MCX", "CASH", "RELIANCE", None, None, false, None).is_none()
        );
        assert!(
            canonical_identity_fields("NSE", "CASH", "lower", None, None, false, None).is_none()
        );
    }

    #[test]
    fn canonical_fno_arms() {
        let fut = canonical_identity_fields(
            "NSE",
            "FNO",
            "NSE-NIFTY-30Jul26-FUT",
            Some("NIFTY"),
            Some("2026-07-30"),
            true,
            Some("NSE"),
        )
        .expect("allowlisted FUT maps");
        assert_eq!(fut.3, "NIFTY-2026-07-30-FUT");
        assert_eq!(fut.4, SEGMENT_NSE_FNO);
        assert_eq!(fut.5, CLASS_FUT);
        // Unparsed dir (Python parse None) → the fno args arrive as None.
        assert!(canonical_identity_fields("NSE", "FNO", "JUNK", None, None, false, None).is_none());
        // An option leg parses but is not a future.
        assert!(
            canonical_identity_fields(
                "NSE",
                "FNO",
                "NSE-NIFTY-30Jul26-24000-CE",
                Some("NIFTY"),
                Some("2026-07-30"),
                false,
                Some("NSE"),
            )
            .is_none()
        );
        // Non-allowlisted underlying refuses.
        assert!(
            canonical_identity_fields(
                "NSE",
                "FNO",
                "NSE-RELIANCE-30Jul26-FUT",
                Some("RELIANCE"),
                Some("2026-07-30"),
                true,
                Some("NSE"),
            )
            .is_none()
        );
        // A dir filed under the wrong exchange is junk.
        assert!(
            canonical_identity_fields(
                "BSE",
                "FNO",
                "NSE-NIFTY-30Jul26-FUT",
                Some("NIFTY"),
                Some("2026-07-30"),
                true,
                Some("NSE"),
            )
            .is_none()
        );
        // Unknown segment refuses.
        assert!(
            canonical_identity_fields("NSE", "OPTIONS", "X", None, None, false, None).is_none()
        );
    }

    #[test]
    fn s3_key_law() {
        assert_eq!(
            s3_key_assemble("crossverify/groww", "2026-07-10", "NSE_EQ", "M&M"),
            "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        );
    }
}
