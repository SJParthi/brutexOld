//! The `brutex/config/dotenv.py` LINE-PARSE law (milestone 3).
//!
//! DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). ONE law —
//! [`parse_pairs`], the `parse_dotenv` twin: parse `.env` text into
//! ordered key/value DECISIONS. File reads, the never-override apply law
//! and the masked-key-names logging all STAY PYTHON (side-effect
//! surfaces — the honest seam split recorded in the facade docstring).
//!
//! ## The parse law (token-for-token from `parse_dotenv`)
//!
//! For each `str.splitlines()` line: strip; skip blank and `#` lines;
//! tolerate one leading `"export "` (then re-strip); skip lines with no
//! `=`; split on the FIRST `=`; strip key (skip when empty) and value;
//! strip ONE pair of surrounding matching single/double quotes
//! (`len >= 2`); later duplicate keys OVERRIDE earlier ones at the
//! FIRST-insertion position (the CPython `dict` re-assignment law — the
//! returned pairs are exactly `list(parsed.items())`).
//!
//! ## CPython ASCII whitespace semantics (probed 2026-07-17, pinned)
//!
//! The facade gates crossings to ASCII text, where the CPython laws are:
//!
//! * `str.strip()` strips `\t \n \x0b \x0c \r \x1c \x1d \x1e \x1f` and
//!   space — NOTE `\x1c..\x1f`, which are NOT Rust `char::is_whitespace`
//!   members (the divergence class the ASCII gate + these twins close);
//! * `str.splitlines()` breaks on `\n \x0b \x0c \r \x1c \x1d \x1e` with
//!   `\r\n` as ONE break and NO trailing empty line.
//!
//! ONLY strings cross: text in, `Vec<(key, value)>` out (a list of
//! 2-tuples on the Python side). The facade validates shape, compares
//! the pairs against `list(oracle.items())` during burn-in, and rebuilds
//! the dict via `dict(pairs)`. Values may be operator SECRETS — this
//! module never logs, never writes, never keeps state; the facade's
//! observability is value-REDACTED for this seam.

/// Defensive text-length cap (~16× a plausible operator `.env`; the
/// facade eligibility gate bounds crossings at 1 MiB too).
pub const MAX_TEXT_LEN: usize = 1 << 20;

/// CPython `str.strip()` ASCII whitespace set (probed — module docs).
fn is_py_ascii_space(b: u8) -> bool {
    matches!(
        b,
        b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | 0x1c | 0x1d | 0x1e | 0x1f | b' '
    )
}

/// `str.strip()` over ASCII text (both ends, the probed set).
fn py_strip(s: &str) -> &str {
    let bytes = s.as_bytes();
    let mut start = 0usize;
    while start < bytes.len() && is_py_ascii_space(bytes[start]) {
        start += 1;
    }
    let mut end = bytes.len();
    while end > start && is_py_ascii_space(bytes[end - 1]) {
        end -= 1;
    }
    &s[start..end]
}

/// `str.splitlines()` over ASCII text (probed boundary set, `\r\n` one
/// break, no trailing empty line).
fn py_splitlines(s: &str) -> Vec<&str> {
    let bytes = s.as_bytes();
    let mut lines = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'\n' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e => {
                lines.push(&s[start..i]);
                i += 1;
                start = i;
            }
            b'\r' => {
                lines.push(&s[start..i]);
                i += 1;
                if i < bytes.len() && bytes[i] == b'\n' {
                    i += 1;
                }
                start = i;
            }
            _ => i += 1,
        }
    }
    if start < bytes.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// The `parse_dotenv` twin (module docs) — returns the deduped ordered
