//! # `results_report` — the leaderboard / cascade-sidecar RENDER laws
//!
//! DEC-RUST-CASCADE-RESULTS-001 phase B, slice 2c: the RENDER-LAW family of
//! the deleted `brutex/engine/results_report.py` bodies, ported
//! token-for-token and verified against the FROZEN pre-conversion parity
//! corpus (`tests/golden/fixtures/cascade_rust_parity/results_report.json`).
//! The store-coupled I/O half (discovery / merge / snapshot quarantine /
//! manifest attribution) STAYS shim-side Python — this module computes text
//! and decisions only.
//!
//! ## The byte laws hosted here
//!
//! * the aliases-ONLY `--sort-by` vocabulary resolution (the canonical
//!   `ResultRow` column names themselves REFUSE) with the verbatim
//!   `unknown sort key {token!r}; choose one of [...]` message;
//! * `_format_deflated` — CPython `f"{v:+.4f}"`; `None` → `"—"` (em dash);
//! * `_format_conditions` — falsy-empty → the no-condition note; every
//!   truthy string passes through as the ORIGINAL object;
//! * `render_leaderboard` — the two-line-per-finalist winners table
//!   (plain concatenation, rows joined `"\n"`), incl. the exact-integer
//!   `format_inr` money twin (never f64) and the `win_rate * 100` f64
//!   PRODUCT before the `.1f` format;
//! * `_provisional_note` — the counted pool-N pass label under the strict
//!   `is True` IDENTITY law (int `1` never counts, either key);
//! * `_render_sidecar_group` — one cascade-sidecar group line (`.get`
//!   fallbacks fire on ABSENCE only; `float(pbo)` runs CPython's own
//!   `float()` so the coercion grammar + the `could not convert string to
//!   float: 'junk'` message are byte-identical);
//! * `render_cascade_sidecar`'s render law over ALREADY-READ file content
//!   (the path probe + file read stay shim-side — the OSError read arm's
//!   message is path-dependent and deliberately unfrozen), incl. the
//!   `version != REPORT_VERSION` rich-compare gate (the `version: true`
//!   bool==int pun LOADS) and the `UNREADABLE:` funnels;
//! * `results_show_report`'s text-assembly law (header · the two frozen
//!   provenance notes selected by origins `None`-ness · leaderboard ·
//!   conditional sidecar append).
//!
//! ## Marshalling (the decision-(iii) record)
//!
//! String-typed values NEVER cross into Rust: every interpolated fragment
//! is stringified by CPython `str()` (`f"{x}"` ≡ `format(x, "")` ≡
//! `str(x)`) and the output text is assembled with Python `str.join` over
//! a fragment list — so a lone-surrogate instrument / JSON-escape-minted
//! label flows through byte-identically (the frozen
//! `lone_surrogate_instrument` corpus case). Rust computes only the
//! decisions + the ASCII numeric fragments (money grouping, fixed-point
//! floats, rank counters).
//!
//! ## The fixed-point float twin (the decision-(iv) record)
//!
//! [`py_fixed`] delegates to Rust `format!("{:+.p$}")` — probed EQUAL to
//! CPython `f"{v:+.pf}"` over 300k random-bit-pattern doubles plus the
//! exact-tie / subnormal / huge cases (both sides correctly round the
//! exact binary value, ties-to-even). The ONE divergence is NaN (Rust
//! `"NaN"`, no sign; CPython `"nan"` / `"+nan"` with the sign flag,
//! sign-bit ignored) — special-cased. `float("inf")` therefore renders
//! `pool PBO=inf` exactly as the Python law did (uncaptured by the frozen
//! corpus; pinned by a dedicated unit + cargo test instead).

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::CompareOp;
use pyo3::types::{PyBool, PyDict, PyList, PyString};

use crate::{capture_repr, domain_err, mapping_get};

/// The `_format_conditions` empty-mask note (verbatim, em dash).
const NO_CONDITIONS_NOTE: &str = "(no conditions — enters every bar)";

/// The empty-leaderboard placeholder (verbatim).
const NO_RESULTS_NOTE: &str = "(no persisted results)";

/// The blanket provenance disclaimer (`origins is None`, verbatim).
const NOTE_BLANKET: &str = "  (Stage-2 grid rows are gate survivors; a --stage3-enable sweep also \
                            persists UNGATED Stage-3 refined-eval trials — no per-row gate verdict)";

