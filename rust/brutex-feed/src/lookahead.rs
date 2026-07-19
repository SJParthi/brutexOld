//! The P-06 look-ahead barrier STATE MACHINE — `DEC-FEED-RUST-MATERIALIZE-002`.
//!
//! [`BarrierState`] is the pure-Rust twin of the deleted Python body of
//! `brutex/feed/lookahead.py::LookAheadBarrier`: one forward-only cursor
//! (the current bar index `N`), verdict-coded validation of every
//! `advance_to` / `access`, and the optional touched-index trail. The
//! Python class survives as a thin delegating wrapper that maps each
//! verdict to the EXACT pre-existing exception type + message
//! (`LookAheadError` / `ValueError` — exception materialization never
//! crosses the FFI, the house integers-only discipline), so every pinned
//! message byte and the exception TYPE are preserved by construction.
//!
//! ## Verdict vocabulary (the FFI contract — u8 codes, never exceptions)
//!
//! * [`ADVANCE_OK`] / [`ADVANCE_NEGATIVE`] / [`ADVANCE_BACKWARDS`] — the
//!   `advance_to` outcomes, in the deleted oracle's check order
//!   (negative FIRST, then backwards).
//! * [`ACCESS_OK`] / [`ACCESS_NO_CURSOR`] / [`ACCESS_NEGATIVE`] /
//!   [`ACCESS_LOOKAHEAD`] — the `access` outcomes, in the deleted
//!   oracle's check order (no-cursor FIRST — even a negative index before
//!   the first `advance_to` reads "no bar reached yet"; then negative;
//!   then the `index > N` look-ahead).
//!
//! ## O(1) discipline (CLAUDE.md Rule #5 — unchanged claims)
//!
//! Every `advance_to` / `access` is O(1) per operation — integer
//! comparisons plus (record mode only) one `Vec` push. `record == false`
//! holds O(1) MEMORY (the always-on sweep-guard mode); `record == true`
//! retains the trail for the bounded manual recheck + property tests.
//! Not an O(1)-total claim: a full sweep touches Σ bars.

/// `advance_to` verdict: cursor advanced (or held equal — re-evaluating
/// the same bar is legal).
pub const ADVANCE_OK: u8 = 0;
/// `advance_to` verdict: the index is negative (Python raises
/// `ValueError("advance_to: bar index must be >= 0, got …")`).
pub const ADVANCE_NEGATIVE: u8 = 1;
/// `advance_to` verdict: the cursor would move backwards (Python raises
/// `ValueError("advance_to: cursor cannot move backwards …")`).
pub const ADVANCE_BACKWARDS: u8 = 2;

/// `access` verdict: the read is permitted (recorded in record mode).
pub const ACCESS_OK: u8 = 0;
/// `access` verdict: no bar reached yet (`cursor == -1`; Python raises
/// `LookAheadError("no bar reached yet …")`).
pub const ACCESS_NO_CURSOR: u8 = 1;
/// `access` verdict: the index is negative (Python raises
/// `ValueError("access: bar index must be >= 0, got …")`).
pub const ACCESS_NEGATIVE: u8 = 2;
/// `access` verdict: the index is strictly past the cursor (Python raises
/// `LookAheadError("… look-ahead read forbidden (P-06 / FR-INT-004)")`).
pub const ACCESS_LOOKAHEAD: u8 = 3;

/// The pure barrier core — cursor + optional touched-index trail.
#[derive(Debug, Clone)]
pub struct BarrierState {
    record: bool,
    current_index: i64,
    touched: Vec<i64>,
}

impl BarrierState {
    /// A just-constructed barrier: cursor `-1`, empty trail.
    #[must_use]
    pub fn new(record: bool) -> Self {
        Self {
            record,
            current_index: -1,
            touched: Vec::new(),
        }
    }

    /// Current bar index `N` (`-1` before the first successful advance).
    #[must_use]
    pub fn current_index(&self) -> i64 {
        self.current_index
    }

    /// Whether this barrier retains the touched-index trail.
    #[must_use]
    pub fn records(&self) -> bool {
        self.record
    }

    /// Every *permitted* index passed to [`Self::access`], in call order.
    /// Empty when `record == false` (validated but not retained).
    #[must_use]
    pub fn touched(&self) -> &[i64] {
        &self.touched
    }

    /// Largest *permitted* index touched, or `None` when nothing was
    /// recorded.
    #[must_use]
    pub fn max_touched(&self) -> Option<i64> {
        self.touched.iter().copied().max()
    }

    /// Advance the forward-only cursor — check order: negative FIRST,
    /// then backwards (an equal move is legal). The cursor moves ONLY on
    /// [`ADVANCE_OK`].
    pub fn advance_to(&mut self, index: i64) -> u8 {
        if index < 0 {
            return ADVANCE_NEGATIVE;
        }
        if index < self.current_index {
            return ADVANCE_BACKWARDS;
        }
        self.current_index = index;
        ADVANCE_OK
    }

    /// Validate (and in record mode retain) a read of bar `index` — check
    /// order: no-cursor FIRST, then negative, then the `index > N`
    /// look-ahead. The trail records ONLY a permitted access (the
    /// `DEC-LOOKAHEAD-HONESTY-001` law: a rejected future index never
    /// poisons `touched` / `max_touched`).
    pub fn access(&mut self, index: i64) -> u8 {
        if self.current_index < 0 {
            return ACCESS_NO_CURSOR;
        }
        if index < 0 {
            return ACCESS_NEGATIVE;
        }
        if index > self.current_index {
            return ACCESS_LOOKAHEAD;
        }
        if self.record {
            self.touched.push(index);
        }
        ACCESS_OK
    }

    /// Restore the just-constructed form (cursor `-1`, empty trail).
    pub fn reset(&mut self) {
        self.current_index = -1;
        self.touched.clear();
    }
}
