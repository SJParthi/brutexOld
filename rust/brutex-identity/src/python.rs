//! PyO3 binding (feature `python`) — the FFI edge of `brutex_identity`.
//!
//! Thin mapping only: every LAW lives in the pure-core modules
//! ([`crate::store_law`], [`crate::ledger`], [`crate::parquet_codec`],
//! [`crate::snapshot`]); this module extracts/builds Python objects and
//! raises `ValueError` with the exact retired-Python message bytes (the
//! Python wrappers re-raise their own domain error classes).
//!
//! PANIC POSTURE (honest — the two independent refuter rounds of
//! DEC-RUST-RUNID-STORES-001): the shipped wheel originally built with
//! `panic = "abort"`, under which pyo3's unwound-panic →
//! `pyo3_runtime.PanicException` conversion NEVER runs — a panic anywhere
//! in the kernel was an uncatchable process SIGABRT (the earlier "total
//! functions … PanicException" claim here was an overclaim, reproduced
//! twice: an unchecked utcoffset fold overflow, and parquet-rs panicking on
//! corrupt ledger bytes). The posture now: `panic = "abort"` is REMOVED
//! (Cargo.toml records why), so a residual panic unwinds into
//! `PanicException` — but that is a BaseException outside every caller's
//! typed degrade set, so the boundary never RELIES on it: the one
//! hostile-reachable arithmetic fold is checked-arithmetic total
//! ([`isoformat_of`] → typed `ValueError` on overflow), and the one
//! untrusted-bytes decode runs under the pure core's `catch_unwind`
//! boundary ([`crate::parquet_codec::read_ledger_parquet`] → the plain
//! error path → typed `ValueError`).

use crate::ledger::{LedgerRow, RowKey};
use crate::store_law::{AppendDecision, RankKey, ResultRowFfi};
use crate::{ledger, parquet_codec, snapshot, store_law};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::wrap_pyfunction;
use std::collections::HashMap;
use std::collections::HashSet;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

// ── bxsnap2 snapshot hasher ────────────────────────────────────────────────

/// One streamed `bxsnap2` digest (`data_snapshot_hash`'s computation).
#[pyclass]
struct SnapshotHasher {
    inner: snapshot::SnapshotHasher,
}

#[pymethods]
impl SnapshotHasher {
    #[new]
    fn new(exchange: &str, segment: &str, start: &str, end: &str) -> Self {
        SnapshotHasher {
            inner: snapshot::SnapshotHasher::new(exchange, segment, start, end),
        }
    }

    /// Fold one slice header (`\n{symbol}|{interval}|{bar_count}`).
    fn add_slice(&mut self, symbol: &str, interval: &str, bar_count: u64) {
        self.inner.add_slice(symbol, interval, bar_count);
    }

    /// Fold one bar line. `ts` is the bar's `datetime` (aware with a
    /// whole-second UTC offset, or naive); prices arrive as their exact
    /// `str(Decimal)` texts; a `None` open-interest folds as `"None"`.
    #[allow(clippy::too_many_arguments)]
    fn add_bar(
        &mut self,
        ts: &Bound<'_, PyAny>,
        open: &str,
        high: &str,
        low: &str,
        close: &str,
        volume: i64,
        open_interest: Option<i64>,
    ) -> PyResult<()> {
        let iso = isoformat_of(ts)?;
        self.inner
            .add_bar_line(&iso, open, high, low, close, volume, open_interest);
        Ok(())
    }

    /// Finalise to the 64-char lowercase BLAKE3 hex digest.
    fn hexdigest(&self) -> String {
        self.inner.hexdigest()
    }
}