/// The src-markers provenance note (origins attributed, verbatim).
const NOTE_MARKERS: &str = "  (per-row src= markers from the cascade manifest: src=grid rows are \
                            gate survivors; src=stage3 rows are UNGATED refined trials — \
                            DEC-ROW-PROVENANCE-001)";

// ───────────────────────── pure numeric twins (cargo-tested) ─────────────

/// CPython `f"{v:.<prec>f}"` / `f"{v:+.<prec>f}"` twin.
///
/// Finite values + infinities delegate to Rust's `format!` — probed
/// byte-equal to CPython over 300k random doubles + the exact-tie cases
/// (both correctly round the exact binary value, ties-to-even). NaN is the
/// one special case: CPython renders `"nan"` (`"+nan"` under the sign
/// flag, the sign BIT ignored either way); Rust would render `"NaN"`.
pub fn py_fixed(v: f64, prec: usize, plus: bool) -> String {
    if v.is_nan() {
        return if plus {
            "+nan".to_string()
        } else {
            "nan".to_string()
        };
    }
    if plus {
        format!("{v:+.prec$}")
    } else {
        format!("{v:.prec$}")
    }
}

/// `brutex.money.format_inr` twin over int64 paisa — pure INTEGER math.
///
/// The Python law is `f"₹{paisa_to_rupees(paisa):,.2f}"` where
/// `paisa_to_rupees` returns an EXACT 2dp-quantized `Decimal`
/// (`Decimal(paisa) / 100`, `ROUND_HALF_UP` quantize — exact for every
/// integer paisa), and `{:,.2f}` on a `Decimal` is exact decimal
/// formatting with WESTERN 3-digit comma grouping. For an i64 that is
/// sign + integer digit grouping + a 2-digit paisa remainder — no float
/// may ever enter (exact beyond 2^53; frozen at 2**62 paisa →
/// `₹46,116,860,184,273,879.04`). The sign renders AFTER the `₹`
/// (`₹-0.01`).
pub fn format_inr_paisa(paisa: i64) -> String {
    let mag = (paisa as i128).unsigned_abs(); // i64::MIN-safe
    let rupees = mag / 100;
    let cents = mag % 100;
    let digits = rupees.to_string();
    let mut grouped = String::with_capacity(digits.len() + digits.len() / 3);
    let len = digits.len();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (len - i).is_multiple_of(3) {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    let sign = if paisa < 0 { "-" } else { "" };
    format!("₹{sign}{grouped}.{cents:02}")
}

// ───────────────────────── fragment assembly (decision iii) ──────────────

/// A text under assembly: PyString fragments joined by CPython `str.join`,
/// so surrogate-carrying parts never cross into Rust `String`s.
struct Frags<'py> {
    py: Python<'py>,
    parts: Bound<'py, PyList>,
}

impl<'py> Frags<'py> {
    fn new(py: Python<'py>) -> Self {
        Self {
            py,
            parts: PyList::empty(py),
        }
    }

    /// Append a static ASCII/UTF-8 literal fragment.
    fn lit(&self, s: &str) -> PyResult<()> {
        self.parts.append(PyString::new(self.py, s))
    }

    /// Append a Rust-computed fragment.
    fn owned(&self, s: &str) -> PyResult<()> {
        self.parts.append(PyString::new(self.py, s))
    }

    /// Append `str(v)` — the f-string interpolation law
    /// (`f"{x}"` ≡ `format(x, "")` ≡ `str(x)`).
    fn obj(&self, v: &Bound<'py, PyAny>) -> PyResult<()> {
        self.parts.append(v.str()?)
    }

    /// Append an already-stringified fragment object verbatim.
    fn frag(&self, v: &Bound<'py, PyAny>) -> PyResult<()> {
        self.parts.append(v)
    }

    /// `"".join(parts)` — one CPython join builds the final text.
    fn join(self) -> PyResult<Bound<'py, PyAny>> {
        PyString::new(self.py, "").call_method1("join", (self.parts,))
    }
}

/// `v is True` — the strict identity law (`bool` has exactly two
/// singletons, so `isinstance(v, bool) and v` ≡ `v is True`; int `1`
/// satisfies NEITHER `provisional` nor `passed`).
fn is_py_true(v: &Bound<'_, PyAny>) -> bool {
    v.is_instance_of::<PyBool>() && v.is_truthy().unwrap_or(false)
}

// ───────────────────────────── the laws ──────────────────────────────────

