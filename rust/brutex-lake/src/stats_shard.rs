//! Canonical stats-shard JSON render — byte-locked to CPython `json.dumps`
//! (`sort_keys=True, separators=(",", ":")`, `ensure_ascii=True` default).
//!
//! ADR-0020 Wave C row 4 (G6) + the row-5 rider (G7) —
//! DEC-LAKE-RUST-STATS-MANIFEST-001 (MIGRATION.md #1914). Re-hosts the ONE
//! pure serializer `brutex/data/lake_stats_manifest.py::render_shard` (the
//! per-dataset stats shard body `_write_shard` persists). EVERYTHING ELSE
//! stays Python: the loader, the validator, the bounded 16 MiB read, the
//! flock/`atomic_write_simple` write mechanics and the one-`os.stat`
//! fast path (a PyO3 crossing there is a likely net loss — ADR-0020 Wave C).
//!
//! ## The ints+strings-only FFI law
//!
//! Python pre-formats EVERY timestamp (`datetime.isoformat()` stays
//! CPython's own formatter — seconds always present, microseconds 6-digit
//! only when nonzero, tz-aware UTC renders `+00:00`) and passes ONLY ints,
//! strings and `None` across the boundary; Rust does ONLY canonical JSON
//! assembly. NO FLOATS exist in the shard schema, and this crate makes no
//! attempt to reproduce `float.__repr__` — a float smuggled into an int
//! field fails PyO3 extraction (`TypeError`/`OverflowError`) and the Python
//! seam (`brutex/data/lake_rust_stats.py`) degrades to the stdlib oracle.
//!
//! ## The CPython `json.dumps` byte law reproduced here
//!
//! * Object keys emitted in code-point-sorted order (the schema keys are
//!   compile-time literals emitted directly in sorted order; only the
//!   dataset `key` mapping sorts at run time — UTF-8 byte order == code
//!   point order, so `str::cmp` is exact).
//! * Separators `,` / `:` with no whitespace.
//! * `ensure_ascii=True`: [`push_json_string`] is the
//!   `py_encode_basestring_ascii` twin — short escapes `\" \\ \b \t \n \f
//!   \r`, other C0 controls as `\u00xx`, `0x7f` and ALL non-ASCII as
//!   lowercase `\uxxxx`, astral (> U+FFFF) as a lowercase UTF-16 surrogate
//!   pair. Forward slash is NOT escaped.
//! * Ints as plain decimal; `None` → `null`.
//!
//! A Python `str` carrying a LONE SURROGATE (which CPython would escape as
//! `\udXXX`) cannot cross PyO3 into a Rust `String` — extraction raises
//! `UnicodeEncodeError` (⊂ `ValueError`), a seam degrade member, so the
//! stdlib oracle serves that shard (parity-pinned).
//!
//! ## The G7 rider — crate-internal partition twins
//!
//! [`option_partition_stem`] + [`validate_year_month`] mirror
//! `brutex/data/partition.py` (`option_partition_stem` /
//! `_validate_year_month`) as CRATE-INTERNAL helpers per the ADR-0020 row-5
//! ruling ("`partition.py` as an internal crate helper — never
//! standalone"): re-routing Python callers through FFI for a one-line
//! f-string is a guaranteed net loss, so Python `partition.py` stays the
//! source for Python consumers and the twins here are cargo-pinned to the
//! exact Python outputs (grid + boundary tests below; the parity pytest
//! pins the same literals on the Python side). The interval-vocabulary
//! inversion clause of ADR row 5 belongs to the FULL `partition.py` port —
//! OUT of this rider's scope.
//!
//! ## The re.ASCII month-grammar law (documented, not yet exercised)
//!
//! This module never PARSES rels — render is grammar-blind (the `re.ASCII`
//! `_BARS_REL_RE`/`_OPTIONS_REL_RE` law lives in the Python VALIDATOR). Any
//! FUTURE Rust-side rel parsing MUST use ASCII-only digit checks
//! (`u8::is_ascii_digit`), never `char::is_numeric` — Unicode digits like
//! `٢٠٢٣` must not parse (the round-3 `rel_grammar` hardening).

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

