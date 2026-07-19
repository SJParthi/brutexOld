//! Canonical-JSON render core — byte-locked to CPython
//! `json.dumps(tree, sort_keys=True, separators=(",", ":"),
//! ensure_ascii=True)` over the ALREADY-lowered tree
//! (`brutex/strategies/combo.py::canonicalise_params`,
//! DEC-STRATEGIES-RUST-COMBO-001).
//!
//! ## The lowered-tree law (LOAD-BEARING byte-safety property)
//!
//! The crossing tree is dict / list / str / int / bool / None ONLY —
//! float-free and Decimal-free BY LAW (`_canonicalise_value` lowers every
//! float to a `repr()` STRING and every Decimal to a `str()` STRING in
//! PYTHON before the seam), so this module NEVER reproduces
//! `float.__repr__` / `Decimal.__str__` — the hardest byte risk of any
//! CPython-json twin is eliminated BY CONSTRUCTION (the #1914
//! "Python pre-formats; ints+strings-only FFI" precedent applied to a tree).
//!
//! ## The CPython `json.dumps` byte law reproduced here
//!
//! * Object keys emitted in code-point-sorted order. Rust `String` byte-wise
//!   UTF-8 ordering == Unicode code-point order (including astral), which
//!   equals CPython's exact-`str` `sort_keys` comparison (`str.__lt__` IS
//!   code-point comparison). Str-SUBCLASS keys (whose overridden `__lt__`
//!   could legally sort differently) are REFUSED at the FFI boundary and
//!   never reach this core (src/python.rs).
//! * Separators `,` / `:` with no whitespace.
//! * `ensure_ascii=True`: [`push_json_string`] is the
//!   `py_encode_basestring_ascii` twin — short escapes `\" \\ \b \t \n \f
//!   \r`, other C0 controls as `\u00xx`, `0x7f` and ALL non-ASCII BMP as
//!   lowercase `\uxxxx`, astral (> U+FFFF) as a lowercase UTF-16 surrogate
//!   pair. Forward slash is NOT escaped.
//! * `true` / `false` / `null` literals; ints as plain decimal (`i128`
//!   Display == CPython int decimal digits over the full range).
//!
//! ## Cross-crate COMMON deviation (recorded honestly, design §2.3)
//!
//! [`push_json_string`] is a SECOND Rust copy of the CPython-json ASCII
//! escaper (first: `rust/brutex-lake/src/stats_shard.rs::push_json_string`).
//! The crates share no workspace, and refactoring brutex-lake to a shared
//! `include!` core would diff the just-merged brutex-lake crate (this
//! slice's zero-collision rule), so PR-1 takes duplication + a mechanical
//! anti-drift gate: the SHARED-LITERAL cross-crate parity corpus — the
//! stats_shard escaper matrix literals duplicated VERBATIM into this crate's
//! cargo tests (tests/canon_tests.rs) and pinned a third time from Python.
//! Consolidation onto a shared `json_ascii_escape_core.rs` include is the
//! named follow-up DEC-RUST-JSON-ESCAPE-COMMON-001 (unminted).

use std::fmt::Write as _;

/// One node of the ALREADY-lowered canonical tree (design §2.2).
///
/// Dict pairs are stored AS EXTRACTED (insertion order); [`render`] sorts
/// them by key at emit time (phase 2), so extraction stays a single ordered
/// walk under the GIL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    /// Python `None` → JSON `null`.
    Null,
    /// Python `bool` → JSON `true` / `false` (checked BEFORE int — bool ⊂
    /// int in Python; the order is a byte law).
    Bool(bool),
    /// Python `int` within `[i128::MIN, i128::MAX]` — the routing law is
    /// EXACTLY `extract::<i128>()`'s native range; outside it the FFI raises
    /// `OverflowError` and the facade degrades to the oracle.
    Int(i128),
    /// Python `str` (exact or subclass — the encoder reads the buffer).
    Str(String),
    /// Python `list` → JSON array, element order preserved.
    List(Vec<Node>),
    /// Python `dict` → JSON object; keys are EXACT `str` only (enforced at
    /// the FFI boundary), sorted by code point at render time.
    Dict(Vec<(String, Node)>),
}

/// Append *s* to *out* as a JSON string literal, byte-identical to
/// CPython's `py_encode_basestring_ascii` (`ensure_ascii=True`).
///
/// The `rust/brutex-lake/src/stats_shard.rs::push_json_string` byte law,
/// second copy (see the module doc's COMMON deviation note +
/// DEC-RUST-JSON-ESCAPE-COMMON-001).
fn push_json_string(out: &mut String, s: &str) {
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

/// Render *node* exactly as CPython `json.dumps(node, sort_keys=True,
/// separators=(",", ":"), ensure_ascii=True)` renders the corresponding
/// Python tree. Pure; the output is guaranteed pure ASCII.
#[must_use]
pub fn render(node: &Node) -> String {
    let mut out = String::new();
    render_into(&mut out, node);
    out
}

fn render_into(out: &mut String, node: &Node) {
    match node {
        Node::Null => out.push_str("null"),
        Node::Bool(true) => out.push_str("true"),
        Node::Bool(false) => out.push_str("false"),
        Node::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Node::Str(s) => push_json_string(out, s),
        Node::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                render_into(out, item);
            }
            out.push(']');
        }
        Node::Dict(pairs) => {
            // sort_keys=True over EXACT-str keys: CPython's comparison is
            // code-point order == Rust String byte-wise UTF-8 order
            // (subclass keys never reach here — refused at the FFI).
            let mut sorted: Vec<&(String, Node)> = pairs.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (key, value)) in sorted.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                push_json_string(out, key);
                out.push(':');
                render_into(out, value);
            }
            out.push('}');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The escaper is reachable for direct pinning by the in-crate unit
    /// tests AND (via `render` over `Node::Str`) the integration corpus.
    #[test]
    fn escaper_smoke_via_render() {
        assert_eq!(render(&Node::Str("é".to_string())), "\"\\u00e9\"");
    }
}