/// Render a Python datetime exactly as CPython `isoformat()` does: the
/// WALL-CLOCK attributes verbatim + the `utcoffset()` suffix — exact for ANY
/// tzinfo (fixed-offset, ZoneInfo, custom), never a lossy float epoch. A
/// sub-second UTC offset is rejected (CandleBar timestamps never carry one;
/// rendering it would need the `±HH:MM:SS.ffffff` isoformat tail — refused
/// loudly rather than silently truncated).
fn isoformat_of(ts: &Bound<'_, PyAny>) -> PyResult<String> {
    let bad_shape = || {
        PyValueError::new_err(
            "data_snapshot_hash: bar timestamp must be a datetime (tz-aware with a \
             whole-second UTC offset, or naive)",
        )
    };
    let attr = |name: &str| -> PyResult<i64> {
        ts.getattr(name)
            .and_then(|v| v.extract::<i64>())
            .map_err(|_| bad_shape())
    };
    let year = attr("year")?;
    let month = attr("month")?;
    let day = attr("day")?;
    let hour = attr("hour")?;
    let minute = attr("minute")?;
    let second = attr("second")?;
    let microsecond = attr("microsecond")?;
    let offset = ts.call_method0("utcoffset").map_err(|_| bad_shape())?;
    let offset_seconds: Option<i32> = if offset.is_none() {
        None
    } else {
        let micro_offset = offset
            .getattr("microseconds")
            .and_then(|v| v.extract::<i64>())
            .map_err(|_| bad_shape())?;
        if micro_offset != 0 {
            return Err(bad_shape());
        }
        let days = offset
            .getattr("days")
            .and_then(|v| v.extract::<i64>())
            .map_err(|_| bad_shape())?;
        let seconds = offset
            .getattr("seconds")
            .and_then(|v| v.extract::<i64>())
            .map_err(|_| bad_shape())?;
        // Checked fold (independent refuter round of DEC-RUST-RUNID-STORES-001):
        // a hostile object's `.days`/`.seconds` are arbitrary i64s, and with
        // `overflow-checks = true` an unchecked `days * 86_400 + seconds`
        // PANICS on e.g. `days = 1 << 60` (under the retired
        // `panic = "abort"` profile that was a process SIGABRT — reproduced,
        // exit 134). Overflow is the SAME typed bad-shape ValueError as
        // every other malformed timestamp; real datetime offsets
        // (days ∈ {−1, 0}) never take this arm.
        let total = days
            .checked_mul(86_400)
            .and_then(|x| x.checked_add(seconds))
            .ok_or_else(bad_shape)?;
        Some(i32::try_from(total).map_err(|_| bad_shape())?)
    };
    Ok(snapshot::format_py_isoformat(
        i32::try_from(year).map_err(|_| bad_shape())?,
        u32::try_from(month).map_err(|_| bad_shape())?,
        u32::try_from(day).map_err(|_| bad_shape())?,
        u32::try_from(hour).map_err(|_| bad_shape())?,
        u32::try_from(minute).map_err(|_| bad_shape())?,
        u32::try_from(second).map_err(|_| bad_shape())?,
        u32::try_from(microsecond).map_err(|_| bad_shape())?,
        offset_seconds,
    ))
}

// ── results-store laws ─────────────────────────────────────────────────────

/// Extract one marshalled `ResultRow` 27-tuple (dataclass field order,
/// `condition_names` last; prices as their `str(Decimal)` texts).
fn extract_result_row(obj: &Bound<'_, PyAny>) -> PyResult<ResultRowFfi> {
    let t = obj
        .cast::<PyTuple>()
        .map_err(|_| PyValueError::new_err("result row marshalling: expected a 27-tuple"))?;
    if t.len() != 27 {
        return Err(PyValueError::new_err(format!(
            "result row marshalling: expected a 27-tuple, got {} fields",
            t.len()
        )));
    }
    Ok(ResultRowFfi {
        run_id: t.get_item(0)?.extract()?,
        instrument: t.get_item(1)?.extract()?,
        tf: t.get_item(2)?.extract()?,
        direction: t.get_item(3)?.extract()?,
        mode: t.get_item(4)?.extract()?,
        mask_bits: t.get_item(5)?.extract()?,
        net_pnl_paisa: t.get_item(6)?.extract()?,
        gross_profit_paisa: t.get_item(7)?.extract()?,
        gross_loss_paisa: t.get_item(8)?.extract()?,
        max_drawdown_paisa: t.get_item(9)?.extract()?,
        expectancy_paisa: t.get_item(10)?.extract()?,
        total_trades: t.get_item(11)?.extract()?,
        winning_trades: t.get_item(12)?.extract()?,
        losing_trades: t.get_item(13)?.extract()?,
        sharpe_ratio: t.get_item(14)?.extract()?,
        sortino_ratio: t.get_item(15)?.extract()?,
        calmar_ratio: t.get_item(16)?.extract()?,
        profit_factor: t.get_item(17)?.extract()?,
        win_rate: t.get_item(18)?.extract()?,
        max_drawdown_pct: t.get_item(19)?.extract()?,
        avg_entry_price: t.get_item(20)?.extract()?,
        avg_exit_price: t.get_item(21)?.extract()?,
        deflated_sharpe: t.get_item(22)?.extract()?,
        vix_mean: t.get_item(23)?.extract()?,
        vix_min: t.get_item(24)?.extract()?,
        vix_max: t.get_item(25)?.extract()?,
        condition_names: t.get_item(26)?.extract()?,
    })
}