/// One `StatsFileRow` crossing the FFI as a plain 7-tuple:
/// `(rel, rows, min_ts_iso, max_ts_iso, mtime_ns, size, raw_hash)`.
///
/// Field indices (tuple struct — PyO3 extracts from a Python tuple):
/// `.0` rel: `str` · `.1` rows: `int >= 0` · `.2` min_ts isoformat `str` ·
/// `.3` max_ts isoformat `str` · `.4` mtime_ns: `int` (i64) · `.5` size:
/// `int >= 0` · `.6` raw_hash: `str | None`. An out-of-range int
/// (`mtime_ns` beyond i64, `rows`/`size` beyond u64 or negative) fails
/// extraction with `OverflowError` — a seam degrade member.
#[derive(FromPyObject)]
pub struct FileRowIn(
    pub String,
    pub u64,
    pub String,
    pub String,
    pub i64,
    pub u64,
    pub Option<String>,
);

/// Append *s* to *out* as a JSON string literal, byte-identical to
/// CPython's `py_encode_basestring_ascii` (`ensure_ascii=True`).
fn push_json_string(out: &mut String, s: &str) {
    use std::fmt::Write as _;

    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{0c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            ' '..='~' => out.push(ch),
            _ => {
                let code = ch as u32;
                if code < 0x10000 {
                    // C0 controls (\u00xx), 0x7f and all non-ASCII BMP —
                    // lowercase 4-digit hex, exactly CPython's `\u{0:04x}`.
                    let _ = write!(out, "\\u{code:04x}");
                } else {
                    // Astral plane → UTF-16 surrogate pair, both lowercase.
                    let c = code - 0x10000;
                    let hi = 0xd800 + (c >> 10);
                    let lo = 0xdc00 + (c & 0x3ff);
                    let _ = write!(out, "\\u{hi:04x}\\u{lo:04x}");
                }
            }
        }
    }
    out.push('"');
}

/// Pure render core: assemble the canonical shard JSON exactly as
/// `render_shard`'s `json.dumps(payload, sort_keys=True,
/// separators=(",", ":"))` does. Exercised directly by `cargo test`
/// (the frozen `_EXPECTED_BARS_SHARD` golden is hardcoded below); the
/// `#[pyfunction]` wrapper adds only the panic boundary + PyErr mapping.
///
/// Top-level keys in sorted order are literally
/// `"files","identity_key","key","kind","max_ts","min_ts","row_count",
/// "updated_utc","version"`; per-file row keys sorted are
/// `"max_ts","min_ts","mtime_ns","raw_hash","rel","rows","size"` — all
/// compile-time literals emitted in that order (no runtime sort needed
/// except the dataset `key` pairs, sorted by field code point).
#[allow(clippy::too_many_arguments)] // Mirrors the Python payload 1:1 (9 shard fields).
pub fn render_stats_shard_json_impl(
    version: u64,
    kind: &str,
    key: &[(String, String)],
    identity_key: &[String],
    row_count: u64,
    min_ts_iso: &str,
    max_ts_iso: &str,
    files: &[FileRowIn],
    updated_utc: &str,
) -> String {
    use std::fmt::Write as _;

    let mut key_sorted: Vec<&(String, String)> = key.iter().collect();
    key_sorted.sort_by(|a, b| a.0.cmp(&b.0));

    let mut out = String::new();
    out.push_str("{\"files\":[");
    for (i, row) in files.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str("{\"max_ts\":");
        push_json_string(&mut out, &row.3);
        out.push_str(",\"min_ts\":");
        push_json_string(&mut out, &row.2);
        let _ = write!(out, ",\"mtime_ns\":{}", row.4);
        out.push_str(",\"raw_hash\":");
        match &row.6 {
            Some(hash) => push_json_string(&mut out, hash),
            None => out.push_str("null"),
        }
        out.push_str(",\"rel\":");
        push_json_string(&mut out, &row.0);
        let _ = write!(out, ",\"rows\":{},\"size\":{}}}", row.1, row.5);
    }
    out.push_str("],\"identity_key\":[");
    for (i, column) in identity_key.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        push_json_string(&mut out, column);
    }
    out.push_str("],\"key\":{");
    for (i, (field, value)) in key_sorted.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        push_json_string(&mut out, field);
        out.push(':');
        push_json_string(&mut out, value);
    }
    out.push_str("},\"kind\":");
    push_json_string(&mut out, kind);
    out.push_str(",\"max_ts\":");
    push_json_string(&mut out, max_ts_iso);
    out.push_str(",\"min_ts\":");
    push_json_string(&mut out, min_ts_iso);
    let _ = write!(out, ",\"row_count\":{row_count}");
    out.push_str(",\"updated_utc\":");
    push_json_string(&mut out, updated_utc);
    let _ = write!(out, ",\"version\":{version}}}");
    out
}

