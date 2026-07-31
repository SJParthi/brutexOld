//! CLASS-C input-envelope refusal marker — DEC-RUST-STRICT-NO-FALLBACK-001.
//!
//! Under the strict-Rust mandate a Python fall-back is a REFUSAL, not a
//! degrade — unless the crate declined the call because the *INPUT* was
//! outside its envelope and the Python oracle was always the intended
//! handler. Those two populations are indistinguishable at the Python choke
//! point when they share an exception CLASS (`ValueError` / `TypeError` /
//! `OverflowError` all arrive from both), so the crate must SAY which one it
//! was.
//!
//! The doctrine (identical to `rust/brutex-strategies/src/python.rs`, the
//! lane this shape shipped in first — same literal marker text so there is
//! ONE shape repo-wide):
//!
//! * a **MARKED** error carries [`INPUT_REFUSED_MARKER`] and means "*this
//!   input is outside the crate's envelope; serve the Python oracle*" — the
//!   facade logs a distinct `*_input_refused` reason which strict mode does
//!   NOT refuse, because refusing it would crash a legitimate, byte-equal,
//!   pinned call;
//! * an **UNMARKED** error is, by construction, a genuine crate FAULT (a
//!   stale wheel whose call shape drifted, a foreign/byzantine module,
//!   broken internals) — the facade logs its plain `*_error` reason and
//!   strict mode REFUSES it.
//!
//! Placement note (a deliberate deviation from the brutex-strategies
//! template, which homes the marker in its `python.rs`): `fno-math`'s
//! `python.rs` is a *kernel* module (bitcount / supermask counting), not a
//! bridge-helper module, and it is contended by several in-flight lanes.
//! A dedicated leaf module keeps the marker importable from every kernel
//! (`crate::input_refused::…`) with an append-only one-line `lib.rs` edit.
//!
//! Keep [`INPUT_REFUSED_MARKER`] stable: it is part of the facade contract
//! (`brutex/ranking/cvar_gate.py::_INPUT_REFUSED_MARKER`), pinned
//! cross-language against the real wheel.

use pyo3::exceptions::{PyOverflowError, PyTypeError, PyValueError};
use pyo3::prelude::*;

/// Marker text stamped on EVERY input-envelope refusal this crate raises.
///
/// Byte-identical to `rust/brutex-strategies/src/python.rs`'s constant of
/// the same name — one doctrine, one literal.
pub const INPUT_REFUSED_MARKER: &str = "input outside the crate envelope";

/// Compose a MARKED refusal message — the pure half of
/// [`mark_input_refusal`], split out so `cargo test` can pin the byte shape
/// without linking libpython (the crate's `extension-module` build strips
/// the Python lib, so no `#[cfg(test)]` code may touch a `PyErr`).
#[must_use]
pub fn input_refused_message(context: &str, detail: &str) -> String {
    format!("{context} — {INPUT_REFUSED_MARKER}: {detail}")
}

/// Re-stamp a PyO3 extraction failure as a MARKED input-envelope refusal.
///
/// The original exception's text is preserved as the trailing detail. The
/// exception CLASS is PRESERVED for the three classes the facade degrade
/// sets carry (`TypeError`, `OverflowError`, `ValueError`) and collapses to
/// `ValueError` otherwise, because several stdlib extraction failures
/// cannot be reconstructed from a single message argument. (The
/// brutex-strategies template collapses `OverflowError` → `ValueError`; the
/// wider preservation here keeps `brutex.ranking.cvar_gate`'s documented
/// "an int past i64 range degrades with `OverflowError`" envelope literally
/// true.) Every preserved class is a member of the facade's degrade set, so
/// routing is unchanged — only the marker (and hence the strict-mode class)
/// is added.
#[must_use]
pub fn mark_input_refusal(py: Python<'_>, context: &str, err: &PyErr) -> PyErr {
    let detail = err.value(py).str().map_or_else(
        |_| "<unprintable>".to_owned(),
        |s| s.to_string_lossy().into_owned(),
    );
    let message = input_refused_message(context, &detail);
    if err.is_instance_of::<PyTypeError>(py) {
        PyTypeError::new_err(message)
    } else if err.is_instance_of::<PyOverflowError>(py) {
        PyOverflowError::new_err(message)
    } else {
        PyValueError::new_err(message)
    }
}

#[cfg(test)]
mod tests {
    use super::{INPUT_REFUSED_MARKER, input_refused_message};

    #[test]
    fn marker_literal_is_the_repo_wide_doctrine_text() {
        // Byte-pinned: `brutex/ranking/cvar_gate.py::_INPUT_REFUSED_MARKER`
        // and `rust/brutex-strategies/src/python.rs` carry this SAME text.
        assert_eq!(INPUT_REFUSED_MARKER, "input outside the crate envelope");
    }

    #[test]
    fn message_carries_context_marker_and_detail() {
        let message = input_refused_message("stats_cvar_tail_fold: pnls", "boom");
        assert_eq!(
            message,
            "stats_cvar_tail_fold: pnls — input outside the crate envelope: boom"
        );
        assert!(message.contains(INPUT_REFUSED_MARKER));
    }
}
