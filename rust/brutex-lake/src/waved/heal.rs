//! # Wave-D W-2 — the `lake_heal` mutation core (`BRUTEX_LAKE_WAVED_RUST`)
//!
//! DEC-RUST-LAKE-WAVED-001, design §B2/§C-4/§C-5 (`scratchpad/waved_design.md`):
//! the batch heal driver + the single-file heal algorithms move here, rendering
//! through the EXISTING byte-locked render cores (`render_parquet_from_ipc_impl`
//! bars profile / `render_parquet_defaults_from_ipc_impl` options-defaults
//! profile) and locking through the PR-4 flock primitives (`locks.rs`) under
//! the SAME `.locks/<sha256(realpath(dest))>.lock` sidecar path law. Python
//! keeps: `heal_findings`' policy shell (PriceSanityRows filter, observe mode,
//! mass rail, quarantine-symlink refusal, stamp/serial minting, ordering), the
//! sidecars, the recall, and every structlog/audit/catalog/stats side effect —
//! this module returns/streams STRUCTURED OUTCOMES through the ONE `on_event`
//! channel and the shell replays them (Wave-D house laws i-iii).
//!
//! ## The ENGAGED SET (W-2 + W-2b — deviations D-7/D-22, `waved_deviations.md`)
//!
//! Natively healed: `IntraFileDup` (bars keep-first dedup rewrite),
//! `OptionIntraFileDup` (options `(side, timestamp)` dedup rewrite, both
//! containment rails enforced), `SchemaAlienFile` (whole-file quarantine
//! move, both trees), and — since W-2b (`waved/heal_multi.rs`) — the three
//! multi-file algorithms: `ForeignMonthRows` (bars foreign-month
//! reassignment, copy-forward-FIRST/remove-SECOND), `OptionForeignMonthRows`
//! (options reassignment through the canonical-sibling keep-first merge) and
//! `LegacyKeyDrift` (the drifted-dir keep-first merge + quarantine move).
//! Plus the `lake_content_token` walk (§C-5). EVERY production finding kind
//! now engages. Since the W-6a endgame there is NO whole-batch degrade and
//! no Python core to degrade to: a finding on a file shape outside the
//! native envelope below — or, for the multi-file kinds, any shape whose
//! deleted Python heal would have produced an exception-MESSAGE-bearing
//! isolated error (an unreadable/symlinked source or destination, a
//! schema-incompatible merge sibling, an int()-ambiguous drifted dir name —
//! the D-22 posture) — is a PER-FINDING ISOLATED ERROR: counted, logged,
//! byte-untouched, the batch CONTINUES (see [`preflight_one`]).
//!
//! ## The native heal envelope (preflight-enforced, read-only)
//!
//! * bars files, single-file DEDUP ([`heal_bars_dup`]): the canonical
//!   7-column schema heals through the byte-locked `BarRow` render (every
//!   timestamp inside Python's `datetime` range, every price 2dp-snappable
//!   with `|paisa| < 2^53`, every snapped row satisfying the `CandleBar`
//!   validation laws). Since W-6b two further shapes heal through the
//!   frame-level PRESERVE arm ([`Envelope::Preserve`] —
//!   `heal_bars_dup_preserve`): the EXACT 17-column Greeks-overlay schema
//!   the enriched F&O writer mints ([`BARS_OVERLAY_COLUMNS`] — the
//!   production-reachable shape Greeks enrichment writes) and a canonical
//!   7-column file whose only envelope violation is FINITE prices beyond
//!   the 2dp snap band (`|paisa| >= 2^53`). The preserve arm is STRICTLY
//!   row-removing — keep-FIRST unique on `timestamp`, every kept row's
//!   stored values VERBATIM (never value-editing, the #1594 law) — and
//!   renders through the same PR-1 bars render profile. Non-finite prices,
//!   nulls in required columns, law-violating rows, out-of-range
//!   timestamps and every OTHER schema stay per-finding isolated errors.
//! * bars files, MULTI-FILE heals (`heal_multi.rs`): the canonical
//!   7-column snap envelope ONLY — a preserve-class file (overlay /
//!   big-price) in a foreign/drift reassignment is a per-finding isolated
//!   error ([`PRESERVE_MULTI_REASON`] — the recorded W-6b residual; the
//!   merge legs rebuild values through `BarRow`, which cannot preserve
//!   overlay columns).
//! * options files: `side` dtype `String`, `timestamp` dtype `Datetime`
//!   (any unit, aware or naive — naive routes as-if-UTC, the `utc_aware`
//!   law) with in-`datetime`-range values. Row payloads move VERBATIM
//!   (arbitrary sibling columns are fine — the defaults render profile
//!   handles any schema, PR-2).
//! * an UNREADABLE/vanished file does NOT degrade: both arms classify it as
//!   the same per-finding isolated error (counts equal; message text is a
//!   log-only surface the differential does not compare).
//!
//! ## Error law
//!
//! Per-finding faults are caught INSIDE the driver and counted (`errors` —
//! the `_PER_FINDING_HEAL_ERRORS` isolation twin). An `io::Error` whose errno
//! is `ENOSPC`/`EDQUOT` is FATAL: it propagates as a real `OSError(errno)` so
//! the shell's `lake_write_guard` maps it to `LakeWriteHalt("disk_full")`
//! exactly as today. A raising `on_event` callback propagates (the shell
//! wraps the best-effort kinds itself; a catalog error is real — today's
//! behavior). A Rust panic maps to `RuntimeError` at the FFI boundary — loud;
//! the batch falls to the documented idempotent Python re-run posture.

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use polars::prelude::*;
use pyo3::BoundObject;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sha2::{Digest, Sha256};

use super::read::{DATETIME_MAX_US, DATETIME_MIN_US, frame_to_ipc};
use crate::locks::try_lock_exclusive_impl;

/// `2^53` — the largest integer band where `paisa as f64` is exact; a
/// snapped price beyond it would diverge from Python's `float(Decimal)`.
const MAX_EXACT_PAISA: i64 = 1 << 53;

/// The canonical bars schema (name order = the writer's render order).
const BARS_COLUMNS: [&str; 7] = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
];

/// The Greeks-ENRICHED bars schema (`ParquetWriter._render_bytes`'s overlay
/// branch — the 7 base columns + `GREEKS_OVERLAY_FIELDS` in canonical order).
/// W-6b: files carrying EXACTLY this schema heal through the frame-level
/// preserve arm ([`Envelope::Preserve`]) instead of refusing.
const BARS_OVERLAY_COLUMNS: [&str; 17] = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
    "greeks_provenance_id",
];

/// The multi-file heals' isolated-error reason for a preserve-class bars
/// file (W-6b): only the SINGLE-FILE dedup arm preserves overlay/big-price
/// rows — the foreign/drift merge legs rebuild values through [`BarRow`]
/// and would drop overlay columns, so they refuse loudly instead.
pub(super) const PRESERVE_MULTI_REASON: &str = "preserve-class bars file (Greeks overlay schema \
     or beyond-2^53-paisa prices) — only the single-file dedup heal preserves it; \
     the multi-file reassignment/merge heals refuse rather than drop columns";

// ── error plumbing ───────────────────────────────────────────────────────────

/// One heal fault, classified per the Python isolation law.
pub(super) enum HealErr {
    /// The `_PER_FINDING_HEAL_ERRORS` twin — counted, batch continues.
    Isolated(String),
    /// An isolated refusal whose MESSAGE the crate reproduces byte-exactly
    /// from the Python arm (W-5 phase A — the crate OWNS the verdict, so the
    /// read-only preflight must NOT degrade the batch for it). Handled at
    /// the isolation loop exactly like [`HealErr::Isolated`]; the variant
    /// exists so the preflight probes can DISTINGUISH a crate-owned refusal
    /// (engage) from a message we cannot reproduce (degrade).
    Refuse(String),
    /// Disk-full class — propagates as `OSError(errno)` for the shell's
    /// `lake_write_guard` → `LakeWriteHalt` mapping.
    Fatal(std::io::Error),
    /// A raising `on_event` callback — propagates verbatim (shell-owned).
    Py(PyErr),
}

impl From<PyErr> for HealErr {
    fn from(err: PyErr) -> Self {
        HealErr::Py(err)
    }
}

impl From<PolarsError> for HealErr {
    fn from(err: PolarsError) -> Self {
        HealErr::Isolated(format!("polars: {err}"))
    }
}

/// Classify an `io::Error`: `ENOSPC`/`EDQUOT` are the pull-wide halt class
/// (the `lake_write_guard` law); everything else is a per-finding isolated
/// fault (`OSError` is a `parquet_read_skip_errors` member).
pub(super) fn classify_io(err: std::io::Error) -> HealErr {
    match err.raw_os_error() {
        Some(code) if code == libc::ENOSPC || code == libc::EDQUOT => HealErr::Fatal(err),
        _ => HealErr::Isolated(format!("io: {err}")),
    }
}

// ── small pure helpers ───────────────────────────────────────────────────────

/// sha256 hex digest (lowercase) — the `hashlib.sha256(...).hexdigest()` twin.
pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// `os.path.realpath` (non-strict) for the absolute, `..`-free paths the
/// healer handles: canonicalize the longest EXISTING ancestor, re-append the
/// non-existent tail verbatim. Identity on an already-canonical path.
fn realpath_nonstrict(path: &Path) -> PathBuf {
    if let Ok(resolved) = std::fs::canonicalize(path) {
        return resolved;
    }
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    let mut cursor = path.to_path_buf();
    loop {
        if let Ok(resolved) = std::fs::canonicalize(&cursor) {
            let mut out = resolved;
            for component in tail.iter().rev() {
                out.push(component);
            }
            return out;
        }
        match (cursor.file_name(), cursor.parent()) {
            (Some(name), Some(parent)) => {
                tail.push(name.to_os_string());
                cursor = parent.to_path_buf();
            }
            _ => return path.to_path_buf(),
        }
    }
}

/// The byte-exact Python refusal for a NAIVE-timestamp bars file (W-5
/// phase A): `_read_existing_bars` wraps the first row's `CandleBar`
/// tz-aware validation `ValueError` into
/// `WriterError(f"corrupt existing partition {dest}: {exc}")`. Pure
/// (pyo3-free) so the cargo suite can pin the bytes without libpython.
pub(super) fn naive_ts_refusal_message(path: &Path, first_us: i64) -> String {
    format!(
        "corrupt existing partition {}: CandleBar.timestamp must be \
         timezone-aware, got naive datetime {}",
        path.display(),
        super::py_naive_datetime_repr_us(first_us),
    )
}

/// The byte-exact Python refusal for a symlink-LOOP containment dest (W-5
/// phase A): Python ≤3.12 `Path.resolve(strict=False)` realpath leaves the
/// loop unresolved, the ensuing `stat()` ELOOP converts to
/// `RuntimeError(f"Symlink loop from {e.filename!r}")`, and
/// `_assert_options_containment` wraps it. *looped* is the
/// realpath-nonstrict output (the `e.filename` twin). Pure (pyo3-free).
pub(super) fn symlink_loop_refusal_message(dest: &Path, looped: &Path) -> String {
    format!(
        "cannot resolve {} for the options-containment check: \
         Symlink loop from {} — refusing to touch it",
        dest.display(),
        super::py_str_repr(&looped.to_string_lossy()),
    )
}

