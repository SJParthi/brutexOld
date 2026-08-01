//! PyO3 binding (feature `python`) — the thin extraction + boundary edge.
//!
//! Two `#[pyfunction]`s (DEC-STRATEGIES-RUST-COMBO-001 §2.2):
//!
//! * [`first_unit_separator`] — delegates to the pure scan core; the
//!   explicit `Vec<String>` extraction is the only edge (a lone-surrogate or
//!   non-str element raises `UnicodeEncodeError ⊂ ValueError` / `TypeError`
//!   BEFORE the scan runs — the facade degrades to the oracle).
//! * [`canonical_json`] — two-phase (the stats_shard "extract owned first,
//!   detach for assembly" discipline): phase 1 walks the `Bound<PyAny>`
//!   tree into an owned [`Node`] UNDER the GIL (every violation a typed
//!   PyErr in the facade's degrade set); phase 2 renders WITHOUT the GIL
//!   under `catch_unwind` (a panic surfaces as a catchable `RuntimeError`,
//!   never a process abort — the crate profile deliberately keeps
//!   unwinding, Cargo.toml §2.5 note).
//!
//! Both edges classify their failures for the facade's strict mode
//! (DEC-RUST-STRICT-NO-FALLBACK-001): an INPUT this crate declines carries
//! [`INPUT_REFUSED_MARKER`] (or, for the str-subclass dict key, the older
//! [`NONEXACT_KEY_MSG`]) and keeps degrading to the Python oracle; anything
//! UNMARKED is a crate FAULT and strict mode refuses it.
//!
//! Python owns EVERY error message consumers raise, the exception taxonomy
//! of `canonicalise_params` / `reject_unit_separator`, and the field-name
//! lookup — nothing that reaches hashed/logged/raised output is minted here
//! except the canonical JSON bytes themselves (whose law is pinned by the
//! frozen cross-language golden + the wheel-ON hypothesis differential).

use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString};
use pyo3::wrap_pyfunction;

use crate::canon::{self, Node};
use crate::scan_sep;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Marker text stamped on EVERY input-envelope refusal this crate raises
/// (DEC-RUST-STRICT-NO-FALLBACK-001 — the CLASS-C generalisation of
/// [`NONEXACT_KEY_MSG`]).
///
/// A marked error means "*the INPUT is outside this crate's envelope and the
/// Python oracle was always the intended handler*" — the facade logs a
/// distinct `*_input_refused` reason which strict mode does NOT refuse,
/// because refusing it would crash a legitimate, byte-equal, pinned call
/// (an out-of-`i128` int, a lone-surrogate `str`, a bare float on a hostile
/// direct call, a malformed scan pair …).
///
/// An UNMARKED error from this crate is, by construction, a genuine crate
/// FAULT (a caught render panic → `RuntimeError`, or broken wheel
/// internals) — the facade logs `canonical_json_error` / `scan_error` and
/// strict mode REFUSES it.
///
/// Keep the fragment stable: it is part of the facade contract
/// (`brutex/strategies/combo_rust.py::_INPUT_REFUSED_MARKER`).
const INPUT_REFUSED_MARKER: &str = "input outside the crate envelope";

/// Re-stamp a PyO3 extraction failure as a MARKED input-envelope refusal.
///
/// The original exception's text is preserved as the trailing detail; the
/// exception CLASS collapses to `TypeError` (when the original was one) or
/// `ValueError` otherwise, because several stdlib extraction failures
/// (notably `UnicodeEncodeError`) cannot be reconstructed from a single
/// message argument. Both classes are members of the facade's degrade set,
/// so routing is unchanged — only the marker (and hence the strict-mode
/// class) is added.
fn mark_input_refusal(py: Python<'_>, context: &str, err: &PyErr) -> PyErr {
    let detail = err.value(py).str().map_or_else(
        |_| "<unprintable>".to_owned(),
        |s| s.to_string_lossy().into_owned(),
    );
    let message = format!("{context} — {INPUT_REFUSED_MARKER}: {detail}");
    if err.is_instance_of::<PyTypeError>(py) {
        PyTypeError::new_err(message)
    } else {
        PyValueError::new_err(message)
    }
}