/// Render the canonical stats-shard JSON (see [`render_stats_shard_json_impl`]).
///
/// Raises `RuntimeError` if the assembly panics (caught at this boundary —
/// a panic never crosses the FFI raw); argument-extraction failures
/// (`TypeError`/`OverflowError`/`UnicodeEncodeError`) raise BEFORE this body
/// runs. The assembly runs WITHOUT the GIL (`Python::detach` — sound: every
/// argument is extracted to owned Rust data first), so an engaged render
/// never serializes sibling writer threads.
#[pyfunction]
#[allow(clippy::too_many_arguments)] // Mirrors the Python payload 1:1 (9 shard fields).
pub(crate) fn render_stats_shard_json(
    py: Python<'_>,
    version: u64,
    kind: String,
    key: Vec<(String, String)>,
    identity_key: Vec<String>,
    row_count: u64,
    min_ts_iso: String,
    max_ts_iso: String,
    files: Vec<FileRowIn>,
    updated_utc: String,
) -> PyResult<String> {
    py.detach(|| {
        std::panic::catch_unwind(|| {
            render_stats_shard_json_impl(
                version,
                &kind,
                &key,
                &identity_key,
                row_count,
                &min_ts_iso,
                &max_ts_iso,
                &files,
                &updated_utc,
            )
        })
    })
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake stats-shard render panicked (caught at the FFI boundary)",
        )
    })
}

// ────────────────────────────────────────────────────────────────────────
// G7 rider — crate-internal partition twins (module docs)
// ────────────────────────────────────────────────────────────────────────

/// Twin of `brutex/data/partition.py::MIN_PARTITION_YEAR` (2000).
pub const MIN_PARTITION_YEAR: i32 = 2000;

/// Twin of `brutex/data/partition.py::MAX_PARTITION_YEAR` (2100).
pub const MAX_PARTITION_YEAR: i32 = 2100;

/// Twin of `brutex/data/partition.py::MONTHS_PER_YEAR` (12).
pub const MONTHS_PER_YEAR: u8 = 12;

/// The canonical zero-padded stem of one expired-options month partition —
/// the crate-internal twin of `brutex/data/partition.py::
/// option_partition_stem` (`f"{year:04d}-{month:02d}"`, pure formatting;
/// like its Python source it never validates — the inverse parse + bounds
/// gates live in `lake_invariants._option_file_month`). Cargo-pinned to
/// the exact Python outputs including the 4-digit padding boundaries; the
/// parity pytest pins the same literal grid on the Python side.
pub fn option_partition_stem(year: i32, month: u8) -> String {
    format!("{year:04}-{month:02}")
}

