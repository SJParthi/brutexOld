//! # detectors — the cold data-integrity DETECTOR pure laws (SLICE T24, PR-1)
//!
//! DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). Mirrors,
//! token for token, THREE pure decision laws of the `brutex/data/`
//! read-boundary detector family behind the default-OFF facade
//! `brutex/data/detectors_rust.py` (three flags —
//! `BRUTEX_LAKE_RUST_RAW_INTEGRITY` / `BRUTEX_LAKE_RUST_INTERIOR_GAP` /
//! `BRUTEX_LAKE_RUST_PARTIAL_WINDOW`):
//!
//! 1. **Raw-integrity digest** — the `hashlib.blake2b(canonical,
//!    digest_size=32).hexdigest()` leg of
//!    `brutex/data/raw_integrity.py::hash_payload`
//!    (DEC-RAW-DATA-INTEGRITY-001). The CANONICALISATION
//!    (`json.dumps(candles, sort_keys=True, separators=(",", ":"))`) stays
//!    PYTHON — raw vendor candles carry float prices and this crate makes
//!    no attempt to reproduce CPython's `float.__repr__` (the stats_shard
//!    ruling, verbatim): Python computes the canonical BYTES, Rust derives
//!    the hex digest. The blake2b core below is written in-crate from
//!    RFC 7693 (unkeyed, sequential) so `Cargo.toml`/`Cargo.lock` stay
//!    byte-untouched — pinned against the RFC appendix-A vector AND frozen
//!    `hashlib.blake2b` vectors generated from the live CPython (tests).
//! 2. **Interior bar-less run walker** — the grouping decision of
//!    `brutex/data/interior_gap_detect.py::_interior_barless_runs`
//!    (DEC-INTERIOR-GAP-DETECT-001): given the covered-date ordinals and
//!    the Python-walked INTERIOR trading-session ordinals (the NSE
//!    calendar + observed overlay stay Python — never re-implemented
//!    here), emit every maximal run of interior sessions carrying no bars
//!    as `(run_first, run_last, run_sessions)`, bounds inclusive.
//! 3. **Partial-window truncation verdict** — the WHOLE decision tree of
//!    `brutex/data/partial_window_detect.py::classify_partial_window`
//!    (DEC-PARTIAL-WINDOW-DETECT-001): empty / out-of-coverage / at-expiry
//!    / live-edge / complete / truncated / sparse_complete over plain
//!    ordinals + a Python-computed SESSION GRID (per-day trading flags).
//!    Any walk stepping OFF the grid ABSTAINS (`None` — the
//!    DEC-FEED-RUST-CORE-001 envelope contract; the facade serves the
//!    Python oracle).
//!
//! NO I/O, NO polars — the crate's polars REV pin is irrelevant to this
//! module; the facade's engagement guard is the SYMBOL handshake (all
//! three functions present + callable), never the polars version guard.
//!
//! HONESTY (ADR-0020 banner, verbatim): expected payoff ≈ 0 — conversion
//! is for uniformity, not speed. All three laws run on cold pull-tail /
//! doctor diagnostics; the FFI hop plus the facade's payload prep is a
//! small SLOWDOWN, accepted because the seams ship DEFAULT-OFF and the
//! surrounding paths are vendor-I/O-dominated. These detectors gate REAL
//! ledger demotions (#1031) — decision parity, not speed, is the bar.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

// ────────────────────────────────────────────────────────────────────────
// Law 1 — blake2b (RFC 7693, unkeyed, sequential) → lowercase hex
// ────────────────────────────────────────────────────────────────────────

/// The blake2b initialization vector (RFC 7693 §2.6 — the SHA-512 IVs).
const BLAKE2B_IV: [u64; 8] = [
    0x6a09_e667_f3bc_c908,
    0xbb67_ae85_84ca_a73b,
    0x3c6e_f372_fe94_f82b,
    0xa54f_f53a_5f1d_36f1,
    0x510e_527f_ade6_82d1,
    0x9b05_688c_2b3e_6c1f,
    0x1f83_d9ab_fb41_bd6b,
    0x5be0_cd19_137e_2179,
];