/// The cross-process lock sidecar path —
/// `<lake_root>/.locks/<sha256(realpath(dest))>.lock` (`partition_lock_path`).
fn lock_path(lake_root: &Path, dest: &Path) -> PathBuf {
    let real = realpath_nonstrict(dest);
    let digest = sha256_hex(real.to_string_lossy().as_bytes());
    lake_root.join(".locks").join(format!("{digest}.lock"))
}

/// Convert a physical datetime value to µs. `None` on ms overflow (degrade).
pub(super) fn to_us(value: i64, unit: TimeUnit) -> Option<i64> {
    match unit {
        TimeUnit::Microseconds => Some(value),
        TimeUnit::Milliseconds => value.checked_mul(1_000),
        TimeUnit::Nanoseconds => Some(value.div_euclid(1_000)),
    }
}

/// Snap one stored f64 price to the 2dp tick grid — the numeric twin of
/// `round_money(Decimal(str(x)))` / `_snap_price_expr` (`ROUND_HALF_UP`,
/// away from zero). Returns `(paisa, rendered f64)` where the rendered value
/// equals Python's `float(Decimal)` exactly (`paisa as f64 / 100.0` is the
/// correctly-rounded value of `paisa/100`, as is CPython's Decimal→float).
/// `None` = outside the parity envelope (non-finite / too many significant
/// digits / `|paisa| >= 2^53`) — the caller degrades, never guesses.
pub(crate) fn snap_2dp(value: f64) -> Option<(i64, f64)> {
    if !value.is_finite() {
        return None;
    }
    // Rust `Display` for f64 is the shortest round-trip decimal (never
    // e-notation) — the same NUMERIC value Python's `str(float)` yields.
    let repr = format!("{value}");
    let (negative, body) = match repr.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, repr.as_str()),
    };
    let (int_part, frac_part) = match body.split_once('.') {
        Some((i, f)) => (i, f),
        None => (body, ""),
    };
    let frac_len = frac_part.len() as i64;
    let mut digits: i128 = 0;
    for ch in int_part.chars().chain(frac_part.chars()) {
        let d = ch.to_digit(10)?;
        digits = digits.checked_mul(10)?.checked_add(i128::from(d))?;
    }
    let paisa_abs: i128 = if frac_len <= 2 {
        let mut scaled = digits;
        for _ in 0..(2 - frac_len) {
            scaled = scaled.checked_mul(10)?;
        }
        scaled
    } else {
        let shift = frac_len - 2;
        if shift >= 19 {
            // digits < 10^18 ⇒ value·100 < 10^18·10^-19 = 0.1 < 0.5 ⇒ 0.
            0
        } else {
            let mut divisor: i128 = 1;
            for _ in 0..shift {
                divisor = divisor.checked_mul(10)?;
            }
            let q = digits / divisor;
            let r = digits % divisor;
            q + i128::from(r * 2 >= divisor)
        }
    };
    if paisa_abs >= i128::from(MAX_EXACT_PAISA) {
        return None;
    }
    let paisa = if negative {
        -(paisa_abs as i64)
    } else {
        paisa_abs as i64
    };
    Some((paisa, paisa as f64 / 100.0))
}

// ── the event sink ───────────────────────────────────────────────────────────

/// The ONE `on_event(kind, payload_dict)` channel (design §C-4). A raising
/// callback propagates — the shell wraps the best-effort kinds itself.
pub(super) struct EventSink<'a, 'py> {
    pub(super) py: Python<'py>,
    pub(super) on_event: &'a Bound<'py, PyAny>,
}

impl<'py> EventSink<'_, 'py> {
    pub(super) fn dict(&self) -> Bound<'py, PyDict> {
        PyDict::new(self.py)
    }

    pub(super) fn emit(&self, kind: &str, payload: &Bound<'py, PyDict>) -> PyResult<()> {
        self.on_event.call1((kind, payload))?;
        Ok(())
    }

    pub(super) fn beat(&self) -> PyResult<()> {
        self.emit("beat", &self.dict())
    }

    pub(super) fn log(&self, event: &str, fields: &[(&str, Py<PyAny>)]) -> PyResult<()> {
        self.log_ex(event, None, &[], fields)
    }

    /// The full-control log emitter: an optional non-WARNING `level`
    /// (`"info"`/`"debug"` — the `_partition_log` twin) and the W-2b
    /// `repr_fields` wire (field NAMES the shell wraps `repr(str(v))` at
    /// the replay site — the #907 log-injection law stays Python-applied;
    /// `partition_repr` remains the W-2 back-compat single-field form).
    pub(super) fn log_ex(
        &self,
        event: &str,
        level: Option<&str>,
        repr_fields: &[&str],
        fields: &[(&str, Py<PyAny>)],
    ) -> PyResult<()> {
        let payload = self.dict();
        payload.set_item("event", event)?;
        if let Some(level) = level {
            payload.set_item("level", level)?;
        }
        if !repr_fields.is_empty() {
            payload.set_item("repr_fields", repr_fields.to_vec())?;
        }
        for (key, value) in fields {
            payload.set_item(*key, value)?;
        }
        self.emit("log", &payload)
    }

    pub(super) fn audit(
        &self,
        action: &str,
        subject: &str,
        payload: &Bound<'py, PyDict>,
    ) -> PyResult<()> {
        let wrapper = self.dict();
        wrapper.set_item("action", action)?;
        wrapper.set_item("subject", subject)?;
        wrapper.set_item("payload", payload)?;
        self.emit("audit", &wrapper)
    }
}

/// Convenience: a Python object from anything `IntoPyObject`.
pub(super) fn obj<'py, T: pyo3::IntoPyObject<'py>>(py: Python<'py>, value: T) -> Py<PyAny> {
    match value.into_pyobject(py) {
        Ok(bound) => bound.into_any().unbind(),
        Err(_) => py.None(),
    }
}

// ── locks + atomic write ─────────────────────────────────────────────────────

/// A held cross-process partition flock (released on drop — the fd close
/// releases the advisory lock, kernel-guaranteed).
pub(super) struct PartitionFlock {
    _file: std::fs::File,
}

/// Bounded exclusive flock on *dest*'s lock sidecar — the
/// `partition_write_lock` twin: 50 ms polls under the env-tunable deadline
/// (default 75 s), one liveness `beat` per contended poll, `lock_timeout`
/// (the `PartitionLockTimeout` isolation class) on expiry. Flock is
/// per-OPEN-FILE-DESCRIPTION, so this single layer also excludes
/// same-process Python writers (the #1785 G10 lesson; deviation D-8 —
/// the in-process `BoundedLockRegistry` layer is not separately taken).
pub(super) fn acquire_partition_flock(
    lake_root: &Path,
    dest: &Path,
    sink: &EventSink<'_, '_>,
) -> Result<PartitionFlock, HealErr> {
    let path = lock_path(lake_root, dest);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(classify_io)?;
    let timeout = partition_lock_timeout_seconds();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(timeout);
    loop {
        use std::os::fd::AsRawFd;
        match try_lock_exclusive_impl(file.as_raw_fd()) {
            Ok(true) => return Ok(PartitionFlock { _file: file }),
            Ok(false) => {
                sink.beat()?;
                if std::time::Instant::now() >= deadline {
                    return Err(HealErr::Isolated(format!(
                        "lock_timeout: could not acquire the partition lock within {timeout}s"
                    )));
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(err) => return Err(classify_io(err)),
        }
    }
}

/// `resolve_partition_lock_timeout_seconds` twin (tolerant env parse).
fn partition_lock_timeout_seconds() -> f64 {
    match std::env::var("BRUTEX_PARTITION_LOCK_TIMEOUT_S") {
        Ok(raw) => match raw.trim().parse::<f64>() {
            Ok(value) if value.is_finite() && value > 0.0 => value,
            _ => 75.0,
        },
        Err(_) => 75.0,
    }
}

/// The `atomic_write_via(dest, tmp.write_bytes)` twin: a same-dir
/// `.brutex-write-*.parquet.tmp` temp (mode 0600, the `mkstemp` default) →
/// write → `fsync` → atomic `rename`; the temp is removed on any error.
pub(super) fn atomic_write_bytes(dest: &Path, content: &[u8]) -> Result<(), HealErr> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let parent = dest
        .parent()
        .ok_or_else(|| HealErr::Isolated(format!("no parent dir for {}", dest.display())))?;
    let mut tmp: Option<PathBuf> = None;
    let result = (|| -> Result<(), HealErr> {
        let mut file = None;
        for attempt in 0..1_000u32 {
            let candidate = parent.join(format!(
                ".brutex-write-{}-{attempt}.parquet.tmp",
                std::process::id()
            ));
            match std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&candidate)
            {
                Ok(handle) => {
                    tmp = Some(candidate);
                    file = Some(handle);
                    break;
                }
                Err(err) if err.kind() == ErrorKind::AlreadyExists => continue,
                Err(err) => return Err(classify_io(err)),
            }
        }
        let (Some(mut handle), Some(tmp_path)) = (file, tmp.clone()) else {
            return Err(HealErr::Isolated("temp-name space exhausted".to_string()));
        };
        handle.write_all(content).map_err(classify_io)?;
        handle.sync_all().map_err(classify_io)?;
        drop(handle);
        std::fs::rename(&tmp_path, dest).map_err(classify_io)?;
        tmp = None; // consumed by the rename — nothing to clean
        Ok(())
    })();
    if let Some(tmp_path) = tmp {
        let _ = std::fs::remove_file(tmp_path);
    }
    result
}

// ── bars decode / render ─────────────────────────────────────────────────────

/// One decoded + 2dp-snapped bars row (the `CandleBar` value twin: the f64
/// fields are exactly Python's `float(Decimal)` renders).
#[derive(Clone)]
pub(super) struct BarRow {
    pub ts_us: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
    pub oi: Option<i64>,
}

/// A bars file's native-heal envelope verdict.
pub(super) enum Envelope {
    /// The canonical 7-column snap envelope — every heal arm engages.
    Ok,
    /// W-6b: a PRESERVE-class file — the exact 17-column Greeks-overlay
    /// schema, or a canonical file whose only violation is finite prices
    /// beyond the 2dp snap band. The single-file dedup heals it via the
    /// verbatim frame-level arm; the multi-file heals refuse
    /// ([`PRESERVE_MULTI_REASON`]).
    Preserve,
    /// Outside every native arm — a per-finding isolated error (the W-6a
    /// endgame posture; there is no Python core to degrade to).
    Degrade(String),
}

/// The pure schema-mode classifier (W-6b): `Some(false)` = the canonical
/// 7-column schema, `Some(true)` = the EXACT 17-column Greeks-overlay
/// schema (the frame-level preserve arm), `None` = alien (isolated error).
/// Name ORDER is part of the law — both writers render in canonical order.
fn bars_schema_mode(names: &[String]) -> Option<bool> {
    if names == BARS_COLUMNS {
        Some(false)
    } else if names == BARS_OVERLAY_COLUMNS {
        Some(true)
    } else {
        None
    }
}