/// Crate-internal twin of `brutex/data/partition.py::_validate_year_month`
/// — the SAME bounds ([`MIN_PARTITION_YEAR`], [`MAX_PARTITION_YEAR`],
/// month 1..=[`MONTHS_PER_YEAR`]) with the exact message text, returned as
/// `Err` (the Python source raises `PartitionError`).
pub fn validate_year_month(year: i32, month: u8) -> Result<(), String> {
    if !(MIN_PARTITION_YEAR..=MAX_PARTITION_YEAR).contains(&year) {
        return Err(format!(
            "year must be a 4-digit Gregorian year in \
             [{MIN_PARTITION_YEAR}, {MAX_PARTITION_YEAR}], got {year}"
        ));
    }
    if !(1..=MONTHS_PER_YEAR).contains(&month) {
        return Err(format!(
            "month must be in [1, {MONTHS_PER_YEAR}], got {month}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(
        rel: &str,
        rows: u64,
        min_ts: &str,
        max_ts: &str,
        mtime_ns: i64,
        size: u64,
        raw_hash: Option<&str>,
    ) -> FileRowIn {
        FileRowIn(
            rel.to_string(),
            rows,
            min_ts.to_string(),
            max_ts.to_string(),
            mtime_ns,
            size,
            raw_hash.map(str::to_string),
        )
    }

    fn bars_key() -> Vec<(String, String)> {
        // Deliberately UNSORTED input — the render sorts by field.
        [
            ("symbol", "NIFTY"),
            ("exchange", "NSE"),
            ("interval", "1minute"),
            ("segment", "INDEX"),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
    }

    /// The frozen `_EXPECTED_BARS_SHARD` golden — copied VERBATIM from
    /// `tests/golden/test_lake_stats_manifest_golden.py` (the schema-freeze
    /// pin; the ADR-0020 row-4 cross-language byte oracle). Never
    /// regenerated: any diff here is a schema break that must bump
    /// `STATS_MANIFEST_VERSION` on the Python side first.
    const EXPECTED_BARS_SHARD: &str = concat!(
        "{\"files\":[",
        "{\"max_ts\":\"2026-06-30T10:00:00+00:00\",\"min_ts\":\"2026-06-01T03:45:00+00:00\",",
        "\"mtime_ns\":1751000000000000001,\"raw_hash\":\"",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "\",\"rel\":\"2026/06.parquet\",\"rows\":5,\"size\":1024},",
        "{\"max_ts\":\"2026-07-15T09:59:00+00:00\",\"min_ts\":\"2026-07-01T03:45:00+00:00\",",
        "\"mtime_ns\":1751500000000000002,\"raw_hash\":null,\"rel\":\"2026/07.parquet\",\"rows\":3,",
        "\"size\":2048}],",
        "\"identity_key\":[\"timestamp\"],",
        "\"key\":{\"exchange\":\"NSE\",\"interval\":\"1minute\",\"segment\":\"INDEX\",\"symbol\":\"NIFTY\"},",
        "\"kind\":\"bars\",",
        "\"max_ts\":\"2026-07-15T09:59:00+00:00\",",
        "\"min_ts\":\"2026-06-01T03:45:00+00:00\",",
        "\"row_count\":8,",
        "\"updated_utc\":\"2026-07-06T00:00:00+00:00\",",
        "\"version\":1}",
    );

    #[test]
    fn frozen_bars_shard_golden_byte_identical() {
        let files = vec![
            row(
                "2026/06.parquet",
                5,
                "2026-06-01T03:45:00+00:00",
                "2026-06-30T10:00:00+00:00",
                1_751_000_000_000_000_001,
                1024,
                Some(&"a".repeat(64)),
            ),
            row(
                "2026/07.parquet",
                3,
                "2026-07-01T03:45:00+00:00",
                "2026-07-15T09:59:00+00:00",
                1_751_500_000_000_000_002,
                2048,
                None,
            ),
        ];
        let body = render_stats_shard_json_impl(
            1,
            "bars",
            &bars_key(),
            &["timestamp".to_string()],
            8,
            "2026-06-01T03:45:00+00:00",
            "2026-07-15T09:59:00+00:00",
            &files,
            "2026-07-06T00:00:00+00:00",
        );
        assert_eq!(body, EXPECTED_BARS_SHARD);
    }

    #[test]
    fn empty_files_and_options_kind() {
        let body = render_stats_shard_json_impl(
            1,
            "options",
            &[("series".to_string(), "NIFTY24DEC".to_string())],
            &["side".to_string(), "timestamp".to_string()],
            0,
            "2026-07-01T09:15:00+00:00",
            "2026-07-01T09:15:00+00:00",
            &[],
            "2026-07-07T00:00:00+00:00",
        );
        assert_eq!(
            body,
            concat!(
                "{\"files\":[],\"identity_key\":[\"side\",\"timestamp\"],",
                "\"key\":{\"series\":\"NIFTY24DEC\"},\"kind\":\"options\",",
                "\"max_ts\":\"2026-07-01T09:15:00+00:00\",",
                "\"min_ts\":\"2026-07-01T09:15:00+00:00\",\"row_count\":0,",
                "\"updated_utc\":\"2026-07-07T00:00:00+00:00\",\"version\":1}",
            )
        );
    }

    #[test]
    fn escaper_matrix_matches_cpython_ensure_ascii() {
        let cases: &[(&str, &str)] = &[
            ("plain", "\"plain\""),
            ("a\"b\\c", "\"a\\\"b\\\\c\""),
            ("\u{08}\t\n\u{0c}\r", "\"\\b\\t\\n\\f\\r\""),
            ("\u{0b}\u{1b}\u{00}", "\"\\u000b\\u001b\\u0000\""),
            ("\u{7f}", "\"\\u007f\""),
            ("é", "\"\\u00e9\""),
            ("₹", "\"\\u20b9\""),
            ("😀", "\"\\ud83d\\ude00\""),
            ("a/b", "\"a/b\""),
            (
                "٢٠٢٣-١٢.parquet",
                "\"\\u0662\\u0660\\u0662\\u0663-\\u0661\\u0662.parquet\"",
            ),
        ];
        for (input, expected) in cases {
            let mut out = String::new();
            push_json_string(&mut out, input);
            assert_eq!(&out, expected, "escaper diverged for {input:?}");
        }
    }

    #[test]
    fn key_pairs_sort_by_field_code_point() {
        let body = render_stats_shard_json_impl(
            1,
            "bars",
            &bars_key(),
            &["timestamp".to_string()],
            0,
            "2026-07-01T09:15:00+00:00",
            "2026-07-01T09:15:00+00:00",
            &[],
            "u",
        );
        let key_part = "\"key\":{\"exchange\":\"NSE\",\"interval\":\"1minute\",\"segment\":\"INDEX\",\"symbol\":\"NIFTY\"}";
        assert!(body.contains(key_part), "unsorted key mapping in {body}");
    }

    #[test]
    fn mtime_ns_i64_boundaries_render_plain_decimal() {
        let files = vec![
            row("2026/07.parquet", 1, "t", "t", i64::MAX, 0, None),
            row("2026/08.parquet", 1, "t", "t", i64::MIN, 0, None),
        ];
        let body = render_stats_shard_json_impl(1, "bars", &[], &[], 2, "t", "t", &files, "u");
        assert!(body.contains("\"mtime_ns\":9223372036854775807"));
        assert!(body.contains("\"mtime_ns\":-9223372036854775808"));
    }

    // ── G7 rider twins ──

    #[test]
    fn option_partition_stem_matches_python_grid() {
        // The §2.2 cross-language grid — the parity pytest pins the SAME
        // literals against brutex.data.partition.option_partition_stem.
        assert_eq!(option_partition_stem(1, 1), "0001-01");
        assert_eq!(option_partition_stem(9999, 12), "9999-12");
        assert_eq!(option_partition_stem(2024, 6), "2024-06");
        assert_eq!(option_partition_stem(2026, 7), "2026-07");
    }

    #[test]
    fn validate_year_month_bounds_and_messages() {
        assert!(validate_year_month(2000, 1).is_ok());
        assert!(validate_year_month(2100, 12).is_ok());
        assert_eq!(
            validate_year_month(1999, 1).unwrap_err(),
            "year must be a 4-digit Gregorian year in [2000, 2100], got 1999"
        );
        assert_eq!(
            validate_year_month(2101, 1).unwrap_err(),
            "year must be a 4-digit Gregorian year in [2000, 2100], got 2101"
        );
        assert_eq!(
            validate_year_month(2026, 0).unwrap_err(),
            "month must be in [1, 12], got 0"
        );
        assert_eq!(
            validate_year_month(2026, 13).unwrap_err(),
            "month must be in [1, 12], got 13"
        );
    }
}
