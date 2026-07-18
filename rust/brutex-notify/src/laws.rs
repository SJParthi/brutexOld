//! The four PURE notification laws (DEC-NOTIFY-RUST-CORE-001).
//!
//! Each function is a token-for-token transcription of its Python oracle:
//!
//! * [`escape_markdown_v2`] ← `brutex/notifications/sweep_alerts.py`
//!   `_escape_markdown_v2_python` (the MarkdownV2 escape law).
//! * [`count_or_question`] ← `sweep_alerts.py` `_count_or_question_python`
//!   (the honest-unknown count law — `"?"` on `None`, NEVER a fabricated 0).
//! * [`dedupe_should_drop`] ← `brutex/notifications/telegram.py`
//!   `_dedupe_should_drop_python` (the dedupe-window drop predicate).
//! * [`truncate_id_12`] ← `sweep_alerts.py` `_truncate_id_12_python`
//!   (the `text[:12]` CODE-POINT id truncation).
//!
//! ## Byte-identity laws (the parity contract)
//!
//! * Iteration is per Unicode **scalar value** (`str::chars()`), matching
//!   CPython's per-code-point `str` iteration — astral emoji (U+1F6D1 🛑)
//!   and variation selectors (U+FE0F) pass through UNTOUCHED. Lone
//!   surrogates cannot reach this crate: they fail UTF-8 conversion at the
//!   pyo3 edge and the Python facade degrades to the oracle.
//! * The escape set is EXACTLY the 18 ASCII chars of
//!   `sweep_alerts._MDV2_SPECIALS` — the backslash itself is DELIBERATELY
//!   NOT escaped (a pre-existing Python property preserved bit-exactly; a
//!   "more correct" escaper would be a parity break).
//! * The dedupe predicate takes IEEE-754 f64 **bit patterns** as `u64`
//!   (`f64::from_bits`) so the Python `float` subtraction + strict `<`
//!   compare is reproduced bit-exactly — including NaN semantics (any NaN
//!   operand compares `false` → never dropped) — while keeping the house
//!   ints-only FFI discipline (no float OBJECT crosses).
//! * `count_or_question` formats `i64` exactly like CPython's `str(int)`
//!   over the i64 domain (plain decimal digits, `-` sign, no separators);
//!   an out-of-`i64` Python int never reaches the crate (pyo3 raises
//!   `OverflowError` at the edge → the facade degrades to the oracle).
//!
//! Every law is panic-free by construction over ANY `&str` / bit-pattern
//! input: no indexing, no `unwrap`, no arithmetic that can overflow
//! (`String` growth can only abort on OOM — the same envelope as every
//! sibling kernel). The proptest differential (`tests/laws_proptest.rs`)
//! pins each law against a naive in-crate reference.

/// The MarkdownV2 reserved characters — EXACTLY the 18 ASCII chars of
/// `sweep_alerts._MDV2_SPECIALS` (`_ * [ ] ( ) ~ ` > # + - = | { } . !`).
/// The backslash is deliberately ABSENT (the Python law does not escape it).
pub const MDV2_SPECIALS: &str = "_*[]()~`>#+-=|{}.!";

/// How many leading code points [`truncate_id_12`] keeps (`text[:12]`).
pub const ID_TRUNCATE_CODE_POINTS: usize = 12;

/// Backslash-escape every MarkdownV2-reserved character in `text`.
///
/// The exact `escape_markdown_v2` law: per-code-point scan, prepend `\` when
/// the code point is in [`MDV2_SPECIALS`], every other code point (emoji,
/// astral planes, control chars, the backslash itself) passes through
/// untouched.
#[must_use]
pub fn escape_markdown_v2(text: &str) -> String {
    // Worst case doubles the char count; reserving len*2 bytes over-covers
    // ASCII-heavy alert bodies without a second pass.
    let mut out = String::with_capacity(text.len().saturating_mul(2));
    for ch in text.chars() {
        if MDV2_SPECIALS.contains(ch) {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

/// Render an optional count: `"?"` for `None` (the honest unknown — NEVER a
/// fabricated 0), else the plain decimal rendering of the value.
#[must_use]
pub fn count_or_question(value: Option<i64>) -> String {
    match value {
        None => "?".to_string(),
        Some(v) => v.to_string(),
    }
}

/// The dedupe-window drop predicate over IEEE-754 f64 BIT PATTERNS.
///
/// `last_seen_bits` is `None` when no prior send is stamped (never dropped);
/// otherwise the decision is the Python oracle's exact float arithmetic —
/// `(now - last_seen) < window` — reproduced via `f64::from_bits` (IEEE-754
/// subtraction + strict less-than are deterministic and identical between
/// CPython and Rust on the same bit patterns; a NaN in ANY operand makes the
/// compare `false`, i.e. never dropped — matching Python).
#[must_use]
pub fn dedupe_should_drop(window_bits: u64, last_seen_bits: Option<u64>, now_bits: u64) -> bool {
    match last_seen_bits {
        None => false,
        Some(bits) => {
            let window = f64::from_bits(window_bits);
            let last_seen = f64::from_bits(bits);
            let now = f64::from_bits(now_bits);
            (now - last_seen) < window
        }
    }
}

/// Keep the first [`ID_TRUNCATE_CODE_POINTS`] Unicode scalar values of
/// `text` — the exact semantics of Python's code-point slice `text[:12]`
/// (never a byte slice: an astral char inside the window survives whole,
/// and a shorter string is returned unchanged).
#[must_use]
pub fn truncate_id_12(text: &str) -> String {
    text.chars().take(ID_TRUNCATE_CODE_POINTS).collect()
}
