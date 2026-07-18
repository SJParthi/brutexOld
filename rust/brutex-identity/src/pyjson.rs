//! CPython `json.dumps` byte-law twin — the ensure-ascii escaper + emitters.
//!
//! `brutex/strategies/combo.py::canonicalise_params` encodes the canonical
//! param tree with `json.dumps(..., sort_keys=True, separators=(",", ":"),
//! ensure_ascii=True)`. The BLAKE3 run-identity folds those bytes, so this
//! module reproduces CPython's `py_encode_basestring_ascii` escaper exactly:
//! the two-char shortcuts (`\" \\ \b \f \n \r \t`), lowercase-hex `\uXXXX`
//! for every other char outside `0x20..=0x7e`, and UTF-16 surrogate PAIRS for
//! astral code points. Proven by the differential pytest against live
//! `json.dumps` (`tests/unit/test_brutex_identity_kernels.py`) plus the
//! in-crate corpus below.
//!
//! In-repo precedent: `rust/brutex-lake/src/stats_shard.rs` carries the same
//! escaper law for the stats-manifest shard (DEC-LAKE-RUST-STATS-MANIFEST-001
//! #1914). A shared crate was deliberately NOT minted: depending on the
//! polars-heavy brutex-lake lib from this light crate was rejected on
//! build-cost grounds — the duplication is disclosed in
//! DEC-RUST-RUNID-STORES-001 and each copy is pinned by its own
//! live-`json.dumps` differential (the true oracle).

/// Append the CPython `ensure_ascii` JSON encoding of `s` (quotes included).
pub fn write_json_escaped(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if !(' '..='\u{7e}').contains(&c) => {
                let cp = c as u32;
                if cp > 0xffff {
                    // Astral plane → UTF-16 surrogate pair, lowercase hex
                    // (CPython py_encode_basestring_ascii).
                    let v = cp - 0x1_0000;
                    let high = 0xd800 + (v >> 10);
                    let low = 0xdc00 + (v & 0x3ff);
                    out.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                } else {
                    out.push_str(&format!("\\u{cp:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::write_json_escaped;

    fn escaped(s: &str) -> String {
        let mut out = String::new();
        write_json_escaped(&mut out, s);
        out
    }

    /// Expected strings captured from CPython 3.12 `json.dumps` (ensure_ascii).
    #[test]
    fn cpython_escape_corpus() {
        let cases: &[(&str, &str)] = &[
            ("plain", "\"plain\""),
            ("", "\"\""),
            ("quo\"te\\back", "\"quo\\\"te\\\\back\""),
            ("line\nfeed\ttab\rret", "\"line\\nfeed\\ttab\\rret\""),
            ("bell\u{07}", "\"bell\\u0007\""),
            ("nul\u{0}byte", "\"nul\\u0000byte\""),
            ("back\u{08}form\u{0c}", "\"back\\bform\\f\""),
            ("del\u{7f}char", "\"del\\u007fchar\""),
            ("caf\u{e9}", "\"caf\\u00e9\""),
            ("\u{b5}", "\"\\u00b5\""),
            ("\u{6307}\u{6570}", "\"\\u6307\\u6570\""),
            ("mask\u{1F600}tail", "\"mask\\ud83d\\ude00tail\""),
            ("a/b", "\"a/b\""), // solidus is NOT escaped by json.dumps
            (" ~", "\" ~\""),   // the 0x20..0x7e passthrough boundary
        ];
        for (input, expected) in cases {
            assert_eq!(&escaped(input), expected, "input {input:?}");
        }
    }
}