/// `_resolve_sort_field` — aliases-ONLY vocabulary; the refusal message is
/// the verbatim `unknown sort key {token!r}; choose one of {sorted(...)}`
/// (repr-quoted token; the choose-list is CPython `str(sorted(aliases))`).
/// The alias map + the domain-error class are THREADED from the shim (the
/// single source stays the Python `SORT_ALIASES` constant).
pub fn resolve_sort_field<'py>(
    py: Python<'py>,
    token: &Bound<'py, PyAny>,
    aliases: &Bound<'py, PyAny>,
    error_cls: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let field = aliases.call_method1("get", (token,))?;
    if field.is_none() {
        let sorted_list = py
            .import("builtins")?
            .getattr("sorted")?
            .call1((aliases,))?;
        let msg = format!(
            "unknown sort key {}; choose one of {}",
            capture_repr(token),
            sorted_list.str()?.to_string_lossy()
        );
        return Err(domain_err(error_cls, msg));
    }
    Ok(field)
}

/// `_format_deflated` — `None` → `"—"`; else the always-signed `+.4f`.
pub fn format_deflated(value: Option<f64>) -> String {
    match value {
        None => "—".to_string(),
        Some(v) => py_fixed(v, 4, true),
    }
}

/// `_format_conditions` — falsy-empty → the note; a truthy string returns
/// the ORIGINAL object (verbatim passthrough, whitespace-only included).
pub fn format_conditions<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !value.is_truthy()? {
        return Ok(PyString::new(py, NO_CONDITIONS_NOTE).into_any());
    }
    Ok(value.clone())
}

/// `render_leaderboard` — the winners-table byte law. Rows arrive in the
/// caller's order; attribute reads + the `origins.get(run_id, '?')` marker
/// probe happen in the deleted f-string's exact evaluation order.
pub fn render_leaderboard<'py>(
    py: Python<'py>,
    rows: &Bound<'py, PyAny>,
    origins: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    if !rows.is_truthy()? {
        return Ok(PyString::new(py, NO_RESULTS_NOTE).into_any());
    }
    let f = Frags::new(py);
    let mut rank: u64 = 0;
    for row in rows.try_iter()? {
        let row = row?;
        rank += 1;
        if rank > 1 {
            f.lit("\n")?; // "\n".join(lines)
        }
        // The marker is computed BEFORE the line f-string (source order):
        // `"" if origins is None else f"  src={origins.get(row.run_id, '?')}"`.
        let marker: Option<Bound<'py, PyAny>> = match origins {
            None => None,
            Some(o) => {
                let run_id = row.getattr("run_id")?;
                Some(o.call_method1("get", (run_id, "?"))?.str()?.into_any())
            }
        };
        f.owned(&format!("#{rank}  "))?;
        f.obj(&row.getattr("instrument")?)?;
        f.lit(" ")?;
        f.obj(&row.getattr("tf")?)?;
        f.lit(" ")?;
        f.obj(&row.getattr("direction")?)?;
        f.lit("  pnl=")?;
        f.owned(&format_inr_paisa(
            row.getattr("net_pnl_paisa")?.extract::<i64>()?,
        ))?;
        f.lit("  trades=")?;
        f.obj(&row.getattr("total_trades")?)?;
        f.lit("  win=")?;
        // The MULTIPLY is an f64 product BEFORE the `.1f` format
        // (0.335 → 33.500000000000004 → "33.5"; 0.99999 → "100.0").
        f.owned(&py_fixed(
            row.getattr("win_rate")?.extract::<f64>()? * 100.0,
            1,
            false,
        ))?;
        f.lit("%  sharpe=")?;
        f.owned(&py_fixed(
            row.getattr("sharpe_ratio")?.extract::<f64>()?,
            3,
            true,
        ))?;
        f.lit("  dsr@tupleN=")?;
        let dsr = row.getattr("deflated_sharpe")?;
        let dsr_value: Option<f64> = if dsr.is_none() {
            None
        } else {
            Some(dsr.extract::<f64>()?)
        };
        f.owned(&format_deflated(dsr_value))?;
        if let Some(m) = marker {
            f.lit("  src=")?;
            f.frag(&m)?;
        }
        f.lit("\n    ")?;
        let conds = row.getattr("condition_names")?;
        if conds.is_truthy()? {
            f.obj(&conds)?;
        } else {
            f.lit(NO_CONDITIONS_NOTE)?;
        }
    }
    f.join()
}