/// Return the index of the first *value* containing `\x1f`, else `None`.
///
/// The decision half of `combo.reject_unit_separator` — Python maps the
/// index back to the field NAME and raises the injected error class with
/// the verbatim message bytes (the crate carries NO message and NO
/// exception class).
///
/// The `Vec<String>` extraction is performed EXPLICITLY (rather than via the
/// argument signature) so its failures — a non-`str` element, a
/// lone-surrogate `str`, a non-sequence argument — are re-stamped with
/// [`INPUT_REFUSED_MARKER`]: they are inputs outside this crate's envelope,
/// not crate faults, so the facade must keep degrading to the Python oracle
/// even under strict mode (DEC-RUST-STRICT-NO-FALLBACK-001). The extracted
/// sequence is otherwise byte-identical to the previous signature-level
/// extraction, and the scan core itself is infallible.
#[pyfunction]
fn first_unit_separator(py: Python<'_>, values: &Bound<'_, PyAny>) -> PyResult<Option<usize>> {
    let owned: Vec<String> = values
        .extract()
        .map_err(|err| mark_input_refusal(py, "first_unit_separator: values", &err))?;
    Ok(scan_sep::first_unit_separator(&owned))
}

/// Defensive walk depth cap (design §2.2). Unreachable from
/// `canonicalise_params` — the Python canonicaliser rejects depth > 64
/// BEFORE the seam; reachable only via a hostile direct facade call, which
/// degrades to the oracle.
const MAX_WALK_DEPTH: usize = 256;

/// Marker text for a str-SUBCLASS dict key refusal. The facade matches this
/// stable fragment to log the DISTINCT `nonexact_key_refused` warn-once
/// (design §2.2: CPython `sort_keys` sorts with the ORIGINAL key objects'
/// `__lt__`, so a subclass key with overridden ordering could legally sort
/// differently from code-point order — refuse-to-oracle, the #1915
/// exact-type `date` gate analog). Keep the fragment stable: it is part of
/// the facade contract.
const NONEXACT_KEY_MSG: &str =
    "canonical_json: dict key is a str subclass (non-exact str) — refused to the oracle";