/// Read + decode one canonical bars partition file: raw bytes, sha256, and
/// the physical rows snapped to the 2dp grid. `Err(Isolated)` = the same
/// unreadable/corrupt classification the Python arm makes (`WriterError`);
/// `Ok(Envelope::Degrade)` = readable but outside the parity envelope.
#[allow(clippy::type_complexity)]
pub(super) fn read_bars_file(
    path: &Path,
) -> Result<(Vec<u8>, String, Vec<BarRow>, Envelope), HealErr> {
    let bytes = std::fs::read(path).map_err(|err| match err.raw_os_error() {
        Some(code) if code == libc::ENOSPC || code == libc::EDQUOT => HealErr::Fatal(err),
        _ => HealErr::Isolated(format!("unreadable existing partition: {err}")),
    })?;
    let hash = sha256_hex(&bytes);
    let df = ParquetReader::new(std::io::Cursor::new(&bytes))
        .finish()
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
    let names: Vec<String> = df
        .get_column_names()
        .iter()
        .map(|n| n.to_string())
        .collect();
    let Some(overlay) = bars_schema_mode(&names) else {
        return Ok((
            bytes,
            hash,
            Vec::new(),
            Envelope::Degrade(format!("bars schema not canonical: {names:?}")),
        ));
    };
    let ts = df.column("timestamp")?;
    let DataType::Datetime(unit, tz) = ts.dtype() else {
        return Ok((
            bytes,
            hash,
            Vec::new(),
            Envelope::Degrade(format!(
                "bars timestamp dtype {:?} not Datetime(UTC)",
                ts.dtype()
            )),
        ));
    };
    let Some(tz) = tz else {
        // A tz-NAIVE column (W-5 phase A, `bars_naive_timestamp_routing`):
        // the Python arm's `_read_existing_bars` builds the FIRST physical
        // row's `CandleBar` and refuses with the tz-aware validation message
        // (`corrupt existing partition {path}: CandleBar.timestamp must be
        // timezone-aware, got naive datetime {ts!r}` — the timestamp check
        // runs FIRST in `_validate_price_bar`, so it wins regardless of the
        // row's prices). Reproduced byte-exactly for the µs unit with a
        // non-null in-range first stamp — the crate OWNS the refusal
        // (`HealErr::Refuse`, never a whole-batch degrade). Every other
        // naive shape (ms/ns unit, null / out-of-range first stamp — where
        // the Python arm raises differently or panics) stays the degrade
        // envelope.
        if *unit == TimeUnit::Microseconds {
            let phys_col = ts.to_physical_repr();
            if let Ok(phys) = phys_col.i64()
                && let Some(first) = phys.get(0)
                && (DATETIME_MIN_US..=DATETIME_MAX_US).contains(&first)
            {
                return Err(HealErr::Refuse(naive_ts_refusal_message(path, first)));
            }
        }
        return Ok((
            bytes,
            hash,
            Vec::new(),
            Envelope::Degrade(format!(
                "bars timestamp dtype {:?} not Datetime(UTC)",
                ts.dtype()
            )),
        ));
    };
    if *unit != TimeUnit::Microseconds || tz.as_str() != "UTC" {
        return Ok((
            bytes,
            hash,
            Vec::new(),
            Envelope::Degrade(format!("bars timestamp dtype {:?} not us/UTC", ts.dtype())),
        ));
    }
    if overlay {
        // W-6b: the overlay schema never parses into `BarRow`s — the heal
        // preserves the stored rows verbatim (frame-level keep-first).
        let verdict = validate_preserve_rows(&df);
        return Ok((bytes, hash, Vec::new(), verdict));
    }
    let ts_phys_col = ts.to_physical_repr();
    let ts_phys = ts_phys_col.i64()?;
    let opens = df.column("open")?.f64()?.clone();
    let highs = df.column("high")?.f64()?.clone();
    let lows = df.column("low")?.f64()?.clone();
    let closes = df.column("close")?.f64()?.clone();
    let volumes = df.column("volume")?.i64()?.clone();
    let ois = df.column("open_interest")?.i64()?.clone();
    let mut rows: Vec<BarRow> = Vec::with_capacity(df.height());
    for idx in 0..df.height() {
        let (Some(ts_us), Some(op), Some(hi), Some(lo), Some(cl), Some(vol)) = (
            ts_phys.get(idx),
            opens.get(idx),
            highs.get(idx),
            lows.get(idx),
            closes.get(idx),
            volumes.get(idx),
        ) else {
            return Ok((
                bytes,
                hash,
                Vec::new(),
                Envelope::Degrade("bars row carries a null in a required column".to_string()),
            ));
        };
        if !(DATETIME_MIN_US..=DATETIME_MAX_US).contains(&ts_us) {
            return Ok((
                bytes,
                hash,
                Vec::new(),
                Envelope::Degrade("bars timestamp outside the datetime range".to_string()),
            ));
        }
        let snapped = [snap_2dp(op), snap_2dp(hi), snap_2dp(lo), snap_2dp(cl)];
        let [
            Some((_, open)),
            Some((_, high)),
            Some((_, low)),
            Some((_, close)),
        ] = snapped
        else {
            // W-6b: a FINITE price beyond the 2dp snap band (>= 2^53 paisa)
            // routes the whole file to the frame-level preserve arm — the
            // stored values are kept verbatim, so no snap is needed. A
            // NON-finite price stays an isolated error (corrupt values are
            // never silently blessed — the loud posture).
            if [op, hi, lo, cl].iter().all(|v| v.is_finite()) {
                let verdict = validate_preserve_rows(&df);
                return Ok((bytes, hash, Vec::new(), verdict));
            }
            return Ok((
                bytes,
                hash,
                Vec::new(),
                Envelope::Degrade("bars price not finite".to_string()),
            ));
        };
        let oi = ois.get(idx);
        // The `_validate_price_bar` laws on the SNAPPED values — a violating
        // row routes the Python arm into corrupt-bar classification, so it
        // degrades here rather than risking a divergent verdict.
        if vol < 0
            || oi.is_some_and(|v| v < 0)
            || high < low
            || !(low <= open && open <= high)
            || !(low <= close && close <= high)
        {
            return Ok((
                bytes,
                hash,
                Vec::new(),
                Envelope::Degrade("bars row violates the CandleBar validation laws".to_string()),
            ));
        }
        rows.push(BarRow {
            ts_us,
            open,
            high,
            low,
            close,
            volume: vol,
            oi,
        });
    }
    Ok((bytes, hash, rows, Envelope::Ok))
}

/// W-6b: validate a PRESERVE-candidate frame's base columns row by row.
/// The caller has already verified the schema (canonical 7-col or the exact
/// 17-col overlay) and the `timestamp` dtype (`Datetime[us, UTC]`). Rows
/// are preserved VERBATIM by the heal, so no snap is applied — but corrupt
/// VALUES (nulls in required columns, non-finite prices, law-violating
/// rows, out-of-range timestamps) still refuse as isolated errors (the
/// loud posture; the price-bar laws run on the RAW stored values). The
/// overlay columns themselves are nullable by design and never validated.
fn validate_preserve_rows(df: &DataFrame) -> Envelope {
    macro_rules! base_col {
        ($name:literal, $cast:ident) => {
            match df.column($name).and_then(|c| Ok(c.$cast()?.clone())) {
                Ok(col) => col,
                Err(err) => {
                    return Envelope::Degrade(format!(
                        "bars base column {} dtype outside the envelope: {err}",
                        $name
                    ));
                }
            }
        };
    }
    let ts_phys_col = match df.column("timestamp") {
        Ok(col) => col.to_physical_repr(),
        Err(err) => return Envelope::Degrade(format!("bars timestamp column missing: {err}")),
    };
    let Ok(ts_phys) = ts_phys_col.i64() else {
        return Envelope::Degrade("bars timestamp physical repr not i64".to_string());
    };
    let opens = base_col!("open", f64);
    let highs = base_col!("high", f64);
    let lows = base_col!("low", f64);
    let closes = base_col!("close", f64);
    let volumes = base_col!("volume", i64);
    let ois = base_col!("open_interest", i64);
    for idx in 0..df.height() {
        let (Some(ts_us), Some(op), Some(hi), Some(lo), Some(cl), Some(vol)) = (
            ts_phys.get(idx),
            opens.get(idx),
            highs.get(idx),
            lows.get(idx),
            closes.get(idx),
            volumes.get(idx),
        ) else {
            return Envelope::Degrade("bars row carries a null in a required column".to_string());
        };
        if !(DATETIME_MIN_US..=DATETIME_MAX_US).contains(&ts_us) {
            return Envelope::Degrade("bars timestamp outside the datetime range".to_string());
        }
        if ![op, hi, lo, cl].iter().all(|v| v.is_finite()) {
            return Envelope::Degrade("bars price not finite".to_string());
        }
        let oi = ois.get(idx);
        if vol < 0
            || oi.is_some_and(|v| v < 0)
            || hi < lo
            || !(lo <= op && op <= hi)
            || !(lo <= cl && cl <= hi)
        {
            return Envelope::Degrade(
                "bars row violates the CandleBar validation laws".to_string(),
            );
        }
    }
    Envelope::Preserve
}

/// Keep-FIRST collapse by timestamp, preserving first-physical order — the
/// `_read_existing_bars` dict law.
pub(super) fn collapse_keep_first(rows: Vec<BarRow>) -> Vec<BarRow> {
    let mut seen: HashMap<i64, ()> = HashMap::with_capacity(rows.len());
    let mut out: Vec<BarRow> = Vec::with_capacity(rows.len());
    for row in rows {
        if seen.insert(row.ts_us, ()).is_none() {
            out.push(row);
        }
    }
    out
}

/// Render sorted bars rows through the byte-locked bars render core — the
/// `_render_bytes` twin (7-column frame; overlay-bearing files never reach
/// this — the single-file dedup routes them through the frame-level
/// preserve arm, and the multi-file heals refuse them upfront, W-6b).
pub(super) fn render_bars(rows: &[BarRow]) -> Result<Vec<u8>, HealErr> {
    let ts: Vec<i64> = rows.iter().map(|r| r.ts_us).collect();
    let timestamp = Series::new("timestamp".into(), ts)
        .cast(&DataType::Datetime(
            TimeUnit::Microseconds,
            Some(TimeZone::UTC),
        ))
        .map_err(HealErr::from)?;
    let mut df = DataFrame::new(
        rows.len(),
        vec![
            timestamp.into(),
            Series::new(
                "open".into(),
                rows.iter().map(|r| r.open).collect::<Vec<_>>(),
            )
            .into(),
            Series::new(
                "high".into(),
                rows.iter().map(|r| r.high).collect::<Vec<_>>(),
            )
            .into(),
            Series::new("low".into(), rows.iter().map(|r| r.low).collect::<Vec<_>>()).into(),
            Series::new(
                "close".into(),
                rows.iter().map(|r| r.close).collect::<Vec<_>>(),
            )
            .into(),
            Series::new(
                "volume".into(),
                rows.iter().map(|r| r.volume).collect::<Vec<_>>(),
            )
            .into(),
            Series::new(
                "open_interest".into(),
                rows.iter().map(|r| r.oi).collect::<Vec<_>>(),
            )
            .into(),
        ],
    )
    .map_err(HealErr::from)?;
    let ipc = frame_to_ipc(&mut df).map_err(HealErr::from)?;
    crate::render_parquet_from_ipc_impl(&ipc).map_err(HealErr::from)
}