/// `_provisional_note` — the counted pool-N pass label, or `""`.
///
/// Non-list `verdicts` → `""`; "stamped" = key PRESENCE on dict entries
/// only; the count demands `v.get("provisional") is True and
/// v.get("passed") is True` (strict identity, short-circuit preserved).
pub fn provisional_note(report: &Bound<'_, PyAny>) -> PyResult<String> {
    let verdicts = mapping_get(report, "verdicts")?;
    if !verdicts.is_instance_of::<PyList>() {
        return Ok(String::new());
    }
    let mut stamped: Vec<Bound<'_, PyAny>> = Vec::new();
    for v in verdicts.try_iter()? {
        let v = v?;
        if v.is_instance_of::<PyDict>() && v.contains("provisional")? {
            stamped.push(v);
        }
    }
    if stamped.is_empty() {
        return Ok(String::new());
    }
    let mut n_provisional: u64 = 0;
    for v in &stamped {
        if is_py_true(&v.call_method1("get", ("provisional",))?)
            && is_py_true(&v.call_method1("get", ("passed",))?)
        {
            n_provisional += 1;
        }
    }
    if n_provisional == 0 {
        return Ok(String::new());
    }
    Ok(format!(
        "  provisional={n_provisional} (pool-N DSR pass — not proven at leaderboard N)"
    ))
}

/// `_render_sidecar_group` — one group line, or `ValueError` on a bad
/// shape. Fallback law: `.get(key, '?')` fires on ABSENCE only (a present
/// `None`/int label is echoed verbatim); `dsr_n_source` is echoed even
/// when junk; `float(pbo)` is CPython's own `float()` (int / bool /
/// numeric-string coerce; a junk string raises CPython's verbatim
/// `ValueError`, which the sidecar render funnels into `UNREADABLE:`).
pub fn render_sidecar_group<'py>(
    py: Python<'py>,
    entry: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    if !entry.is_instance_of::<PyDict>() {
        return Err(PyValueError::new_err("group entry is not an object"));
    }
    let report = mapping_get(entry, "report")?;
    if !report.is_instance_of::<PyDict>() {
        return Err(PyValueError::new_err("group report is not an object"));
    }
    // label = f"{entry.get('instrument', '?')} {entry.get('tf', '?')}"
    let label_instrument = entry.call_method1("get", ("instrument", "?"))?.str()?;
    let label_tf = entry.call_method1("get", ("tf", "?"))?.str()?;
    // counts = f"in={report.get('n_input', '?')} survived={report.get('n_survived', '?')}"
    let n_input = report.call_method1("get", ("n_input", "?"))?.str()?;
    let n_survived = report.call_method1("get", ("n_survived", "?"))?.str()?;
    // The dsr fragment (three-state stamp law), then += _provisional_note.
    let mut dsr_parts: Vec<Bound<'py, PyAny>> = Vec::new();
    if !report.contains("dsr_n_source")? {
        dsr_parts.push(PyString::new(py, "dsr N not stamped (pre-ADR-0008 report)").into_any());
    } else {
        dsr_parts.push(PyString::new(py, "dsr N=").into_any());
        dsr_parts.push(
            report
                .call_method1("get", ("dsr_n_trials", "?"))?
                .str()?
                .into_any(),
        );
        dsr_parts.push(PyString::new(py, " source=").into_any());
        dsr_parts.push(report.get_item("dsr_n_source")?.str()?.into_any());
    }
    let note = provisional_note(&report)?;
    if !note.is_empty() {
        dsr_parts.push(PyString::new(py, &note).into_any());
    }
    // The three pool-PBO states decide which line shape renders.
    let pbo_fragment: String = if !report.contains("pool_pbo")? {
        "pool PBO not recorded (pre-sidecar report)".to_string()
    } else {
        let pbo = report.get_item("pool_pbo")?;
        if pbo.is_none() {
            "pool PBO skipped (pool < 2 candidates / short series / pool empty at #5)".to_string()
        } else {
            let coerced = py.import("builtins")?.getattr("float")?.call1((pbo,))?;
            format!("pool PBO={}", py_fixed(coerced.extract::<f64>()?, 4, false))
        }
    };
    let f = Frags::new(py);
    f.lit("  ")?;
    f.frag(&label_instrument.into_any())?;
    f.lit(" ")?;
    f.frag(&label_tf.into_any())?;
    f.lit("  ")?;
    f.owned(&pbo_fragment)?;
    f.lit("  in=")?;
    f.frag(&n_input.into_any())?;
    f.lit(" survived=")?;
    f.frag(&n_survived.into_any())?;
    f.lit("  ")?;
    for part in &dsr_parts {
        f.frag(part)?;
    }
    f.join()
}

