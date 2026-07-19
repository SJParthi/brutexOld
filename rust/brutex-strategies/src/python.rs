//! PyO3 binding (feature `python`) — the thin extraction + boundary edge.
//!
//! Two `#[pyfunction]`s (DEC-STRATEGIES-RUST-COMBO-001 §2.2):
//!
//! * [`first_unit_separator`] — delegates to the pure scan core; PyO3's
//!   `Vec<String>` extraction is the only edge (a lone-surrogate or non-str
//!   element raises `UnicodeEncodeError ⊂ ValueError` / `TypeError` BEFORE
//!   the body runs — the facade degrades to the oracle).
//! * [`canonical_json`] — two-phase (the stats_shard "extract owned first,
//!   detach for assembly" discipline): phase 1 walks the `Bound<PyAny>`
//!   tree into an owned [`Node`] UNDER the GIL (every violation a typed
//!   PyErr in the facade's degrade set); phase 2 renders WITHOUT the GIL
//!   under `catch_unwind` (a panic surfaces as a catchable `RuntimeError`,
//!   never a process abort — the crate profile deliberately keeps
//!   unwinding, Cargo.toml §2.5 note).
//!
//! Python owns EVERY error message consumers raise, the exception taxonomy
//! of `canonicalise_params` / `reject_unit_separator`, and the field-name
//! lookup — nothing that reaches hashed/logged/raised output is minted here
//! except the canonical JSON bytes themselves (whose law is pinned by the
//! frozen cross-language golden + the wheel-ON hypothesis differential).

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
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

/// Return the index of the first *value* containing `\x1f`, else `None`.
///
/// The decision half of `combo.reject_unit_separator` — Python maps the
/// index back to the field NAME and raises the injected error class with
/// the verbatim message bytes (the crate carries NO message and NO
/// exception class). Extraction failures (non-str element → `TypeError`;
/// lone-surrogate str → `UnicodeEncodeError ⊂ ValueError`) raise before the
/// body runs; the facade degrades such calls to the Python oracle.
#[pyfunction]
fn first_unit_separator(values: Vec<String>) -> Option<usize> {
    scan_sep::first_unit_separator(&values)
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
/// `ValueError` / `TypeError` / `OverflowError`):
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
    if depth > MAX_WALK_DEPTH {
        return Err(PyValueError::new_err(format!(
            "canonical_json: tree depth exceeds the defensive cap of {MAX_WALK_DEPTH}"
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
        // Propagate OverflowError for out-of-i128 ints — the routing law is
        // EXACTLY extract::<i128>()'s native range (design §2.2/§2.4:
        // i128::MIN itself ROUTES; no separate magnitude gate).
        return Ok(Node::Int(value.extract::<i128>()?));
    }
    if value.is_instance_of::<PyString>() {
        // Subclass str VALUES are fine (buffer read); a lone surrogate
        // raises UnicodeEncodeError (⊂ ValueError) here.
        return Ok(Node::Str(value.extract::<String>()?));
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        let mut pairs: Vec<(String, Node)> = Vec::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            if key.cast_exact::<PyString>().is_err() {
                if key.is_instance_of::<PyString>() {
                    // str subclass: overridden __lt__ could legally sort
                    // differently from code-point order — refuse.
                    return Err(PyTypeError::new_err(NONEXACT_KEY_MSG));
                }
                // Not a str at all — unreachable from _canonicalise_value
                // (it rejects non-str keys first); hostile direct call.
                return Err(PyTypeError::new_err("canonical_json: dict key is not str"));
            }
            // Exact-str key: extraction can STILL fail (lone surrogate) —
            // UnicodeEncodeError ⊂ ValueError, a degrade member.
            let key_string = key.extract::<String>()?;
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
         only dict/list/str/int/bool/None)",
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
/// Extraction failures raise typed errors BEFORE the render (see
/// [`extract_node`]); the assembly runs WITHOUT the GIL (`Python::detach` —
/// sound: the tree is extracted to owned Rust data first) under
/// `catch_unwind`, so a render panic raises `RuntimeError` (caught at this
/// boundary — a panic never crosses the FFI raw and never aborts the
/// process; the crate profile keeps unwinding for exactly this).
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