// ── shared context ───────────────────────────────────────────────────────────

/// Mutable batch state (the `_BatchState` counters that live crate-side).
pub(super) struct BatchCtx {
    pub(super) lake_root: PathBuf,
    pub(super) quarantine_root: PathBuf,
    pub(super) healed: u64,
    pub(super) skipped: u64,
    pub(super) errors: u64,
    pub(super) quarantined_files: u64,
    pub(super) actions: Vec<Py<PyDict>>,
    pub(super) touched: BTreeMap<(String, String, String), std::collections::BTreeSet<(i32, u32)>>,
}

impl BatchCtx {
    pub(super) fn quarantine_path(&self, source: &Path) -> Result<PathBuf, HealErr> {
        let rel = source.strip_prefix(&self.lake_root).map_err(|_| {
            HealErr::Isolated(format!("{} is outside the lake root", source.display()))
        })?;
        Ok(self.quarantine_root.join(rel))
    }

    pub(super) fn relpath(&self, source: &Path) -> String {
        source
            .strip_prefix(&self.lake_root)
            .map(|rel| {
                rel.to_string_lossy()
                    .replace(std::path::MAIN_SEPARATOR, "/")
            })
            .unwrap_or_else(|_| source.to_string_lossy().into_owned())
    }

    pub(super) fn touch(&mut self, exchange: &str, segment: &str, symbol: &str, month: (i32, u32)) {
        self.touched
            .entry((
                exchange.to_string(),
                segment.to_string(),
                symbol.to_string(),
            ))
            .or_default()
            .insert(month);
    }
}

/// Append one `HealAction`-shaped dict to the batch's action list.
#[allow(clippy::too_many_arguments)]
pub(super) fn push_action(
    py: Python<'_>,
    ctx: &mut BatchCtx,
    action: &str,
    partition: &str,
    kept: i64,
    dropped: i64,
    moved: i64,
    conflicts: i64,
    raw_hash_before: Option<&str>,
    raw_hash_after: Option<&str>,
    quarantine_path: Option<&str>,
) -> PyResult<()> {
    let dict = PyDict::new(py);
    dict.set_item("action", action)?;
    dict.set_item("partition", partition)?;
    dict.set_item("kept", kept)?;
    dict.set_item("dropped", dropped)?;
    dict.set_item("moved", moved)?;
    dict.set_item("conflicts", conflicts)?;
    dict.set_item("raw_hash_before", raw_hash_before)?;
    dict.set_item("raw_hash_after", raw_hash_after)?;
    dict.set_item("quarantine_path", quarantine_path)?;
    ctx.actions.push(dict.unbind());
    Ok(())
}

// ── quarantine move (the `_quarantine_move` twin) ────────────────────────────

pub(super) fn quarantine_move(
    ctx: &mut BatchCtx,
    source: &Path,
    sink: &EventSink<'_, '_>,
) -> Result<PathBuf, HealErr> {
    let dest = ctx.quarantine_path(source)?;
    let content = std::fs::read(source).map_err(classify_io)?;
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(&dest, &content)?;
    std::fs::remove_file(source).map_err(classify_io)?;
    if let Some(parent) = source.parent() {
        let _ = std::fs::remove_dir(parent); // prune a now-empty dir; non-empty is fine
    }
    // Manifest + stats hooks stay Python (house law ii) — the shell replays
    // `invalidate_manifest` + `_remove_dataset_stats_for` off this event.
    let payload = sink.dict();
    payload.set_item("path", source.to_string_lossy().as_ref())?;
    sink.emit("file_removed_move", &payload)?;
    ctx.quarantined_files += 1;
    sink.log(
        "lake_heal.file_quarantined",
        &[
            ("source", obj(sink.py, source.to_string_lossy().as_ref())),
            ("quarantine", obj(sink.py, dest.to_string_lossy().as_ref())),
        ],
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("quarantine", dest.to_string_lossy().as_ref())?;
    sink.audit(
        "LAKE_HEAL_QUARANTINE_FILE",
        &ctx.relpath(source),
        &audit_payload,
    )?;
    Ok(dest)
}

// ── the finding model ────────────────────────────────────────────────────────

/// One parsed healable finding (the shell serializes the dataclasses into
/// plain dicts — deviation D-10: typed Python objects cross, not JSON).
pub(super) struct Finding {
    pub(super) kind: String,
    pub(super) path: PathBuf,
    pub(super) exchange: String,
    pub(super) segment: String,
    pub(super) symbol: String,
    pub(super) interval: String,
    pub(super) year: i32,
    pub(super) month: u32,
    pub(super) series: String,
    pub(super) has_coords: bool,
    pub(super) reason: String,
    pub(super) canonical: String,
}

fn get_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    match dict.get_item(key)? {
        Some(value) => value.extract::<String>(),
        None => Ok(String::new()),
    }
}

fn get_int(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
    match dict.get_item(key)? {
        Some(value) => value.extract::<i64>(),
        None => Ok(0),
    }
}

fn parse_findings(findings: &Bound<'_, PyList>) -> PyResult<Vec<Finding>> {
    let mut out: Vec<Finding> = Vec::with_capacity(findings.len());
    for item in findings.iter() {
        let dict = item
            .cast::<PyDict>()
            .map_err(|_| PyValueError::new_err("finding payload must be a dict"))?;
        let has_coords = match dict.get_item("has_coords")? {
            Some(value) => value.extract::<bool>()?,
            None => false,
        };
        out.push(Finding {
            kind: get_str(dict, "kind")?,
            path: PathBuf::from(get_str(dict, "path")?),
            exchange: get_str(dict, "exchange")?,
            segment: get_str(dict, "segment")?,
            symbol: get_str(dict, "symbol")?,
            interval: get_str(dict, "interval")?,
            year: get_int(dict, "year")? as i32,
            month: get_int(dict, "month")? as u32,
            series: get_str(dict, "series")?,
            has_coords,
            reason: get_str(dict, "reason")?,
            canonical: get_str(dict, "canonical")?,
        });
    }
    Ok(out)
}

// ── preflight (read-only; ANY degrade verdict = zero mutation) ───────────────

/// Probe an options file's key columns for the W-2 envelope. Unreadable →
/// `None` (both arms classify it identically at heal time).
pub(super) fn option_file_envelope(path: &Path) -> Option<String> {
    let Ok(bytes) = std::fs::read(path) else {
        return None;
    };
    let Ok(df) = ParquetReader::new(std::io::Cursor::new(&bytes)).finish() else {
        return None;
    };
    let Ok(side) = df.column("side") else {
        return Some("option side column missing".to_string());
    };
    if !matches!(side.dtype(), DataType::String) {
        return Some(format!(
            "option side dtype {:?} outside the envelope",
            side.dtype()
        ));
    }
    let Ok(ts) = df.column("timestamp") else {
        return Some("option timestamp column missing".to_string());
    };
    let DataType::Datetime(unit, _tz) = ts.dtype() else {
        return Some(format!(
            "option timestamp dtype {:?} outside the envelope",
            ts.dtype()
        ));
    };
    let Ok(phys_col) = std::convert::identity(ts.to_physical_repr().i64().cloned()) else {
        return Some("option timestamp physical repr not i64".to_string());
    };
    for value in phys_col.iter().flatten() {
        let Some(us) = to_us(value, *unit) else {
            return Some("option timestamp overflows µs".to_string());
        };
        if !(DATETIME_MIN_US..=DATETIME_MAX_US).contains(&us) {
            return Some("option timestamp outside the datetime range".to_string());
        }
    }
    None
}

/// Per-finding read-only preflight (the W-6a endgame shape): `Some(reason)`
/// = THIS finding is outside the native heal envelope. With the Python heal
/// core DELETED (DEC-RUST-LAKE-WAVED-001 W-6a) the verdict is no longer a
/// whole-batch degrade — the batch loop converts it into a per-finding
/// ISOLATED error (counted in `errors`, `lake_heal.partition_error` logged,
/// the batch CONTINUES) BEFORE any byte of that finding is touched. The
/// probes themselves are read-only, so a refused finding is left
/// byte-untouched (the pre-mutation law survives per finding).
fn preflight_one(lake_root: &Path, finding: &Finding) -> Option<String> {
    match finding.kind.as_str() {
        "IntraFileDup" => {
            if !finding.path.exists() {
                return None; // a vanished path skips identically at heal time
            }
            match read_bars_file(&finding.path) {
                Ok((_, _, _, Envelope::Degrade(reason))) => {
                    Some(format!("bars_envelope: {reason}"))
                }
                // Unreadable — the heal errors identically; a `Refuse`
                // (naive-ts, W-5 phase A) is the crate-owned isolated
                // verdict — engage, never refuse here.
                _ => None,
            }
        }
        "OptionIntraFileDup" => {
            if !finding.path.exists() {
                return None;
            }
            option_file_envelope(&finding.path).map(|reason| format!("option_envelope: {reason}"))
        }
        "SchemaAlienFile" => None, // a verbatim byte move — any content engages
        // The W-2b multi-file algorithms (deviation D-22): every shape
        // whose Python heal would have raised mid-mutation refuses UPFRONT
        // instead — read-only probes.
        "ForeignMonthRows" => super::heal_multi::preflight_bars_foreign(lake_root, finding),
        "OptionForeignMonthRows" => super::heal_multi::preflight_option_foreign(finding),
        "LegacyKeyDrift" => super::heal_multi::preflight_drift(lake_root, finding),
        // An unknown kind is `heal_one`'s own isolated arm — no probe here.
        _ => None,
    }
}

// ── options containment + heal ───────────────────────────────────────────────

/// `Path.resolve(strict=False)`'s twin for the containment probe (W-2b):
/// canonicalize the deepest EXISTING ancestor (symlinks resolved — a
/// symlink LOOP surfaces as the error, exactly the Python round-5 guarded
/// class) and append the non-existent tail lexically (a non-existent
/// component cannot be a symlink). A FRESH reassignment destination — the
/// W-2b copy-forward leg's normal shape — resolves through its existing
/// series dir instead of refusing on strict-`canonicalize` ENOENT.
pub(super) fn resolve_nonstrict(path: &Path) -> std::io::Result<PathBuf> {
    match std::fs::canonicalize(path) {
        Ok(resolved) => Ok(resolved),
        Err(err) if err.kind() == ErrorKind::NotFound => {
            let Some(parent) = path.parent() else {
                return Err(err);
            };
            let Some(name) = path.file_name() else {
                return Err(err);
            };
            Ok(resolve_nonstrict(parent)?.join(name))
        }
        Err(err) => Err(err),
    }
}