/// `f"{header}\n  UNREADABLE: {reason}"` with a fragment reason.
fn unreadable<'py>(
    py: Python<'py>,
    header: &Bound<'py, PyAny>,
    reason: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let f = Frags::new(py);
    f.obj(header)?;
    f.lit("\n  UNREADABLE: ")?;
    f.obj(reason)?;
    f.join()
}

/// `f"{header}\n  UNREADABLE: {reason}"` with a Rust-composed reason.
fn unreadable_lit<'py>(
    py: Python<'py>,
    header: &Bound<'py, PyAny>,
    reason: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let f = Frags::new(py);
    f.obj(header)?;
    f.lit("\n  UNREADABLE: ")?;
    f.owned(reason)?;
    f.join()
}

/// `render_cascade_sidecar`'s render law over ALREADY-READ *content* (the
/// path probe + file read + the path-dependent OSError arm stay shim-side).
///
/// `json.loads` is CPython's (the raw `JSONDecodeError` str is the frozen
/// `UNREADABLE:` payload; the `(OSError, ValueError)` catch set is
/// mirrored verbatim, non-members propagate raw); the version gate is the
/// Python rich-compare `!=` (the `version: true` bool==int pun LOADS; a
/// refusal interpolates the CPython `repr`); a group-level `ValueError`
/// (shape OR float coercion) funnels into `UNREADABLE: {exc}`.
pub fn render_sidecar_content<'py>(
    py: Python<'py>,
    header: &Bound<'py, PyAny>,
    content: &Bound<'py, PyAny>,
    report_version: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let loads = py.import("json")?.getattr("loads")?;
    let payload = match loads.call1((content,)) {
        Ok(payload) => payload,
        Err(err)
            if err.is_instance_of::<PyOSError>(py) || err.is_instance_of::<PyValueError>(py) =>
        {
            return unreadable(py, header, &err.value(py).str()?.into_any());
        }
        Err(err) => return Err(err),
    };
    // `not isinstance(payload, dict) or not isinstance(payload.get("groups"), list)`
    // — short-circuit preserved: a non-dict payload never sees `.get`.
    let shape_ok = payload.is_instance_of::<PyDict>()
        && mapping_get(&payload, "groups")?.is_instance_of::<PyList>();
    if !shape_ok {
        return unreadable_lit(py, header, "not a cascade report object");
    }
    let version = mapping_get(&payload, "version")?;
    if version
        .rich_compare(report_version, CompareOp::Ne)?
        .is_truthy()?
    {
        return unreadable_lit(
            py,
            header,
            &format!("unsupported report version {}", capture_repr(&version)),
        );
    }
    let mut groups: Vec<Bound<'py, PyAny>> = Vec::new();
    for group_entry in payload.get_item("groups")?.try_iter()? {
        match render_sidecar_group(py, &group_entry?) {
            Ok(line) => groups.push(line),
            Err(err) if err.is_instance_of::<PyValueError>(py) => {
                return unreadable(py, header, &err.value(py).str()?.into_any());
            }
            Err(err) => return Err(err),
        }
    }
    if groups.is_empty() {
        let f = Frags::new(py);
        f.obj(header)?;
        f.lit("\n  (no pooled slices)")?;
        return f.join();
    }
    // "\n".join([header, *groups])
    let parts = PyList::empty(py);
    parts.append(header.str()?)?;
    for line in &groups {
        parts.append(line)?;
    }
    PyString::new(py, "\n").call_method1("join", (parts,))
}

/// `results_show_report`'s text-assembly law (the orchestration — top
/// check, sort resolve, discovery, merge, top_k, origins build, sidecar
/// file read — is the shim's; the lengths arrive pre-computed).
#[allow(clippy::too_many_arguments)]
pub fn show_render<'py>(
    py: Python<'py>,
    root: &Bound<'py, PyAny>,
    files_n: usize,
    finalists_n: usize,
    sort_by: &Bound<'py, PyAny>,
    showing_n: usize,
    leaders: &Bound<'py, PyAny>,
    origins: Option<&Bound<'py, PyAny>>,
    sidecar: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let provenance_note = if origins.is_none() {
        NOTE_BLANKET
    } else {
        NOTE_MARKERS
    };
    let leaderboard = render_leaderboard(py, leaders, origins)?;
    let f = Frags::new(py);
    f.lit("BruteX results — ")?;
    f.obj(root)?;
    f.owned(&format!("\n  files={files_n}  finalists={finalists_n}  "))?;
    f.lit("sort=")?;
    f.obj(sort_by)?;
    f.owned(&format!("  showing top {showing_n}\n"))?;
    f.lit(provenance_note)?;
    f.lit("\n\n")?;
    f.frag(&leaderboard)?;
    if sidecar.is_truthy()? {
        f.lit("\n\n")?;
        f.obj(sidecar)?;
    }
    f.join()
}