/// pairs, exactly `list(parse_dotenv(text).items())`.
pub fn parse_pairs(text: &str) -> Result<Vec<(String, String)>, String> {
    if text.len() > MAX_TEXT_LEN {
        return Err(format!(
            "parse_pairs: input exceeds {MAX_TEXT_LEN} bytes ({} bytes)",
            text.len()
        ));
    }
    if !text.is_ascii() {
        return Err(
            "parse_pairs: non-ASCII input (the facade eligibility gate routes these to the oracle)"
                .to_string(),
        );
    }
    let mut out: Vec<(String, String)> = Vec::new();
    for raw in py_splitlines(text) {
        let mut line = py_strip(raw);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("export ") {
            line = py_strip(rest);
        }
        let Some(eq) = line.find('=') else {
            continue;
        };
        let key = py_strip(&line[..eq]);
        if key.is_empty() {
            continue;
        }
        let mut value = py_strip(&line[eq + 1..]);
        let vb = value.as_bytes();
        if vb.len() >= 2 && vb[0] == vb[vb.len() - 1] && (vb[0] == b'\'' || vb[0] == b'"') {
            value = &value[1..value.len() - 1];
        }
        if let Some(slot) = out.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value.to_string();
        } else {
            out.push((key.to_string(), value.to_string()));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pairs(text: &str) -> Vec<(String, String)> {
        parse_pairs(text).unwrap()
    }

    fn pair(k: &str, v: &str) -> (String, String) {
        (k.to_string(), v.to_string())
    }

    #[test]
    fn basic_grammar_matches_parse_dotenv() {
        let text =
            "\n# comment\nA=1\nexport B = two \nC='qu oted'\nD=\"dq\"\nE=a=b=c\n=skipme\nnokey\n";
        assert_eq!(
            pairs(text),
            vec![
                pair("A", "1"),
                pair("B", "two"),
                pair("C", "qu oted"),
                pair("D", "dq"),
                pair("E", "a=b=c"),
            ]
        );
    }

    #[test]
    fn duplicate_keys_keep_first_position_last_value() {
        // The CPython dict re-assignment law: first-insertion position,
        // last value — the pairs are list(parsed.items()).
        assert_eq!(
            pairs("A=1\nB=2\nA=3\n"),
            vec![pair("A", "3"), pair("B", "2")]
        );
    }

    #[test]
    fn quote_stripping_is_single_pass_and_matched_only() {
        assert_eq!(pairs("A=''"), vec![pair("A", "")]);
        assert_eq!(pairs("A='"), vec![pair("A", "'")]); // len < 2 — kept
        assert_eq!(pairs("A='x\""), vec![pair("A", "'x\"")]); // mismatched
        assert_eq!(pairs("A=''x''"), vec![pair("A", "'x'")]); // one pass only
    }

    #[test]
    fn export_handling_matches_python_order() {
        // "export" with no trailing space is NOT stripped; the '='-check
        // runs AFTER the export strip (probed against parse_dotenv).
        assert_eq!(pairs("export=1"), vec![pair("export", "1")]);
        assert_eq!(pairs("export A=1"), vec![pair("A", "1")]);
        assert_eq!(pairs("export export A=1"), vec![pair("export A", "1")]);
        assert_eq!(pairs("export = 1"), Vec::new()); // key empties after strip
        assert_eq!(pairs("export"), Vec::new()); // no '='
    }

    #[test]
    fn python_ascii_whitespace_set_is_mirrored() {
        // \x1c..\x1f are CPython-space but NOT Rust char::is_whitespace —
        // the divergence class this module exists to close. \x1f is
        // strip-only (probed: parse_dotenv("\x1fA\x1f = \x1f1\x1f") ==
        // {"A": "1"}); \x1c is BOTH space and a splitlines boundary
        // (probed: "\x1c\x1fA\x1f=\x1c1\x1f" == {"A": ""} — the \x1c
        // after '=' STARTS A NEW LINE, so the value is empty).
        assert_eq!(pairs("\x1fA\x1f = \x1f1\x1f"), vec![pair("A", "1")]);
        assert_eq!(pairs("\x1c\x1fA\x1f=\x1c1\x1f"), vec![pair("A", "")]);
        assert_eq!(pairs("A=1\x1cB=2"), vec![pair("A", "1"), pair("B", "2")]);
    }

    #[test]
    fn python_splitlines_boundaries_are_mirrored() {
        assert_eq!(
            pairs("A=1\r\nB=2\rC=3\x0bD=4\x0cE=5\x1dF=6\x1eG=7"),
            vec![
                pair("A", "1"),
                pair("B", "2"),
                pair("C", "3"),
                pair("D", "4"),
                pair("E", "5"),
                pair("F", "6"),
                pair("G", "7"),
            ]
        );
        // "a\n\rb" is TWO breaks (probed) — the middle empty line skips.
        assert_eq!(pairs("A=1\n\rB=2"), vec![pair("A", "1"), pair("B", "2")]);
    }

    #[test]
    fn refuses_non_ascii_and_oversize() {
        assert!(parse_pairs("A=caf\u{e9}").is_err());
        let long = format!("A={}", "x".repeat(MAX_TEXT_LEN));
        assert!(parse_pairs(&long).is_err());
    }
}