/// The `_assert_options_containment` twin — both rails, message-equivalent
/// control flow (the refusal is an isolated counted error either arm).
pub(super) fn assert_options_containment(lake_root: &Path, dest: &Path) -> Result<(), HealErr> {
    let options_root = match std::fs::canonicalize(lake_root) {
        Ok(root) => root.join("options"),
        Err(err) => {
            return Err(HealErr::Isolated(format!(
                "cannot resolve {} for the options-containment check: {err} — refusing to touch it",
                dest.display()
            )));
        }
    };
    let resolved_parent = match resolve_nonstrict(dest) {
        Ok(resolved) => match resolved.parent() {
            Some(parent) => parent.to_path_buf(),
            None => {
                return Err(HealErr::Isolated(format!(
                    "cannot resolve {} for the options-containment check — refusing to touch it",
                    dest.display()
                )));
            }
        },
        Err(err) => {
            if err.raw_os_error() == Some(libc::ELOOP) {
                // A symlink-LOOP dest (W-5 phase A, `symlink_loop_partition_file`):
                // Python ≤3.12's `Path.resolve(strict=False)` realpath leaves
                // the loop unresolved, the ensuing `stat()` ELOOP converts to
                // `RuntimeError(f"Symlink loop from {e.filename!r}")`, and
                // `_assert_options_containment` wraps it — reproduced
                // byte-exactly on the realpath-nonstrict output (the crate
                // OWNS the refusal, `HealErr::Refuse`).
                let looped = realpath_nonstrict(dest);
                return Err(HealErr::Refuse(symlink_loop_refusal_message(dest, &looped)));
            }
            return Err(HealErr::Isolated(format!(
                "cannot resolve {} for the options-containment check: {err} — refusing to touch it",
                dest.display()
            )));
        }
    };
    if !resolved_parent.starts_with(&options_root) {
        // Byte-aligned with the Python arm's message (W-5 phase A — not
        // corpus-reachable while the preflight degrades redirecting symlink
        // shapes, but fidelity is free here).
        return Err(HealErr::Isolated(format!(
            "destination {} resolves outside the options tree ({} is not under {}) — refusing to mutate",
            dest.display(),
            resolved_parent.display(),
            options_root.display()
        )));
    }
    let dest_is_symlink = std::fs::symlink_metadata(dest)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false);
    let parent_is_symlink = dest
        .parent()
        .and_then(|p| std::fs::symlink_metadata(p).ok())
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false);
    if dest_is_symlink || parent_is_symlink {
        // Byte-aligned with the Python round-6 in-lake swap rail message
        // (W-5 phase A — the preflight degrades these shapes today, so the
        // arm is TOCTOU defense-in-depth; fidelity is free).
        return Err(HealErr::Isolated(format!(
            "destination {} is or sits behind a symlink inside the options tree — \
             the healer only targets real partition files in real series dirs; \
             refusing to touch it",
            dest.display()
        )));
    }
    Ok(())
}

/// One options-tree dedup heal (`OptionParquetWriter.heal_partition` with no
/// `remove_keys`, driven by `_heal_option_dup`). Returns `healed`.
fn heal_option_dup(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let dest = &finding.path;
    if !dest.exists() {
        return Ok(false);
    }
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    assert_options_containment(&ctx.lake_root, dest)?;
    let bytes = std::fs::read(dest)
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
    let df = ParquetReader::new(std::io::Cursor::new(&bytes))
        .finish()
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
    let physical = df.height() as i64;
    let subset: Vec<String> = vec!["side".into(), "timestamp".into()];
    let deduped = df
        .unique_stable(Some(&subset), UniqueKeepStrategy::First, None)
        .map_err(HealErr::from)?
        .sort(["side", "timestamp"], SortMultipleOptions::default())
        .map_err(HealErr::from)?;
    let kept = deduped.height() as i64;
    let dropped = physical - kept;
    if dropped == 0 {
        return Ok(false);
    }
    let quarantine = ctx.quarantine_path(dest)?;
    if let Some(parent) = quarantine.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(&quarantine, &bytes)?;
    // kept > 0 always on the dup path (a zero-row file has no dup finding);
    // render through the byte-locked defaults core + atomic replace.
    let mut render_frame = deduped.clone();
    let ipc = frame_to_ipc(&mut render_frame).map_err(HealErr::from)?;
    let rendered = crate::render_parquet_defaults_from_ipc_impl(&ipc).map_err(HealErr::from)?;
    atomic_write_bytes(dest, &rendered)?;
    // Stats re-stamp stays Python — bounds cross as µs (already
    // envelope-checked in preflight).
    let ts_col = deduped.column("timestamp").map_err(HealErr::from)?;
    let unit = match ts_col.dtype() {
        DataType::Datetime(unit, _) => *unit,
        _ => TimeUnit::Microseconds,
    };
    let ts_phys_col = ts_col.to_physical_repr();
    let ts_phys = ts_phys_col.i64().map_err(HealErr::from)?;
    let mut min_us: Option<i64> = None;
    let mut max_us: Option<i64> = None;
    for value in ts_phys.iter().flatten() {
        let us = to_us(value, unit).unwrap_or(value);
        min_us = Some(min_us.map_or(us, |m: i64| m.min(us)));
        max_us = Some(max_us.map_or(us, |m: i64| m.max(us)));
    }
    let stats = sink.dict();
    stats.set_item("path", dest.to_string_lossy().as_ref())?;
    stats.set_item("rows", kept)?;
    stats.set_item("min_ts_us", min_us)?;
    stats.set_item("max_ts_us", max_us)?;
    sink.emit("option_stats_record", &stats)?;
    ctx.quarantined_files += 1;
    let partition = format!(
        "options/{}/{}",
        finding.series,
        dest.file_name()
            .map(|n| n.to_string_lossy())
            .unwrap_or_default()
    );
    sink.log(
        "lake_heal.partition_healed",
        &[
            ("partition", obj(sink.py, partition.as_str())),
            ("partition_repr", obj(sink.py, true)),
            ("kept", obj(sink.py, kept)),
            ("dropped", obj(sink.py, dropped)),
        ],
    )?;
    let quarantine_str = quarantine.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_DEDUP",
        &partition,
        kept,
        dropped,
        0,
        0,
        None,
        None,
        Some(&quarantine_str),
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("tree", "options")?;
    audit_payload.set_item("kept", kept)?;
    audit_payload.set_item("dropped_duplicates", dropped)?;
    audit_payload.set_item("quarantine_path", quarantine_str.as_str())?;
    sink.audit("LAKE_HEAL_DEDUP", &partition, &audit_payload)?;
    Ok(true)
}

// ── bars dup heal ────────────────────────────────────────────────────────────

/// One bars dedup heal (`ParquetWriter.heal_partition` with no removals,
/// driven by `_heal_dup`). Returns `healed`.
fn heal_bars_dup(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let dest = &finding.path;
    if !dest.exists() {
        return Ok(false);
    }
    let _flock = acquire_partition_flock(&ctx.lake_root, dest, sink)?;
    let (bytes, hash_before, rows, envelope) = read_bars_file(dest)?;
    match envelope {
        Envelope::Ok => {}
        // W-6b: the frame-level verbatim arm (17-col Greeks overlay /
        // finite big-price files) — keep-first on `timestamp`, all columns
        // preserved, rendered through the same PR-1 bars profile.
        Envelope::Preserve => {
            return heal_bars_dup_preserve(ctx, finding, sink, &bytes, &hash_before);
        }
        Envelope::Degrade(reason) => {
            // Defensive: preflight verified the envelope; a mid-batch swap
            // lands here as an isolated error (the loud posture, never a
            // guess).
            return Err(HealErr::Isolated(format!(
                "bars envelope violated mid-batch: {reason}"
            )));
        }
    }
    let physical = rows.len() as i64;
    let unique = collapse_keep_first(rows);
    let stored_unique = unique.len() as i64;
    if physical > stored_unique {
        sink.log(
            "lake.partition.duplicate_collapsed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("physical_rows", obj(sink.py, physical)),
                ("unique_timestamps", obj(sink.py, stored_unique)),
                ("collapsed", obj(sink.py, physical - stored_unique)),
            ],
        )?;
    }
    let dropped_duplicates = physical - stored_unique;
    if dropped_duplicates == 0 {
        return Ok(false);
    }
    let quarantine = ctx.quarantine_path(dest)?;
    if let Some(parent) = quarantine.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(&quarantine, &bytes)?;
    let mut sorted = unique;
    sorted.sort_by_key(|row| row.ts_us);
    let rendered = render_bars(&sorted)?;
    atomic_write_bytes(dest, &rendered)?;
    let hash_after = sha256_hex(&rendered);
    finish_bars_dedup(
        ctx,
        finding,
        sink,
        dest,
        &quarantine,
        &hash_before,
        &hash_after,
        stored_unique,
        dropped_duplicates,
        sorted.first().map(|r| r.ts_us),
        sorted.last().map(|r| r.ts_us),
    )
}

/// W-6b: the frame-level PRESERVE dedup arm ([`Envelope::Preserve`]) — the
/// exact 17-column Greeks-overlay schema Greeks enrichment writes, or a
/// canonical file whose only violation is finite beyond-2^53-paisa prices.
/// Keep-FIRST unique on `timestamp` (first physical row wins — the
/// `_read_existing_bars` dict law), EVERY column preserved with its stored
/// values VERBATIM (strictly row-removing, never value-editing — the #1594
/// law), sorted by timestamp and rendered through the SAME PR-1 bars render
/// profile the canonical arm uses.
fn heal_bars_dup_preserve(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
    bytes: &[u8],
    hash_before: &str,
) -> Result<bool, HealErr> {
    let dest = &finding.path;
    let df = ParquetReader::new(std::io::Cursor::new(bytes))
        .finish()
        .map_err(|err| HealErr::Isolated(format!("corrupt existing partition: {err}")))?;
    let physical = df.height() as i64;
    let subset: Vec<String> = vec!["timestamp".into()];
    let deduped = df
        .unique_stable(Some(&subset), UniqueKeepStrategy::First, None)
        .map_err(HealErr::from)?
        .sort(["timestamp"], SortMultipleOptions::default())
        .map_err(HealErr::from)?;
    let kept = deduped.height() as i64;
    let dropped = physical - kept;
    if dropped > 0 {
        sink.log(
            "lake.partition.duplicate_collapsed",
            &[
                ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
                ("physical_rows", obj(sink.py, physical)),
                ("unique_timestamps", obj(sink.py, kept)),
                ("collapsed", obj(sink.py, dropped)),
            ],
        )?;
    }
    if dropped == 0 {
        return Ok(false);
    }
    let quarantine = ctx.quarantine_path(dest)?;
    if let Some(parent) = quarantine.parent() {
        std::fs::create_dir_all(parent).map_err(classify_io)?;
    }
    atomic_write_bytes(&quarantine, bytes)?;
    let mut render_frame = deduped.clone();
    let ipc = frame_to_ipc(&mut render_frame).map_err(HealErr::from)?;
    let rendered = crate::render_parquet_from_ipc_impl(&ipc).map_err(HealErr::from)?;
    atomic_write_bytes(dest, &rendered)?;
    let hash_after = sha256_hex(&rendered);
    // Stats bounds from the sorted frame (ts is envelope-checked us/UTC).
    let ts_phys_col = deduped
        .column("timestamp")
        .map_err(HealErr::from)?
        .to_physical_repr();
    let ts_phys = ts_phys_col.i64().map_err(HealErr::from)?;
    let first_us = ts_phys.get(0);
    let last_us = if deduped.height() == 0 {
        None
    } else {
        ts_phys.get(deduped.height() - 1)
    };
    finish_bars_dedup(
        ctx,
        finding,
        sink,
        dest,
        &quarantine,
        hash_before,
        &hash_after,
        kept,
        dropped,
        first_us,
        last_us,
    )
}