#[cfg(test)]
mod tests {
    use super::{format_inr_paisa, py_fixed};

    /// The frozen `_format_deflated` `+.4f` cases (CPython-verified).
    #[test]
    fn py_fixed_plus_4dp_matches_cpython() {
        assert_eq!(py_fixed(0.0, 4, true), "+0.0000");
        assert_eq!(py_fixed(-0.0, 4, true), "-0.0000");
        assert_eq!(py_fixed(0.1, 4, true), "+0.1000");
        assert_eq!(py_fixed(1e-7, 4, true), "+0.0000"); // display underflow
        assert_eq!(py_fixed(-1.2345, 4, true), "-1.2345");
        assert_eq!(py_fixed(2.00005, 4, true), "+2.0000");
        assert_eq!(py_fixed(1e18, 4, true), "+1000000000000000000.0000");
        assert_eq!(py_fixed(0.6571428571428571, 4, false), "0.6571");
    }

    /// Exact binary ties round half-to-even in BOTH CPython and Rust.
    #[test]
    fn py_fixed_exact_ties_round_half_even() {
        assert_eq!(py_fixed(0.15625, 4, true), "+0.1562");
        assert_eq!(py_fixed(0.09375, 4, false), "0.0938");
        assert_eq!(py_fixed(0.25, 1, false), "0.2");
        assert_eq!(py_fixed(0.75, 1, false), "0.8");
    }

    /// The win_rate MULTIPLY law feeds `.1f` (f64 product, then format).
    #[test]
    fn py_fixed_1dp_win_rate_products() {
        assert_eq!(py_fixed(0.335 * 100.0, 1, false), "33.5");
        assert_eq!(py_fixed(0.99999 * 100.0, 1, false), "100.0");
        assert_eq!(py_fixed(0.0 * 100.0, 1, false), "0.0");
        assert_eq!(py_fixed(1.0 * 100.0, 1, false), "100.0");
    }

    /// Decision (iv): non-finite `float(pbo)` values render CPython's
    /// spellings — `inf` (Rust native) and the special-cased `nan`
    /// (CPython ignores the NaN sign bit; the sign flag still applies).
    #[test]
    fn py_fixed_non_finite_matches_cpython() {
        assert_eq!(py_fixed(f64::INFINITY, 4, false), "inf");
        assert_eq!(py_fixed(f64::NEG_INFINITY, 4, false), "-inf");
        assert_eq!(py_fixed(f64::INFINITY, 4, true), "+inf");
        assert_eq!(py_fixed(f64::NAN, 4, false), "nan");
        assert_eq!(py_fixed(f64::NAN, 4, true), "+nan");
        assert_eq!(py_fixed(-f64::NAN, 4, false), "nan");
        assert_eq!(py_fixed(-f64::NAN, 3, true), "+nan");
    }

    /// The frozen money edges — exact integer grouping, never f64.
    #[test]
    fn format_inr_matches_frozen_money_edges() {
        assert_eq!(format_inr_paisa(-1), "₹-0.01");
        assert_eq!(format_inr_paisa(0), "₹0.00");
        assert_eq!(format_inr_paisa(5), "₹0.05");
        assert_eq!(format_inr_paisa(1_000), "₹10.00");
        assert_eq!(format_inr_paisa(123_456_789), "₹1,234,567.89");
        assert_eq!(format_inr_paisa(-12_345_678_901), "₹-123,456,789.01");
        // Beyond 2**53 — an f64 round-trip would corrupt the last digits.
        assert_eq!(
            format_inr_paisa(9_007_199_254_740_993),
            "₹90,071,992,547,409.93"
        );
        assert_eq!(
            format_inr_paisa(4_611_686_018_427_387_904), // 2**62
            "₹46,116,860,184,273,879.04"
        );
    }

    /// i64::MIN survives the unsigned_abs widening (CPython-verified:
    /// `format_inr(-2**63)` → `₹-92,233,720,368,547,758.08`).
    #[test]
    fn format_inr_i64_min_exact() {
        assert_eq!(format_inr_paisa(i64::MIN), "₹-92,233,720,368,547,758.08");
    }
}