/// Decide one `ResultsStore.append`: returns
/// `(kind, differing_fields, merged_vix, extra_names)` where `kind` ∈
/// {"insert", "noop", "backfill", "plain_conflict", "gate_conflict",
/// "vix_conflict"}; `merged_vix` is the union trio on "backfill";
/// `extra_names` carries the backfilled names ("backfill") or the
/// conflicting names ("vix_conflict").
#[pyfunction]
#[pyo3(signature = (existing, incoming))]
#[allow(clippy::type_complexity)]
fn classify_result_append(
    existing: Option<&Bound<'_, PyAny>>,
    incoming: &Bound<'_, PyAny>,
) -> PyResult<(
    &'static str,
    Vec<String>,
    Option<(Option<f64>, Option<f64>, Option<f64>)>,
    Vec<String>,
)> {
    let existing_row = existing.map(extract_result_row).transpose()?;
    let incoming_row = extract_result_row(incoming)?;
    Ok(
        match store_law::classify_append(existing_row.as_ref(), &incoming_row) {
            AppendDecision::Insert => ("insert", vec![], None, vec![]),
            AppendDecision::Noop => ("noop", vec![], None, vec![]),
            AppendDecision::Backfill { merged, backfilled } => (
                "backfill",
                vec![],
                Some((merged[0], merged[1], merged[2])),
                backfilled,
            ),
            AppendDecision::PlainConflict { differing } => {
                ("plain_conflict", differing, None, vec![])
            }
            AppendDecision::GateConflict { differing } => {
                ("gate_conflict", differing, None, vec![])
            }
            AppendDecision::VixConflict {
                differing,
                conflicting,
            } => ("vix_conflict", differing, None, conflicting),
        },
    )
}

/// `_validate_run_id`'s law: `None` = valid, else the exact error message.
#[pyfunction]
fn validate_run_id_error(run_id: &str, expected_len: usize) -> Option<String> {
    store_law::validate_run_id_error(run_id, expected_len)
}

/// Split a u128 mask into its two signed-Int64 Parquet limbs `(lo, hi)`.
#[pyfunction]
fn mask_to_limbs(mask: u128) -> (i64, i64) {
    store_law::mask_to_limbs(mask)
}

/// Reassemble a u128 mask from its signed limb columns.
#[pyfunction]
fn mask_from_limbs(lo_signed: i64, hi_signed: i64) -> u128 {
    store_law::mask_from_limbs(lo_signed, hi_signed)
}

/// The `top_k` ordering law: indices of the top `k` rows ranked by `keys`
/// (each an exact int or a float), ties by `run_id` ascending.
#[pyfunction]
fn rank_top_k(
    run_ids: Vec<String>,
    keys: &Bound<'_, PyAny>,
    k: usize,
    descending: bool,
) -> PyResult<Vec<usize>> {
    let mut rank_keys: Vec<RankKey> = Vec::new();
    for item in keys.try_iter()? {
        let item = item?;
        if item.is_instance_of::<PyInt>() && !item.is_instance_of::<pyo3::types::PyBool>() {
            rank_keys.push(RankKey::Int(item.extract()?));
        } else if item.is_instance_of::<PyFloat>() {
            rank_keys.push(RankKey::Float(item.extract()?));
        } else {
            return Err(PyValueError::new_err(
                "rank_top_k: keys must be int or float",
            ));
        }
    }
    if rank_keys.len() != run_ids.len() {
        return Err(PyValueError::new_err(
            "rank_top_k: run_ids and keys must have equal length",
        ));
    }
    Ok(store_law::rank_top_k(&run_ids, &rank_keys, k, descending))
}

// ── window-ledger store laws + codec ───────────────────────────────────────

/// Extract one 17-field ledger row tuple (`WindowLedger.to_rows` order).
fn extract_ledger_row(obj: &Bound<'_, PyAny>) -> PyResult<LedgerRow> {
    let t = obj
        .cast::<PyTuple>()
        .map_err(|_| PyValueError::new_err("ledger row marshalling: expected a 17-tuple"))?;
    if t.len() != 17 {
        return Err(PyValueError::new_err(format!(
            "ledger row marshalling: expected a 17-tuple, got {} fields",
            t.len()
        )));
    }
    Ok(LedgerRow {
        market: t.get_item(0)?.extract()?,
        security_id: t.get_item(1)?.extract()?,
        interval: t.get_item(2)?.extract()?,
        window_start: t.get_item(3)?.extract()?,
        window_end: t.get_item(4)?.extract()?,
        status: t.get_item(5)?.extract()?,
        bars: t.get_item(6)?.extract()?,
        attempts: t.get_item(7)?.extract()?,
        last_error: t.get_item(8)?.extract()?,
        first_seen_at: t.get_item(9)?.extract()?,
        last_attempt_at: t.get_item(10)?.extract()?,
        empty_streak: t.get_item(11)?.extract()?,
        in_pull_fetches: t.get_item(12)?.extract()?,
        bars_net_new: t.get_item(13)?.extract()?,
        empty_retries: t.get_item(14)?.extract()?,
        transient_retries: t.get_item(15)?.extract()?,
        // Opaque passthrough (str | None) — the 17th, appended last.
        streak_provenance: t.get_item(16)?.extract()?,
    })
}