/// The shared bars-dedup completion tail (both arms): dedup logs, stats
/// re-stamp, forensic action, audit line, catalog rotation + the derived-TF
/// touch — all replayed shell-side through the ONE `on_event` channel.
#[allow(clippy::too_many_arguments)]
fn finish_bars_dedup(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
    dest: &Path,
    quarantine: &Path,
    hash_before: &str,
    hash_after: &str,
    stored_unique: i64,
    dropped_duplicates: i64,
    first_us: Option<i64>,
    last_us: Option<i64>,
) -> Result<bool, HealErr> {
    sink.log(
        "lake.partition.dedup_healed",
        &[
            ("partition", obj(sink.py, dest.to_string_lossy().as_ref())),
            ("kept", obj(sink.py, stored_unique)),
            ("dropped", obj(sink.py, dropped_duplicates)),
        ],
    )?;
    // Stats re-stamp (the `_record_stats_hook` replay, under the held flock).
    let stats = sink.dict();
    stats.set_item("path", dest.to_string_lossy().as_ref())?;
    stats.set_item("rows", stored_unique)?;
    stats.set_item("first_ts_us", first_us)?;
    stats.set_item("last_ts_us", last_us)?;
    stats.set_item("raw_hash", hash_after)?;
    sink.emit("bars_stats_record", &stats)?;
    ctx.quarantined_files += 1;
    let partition = format!(
        "bars/{}/{}/{}/{}/{:04}/{:02}.parquet",
        finding.exchange,
        finding.segment,
        finding.symbol,
        finding.interval,
        finding.year,
        finding.month
    );
    sink.log(
        "lake_heal.partition_healed",
        &[
            ("partition", obj(sink.py, partition.as_str())),
            ("partition_repr", obj(sink.py, false)),
            ("kept", obj(sink.py, stored_unique)),
            ("dropped", obj(sink.py, dropped_duplicates)),
        ],
    )?;
    let quarantine_str = quarantine.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_DEDUP",
        &partition,
        stored_unique,
        dropped_duplicates,
        0,
        0,
        Some(hash_before),
        Some(hash_after),
        Some(&quarantine_str),
    )?;
    let audit_payload = sink.dict();
    audit_payload.set_item("kept", stored_unique)?;
    audit_payload.set_item("dropped_duplicates", dropped_duplicates)?;
    audit_payload.set_item("dropped_removed", 0)?;
    audit_payload.set_item("raw_hash_before", hash_before)?;
    audit_payload.set_item("raw_hash_after", hash_after)?;
    audit_payload.set_item("quarantine_path", quarantine_str.as_str())?;
    sink.audit("LAKE_HEAL_DEDUP", &partition, &audit_payload)?;
    // Catalog rotation (Golden Rule #4) — the shell upserts off this event.
    let catalog = sink.dict();
    catalog.set_item("exchange", finding.exchange.as_str())?;
    catalog.set_item("segment", finding.segment.as_str())?;
    catalog.set_item("symbol", finding.symbol.as_str())?;
    catalog.set_item("interval", finding.interval.as_str())?;
    catalog.set_item("year", finding.year)?;
    catalog.set_item("month", finding.month)?;
    catalog.set_item("raw_hash", hash_after)?;
    catalog.set_item("row_count", stored_unique)?;
    catalog.set_item("first_ts_us", first_us)?;
    catalog.set_item("last_ts_us", last_us)?;
    sink.emit("catalog_upsert", &catalog)?;
    if finding.interval == "1minute" {
        ctx.touch(
            &finding.exchange,
            &finding.segment,
            &finding.symbol,
            (finding.year, finding.month),
        );
    }
    Ok(true)
}

// ── schema-alien heal ────────────────────────────────────────────────────────

/// One schema-alien whole-file quarantine move (`_heal_schema_alien`).
fn heal_schema_alien(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    let path = &finding.path;
    if !path.exists() {
        return Ok(false);
    }
    let dest = quarantine_move(ctx, path, sink)?;
    let partition: String = if finding.has_coords {
        let catalog_delete = sink.dict();
        catalog_delete.set_item("exchange", finding.exchange.as_str())?;
        catalog_delete.set_item("segment", finding.segment.as_str())?;
        catalog_delete.set_item("symbol", finding.symbol.as_str())?;
        catalog_delete.set_item("interval", finding.interval.as_str())?;
        catalog_delete.set_item("year", finding.year)?;
        catalog_delete.set_item("month", finding.month)?;
        sink.emit("catalog_delete", &catalog_delete)?;
        if finding.interval == "1minute" {
            ctx.touch(
                &finding.exchange,
                &finding.segment,
                &finding.symbol,
                (finding.year, finding.month),
            );
        }
        format!(
            "bars/{}/{}/{}/{}/{:04}/{:02}.parquet",
            finding.exchange,
            finding.segment,
            finding.symbol,
            finding.interval,
            finding.year,
            finding.month
        )
    } else {
        format!(
            "options/{}/{}",
            finding.series,
            path.file_name()
                .map(|n| n.to_string_lossy())
                .unwrap_or_default()
        )
    };
    sink.log(
        "lake_heal.alien_file_quarantined",
        &[
            ("partition", obj(sink.py, partition.as_str())),
            ("partition_repr", obj(sink.py, !finding.has_coords)),
            ("reason", obj(sink.py, finding.reason.as_str())),
        ],
    )?;
    let dest_str = dest.to_string_lossy().into_owned();
    push_action(
        sink.py,
        ctx,
        "LAKE_HEAL_QUARANTINE_FILE",
        &partition,
        0,
        0,
        0,
        0,
        None,
        None,
        Some(&dest_str),
    )?;
    Ok(true)
}

// ── the batch driver ─────────────────────────────────────────────────────────

fn heal_one(
    ctx: &mut BatchCtx,
    finding: &Finding,
    sink: &EventSink<'_, '_>,
) -> Result<bool, HealErr> {
    match finding.kind.as_str() {
        "IntraFileDup" => heal_bars_dup(ctx, finding, sink),
        "OptionIntraFileDup" => heal_option_dup(ctx, finding, sink),
        "SchemaAlienFile" => heal_schema_alien(ctx, finding, sink),
        "ForeignMonthRows" => super::heal_multi::heal_bars_foreign(ctx, finding, sink),
        "OptionForeignMonthRows" => super::heal_multi::heal_option_foreign(ctx, finding, sink),
        "LegacyKeyDrift" => super::heal_multi::heal_drift(ctx, finding, sink),
        other => Err(HealErr::Isolated(format!("unported finding kind {other}"))),
    }
}

/// The `_finding_token` twin — `(token, needs_repr)`.
fn finding_token(finding: &Finding, ctx: &BatchCtx) -> (String, bool) {
    match finding.kind.as_str() {
        "OptionIntraFileDup" | "OptionForeignMonthRows" => {
            (finding.path.to_string_lossy().into_owned(), true)
        }
        "LegacyKeyDrift" => (
            format!(
                "{}/{}/{}",
                finding.exchange, finding.segment, finding.symbol
            ),
            false,
        ),
        _ => {
            let _ = ctx;
            (finding.path.to_string_lossy().into_owned(), false)
        }
    }
}

/// The batch core: parse → per-finding loop (each finding preflighted
/// read-only, then healed) with isolation → outcome dict. Since the W-6a
/// endgame there is NO whole-batch degrade — an out-of-envelope finding is
/// a per-finding isolated error and the batch continues (the deleted
/// Python core's isolation posture, owned natively).
pub(crate) fn heal_batch_core<'py>(
    py: Python<'py>,
    lake_root: &str,
    quarantine_root: &str,
    findings: &Bound<'py, PyList>,
    on_event: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let parsed = parse_findings(findings)?;
    let out = PyDict::new(py);
    let sink = EventSink { py, on_event };
    let mut ctx = BatchCtx {
        lake_root: PathBuf::from(lake_root),
        quarantine_root: PathBuf::from(quarantine_root),
        healed: 0,
        skipped: 0,
        errors: 0,
        quarantined_files: 0,
        actions: Vec::new(),
        touched: BTreeMap::new(),
    };
    for finding in &parsed {
        sink.beat()?;
        let verdict = match preflight_one(Path::new(lake_root), finding) {
            Some(reason) => Err(HealErr::Isolated(format!(
                "outside the native heal envelope: {reason} — finding left \
                 byte-untouched (DEC-RUST-LAKE-WAVED-001 endgame; inspect the \
                 file, heal or quarantine it manually)"
            ))),
            None => heal_one(&mut ctx, finding, &sink),
        };
        match verdict {
            Ok(true) => ctx.healed += 1,
            Ok(false) => ctx.skipped += 1,
            Err(HealErr::Isolated(message)) | Err(HealErr::Refuse(message)) => {
                ctx.errors += 1;
                let (token, needs_repr) = finding_token(finding, &ctx);
                sink.log(
                    "lake_heal.partition_error",
                    &[
                        ("finding", obj(py, finding.kind.as_str())),
                        ("partition", obj(py, token.as_str())),
                        ("partition_repr", obj(py, needs_repr)),
                        ("error", obj(py, message.as_str())),
                    ],
                )?;
            }
            Err(HealErr::Fatal(err)) => {
                return Err(match err.raw_os_error() {
                    Some(code) => PyOSError::new_err((code, err.to_string())),
                    None => PyOSError::new_err(err.to_string()),
                });
            }
            Err(HealErr::Py(err)) => return Err(err),
        }
    }
    out.set_item("healed", ctx.healed)?;
    out.set_item("skipped", ctx.skipped)?;
    out.set_item("errors", ctx.errors)?;
    out.set_item("quarantined_files", ctx.quarantined_files)?;
    let actions = PyList::empty(py);
    for action in &ctx.actions {
        actions.append(action.bind(py))?;
    }
    out.set_item("actions", actions)?;
    let touched = PyList::empty(py);
    for ((exchange, segment, symbol), months) in &ctx.touched {
        let entry = PyList::empty(py);
        entry.append(exchange)?;
        entry.append(segment)?;
        entry.append(symbol)?;
        let month_list = PyList::empty(py);
        for (year, month) in months {
            month_list.append((*year, *month))?;
        }
        entry.append(month_list)?;
        touched.append(entry)?;
    }
    out.set_item("touched_minute", touched)?;
    Ok(out)
}