/// Phase 1 — under the GIL: walk the lowered tree into an owned [`Node`].
///
/// Per-node rules (each violation a typed PyErr in the facade degrade set —
/// `ValueError` / `TypeError` / `OverflowError`, and — being an INPUT the
/// crate declines rather than a crate fault — every one of them stamped
/// with [`INPUT_REFUSED_MARKER`] except the str-subclass key, which carries
/// its own older [`NONEXACT_KEY_MSG`] marker):
///
/// * `None` → `Null`.
/// * `bool` BEFORE any integer extraction (bool ⊂ int; a byte law — CPython
///   json also checks bool first).
/// * `int` (incl. subclasses, by TRUE PyLong value — `PyNumber_Index`
///   short-circuits for PyLong, a Python-level `__index__` override is NOT
///   invoked) via `extract::<i128>()`; out-of-range raises `OverflowError`.
/// * `str` VALUES accept subclasses (`extract::<String>` reads the buffer);
///   a lone surrogate raises `UnicodeEncodeError ⊂ ValueError`.
/// * dict KEYS require an EXACT-`str` cast (`cast_exact::<PyString>` —
///   pyo3 0.29's spelling of the design's `downcast_exact` law) — a
///   subclass key raises
///   the [`NONEXACT_KEY_MSG`] `TypeError`; an exact-but-unencodable
///   (lone-surrogate) key fails ONE step later at `String` extraction.
/// * `list` → array in order (post-lowering the tree holds only builtin
///   lists; sequence lowering already happened in Python).
/// * anything else (float, set, bytes, custom) → `TypeError` — unreachable
///   from `canonicalise_params` (the lowering rejects or converts every
///   such shape first); a hostile direct call degrades to the oracle.
fn extract_node(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Node> {
    let py = value.py();
    if depth > MAX_WALK_DEPTH {
        return Err(PyValueError::new_err(format!(
            "canonical_json: tree depth exceeds the defensive cap of \
             {MAX_WALK_DEPTH} — {INPUT_REFUSED_MARKER}"
        )));
    }
    if value.is_none() {
        return Ok(Node::Null);
    }
    // bool BEFORE int: PyBool is a PyInt subclass in CPython.
    if let Ok(b) = value.cast::<PyBool>() {
        return Ok(Node::Bool(b.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        // Out-of-i128 ints are an ENVELOPE refusal, not a fault — the
        // routing law is EXACTLY extract::<i128>()'s native range (design
        // §2.2/§2.4: i128::MIN itself ROUTES; no separate magnitude gate).
        // Marked so the facade keeps degrading under strict mode.
        return match value.extract::<i128>() {
            Ok(int_value) => Ok(Node::Int(int_value)),
            Err(err) => Err(PyOverflowError::new_err(format!(
                "canonical_json: int outside the i128 routing range — \
                 {INPUT_REFUSED_MARKER}: {err}"
            ))),
        };
    }
    if value.is_instance_of::<PyString>() {
        // Subclass str VALUES are fine (buffer read); a lone surrogate
        // fails String extraction here — an ENVELOPE refusal.
        return match value.extract::<String>() {
            Ok(text) => Ok(Node::Str(text)),
            Err(err) => Err(mark_input_refusal(py, "canonical_json: str value", &err)),
        };
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        let mut pairs: Vec<(String, Node)> = Vec::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            if key.cast_exact::<PyString>().is_err() {
                if key.is_instance_of::<PyString>() {
                    // str subclass: overridden __lt__ could legally sort
                    // differently from code-point order — refuse. Carries
                    // its OWN marker (NONEXACT_KEY_MSG → the distinct
                    // `nonexact_key_refused` reason), deliberately NOT the
                    // generic one.
                    return Err(PyTypeError::new_err(NONEXACT_KEY_MSG));
                }
                // Not a str at all — unreachable from _canonicalise_value
                // (it rejects non-str keys first); hostile direct call.
                return Err(PyTypeError::new_err(format!(
                    "canonical_json: dict key is not str — {INPUT_REFUSED_MARKER}"
                )));
            }
            // Exact-str key: extraction can STILL fail (lone surrogate) —
            // the second fail-closed KEY path, likewise an ENVELOPE refusal.
            let key_string = key
                .extract::<String>()
                .map_err(|err| mark_input_refusal(py, "canonical_json: dict key", &err))?;
            pairs.push((key_string, extract_node(&item, depth + 1)?));
        }
        return Ok(Node::Dict(pairs));
    }
    if let Ok(list) = value.cast::<PyList>() {
        let mut items: Vec<Node> = Vec::with_capacity(list.len());
        for item in list.iter() {
            items.push(extract_node(&item, depth + 1)?);
        }
        return Ok(Node::List(items));
    }
    Err(PyTypeError::new_err(format!(
        "canonical_json: unsupported node type {} (the lowered tree admits \
         only dict/list/str/int/bool/None) — {INPUT_REFUSED_MARKER}",
        value
            .get_type()
            .name()
            .map_or_else(|_| "<unknown>".to_string(), |name| name.to_string()),
    )))
}

/// Render the CPython canonical JSON of an ALREADY-lowered *tree* —
/// byte-identical to `json.dumps(tree, sort_keys=True,
/// separators=(",", ":"), ensure_ascii=True)`.
///
/// Extraction failures raise typed, [`INPUT_REFUSED_MARKER`]-stamped errors
/// BEFORE the render (see [`extract_node`]); the assembly runs WITHOUT the
/// GIL (`Python::detach` — sound: the tree is extracted to owned Rust data
/// first) under `catch_unwind`, so a render panic raises `RuntimeError`
/// (caught at this boundary — a panic never crosses the FFI raw and never
/// aborts the process; the crate profile keeps unwinding for exactly this).
/// That `RuntimeError` is DELIBERATELY left UNMARKED: a panicking render is
/// a crate FAULT, so strict mode must refuse it rather than degrade.
#[pyfunction]
fn canonical_json(py: Python<'_>, tree: &Bound<'_, PyAny>) -> PyResult<String> {
    let node = extract_node(tree, 0)?;
    py.detach(|| std::panic::catch_unwind(|| canon::render(&node)))
        .map_err(|_| {
            PyRuntimeError::new_err(
                "brutex-strategies canonical render panicked (caught at the FFI boundary)",
            )
        })
}

#[pymodule]
fn _brutex_strategies(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed pattern).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(first_unit_separator, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_json, m)?)?;
    Ok(())
}