/// Extract a sequence of ledger row tuples.
fn extract_ledger_rows(rows: &Bound<'_, PyAny>) -> PyResult<Vec<LedgerRow>> {
    let mut out: Vec<LedgerRow> = Vec::new();
    for item in rows.try_iter()? {
        out.push(extract_ledger_row(&item?)?);
    }
    Ok(out)
}

/// The per-load string-intern pool (DEC-LEDGER-LOAD-SPACE-V1-001 semantics:
/// equal low-cardinality cells share ONE `str` object across every file of
/// one load; the pool dies with the load — never process-lifetime).
#[pyclass]
#[derive(Default)]
struct LedgerStringPool {
    map: HashMap<String, Py<PyString>>,
}

#[pymethods]
impl LedgerStringPool {
    #[new]
    fn new() -> Self {
        LedgerStringPool::default()
    }

    /// Number of distinct interned strings (observability/tests).
    fn __len__(&self) -> usize {
        self.map.len()
    }
}

impl LedgerStringPool {
    fn intern(&mut self, py: Python<'_>, text: &str) -> Py<PyString> {
        if let Some(cached) = self.map.get(text) {
            return cached.clone_ref(py);
        }
        let obj = PyString::new(py, text).unbind();
        self.map.insert(text.to_string(), obj.clone_ref(py));
        obj
    }
}

/// Build one string cell, interning iff `interned` and a pool is supplied.
fn string_cell(
    py: Python<'_>,
    pool: &mut Option<&mut LedgerStringPool>,
    interned: bool,
    text: &str,
) -> Py<PyAny> {
    if interned && let Some(p) = pool.as_deref_mut() {
        return p.intern(py, text).into_any();
    }
    PyString::new(py, text).unbind().into_any()
}

/// Build the 17-tuple for one resolved ledger row. The interned positions
/// mirror `_INTERNED_COLUMNS`: market, security_id, interval, window_start,
/// window_end, status, last_error (timestamps deliberately NOT interned).
fn ledger_row_to_tuple<'py>(
    py: Python<'py>,
    row: &LedgerRow,
    mut pool: Option<&mut LedgerStringPool>,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut items: Vec<Py<PyAny>> = Vec::with_capacity(17);
    items.push(string_cell(py, &mut pool, true, &row.market));
    items.push(string_cell(py, &mut pool, true, &row.security_id));
    items.push(string_cell(py, &mut pool, true, &row.interval));
    items.push(string_cell(py, &mut pool, true, &row.window_start));
    items.push(string_cell(py, &mut pool, true, &row.window_end));
    items.push(string_cell(py, &mut pool, true, &row.status));
    items.push(row.bars.into_pyobject(py)?.unbind().into_any());
    items.push(row.attempts.into_pyobject(py)?.unbind().into_any());
    items.push(match &row.last_error {
        None => py.None(),
        Some(text) => string_cell(py, &mut pool, true, text),
    });
    items.push(string_cell(py, &mut pool, false, &row.first_seen_at));
    items.push(match &row.last_attempt_at {
        None => py.None(),
        Some(text) => string_cell(py, &mut pool, false, text),
    });
    items.push(row.empty_streak.into_pyobject(py)?.unbind().into_any());
    items.push(row.in_pull_fetches.into_pyobject(py)?.unbind().into_any());
    for cell in [row.bars_net_new, row.empty_retries, row.transient_retries] {
        items.push(match cell {
            None => py.None(),
            Some(v) => v.into_pyobject(py)?.unbind().into_any(),
        });
    }
    // The 17th — opaque str | None history (high-cardinality → NOT interned).
    items.push(match &row.streak_provenance {
        None => py.None(),
        Some(text) => string_cell(py, &mut pool, false, text),
    });
    PyTuple::new(py, items)
}

/// Best-effort `empty_streak` for a LEGACY row predating the column
/// (`_derive_empty_streak`'s law; `skipped_streak` = the caller's
/// EMPTY_TERMINAL_THRESHOLD constant).
#[pyfunction]
fn derive_empty_streak(status: &str, attempts: i64, skipped_streak: i64) -> i64 {
    ledger::derive_empty_streak(status, attempts, skipped_streak)
}

