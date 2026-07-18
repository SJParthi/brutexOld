//! CPython-`json.dumps`-byte-identical renders for the combo-aggregate lens
//! payloads.
//!
//! The ONE deterministic JSON rendering law of the deleted
//! `combo_aggregates._dumps` — `json.dumps(payload, sort_keys=True,
//! separators=(",", ":"), ensure_ascii=True, allow_nan=False)` — restricted
//! to the shapes the fold actually renders: `dict[str, int]` maps and the
//! `[[iso_date, int], …]` worst-days list. Every payload value is an exact
//! integer, so no float-repr arm exists here (stated, not hidden).
//!
//! String escaping is the `py_encode_basestring_ascii` twin — the proven
//! `rust/brutex-lake/src/stats_shard.rs` law (short escapes, lowercase
//! `\uXXXX`, astral-plane surrogate pairs). Key ordering: Python `sorted`
//! compares `str` by code point; UTF-8 byte order equals code-point order,
//! so a `BTreeMap<String, _>` walk is byte-identical.

use std::collections::BTreeMap;
use std::fmt::Write as _;

/// Append *s* to *out* as a JSON string literal, byte-identical to
/// CPython's `py_encode_basestring_ascii` (`ensure_ascii=True`).
pub fn push_json_string(out: &mut String, s: &str) {
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
                    let _ = write!(out, "\\u{code:04x}");
                } else {
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

/// Render a `{str: int}` map exactly as `_dumps(dict)` did (sorted keys,
/// compact separators).
pub fn dumps_str_int_map(map: &BTreeMap<String, i128>) -> String {
    let mut out = String::from("{");
    for (i, (key, value)) in map.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        push_json_string(&mut out, key);
        let _ = write!(out, ":{value}");
    }
    out.push('}');
    out
}

/// Render the worst-days `[[iso_date, pnl], …]` list exactly as
/// `_dumps(list)` did.
pub fn dumps_pairs(pairs: &[(String, i128)]) -> String {
    let mut out = String::from("[");
    for (i, (day, pnl)) in pairs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('[');
        push_json_string(&mut out, day);
        let _ = write!(out, ",{pnl}");
        out.push(']');
    }
    out.push(']');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_shapes() {
        assert_eq!(dumps_str_int_map(&BTreeMap::new()), "{}");
        assert_eq!(dumps_pairs(&[]), "[]");
    }

    #[test]
    fn sorted_compact_map() {
        let mut map = BTreeMap::new();
        map.insert("9".to_string(), 2_i128);
        map.insert("10".to_string(), 1_i128);
        map.insert("13".to_string(), -3_i128);
        // Python: json.dumps({"9":2,"10":1,"13":-3}, sort_keys=True,
        // separators=(",",":")) == '{"10":1,"13":-3,"9":2}' — "10" < "13" < "9".
        assert_eq!(dumps_str_int_map(&map), "{\"10\":1,\"13\":-3,\"9\":2}");
    }

    #[test]
    fn pairs_render() {
        let pairs = vec![
            ("2024-01-04".to_string(), -2_500_i128),
            ("2024-01-02".to_string(), 3_000_i128),
        ];
        assert_eq!(
            dumps_pairs(&pairs),
            "[[\"2024-01-04\",-2500],[\"2024-01-02\",3000]]"
        );
    }

    #[test]
    fn escaper_matches_cpython_ensure_ascii() {
        let mut out = String::new();
        push_json_string(&mut out, "a\"b\\c\u{08}\t\n\u{0c}\r\u{7f}é₹\u{1f600}");
        // CPython: json.dumps(...) ==
        // "a\"b\\c\b\t\n\f\ré₹😀"
        assert_eq!(
            out,
            "\"a\\\"b\\\\c\\b\\t\\n\\f\\r\\u007f\\u00e9\\u20b9\\ud83d\\ude00\""
        );
    }
}