/// The message-word schedule (RFC 7693 §2.7 — rows 10/11 repeat rows 0/1).
const BLAKE2B_SIGMA: [[usize; 16]; 12] = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
    [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
    [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
    [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
    [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
    [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
    [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
    [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
    [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
];

/// The blake2b block size in bytes (RFC 7693 `bb`).
const BLAKE2B_BLOCK: usize = 128;

/// Read a little-endian u64 at *off* in *b* (safe indexing — no unwrap).
fn le_u64(b: &[u8], off: usize) -> u64 {
    let mut x = 0u64;
    for i in 0..8 {
        x |= u64::from(b[off + i]) << (8 * i);
    }
    x
}

/// The blake2b mixing function G (RFC 7693 §3.1; rotations 32/24/16/63).
#[allow(clippy::many_single_char_names)] // the RFC's own variable names.
fn blake2b_g(v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64) {
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(x);
    v[d] = (v[d] ^ v[a]).rotate_right(32);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(24);
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(y);
    v[d] = (v[d] ^ v[a]).rotate_right(16);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(63);
}

/// The blake2b compression function F (RFC 7693 §3.2).
fn blake2b_compress(h: &mut [u64; 8], block: &[u8; BLAKE2B_BLOCK], t: u128, last: bool) {
    let mut m = [0u64; 16];
    for (i, word) in m.iter_mut().enumerate() {
        *word = le_u64(block, i * 8);
    }
    let mut v = [0u64; 16];
    v[..8].copy_from_slice(h);
    v[8..].copy_from_slice(&BLAKE2B_IV);
    v[12] ^= t as u64;
    v[13] ^= (t >> 64) as u64;
    if last {
        v[14] = !v[14];
    }
    for sigma in &BLAKE2B_SIGMA {
        blake2b_g(&mut v, 0, 4, 8, 12, m[sigma[0]], m[sigma[1]]);
        blake2b_g(&mut v, 1, 5, 9, 13, m[sigma[2]], m[sigma[3]]);
        blake2b_g(&mut v, 2, 6, 10, 14, m[sigma[4]], m[sigma[5]]);
        blake2b_g(&mut v, 3, 7, 11, 15, m[sigma[6]], m[sigma[7]]);
        blake2b_g(&mut v, 0, 5, 10, 15, m[sigma[8]], m[sigma[9]]);
        blake2b_g(&mut v, 1, 6, 11, 12, m[sigma[10]], m[sigma[11]]);
        blake2b_g(&mut v, 2, 7, 8, 13, m[sigma[12]], m[sigma[13]]);
        blake2b_g(&mut v, 3, 4, 9, 14, m[sigma[14]], m[sigma[15]]);
    }
    for i in 0..8 {
        h[i] ^= v[i] ^ v[i + 8];
    }
}

/// One-shot unkeyed blake2b over *data* — the `hashlib.blake2b(data,
/// digest_size=nn)` twin, rendered as lowercase hex. `digest_size` outside
/// `1..=64` is refused (`Err` — CPython's own bound); everything else is
/// infallible over the full byte domain.
pub fn blake2b_hex_impl(data: &[u8], digest_size: usize) -> Result<String, String> {
    if !(1..=64).contains(&digest_size) {
        return Err(format!("digest_size out of range 1..=64: {digest_size}"));
    }
    let mut h = BLAKE2B_IV;
    // Parameter block word 0 for the unkeyed sequential mode:
    // digest_length ^ (key_length << 8) ^ (fanout=1 << 16) ^ (depth=1 << 24).
    h[0] ^= 0x0101_0000 ^ (digest_size as u64);
    if data.is_empty() {
        blake2b_compress(&mut h, &[0u8; BLAKE2B_BLOCK], 0, true);
    } else {
        let mut chunks = data.chunks(BLAKE2B_BLOCK).peekable();
        let mut t: u128 = 0;
        while let Some(chunk) = chunks.next() {
            let is_last = chunks.peek().is_none();
            t += chunk.len() as u128;
            let mut block = [0u8; BLAKE2B_BLOCK];
            block[..chunk.len()].copy_from_slice(chunk);
            blake2b_compress(&mut h, &block, t, is_last);
        }
    }
    let mut out = String::with_capacity(digest_size * 2);
    let mut emitted = 0usize;
    'words: for word in h {
        for byte_index in 0..8 {
            if emitted == digest_size {
                break 'words;
            }
            let byte = ((word >> (8 * byte_index)) & 0xff) as u8;
            out.push(char::from(HEX_DIGITS[usize::from(byte >> 4)]));
            out.push(char::from(HEX_DIGITS[usize::from(byte & 0x0f)]));
            emitted += 1;
        }
    }
    Ok(out)
}

/// Lowercase hex alphabet (matches CPython's `hexdigest()`).
const HEX_DIGITS: [u8; 16] = *b"0123456789abcdef";

/// FFI face of [`blake2b_hex_impl`] — `bytes` in, lowercase hex `str` out
/// (`ValueError` on an out-of-range `digest_size`; `RuntimeError` if the
/// pure core panics — caught at this boundary, never a raw
/// `PanicException` into the facade).
#[pyfunction]
#[pyo3(signature = (data, digest_size))]
pub(crate) fn raw_integrity_blake2b_hex(data: &[u8], digest_size: usize) -> PyResult<String> {
    std::panic::catch_unwind(|| blake2b_hex_impl(data, digest_size))
        .map_err(|_| {
            PyRuntimeError::new_err(
                "brutex-lake raw_integrity_blake2b_hex panicked (caught at the FFI boundary)",
            )
        })?
        .map_err(PyValueError::new_err)
}

// ────────────────────────────────────────────────────────────────────────
// Law 2 — the interior bar-less run walker
// ────────────────────────────────────────────────────────────────────────

/// The `_MIN_BRACKETING_SESSIONS` twin (an interior hole needs a covered
/// session on BOTH sides).
pub const MIN_BRACKETING_SESSIONS: usize = 2;

/// The grouping decision of `_interior_barless_runs` — the verbatim mirror
/// over the SESSION-LIST projection: *covered* is the covered-date ordinal
/// set (any order — only membership + the wrapper's `covered[0]`/
/// `covered[-1]` bracketing, which the Python facade applies when building
/// *interior_sessions*, matter) and *interior_sessions* is the ordered
/// list of TRADING-session ordinals STRICTLY between the first and last
/// covered date (the Python-walked NSE calendar + observed overlay — a
/// non-trading day never appears, so it can neither break nor extend a
/// run, and a covered NON-trading date likewise never appears: exactly
/// the Python walker's `is_trading_day_observed` gate). Emits one
/// `(run_first, run_last, run_sessions)` per maximal run of sessions NOT
/// in *covered* — both bounds inclusive, ascending. Fewer than
/// [`MIN_BRACKETING_SESSIONS`] covered ordinals → `[]` (no interior).
pub fn interior_barless_runs_impl(
    covered: &[i64],
    interior_sessions: &[i64],
) -> Vec<(i64, i64, i64)> {
    if covered.len() < MIN_BRACKETING_SESSIONS {
        return Vec::new();
    }
    let have: std::collections::HashSet<i64> = covered.iter().copied().collect();
    let mut runs: Vec<(i64, i64, i64)> = Vec::new();
    let mut run_first: Option<i64> = None;
    let mut run_last: Option<i64> = None;
    let mut run: i64 = 0;
    for &session in interior_sessions {
        if have.contains(&session) {
            if let (Some(first), Some(last)) = (run_first, run_last) {
                runs.push((first, last, run));
            }
            run_first = None;
            run_last = None;
            run = 0;
        } else {
            if run_first.is_none() {
                run_first = Some(session);
            }
            run_last = Some(session);
            run += 1;
        }
    }
    if let (Some(first), Some(last)) = (run_first, run_last) {
        runs.push((first, last, run));
    }
    runs
}

/// FFI face of [`interior_barless_runs_impl`] — int ordinals in, int
/// 3-tuples out (the ints-only DEC-FEED-RUST-CORE-001 discipline; date
/// materialization is the Python facade's). `RuntimeError` on a caught
/// panic, never a raw `PanicException`.
#[pyfunction]
#[pyo3(signature = (covered, interior_sessions))]
pub(crate) fn interior_barless_runs(
    covered: Vec<i64>,
    interior_sessions: Vec<i64>,
) -> PyResult<Vec<(i64, i64, i64)>> {
    std::panic::catch_unwind(|| interior_barless_runs_impl(&covered, &interior_sessions)).map_err(
        |_| {
            PyRuntimeError::new_err(
                "brutex-lake interior_barless_runs panicked (caught at the FFI boundary)",
            )
        },
    )
}

// ────────────────────────────────────────────────────────────────────────
// Law 3 — the partial-window truncation verdict
// ────────────────────────────────────────────────────────────────────────

/// `PartialWindowVerdict.reason` tokens — byte-identical to the Python
/// literals (`classify_partial_window`).
pub const REASON_EMPTY: &str = "empty";
/// The out-of-calendar-coverage abstain reason.
pub const REASON_OUT_OF_COVERAGE: &str = "out_of_calendar_coverage";
/// The at-expiry exclusion reason.
pub const REASON_AT_EXPIRY: &str = "at_expiry";
/// The live-edge exclusion reason.
pub const REASON_LIVE_EDGE: &str = "live_edge";
/// The complete-to-bound reason.
pub const REASON_COMPLETE: &str = "complete";
/// The confirmed-truncation reason (the ONE `SUSPECTED_PARTIAL` verdict).
pub const REASON_TRUNCATED: &str = "truncated";
/// The within-tolerance trailing-gap reason.
pub const REASON_SPARSE_COMPLETE: &str = "sparse_complete";

/// The Python-computed session grid: one trading flag per day over
/// `[span_start, span_start + flags.len())` (day ordinals). Every calendar
/// consultation of the verdict runs through [`SessionGrid::is_session`];
/// a day OFF the grid yields `None` — the ABSTAIN contract (the facade
/// serves the Python oracle; correctness never depends on the span
/// margin, only engagement does).
pub struct SessionGrid<'a> {
    span_start: i64,
    flags: &'a [bool],
}

impl<'a> SessionGrid<'a> {
    /// Build a grid anchored at *span_start* over *flags*.
    pub fn new(span_start: i64, flags: &'a [bool]) -> Self {
        Self { span_start, flags }
    }

    /// Is *day* a trading session? `None` = off the grid (abstain).
    fn is_session(&self, day: i64) -> Option<bool> {
        let idx = day.checked_sub(self.span_start)?;
        if idx < 0 {
            return None;
        }
        self.flags.get(usize::try_from(idx).ok()?).copied()
    }

    /// The `last_completed_session_on_or_before` twin — walk backwards
    /// from *d* to the nearest trading session. Off-grid → `None`.
    fn last_session_on_or_before(&self, d: i64) -> Option<i64> {
        let mut cursor = d;
        loop {
            if self.is_session(cursor)? {
                return Some(cursor);
            }
            cursor = cursor.checked_sub(1)?;
        }
    }

    /// The `sessions_between` twin — trading sessions in the half-open
    /// `(start, end]` (`0` when `end <= start`). Off-grid → `None`.
    fn sessions_between(&self, start: i64, end: i64) -> Option<i64> {
        if end <= start {
            return Some(0);
        }
        let mut count: i64 = 0;
        let mut cursor = start.checked_add(1)?;
        while cursor <= end {
            if self.is_session(cursor)? {
                count += 1;
            }
            cursor = cursor.checked_add(1)?;
        }
        Some(count)
    }
}

/// The verdict fields crossing back to Python:
/// `(reason, last_bar_ordinal, expected_end_ordinal, missing_sessions)`.
/// The facade materializes `PartialWindowVerdict` (verdict string derived
/// from the reason; dates via `date.fromordinal`).
pub type PartialWindowFields = (&'static str, Option<i64>, Option<i64>, i64);

/// The WHOLE `classify_partial_window` decision tree, arm for arm (see
/// the Python source for the law prose — every branch below cites its
/// twin). `None` = a session walk stepped off the grid → ABSTAIN.
///
/// * `coverage_ok` is the Python-computed three-way
///   `is_holiday_data_available(window_end/last_bar/today)` conjunction —
///   a calendar-availability FACT, never re-derived here. It is consulted
///   only when a last bar exists (the Python arm order: EMPTY first).
/// * `tolerance` / `live_edge` are the caller-resolved tunables (the env
///   read stays Python).
#[allow(clippy::too_many_arguments)] // Mirrors the Python law's parameter surface 1:1.
pub fn classify_partial_window_impl(
    last_bar_ordinal: Option<i64>,
    window_end_ordinal: i64,
    today_ordinal: i64,
    expiry_ordinal: Option<i64>,
    coverage_ok: bool,
    tolerance: i64,
    live_edge: i64,
    grid: &SessionGrid<'_>,
) -> Option<PartialWindowFields> {
    // EMPTY window — no stored bars to be "truncated".
    let last_bar = match last_bar_ordinal {
        None => return Some((REASON_EMPTY, None, None, 0)),
        Some(value) => value,
    };
    // OUT-OF-CALENDAR-COVERAGE exclusion (the MAJOR-1 arm).
    if !coverage_ok {
        return Some((REASON_OUT_OF_COVERAGE, Some(last_bar), None, 0));
    }
    // END-EXCLUSIVE bound, clamped to expiry then today (earliest wins) —
    // the Python `if expiry is not None and expiry < end_exclusive` arm.
    let mut end_exclusive = window_end_ordinal.saturating_sub(1);
    if let Some(expiry) = expiry_ordinal
        && expiry < end_exclusive
    {
        end_exclusive = expiry;
    }
    end_exclusive = end_exclusive.min(today_ordinal);
    let mut expected_end = grid.last_session_on_or_before(end_exclusive)?;
    // AT-EXPIRY exclusion.
    if let Some(expiry) = expiry_ordinal {
        let expiry_session = grid.last_session_on_or_before(expiry)?;
        if expected_end >= expiry_session {
            expected_end = expiry_session;
            if last_bar >= expiry_session {
                return Some((REASON_AT_EXPIRY, Some(last_bar), Some(expected_end), 0));
            }
        }
    }
    // LIVE-EDGE exclusion.
    let today_session = grid.last_session_on_or_before(today_ordinal)?;
    if grid.sessions_between(expected_end, today_session)? < live_edge {
        return Some((REASON_LIVE_EDGE, Some(last_bar), Some(expected_end), 0));
    }
    // COMPLETE-TO-BOUND (sparse-but-complete is COMPLETE — density never
    // consulted, only temporal coverage).
    let missing = grid.sessions_between(last_bar, expected_end)?;
    if missing == 0 {
        return Some((REASON_COMPLETE, Some(last_bar), Some(expected_end), 0));
    }
    // TRUNCATION vs within-tolerance trailing gap.
    if missing > tolerance {
        return Some((
            REASON_TRUNCATED,
            Some(last_bar),
            Some(expected_end),
            missing,
        ));
    }
    Some((
        REASON_SPARSE_COMPLETE,
        Some(last_bar),
        Some(expected_end),
        missing,
    ))
}

/// FFI face of [`classify_partial_window_impl`] — plain ordinals + the
/// session-grid flags in, the verdict fields (or `None` = abstain) out.
/// `RuntimeError` on a caught panic; PyO3 argument conversion is the only
/// other raise surface (an out-of-`i64` int → `OverflowError`, a facade
/// degrade member).
#[pyfunction]
#[allow(clippy::too_many_arguments)] // Mirrors the Python law's parameter surface 1:1.
#[pyo3(signature = (last_bar_ordinal, window_end_ordinal, today_ordinal, expiry_ordinal, coverage_ok, tolerance, live_edge, span_start_ordinal, trading_flags))]
pub(crate) fn classify_partial_window_verdict(
    last_bar_ordinal: Option<i64>,
    window_end_ordinal: i64,
    today_ordinal: i64,
    expiry_ordinal: Option<i64>,
    coverage_ok: bool,
    tolerance: i64,
    live_edge: i64,
    span_start_ordinal: i64,
    trading_flags: Vec<bool>,
) -> PyResult<Option<PartialWindowFields>> {
    std::panic::catch_unwind(|| {
        let grid = SessionGrid::new(span_start_ordinal, &trading_flags);
        classify_partial_window_impl(
            last_bar_ordinal,
            window_end_ordinal,
            today_ordinal,
            expiry_ordinal,
            coverage_ok,
            tolerance,
            live_edge,
            &grid,
        )
    })
    .map_err(|_| {
        PyRuntimeError::new_err(
            "brutex-lake classify_partial_window_verdict panicked (caught at the FFI boundary)",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // ------------------------------------------------------------------
    // Law 1 — blake2b vectors (RFC 7693 + frozen hashlib pins)
    // ------------------------------------------------------------------

    #[test]
    fn rfc7693_appendix_a_abc_512() {
        // RFC 7693 Appendix A — BLAKE2b-512("abc").
        assert_eq!(
            blake2b_hex_impl(b"abc", 64).as_deref(),
            Ok(concat!(
                "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1",
                "7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923",
            ))
        );
    }

    #[test]
    fn hashlib_frozen_vectors_digest_32() {
        // Frozen `hashlib.blake2b(data, digest_size=32).hexdigest()` pins
        // generated from the live CPython 3.12 (2026-07-17) — the
        // cross-language oracle for the production `_DIGEST_SIZE = 32`.
        let cases: &[(&[u8], &str)] = &[
            (
                b"",
                "0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8",
            ),
            (
                b"abc",
                "bddd813c634239723171ef3fee98579b94964e3bb1cb3e427262c8c068d52319",
            ),
            (
                b"[]",
                "c2c013c86a9c92e22f966f3d8617768f8b7a44c902adcc097bfa1e098afb4e1f",
            ),
            (
                br#"[["2024-01-02T09:15:00",21500.5,21510.0,21490.25,21505.75,125000,null]]"#,
                "322c4cad830c9a95d42f2d838338b5132cf9175d96ba70edc6cb230f6d98f016",
            ),
        ];
        for (data, expected) in cases {
            assert_eq!(
                blake2b_hex_impl(data, 32).as_deref(),
                Ok(*expected),
                "diverged for {data:?}"
            );
        }
    }

    #[test]
    fn hashlib_frozen_vectors_block_boundaries() {
        // One-block-exact (128), just-over (129) and two-blocks-exact (256)
        // shapes — the streaming/last-block flag edges; plus the 0x00..0xff
        // full byte alphabet. All hashlib-pinned.
        assert_eq!(
            blake2b_hex_impl(&[b'b'; 128], 32).as_deref(),
            Ok("3be061d8b7919eea8948a3d8c6f466abf2b177dc34f81378171305ccc6fc98d9")
        );
        assert_eq!(
            blake2b_hex_impl(&[b'a'; 129], 32).as_deref(),
            Ok("2f64744a6de0d2c0b56e64cf6e29a5aaa255010d415d51c75ccc82f73dccd865")
        );
        assert_eq!(
            blake2b_hex_impl(&[b'c'; 256], 32).as_deref(),
            Ok("37036c656905cc725005b14509b5ffdcb598eb271d6065fcf908e66c92b43791")
        );
        let alphabet: Vec<u8> = (0u16..256).map(|b| b as u8).collect();
        assert_eq!(
            blake2b_hex_impl(&alphabet, 32).as_deref(),
            Ok("39a7eb9fedc19aabc83425c6755dd90e6f9d0c804964a1f4aaeea3b9fb599835")
        );
    }

    #[test]
    fn digest_size_bounds() {
        // hashlib pins at both legal extremes; out-of-range refused.
        assert_eq!(blake2b_hex_impl(b"x", 1).as_deref(), Ok("e2"));
        assert_eq!(
            blake2b_hex_impl(b"x", 64).as_deref(),
            Ok(concat!(
                "0909377ad35110cafb2909e185672b7f2728d1f5094f8ad68d6fac6274bf1f49",
                "9485a80ea364c04ed006d29459ea3cb7c600280e2f83e032529906f88ae30d0a",
            ))
        );
        assert!(blake2b_hex_impl(b"x", 0).is_err());
        assert!(blake2b_hex_impl(b"x", 65).is_err());
    }

    #[test]
    fn tamper_changes_digest() {
        let a = blake2b_hex_impl(b"[[1,2,3]]", 32);
        let b = blake2b_hex_impl(b"[[1,2,4]]", 32);
        assert_ne!(a, b);
    }

    // ------------------------------------------------------------------
    // Law 2 — the interior run walker
    // ------------------------------------------------------------------

    #[test]
    fn too_few_covered_is_empty() {
        assert_eq!(interior_barless_runs_impl(&[], &[]), Vec::new());
        assert_eq!(interior_barless_runs_impl(&[100], &[]), Vec::new());
        // Even with (bogus) interior sessions supplied, < 2 covered → [].
        assert_eq!(interior_barless_runs_impl(&[100], &[101, 102]), Vec::new());
    }

    #[test]
    fn continuous_interior_yields_no_runs() {
        // Every interior session covered → zero runs.
        assert_eq!(
            interior_barless_runs_impl(&[100, 101, 102, 103], &[101, 102]),
            Vec::new()
        );
    }

    #[test]
    fn single_interior_hole_bounds_and_length() {
        // covered {100, 105}; interior sessions 101..=104 all bar-less.
        assert_eq!(
            interior_barless_runs_impl(&[100, 105], &[101, 102, 103, 104]),
            vec![(101, 104, 4)]
        );
    }

    #[test]
    fn covered_session_resets_the_run() {
        // Two holes bracketed by a covered interior session: {101,102} and
        // {104} split by covered 103 — the pytest
        // `test_two_holes_bracketed_by_a_covered_session_resets_the_run`
        // shape.
        assert_eq!(
            interior_barless_runs_impl(&[100, 103, 105], &[101, 102, 103, 104]),
            vec![(101, 102, 2), (104, 104, 1)]
        );
    }

    #[test]
    fn trailing_open_run_is_flushed_and_only_covered_sessions_reset() {
        // interior sessions 101, 105, 106 — NONE covered, so they form ONE
        // maximal run (non-trading days between them never appear in the
        // session list and never break a run — the Python walker's law);
        // the run is emitted by the post-loop flush (never closed by a
        // covered session).
        assert_eq!(
            interior_barless_runs_impl(&[100, 110], &[101, 105, 106]),
            vec![(101, 106, 3)]
        );
    }

    #[test]
    fn non_consecutive_ordinals_group_by_list_adjacency() {
        // Sessions are LIST-adjacent (weekends/holidays never appear), so a
        // run's length counts SESSIONS, not calendar days: ordinals 101 and
        // 104 (a weekend between) form ONE 2-session run.
        assert_eq!(
            interior_barless_runs_impl(&[100, 105], &[101, 104]),
            vec![(101, 104, 2)]
        );
    }

    #[test]
    fn covered_membership_is_a_set_lookup_regardless_of_order() {
        // covered arrives unsorted — membership only.
        assert_eq!(
            interior_barless_runs_impl(&[105, 100, 103], &[101, 102, 103, 104]),
            vec![(101, 102, 2), (104, 104, 1)]
        );
    }

    // ------------------------------------------------------------------
    // Law 3 — the partial-window verdict
    // ------------------------------------------------------------------

    /// Weekday grid over [start, start+len): Mon-Fri sessions (proleptic
    /// ordinal 1 = a Monday, so `(ordinal - 1) % 7 < 5`).
    fn weekday_flags(start: i64, len: usize) -> Vec<bool> {
        (0..len as i64).map(|i| (start + i - 1) % 7 < 5).collect()
    }

    /// Monday anchor ordinal: 738886 = 2024-01-01 (a Monday).
    const MON: i64 = 738_886;

    #[allow(clippy::too_many_arguments)] // The law's parameter surface 1:1.
    fn classify(
        last_bar: Option<i64>,
        window_end: i64,
        today: i64,
        expiry: Option<i64>,
        coverage_ok: bool,
        tolerance: i64,
        live_edge: i64,
        span_start: i64,
        flags: &[bool],
    ) -> Option<PartialWindowFields> {
        let grid = SessionGrid::new(span_start, flags);
        classify_partial_window_impl(
            last_bar,
            window_end,
            today,
            expiry,
            coverage_ok,
            tolerance,
            live_edge,
            &grid,
        )
    }

    #[test]
    fn empty_window_short_circuits_before_everything() {
        // No grid, coverage_ok=false — neither is consulted (arm order).
        assert_eq!(
            classify(None, MON + 30, MON + 60, None, false, 5, 3, 0, &[]),
            Some((REASON_EMPTY, None, None, 0))
        );
    }

    #[test]
    fn coverage_abstain_when_flag_false() {
        assert_eq!(
            classify(Some(MON), MON + 30, MON + 60, None, false, 5, 3, 0, &[]),
            Some((REASON_OUT_OF_COVERAGE, Some(MON), None, 0))
        );
    }

    #[test]
    fn truncated_window_flagged() {
        // last bar Mon; window_end 5 weeks out, today well past — missing
        // sessions >> tolerance 5.
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        let window_end = MON + 28; // 4 calendar weeks later (a Monday)
        let today = MON + 42;
        let got = classify(
            Some(MON),
            window_end,
            today,
            None,
            true,
            5,
            3,
            span_start,
            &flags,
        );
        // end_exclusive = MON+27 (a Sunday) → expected_end = MON+25 (Friday).
        // missing = sessions in (MON, MON+25] = 19 > 5 → truncated.
        assert_eq!(got, Some((REASON_TRUNCATED, Some(MON), Some(MON + 25), 19)));
    }

    #[test]
    fn complete_to_bound_not_flagged() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        // last bar == expected end (Friday MON+4); window_end Saturday.
        let got = classify(
            Some(MON + 4),
            MON + 6,
            MON + 42,
            None,
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(
            got,
            Some((REASON_COMPLETE, Some(MON + 4), Some(MON + 4), 0))
        );
    }

    #[test]
    fn sparse_within_tolerance_not_flagged() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        // last bar Wednesday MON+2, expected end Friday MON+4 → missing 2 ≤ 5.
        let got = classify(
            Some(MON + 2),
            MON + 6,
            MON + 42,
            None,
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(
            got,
            Some((REASON_SPARSE_COMPLETE, Some(MON + 2), Some(MON + 4), 2))
        );
    }

    #[test]
    fn live_edge_not_flagged() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        // expected end Friday MON+4; today the following Monday MON+7 →
        // sessions_between(F, M) = 1 < live_edge 3 → live_edge.
        let got = classify(
            Some(MON),
            MON + 6,
            MON + 7,
            None,
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(got, Some((REASON_LIVE_EDGE, Some(MON), Some(MON + 4), 0)));
    }

    #[test]
    fn at_expiry_not_flagged() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        // expiry Friday MON+4; last bar ON expiry session; window_end far
        // past; today far past → at_expiry.
        let got = classify(
            Some(MON + 4),
            MON + 28,
            MON + 42,
            Some(MON + 4),
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(
            got,
            Some((REASON_AT_EXPIRY, Some(MON + 4), Some(MON + 4), 0))
        );
    }

    #[test]
    fn expiry_clamps_expected_end_but_still_truncated_before_it() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 120);
        // expiry Friday MON+25; last bar MON; window_end far past expiry;
        // today far past → expected_end clamps to MON+25;
        // missing = 19 > 5 → truncated (the expiry clamp does not excuse
        // an interiorly-truncated window).
        let got = classify(
            Some(MON),
            MON + 56,
            MON + 70,
            Some(MON + 25),
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(got, Some((REASON_TRUNCATED, Some(MON), Some(MON + 25), 19)));
    }

    #[test]
    fn window_end_past_today_clamps_to_today() {
        let span_start = MON - 10;
        let flags = weekday_flags(span_start, 80);
        // window_end far future; today Friday MON+25 → end_exclusive clamps
        // to today; expected_end = MON+25; today_session = MON+25 →
        // sessions_between = 0 < 3 → live_edge.
        let got = classify(
            Some(MON),
            MON + 300,
            MON + 25,
            None,
            true,
            5,
            3,
            span_start,
            &flags,
        );
        assert_eq!(got, Some((REASON_LIVE_EDGE, Some(MON), Some(MON + 25), 0)));
    }

    #[test]
    fn off_grid_walks_abstain() {
        // A grid too short for the expected-end back-walk → None.
        assert_eq!(
            classify(Some(MON), MON + 28, MON + 42, None, true, 5, 3, MON, &[]),
            None
        );
        // A grid of all-false flags: the back-walk descends off the floor.
        let flags = vec![false; 10];
        assert_eq!(
            classify(Some(MON), MON + 8, MON + 8, None, true, 5, 3, MON, &flags),
            None
        );
    }

    #[test]
    fn grid_helpers_match_python_twins() {
        let flags = weekday_flags(MON, 21);
        let grid = SessionGrid::new(MON, &flags);
        // A Monday is its own last completed session.
        assert_eq!(grid.last_session_on_or_before(MON), Some(MON));
        // A Sunday rolls back to Friday.
        assert_eq!(grid.last_session_on_or_before(MON + 6), Some(MON + 4));
        // sessions_between counts only trading days; (Mon, next Mon] = 5.
        assert_eq!(grid.sessions_between(MON, MON + 7), Some(5));
        // end <= start → 0 (never a walk, even off-grid bounds).
        assert_eq!(grid.sessions_between(MON + 7, MON), Some(0));
        // Walking past the grid tail abstains.
        assert_eq!(grid.sessions_between(MON, MON + 100), None);
        // A day below the grid floor abstains.
        assert_eq!(grid.is_session(MON - 1), None);
    }
}