// ── the content token (§C-5) ─────────────────────────────────────────────────

/// One walk-skip event `(event_name, path, error)` the shell replays.
pub(crate) struct TokenSkip {
    pub event: &'static str,
    pub path: String,
    pub error: String,
}

/// The `lake_content_token` walk twin: sorted-at-each-level depth-first walk
/// over `bars/` + `options/`, one `relpath|size|mtime_ns\n` line per
/// `*.parquet`-named entry (`lstat` — never follows), per-entry/dir skip
/// events, sha256 hex of the concatenation.
pub(crate) fn content_token_impl(lake_root: &str) -> (String, Vec<TokenSkip>) {
    use std::os::unix::fs::MetadataExt;
    let root = Path::new(lake_root);
    let mut hasher = Sha256::new();
    let mut skips: Vec<TokenSkip> = Vec::new();
    for tree in ["bars", "options"] {
        let base = root.join(tree);
        if !base.is_dir() {
            continue;
        }
        let mut stack: Vec<PathBuf> = vec![base];
        while let Some(directory) = stack.pop() {
            let mut children: Vec<PathBuf> = match std::fs::read_dir(&directory) {
                Ok(entries) => {
                    let mut out: Vec<PathBuf> = Vec::new();
                    let mut failed = false;
                    for entry in entries {
                        match entry {
                            Ok(entry) => out.push(entry.path()),
                            Err(err) => {
                                skips.push(TokenSkip {
                                    event: "lake_heal.token_walk_skipped",
                                    path: directory.to_string_lossy().into_owned(),
                                    error: err.to_string(),
                                });
                                failed = true;
                                break;
                            }
                        }
                    }
                    if failed {
                        continue;
                    }
                    out
                }
                Err(err) => {
                    skips.push(TokenSkip {
                        event: "lake_heal.token_walk_skipped",
                        path: directory.to_string_lossy().into_owned(),
                        error: err.to_string(),
                    });
                    continue;
                }
            };
            children.sort();
            let mut subdirs: Vec<PathBuf> = Vec::new();
            for path in children {
                let name_matches = path
                    .file_name()
                    .map(|n| n.to_string_lossy().ends_with(".parquet"))
                    .unwrap_or(false);
                if name_matches {
                    match std::fs::symlink_metadata(&path) {
                        Ok(stat) => {
                            let rel = path
                                .strip_prefix(root)
                                .map(|r| {
                                    r.to_string_lossy().replace(std::path::MAIN_SEPARATOR, "/")
                                })
                                .unwrap_or_else(|_| path.to_string_lossy().into_owned());
                            let mtime_ns = i128::from(stat.mtime()) * 1_000_000_000
                                + i128::from(stat.mtime_nsec());
                            let line = format!("{rel}|{}|{mtime_ns}\n", stat.size());
                            hasher.update(line.as_bytes());
                        }
                        Err(err) => {
                            skips.push(TokenSkip {
                                event: "lake_heal.token_stat_skipped",
                                path: path.to_string_lossy().into_owned(),
                                error: err.to_string(),
                            });
                            continue;
                        }
                    }
                }
                let is_dir = std::fs::metadata(&path)
                    .map(|m| m.is_dir())
                    .unwrap_or(false);
                let is_symlink = std::fs::symlink_metadata(&path)
                    .map(|m| m.file_type().is_symlink())
                    .unwrap_or(false);
                if is_dir && !is_symlink {
                    subdirs.push(path);
                }
            }
            subdirs.reverse();
            stack.extend(subdirs);
        }
    }
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    (hex, skips)
}

// ── FFI boundary ─────────────────────────────────────────────────────────────

/// The Wave-D W-2 heal batch (design §C-4). Findings cross as plain dicts
/// (deviation D-10); events stream through `on_event(kind, payload)`; the
/// return is the batch outcome dict (since the W-6a endgame there is no
/// whole-batch `{"degrade"}` verdict — an out-of-envelope finding is a
/// per-finding isolated error and the batch continues). Runs UNDER the GIL
/// (callbacks interleave with mutations — the audit adjacency law; heal is
/// anomaly-gated, never a hot path — deviation D-11). A Rust panic maps to
/// `RuntimeError` (loud — the idempotent re-run posture), a disk-full to a
/// real `OSError(ENOSPC)` for the shell's `lake_write_guard`.
#[pyfunction]
pub(crate) fn waved_heal_batch<'py>(
    py: Python<'py>,
    lake_root: String,
    quarantine_root: String,
    findings: Bound<'py, PyList>,
    on_event: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        heal_batch_core(py, &lake_root, &quarantine_root, &findings, &on_event)
    }));
    match result {
        Ok(outcome) => outcome,
        Err(_) => Err(PyRuntimeError::new_err(
            "brutex-lake waved_heal_batch panicked (caught at the FFI boundary)",
        )),
    }
}

/// The W-2b lockstep witness: the finding kinds the crate heals NATIVELY.
/// A shell serializing all six kinds against a pre-W-2b wheel would silently
/// run Python for the multi-file classes forever (the D-7 flip blocker), so
/// the facade lists this name in its ALL-OR-NOTHING `_CRATE_API` — a stale
/// wheel latches `crate_unavailable` LOUDLY instead.
#[pyfunction]
pub(crate) fn waved_heal_engaged_kinds() -> Vec<&'static str> {
    vec![
        "IntraFileDup",
        "OptionIntraFileDup",
        "SchemaAlienFile",
        "ForeignMonthRows",
        "OptionForeignMonthRows",
        "LegacyKeyDrift",
    ]
}

/// The W-6a ENDGAME lockstep witness (DEC-RUST-LAKE-WAVED-001): `True` iff
/// this wheel carries the endgame isolation semantics — per-finding heal
/// refusal (no whole-batch `{"degrade"}` outcome) + per-file scan skip
/// conversion. The facade lists this name in its ALL-OR-NOTHING
/// `_CRATE_API`: with the Python cores DELETED, a pre-endgame wheel (whose
/// heal batch could still return a whole-batch degrade the shell no longer
/// has an oracle for) must latch `LakeRustWavedUnavailableError` LOUDLY,
/// never half-work.
#[pyfunction]
pub(crate) fn waved_endgame_witness() -> bool {
    true
}

