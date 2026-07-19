//! The pure `\x1f` first-offender scan — the decision half of
//! `brutex/strategies/combo.py::reject_unit_separator`.
//!
//! The Python law (combo.py, DEC-RUN-IDENTITY-GUARD-PARITY-001): iterate the
//! `(field_name, field_value)` pairs IN ORDER; on the FIRST value containing
//! `"\x1f"` (`UNIT_SEPARATOR` — the identity-payload field-join delimiter)
//! raise the injected error class naming the field. This module carries ONLY
//! the scan: which VALUE (by index) is the first offender. The scan checks
//! VALUES only — the Python law never scans field NAMES. Python owns the
//! name lookup, the message bytes and the exception class
//! (DEC-STRATEGIES-RUST-COMBO-001 §1.1).

/// The identity-payload field-join delimiter (`UNIT_SEPARATOR`, U+001F).
pub const UNIT_SEPARATOR: char = '\u{1f}';

/// Return the index of the first element of *values* containing `\x1f`,
/// else `None`.
///
/// That is the whole law. `\x1f` is ASCII, so a byte scan over the UTF-8
/// buffer is exact (0x1F can never appear inside a multi-byte UTF-8
/// sequence — continuation bytes are `0x80..=0xBF`).
#[must_use]
pub fn first_unit_separator(values: &[String]) -> Option<usize> {
    values
        .iter()
        .position(|value| value.contains(UNIT_SEPARATOR))
}