/// Best-effort `in_pull_fetches` for a LEGACY row predating the column
/// (`_derive_in_pull_fetches`'s law).
#[pyfunction]
fn derive_in_pull_fetches(status: &str) -> i64 {
    ledger::derive_in_pull_fetches(status)
}

/// The merge-on-save conflict law (`merge_ledger_rows`'s computation).
#[pyfunction]
fn merge_ledger_rows<'py>(
    py: Python<'py>,
    memory_rows: &Bound<'py, PyAny>,
    disk_rows: &Bound<'py, PyAny>,
    pruned_keys: HashSet<RowKey>,
    reopened_keys: HashSet<RowKey>,
    degraded_keys: HashSet<RowKey>,
) -> PyResult<Bound<'py, PyList>> {
    let merged = ledger::merge_ledger_rows(
        extract_ledger_rows(memory_rows)?,
        extract_ledger_rows(disk_rows)?,
        &pruned_keys,
        &reopened_keys,
        &degraded_keys,
    );
    let mut out: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(merged.len());
    for row in &merged {
        out.push(ledger_row_to_tuple(py, row, None)?);
    }
    PyList::new(py, out)
}

/// Render ledger rows to canonical Parquet bytes (the #1917 option-(iii)
/// encoder — order-preserving, deterministic, zstd-3).
#[pyfunction]
fn render_ledger_parquet<'py>(
    py: Python<'py>,
    rows: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let rows = extract_ledger_rows(rows)?;
    let rendered = parquet_codec::write_ledger_parquet(&rows).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &rendered))
}

/// Decode one ledger-shaped Parquet byte buffer into the resolved 17-tuples
/// (legacy optional columns derived per `_rows_to_records`; `skipped_streak`
/// = the EMPTY_TERMINAL_THRESHOLD constant). A malformed/wrong-shape buffer
/// raises `ValueError` (a `_LEDGER_READ_ERRORS` member — the caller's
/// degrade contract). `pool=None` interns within THIS call only (the retired
/// `memo=None → fresh memo` per-file law); a threaded pool shares string
/// objects across every file of one load.
#[pyfunction]
#[pyo3(signature = (data, skipped_streak, pool=None))]
fn read_ledger_rows<'py>(
    py: Python<'py>,
    data: &[u8],
    skipped_streak: i64,
    pool: Option<&Bound<'py, LedgerStringPool>>,
) -> PyResult<Bound<'py, PyList>> {
    let raw = parquet_codec::read_ledger_parquet(bytes::Bytes::copy_from_slice(data))
        .map_err(PyValueError::new_err)?;
    let mut local = LedgerStringPool::default();
    let mut borrowed = pool.map(|p| p.borrow_mut());
    let interner: &mut LedgerStringPool = match borrowed.as_deref_mut() {
        Some(shared) => shared,
        None => &mut local,
    };
    let mut out: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(raw.len());
    for raw_row in raw {
        let resolved = ledger::coerce_raw_row(raw_row, skipped_streak);
        out.push(ledger_row_to_tuple(py, &resolved, Some(interner))?);
    }
    PyList::new(py, out)
}

// ── module ─────────────────────────────────────────────────────────────────

#[pymodule]
fn _brutex_identity(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention) + the plain-named twin
    // for the shim's `_EXPECTED_COMPILED_SYMBOLS` handshake surface.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    // The fixed ledger column vocabulary — the ONE schema source the
    // window_ledger_store consumers (and their tests) read directly.
    m.add("LEDGER_COLUMNS", ledger::LEDGER_COLUMNS)?;
    m.add("LEDGER_CORE_COLUMNS", ledger::LEDGER_CORE_COLUMNS)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(classify_result_append, m)?)?;
    m.add_function(wrap_pyfunction!(validate_run_id_error, m)?)?;
    m.add_function(wrap_pyfunction!(mask_to_limbs, m)?)?;
    m.add_function(wrap_pyfunction!(mask_from_limbs, m)?)?;
    m.add_function(wrap_pyfunction!(rank_top_k, m)?)?;
    m.add_function(wrap_pyfunction!(derive_empty_streak, m)?)?;
    m.add_function(wrap_pyfunction!(derive_in_pull_fetches, m)?)?;
    m.add_function(wrap_pyfunction!(merge_ledger_rows, m)?)?;
    m.add_function(wrap_pyfunction!(render_ledger_parquet, m)?)?;
    m.add_function(wrap_pyfunction!(read_ledger_rows, m)?)?;
    m.add_class::<SnapshotHasher>()?;
    m.add_class::<LedgerStringPool>()?;
    Ok(())
}