/// The `lake_content_token` twin (§C-5): `(token_hex, skip_events)` where
/// each skip event is a `{"event", "path", "error"}` dict the shell replays
/// through the pinned WARN sites. Read-only; runs WITHOUT the GIL.
#[pyfunction]
pub(crate) fn waved_lake_content_token<'py>(
    py: Python<'py>,
    lake_root: String,
) -> PyResult<(String, Bound<'py, PyList>)> {
    let result = py.detach(|| std::panic::catch_unwind(|| content_token_impl(&lake_root)));
    let (token, skips) = result.map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake waved_lake_content_token panicked (caught at the FFI boundary)",
        )
    })?;
    let events = PyList::empty(py);
    for skip in skips {
        let payload = PyDict::new(py);
        payload.set_item("event", skip.event)?;
        payload.set_item("path", skip.path)?;
        payload.set_item("error", skip.error)?;
        events.append(payload)?;
    }
    Ok((token, events))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_hex_matches_python_hashlib_vectors() {
        // Known-answer vectors (`hashlib.sha256(b"...").hexdigest()`).
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn snap_2dp_matches_round_money_law() {
        // Exact 2dp values are identities.
        assert_eq!(snap_2dp(100.25), Some((10_025, 100.25)));
        assert_eq!(snap_2dp(0.0), Some((0, 0.0)));
        // ROUND_HALF_UP away from zero at the boundary (str-repr seeded —
        // 100.005 reprs as "100.005" exactly? Its shortest repr IS
        // "100.005", which parses to digits 100005/10^3 → half → 10001).
        assert_eq!(snap_2dp(100.005).map(|(p, _)| p), Some(10_001));
        assert_eq!(snap_2dp(-100.005).map(|(p, _)| p), Some(-10_001));
        // The long-tail float BELOW the boundary keeps the legacy side.
        assert_eq!(snap_2dp(0.00499999999).map(|(p, _)| p), Some(0));
        // Vendor float noise rounds to the tick.
        assert_eq!(snap_2dp(14_020.799_9).map(|(p, _)| p), Some(1_402_080));
        // A subnormal-tiny value rounds to zero, never overflows.
        assert_eq!(snap_2dp(1e-300).map(|(p, _)| p), Some(0));
        // Non-finite + beyond-2^53 → the degrade sentinel.
        assert_eq!(snap_2dp(f64::NAN), None);
        assert_eq!(snap_2dp(f64::INFINITY), None);
        assert_eq!(snap_2dp(1.0e18), None);
    }

    #[test]
    fn naive_ts_refusal_message_matches_the_python_writer_error_bytes() {
        // W-5 phase A: the corpus plant's first row (2024-06-03 04:01 naive,
        // epoch day 19,877) — probed against the executed Python arm:
        // `corrupt existing partition <p>: CandleBar.timestamp must be
        // timezone-aware, got naive datetime datetime.datetime(2024, 6, 3, 4, 1)`.
        let first_us = 19_877i64 * 86_400_000_000 + (4 * 3_600 + 60) * 1_000_000;
        assert_eq!(
            naive_ts_refusal_message(Path::new("/lake/bars/06.parquet"), first_us),
            "corrupt existing partition /lake/bars/06.parquet: CandleBar.timestamp \
             must be timezone-aware, got naive datetime \
             datetime.datetime(2024, 6, 3, 4, 1)"
        );
    }

    #[test]
    fn symlink_loop_refusal_message_matches_the_python_pathlib_bytes() {
        // W-5 phase A: Python ≤3.12 pathlib's
        // `RuntimeError("Symlink loop from '{p}'")` wrapped by
        // `_assert_options_containment` (probed against the executed arm).
        let dest = Path::new("/lake/options/SERIES/2024-07.parquet");
        assert_eq!(
            symlink_loop_refusal_message(dest, dest),
            "cannot resolve /lake/options/SERIES/2024-07.parquet for the \
             options-containment check: Symlink loop from \
             '/lake/options/SERIES/2024-07.parquet' — refusing to touch it"
        );
    }

    #[test]
    fn realpath_nonstrict_resolves_ancestors_of_a_symlink_loop() {
        // A SELF-loop final component: realpath (non-strict) resolves the
        // real ancestors and keeps the loop name — the `e.filename` shape
        // Python's stat() ELOOP reports (probed on CPython 3.12.3).
        let dir = std::env::temp_dir().join(format!(
            "brutex-heal-loop-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let loop_path = dir.join("2024-07.parquet");
        std::os::unix::fs::symlink(&loop_path, &loop_path).unwrap();
        let resolved = realpath_nonstrict(&loop_path);
        assert_eq!(
            resolved,
            std::fs::canonicalize(&dir).unwrap().join("2024-07.parquet")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn collapse_keep_first_keeps_first_physical_row() {
        let rows = vec![
            BarRow {
                ts_us: 1,
                open: 1.0,
                high: 1.0,
                low: 1.0,
                close: 1.0,
                volume: 1,
                oi: None,
            },
            BarRow {
                ts_us: 1,
                open: 2.0,
                high: 2.0,
                low: 2.0,
                close: 2.0,
                volume: 2,
                oi: None,
            },
            BarRow {
                ts_us: 2,
                open: 3.0,
                high: 3.0,
                low: 3.0,
                close: 3.0,
                volume: 3,
                oi: None,
            },
        ];
        let unique = collapse_keep_first(rows);
        assert_eq!(unique.len(), 2);
        assert_eq!(unique[0].close, 1.0); // the FIRST physical copy won
        assert_eq!(unique[1].close, 3.0);
    }

    #[test]
    fn content_token_walk_is_deterministic_and_format_pinned() {
        let dir = std::env::temp_dir().join(format!(
            "brutex-lake-heal-token-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let bars = dir.join("bars/NSE/INDEX/NIFTY/1minute/2024");
        std::fs::create_dir_all(&bars).unwrap();
        std::fs::write(bars.join("06.parquet"), b"x").unwrap();
        let (token_a, skips_a) = content_token_impl(&dir.to_string_lossy());
        let (token_b, _) = content_token_impl(&dir.to_string_lossy());
        assert_eq!(token_a, token_b);
        assert!(skips_a.is_empty());
        assert_eq!(token_a.len(), 64);
        // Rewriting the file moves the token (size/mtime line changes).
        std::fs::write(bars.join("06.parquet"), b"different bytes").unwrap();
        let (token_c, _) = content_token_impl(&dir.to_string_lossy());
        assert_ne!(token_a, token_c);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn content_token_unstattable_entry_skipped_deterministically() {
        // R2 fix-round (DEC-RUST-LAKE-WAVED-001): the walk-stat fault
        // subclass on a REAL on-disk fault — a listable-but-unsearchable
        // directory (mode 0o444: `read_dir` still lists names, the child
        // `symlink_metadata` is EACCES). Root (CAP_DAC_OVERRIDE) makes any
        // chmod fault ineffective — the CI runner executes this as a
        // regular user; under root the fault cannot be planted on-disk
        // (a dangling symlink is NOT a substitute: lstat SUCCEEDS on it —
        // the R11 lstat law), so return early with a loud note.
        #[allow(unsafe_code)] // SAFETY: `libc::geteuid` takes no arguments
        // and only reads process credentials — no memory/fd ownership.
        fn effective_uid() -> libc::uid_t {
            unsafe { libc::geteuid() }
        }
        if effective_uid() == 0 {
            eprintln!("skipped: chmod-based lstat fault is ineffective as root");
            return;
        }
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!(
            "brutex-lake-heal-token-fault-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let blocked = dir.join("bars/NSE/INDEX/NIFTY/1minute/2024");
        std::fs::create_dir_all(&blocked).unwrap();
        std::fs::write(blocked.join("06.parquet"), b"x").unwrap();
        let healthy = dir.join("bars/NSE/INDEX/BANKNIFTY/1minute/2024");
        std::fs::create_dir_all(&healthy).unwrap();
        std::fs::write(healthy.join("06.parquet"), b"y").unwrap();
        std::fs::set_permissions(&blocked, std::fs::Permissions::from_mode(0o444)).unwrap();
        let (token_a, skips_a) = content_token_impl(&dir.to_string_lossy());
        let (token_b, skips_b) = content_token_impl(&dir.to_string_lossy());
        std::fs::set_permissions(&blocked, std::fs::Permissions::from_mode(0o755)).unwrap();
        // A well-formed 64-hex token, the LOUD stat-skip event, and
        // determinism across identical faulted walks.
        assert_eq!(token_a.len(), 64);
        assert_eq!(skips_a.len(), 1, "exactly the unstattable entry skips");
        assert_eq!(skips_a[0].event, "lake_heal.token_stat_skipped");
        assert!(skips_a[0].path.ends_with("2024/06.parquet"));
        assert!(!skips_a[0].error.is_empty());
        assert_eq!(token_a, token_b);
        assert_eq!(skips_b.len(), 1);
        // Token EQUALITY with the same lake genuinely LACKING the
        // unstattable entry (the round-11 determinism law).
        std::fs::remove_file(blocked.join("06.parquet")).unwrap();
        let (token_c, skips_c) = content_token_impl(&dir.to_string_lossy());
        assert!(skips_c.is_empty());
        assert_eq!(token_a, token_c);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn realpath_nonstrict_keeps_nonexistent_tail() {
        let dir = std::env::temp_dir();
        let ghost = dir.join("brutex-heal-no-such-dir/child.parquet");
        let resolved = realpath_nonstrict(&ghost);
        assert!(resolved.ends_with("brutex-heal-no-such-dir/child.parquet"));
    }

    // ── W-6b: the preserve-arm envelope classification ───────────────────────
    //
    // `read_bars_file` itself cannot link in the no-libpython cargo test
    // binary (`HealErr` carries a `PyErr` — the ScanFail law), so the cargo
    // pins live on the PURE pieces: `bars_schema_mode` (the schema dispatch)
    // and `validate_preserve_rows` (the preserve-arm row laws). The
    // end-to-end routing + heal gate is the real-wheel Python differential
    // (`tests/integration/test_waved_heal_differential.py`).

    /// 2024-06-03 04:01 UTC + 2024-06-03 04:02 UTC, epoch µs.
    const T1_US: i64 = 1_717_387_260_000_000;
    const T2_US: i64 = 1_717_387_320_000_000;

    /// Build a bars frame (`timestamp[us, UTC]` + prices), optionally with
    /// the ten Greeks-overlay columns appended in canonical order.
    fn bars_df(ts_us: &[i64], price: f64, overlay: bool) -> DataFrame {
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), ts_us.to_vec())
            .cast(&dtype)
            .unwrap();
        let n = ts_us.len();
        let prices: Vec<f64> = vec![price; n];
        let mut cols: Vec<Column> = vec![
            ts.into(),
            Column::new("open".into(), prices.clone()),
            Column::new("high".into(), prices.clone()),
            Column::new("low".into(), prices.clone()),
            Column::new("close".into(), prices),
            Column::new("volume".into(), vec![10i64; n]),
            Column::new("open_interest".into(), vec![5i64; n]),
        ];
        if overlay {
            for name in &BARS_OVERLAY_COLUMNS[7..] {
                if *name == "greeks_provenance_id" {
                    let vals: Vec<Option<i32>> = (0..n).map(|i| Some(i as i32)).collect();
                    cols.push(Column::new((*name).into(), vals));
                } else {
                    let vals: Vec<Option<f64>> = (0..n)
                        .map(|i| if i == 0 { None } else { Some(0.5) })
                        .collect();
                    cols.push(Column::new((*name).into(), vals));
                }
            }
        }
        DataFrame::new_infer_height(cols).unwrap()
    }

    fn names_of(df: &DataFrame) -> Vec<String> {
        df.get_column_names()
            .iter()
            .map(|n| n.to_string())
            .collect()
    }

    #[test]
    fn schema_mode_classifies_canonical_overlay_and_alien() {
        let canonical = bars_df(&[T1_US], 100.0, false);
        assert_eq!(bars_schema_mode(&names_of(&canonical)), Some(false));
        let overlay = bars_df(&[T1_US], 100.0, true);
        assert_eq!(bars_schema_mode(&names_of(&overlay)), Some(true));
        // A drifted overlay NAME is alien — refused, never guessed.
        let mut drifted = names_of(&overlay);
        drifted[7] = "implied_vol".to_string();
        assert_eq!(bars_schema_mode(&drifted), None);
        // Name ORDER is part of the law.
        let mut reordered = names_of(&canonical);
        reordered.swap(1, 2);
        assert_eq!(bars_schema_mode(&reordered), None);
    }

    #[test]
    fn preserve_rows_accept_overlay_and_big_price_frames() {
        // Overlay frame (nullable overlay values ignored by the validator).
        assert!(matches!(
            validate_preserve_rows(&bars_df(&[T1_US, T1_US, T2_US], 100.25, true)),
            Envelope::Preserve
        ));
        // Finite big-price frame (1e17 rupees -> 1e19 paisa >= 2^53).
        assert!(matches!(
            validate_preserve_rows(&bars_df(&[T1_US, T1_US], 1.0e17, false)),
            Envelope::Preserve
        ));
    }

    #[test]
    fn preserve_rows_refuse_non_finite_prices() {
        match validate_preserve_rows(&bars_df(&[T1_US, T1_US], f64::NAN, false)) {
            Envelope::Degrade(reason) => assert!(reason.contains("not finite"), "{reason}"),
            _ => panic!("expected Degrade"),
        }
    }

    #[test]
    fn preserve_rows_refuse_law_violations_on_raw_values() {
        // high < low on the RAW stored values (the preserve arm never edits
        // values, so corrupt values refuse loudly instead).
        let mut df = bars_df(&[T1_US, T1_US], 100.0, true);
        df.replace(
            "high",
            Series::new("high".into(), vec![99.0f64, 99.0]).into(),
        )
        .unwrap();
        match validate_preserve_rows(&df) {
            Envelope::Degrade(reason) => {
                assert!(reason.contains("CandleBar validation laws"), "{reason}");
            }
            _ => panic!("expected Degrade"),
        }
    }

    #[test]
    fn preserve_rows_refuse_null_required_cells_and_oor_timestamps() {
        // A null timestamp cell.
        let dtype = DataType::Datetime(TimeUnit::Microseconds, Some(TimeZone::UTC));
        let ts = Series::new("timestamp".into(), vec![Some(T1_US), None])
            .cast(&dtype)
            .unwrap();
        let mut df = bars_df(&[T1_US, T2_US], 100.0, true);
        df.replace("timestamp", ts.into()).unwrap();
        match validate_preserve_rows(&df) {
            Envelope::Degrade(reason) => {
                assert!(reason.contains("null in a required column"), "{reason}");
            }
            _ => panic!("expected Degrade"),
        }
        // An out-of-datetime-range stamp (2^62 µs — the #1919 class).
        let oor = Series::new("timestamp".into(), vec![1i64 << 62, T2_US])
            .cast(&dtype)
            .unwrap();
        let mut df2 = bars_df(&[T1_US, T2_US], 100.0, false);
        df2.replace("timestamp", oor.into()).unwrap();
        match validate_preserve_rows(&df2) {
            Envelope::Degrade(reason) => {
                assert!(reason.contains("outside the datetime range"), "{reason}");
            }
            _ => panic!("expected Degrade"),
        }
    }

    #[test]
    fn preserve_rows_refuse_wrong_base_column_dtypes() {
        // Overlay NAMES with a String `open` — dtype outside the envelope.
        let mut df = bars_df(&[T1_US, T2_US], 100.0, true);
        df.replace("open", Series::new("open".into(), vec!["a", "b"]).into())
            .unwrap();
        match validate_preserve_rows(&df) {
            Envelope::Degrade(reason) => {
                assert!(reason.contains("dtype outside the envelope"), "{reason}");
            }
            _ => panic!("expected Degrade"),
        }
    }
}
