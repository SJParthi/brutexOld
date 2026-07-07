//! Resident Stage-2 trade-simulation kernel — the hot trade-walk in Rust.
//!
//! DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). The Rust port of the Python
//! `brutex.engine.trade_sim.TradeSimWalker` state machine, fused with the per-mask
//! entry-signal projection, over a **resident** bar window. It is the next hop
//! after [`crate::python::ResidentBars`] (which holds the per-bar u64 condition
//! bitset for the Stage-2 SUPERSET count): this kernel additionally holds the OHLC
//! (int64 paisa) + the precomputed 15:20-forced-exit flags, so a whole grid of
//! strategy masks is simulated WITHOUT re-marshalling the bar window per combo.
//!
//! ## Why a Rust port
//!
//! `TradeSimWalker.push` is O(1) per bar, but the per-combo sweep runs it ~33
//! billion times; the interpreter overhead per bar dominates the measured ~43-91
//! combos/s end-to-end (DEC-SWEEP-RESIDENT-KERNEL-001 honest envelope). Moving the
//! per-bar state machine into a branch-predictable Rust loop over resident
//! `Vec<i64>` price columns removes that interpreter tax. The bars cross the FFI
//! boundary ONCE (at construction); thereafter only the (small) mask list crosses
//! per `simulate_masks` call.
//!
//! ## Bit-exactness (Golden Rule #4)
//!
//! The price→paisa conversion (`_price_to_paisa`, `ROUND_HALF_UP`), the
//! timeframe-aware 15:20-IST forced-exit detection (`is_forced_exit_bar`) and the
//! per-bar IST session identity (the bar's IST calendar date — the same key the
//! session forced-exit resolution uses) stay in Python — the single source of
//! truth (CLAUDE.md §11 COMMON) — and their pre-computed results
//! (`open/high/low/close` paisa + `is_forced` flags + `session_idx` day ordinals)
//! are handed to this kernel. The kernel therefore depends on NEITHER the Decimal
//! rounding rule NOR the IST timezone math (the functions a port would most
//! easily get wrong); it reproduces only the Python state machine's branch order,
//! which it does EXACTLY. For the same `(bars, mask, direction, sl, tp, policy)`
//! it emits byte-identical trades to `simulate_trades(...)`; a golden cross-check
//! asserts this field-by-field over hypothesis-generated inputs.
//!
//! ## No cross-session pending entry (R2-OC-2)
//!
//! A pending entry may only fill on a bar of the SAME session (`session_idx`
//! equal) it armed on. When the feed steps across a trading-day boundary — the
//! armed bar was a session's last stored bar — the fill bar's `session_idx`
//! differs and the pending entry is DROPPED, never filled at the next session's
//! open (that would be an ~18h-stale fill capturing the overnight gap at zero
//! slippage). Mirrors the Python walker's `_pending_entry_day` gate exactly.
//! The session identity of bar `i` derives from bar `i`'s own timestamp, so it
//! is knowable at bar `i` (P-06: no look-ahead).
//!
//! ## Exit precedence (the traps, from the hostile port review)
//!
//! Per managed bar the order is **pending-next-open → forced-15:20 → SL/TP →
//! Mode-B signal → still-open**. A pending next-open exit fills BEFORE forced is
//! even checked (so "forced overrides everything" is true only relative to
//! SL/TP/signal, NOT to an already-pending exit). The three exit kinds fold
//! excursion differently: a normal bar folds the full `(high, low)`; a
//! pending/forced exit folds only `(open, open)`; the end-of-data close folds
//! nothing. `finish()` silently closes a still-pending exit at the last bar's
//! CLOSE (not the would-be next open). All reproduced verbatim below.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! * Construction is `O(N_bars)` — the one-time copy of the 5 price columns +
//!   flags + bar_bits, paid ONCE.
//! * [`ResidentSweepSim::simulate_masks`] is `O(N_masks · N_bars)` compute (each
//!   mask walks the whole window once) + `O(Σ trades)` output. The per-bar work is
//!   O(1); the per-combo work is O(N_bars); the whole sweep is inherently
//!   O(N_masks · N_bars) — claiming O(1) total is a physical impossibility and is
//!   FORBIDDEN (Golden Rule #10).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// How a stop-loss / take-profit trigger is priced once a bar touches it.
/// Mirrors `brutex.engine.trade_sim.LevelFill`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum LevelFill {
    /// Fill exactly at the trigger (SL / TP) price — the conservative default.
    AtLevel,
    /// Fill at the NEXT bar's OPEN. Falls back to the last bar's CLOSE at end of
    /// data (no next bar) via `finish`.
    NextOpen,
}

/// Which leg fills first when ONE bar's range spans BOTH the stop and target.
/// Mirrors `brutex.engine.trade_sim.SameBarTie`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum SameBarTie {
    /// Pessimistic — assume the stop filled first (worst case). The default.
    SlFirst,
    /// Optimistic — assume the target filled first.
    TpFirst,
}

/// The configurable spot-fill conventions (mirrors `FillPolicy`).
#[derive(Clone, Copy, Debug)]
struct FillPolicy {
    stop_loss: LevelFill,
    take_profit: LevelFill,
    same_bar: SameBarTie,
}

/// One emitted round-trip trade. Field order matches the Python `ClosedTrade`
/// numeric payload; `entry_idx` / `exit_idx` are bar indices the Python wrapper
/// maps to `entry_ts` / `exit_ts` via the bar timestamps. `direction` is a
/// per-batch constant (not stored per trade).
///
/// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
pub type TradeRecord = (i64, i64, i64, u64, u64, u64);

/// Mutable per-trade state while a position is open (int64 paisa). Mirrors the
/// Python `_OpenPosition`; `last_bar` is replaced by `last_idx` since the kernel
/// holds the resident price columns and reads `close[last_idx]` directly.
#[derive(Clone, Copy)]
struct OpenPosition {
    entry_idx: usize,
    entry_price_paisa: i64,
    sl_level_paisa: Option<i64>,
    tp_level_paisa: Option<i64>,
    mfe_paisa: i64,
    mae_paisa: i64,
    last_idx: usize,
}

/// Below this many `mask · bar` ops, run the batch SERIALLY. Mirrors the supermask
/// kernel's `PARALLEL_WORK_THRESHOLD` (DEC-RUST-RAYON-SUPERMASK-001) so the
/// small-N hot path stays serial (O(1) canary protection) and only genuinely
/// large Stage-2 batches cross into the rayon-parallel arm.
const PARALLEL_WORK_THRESHOLD: u64 = 1 << 20;

/// Overflow-safe magnitude ceiling for EVERY paisa value crossing the FFI
/// boundary into this kernel — the four OHLC price columns (validated at
/// [`ResidentSweepSim::build`]) AND the SL/TP distances (validated at the
/// `simulate_masks` / `count_trades` entry) — R2-RC-F1.
///
/// Mirrors the sibling trading-core `MAX_ANCHOR_PAISA` idiom
/// (`rust/trading-core/src/python.rs`): the walker does raw i64 add/sub that,
/// under the crate's `overflow-checks = true` + `panic = "abort"` release
/// profile, would `abort()` the WHOLE host interpreter on overflow (SIGABRT,
/// exit 134 — uncatchable from Python). Constraining every boundary input to
/// `C = i64::MAX / 8` makes ALL of the hot-loop arithmetic provably
/// overflow-free, so the loop stays UNGUARDED (no per-bar checked ops) with
/// the guarantee carried entirely by this boundary validation:
///
/// * [`Walker::levels`] — `entry ± d` with `|entry| ≤ C` (a bar open) and
///   `0 ≤ d ≤ C` → `|level| ≤ 2·C`.
/// * [`Walker::update_excursion`] — `high − entry` / `entry − low` (or the
///   `(open, open)` fold) with both operands `|x| ≤ C` → magnitude `≤ 2·C`;
///   the running-max compares fold no further arithmetic.
/// * [`Walker::close`] — `pnl = exit − entry` where `exit` is a bar open
///   (`≤ C`), a bar close (`≤ C`), or an SL/TP level (`≤ 2·C`) →
///   `|pnl| ≤ 3·C`.
///
/// The worst intermediate is therefore `3 · (i64::MAX / 8) < i64::MAX`
/// (3 operands × max magnitude `C`, with `8·C ≤ i64::MAX`) — provably
/// panic-free. The ceiling is ≈ 1.15·10¹⁸ paisa = ₹1.15·10¹⁶, astronomically
/// above any real Indian price in paisa (SENSEX ≈ 8·10⁶), so NO valid input
/// is ever rejected and every existing golden stays byte-identical.
const MAX_PRICE_PAISA: i64 = i64::MAX / 8;

/// Validate one boundary price column against [`MAX_PRICE_PAISA`]. Pure
/// (`Result<_, String>`, NO `PyErr`) so the no-libpython `cargo test` link
/// exercises every branch; the PyO3 wrappers map the error to `ValueError`.
fn check_price_column(name: &str, column: &[i64]) -> Result<(), String> {
    for &v in column {
        if !(-MAX_PRICE_PAISA..=MAX_PRICE_PAISA).contains(&v) {
            return Err(format!(
                "ResidentSweepSim: {name} value {v} out of the overflow-safe range \
                 [-{MAX_PRICE_PAISA}, {MAX_PRICE_PAISA}] paisa"
            ));
        }
    }
    Ok(())
}

/// Validate an optional SL/TP distance (paisa) at the batch-entry FFI
/// boundary: non-negative (a negative stop/target DISTANCE is nonsensical —
/// same stance as trading-core's bps guard) and `≤` [`MAX_PRICE_PAISA`] so
/// [`Walker::levels`]'s `entry ± d` is bounded per the ceiling's derivation.
/// `None` (leg disabled) is always valid. Pure — see [`check_price_column`].
fn check_level_distance(name: &str, distance: Option<i64>) -> Result<(), String> {
    if let Some(d) = distance {
        if !(0..=MAX_PRICE_PAISA).contains(&d) {
            return Err(format!(
                "ResidentSweepSim: {name} ({d}) out of the overflow-safe range \
                 [0, {MAX_PRICE_PAISA}] paisa"
            ));
        }
    }
    Ok(())
}

/// Pure-Rust faithful port of `TradeSimWalker` — a forward-only per-bar state
/// machine. No look-ahead: at bar `i` it reads only `open/high/low/close[i]` and
/// `is_forced[i]` (plus the entry signal derived from the resident `bar_bits[i]`).
struct Walker {
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
    policy: FillPolicy,
    position: Option<OpenPosition>,
    pending_entry: bool,
    /// The `session_idx` of the bar the pending entry ARMED on — only meaningful
    /// while `pending_entry` is true. A pending entry may only fill on a bar of
    /// the SAME session; a differing fill-bar session drops it (R2-OC-2).
    /// Mirrors the Python walker's `_pending_entry_day`.
    pending_entry_session: i64,
    /// True iff a next-open exit (SL/TP NEXT_OPEN, or Mode-B signal) is scheduled.
    /// The Python walker stores the reason too, but the emitted trade carries no
    /// reason, so a bool is sufficient + bit-identical.
    pending_exit: bool,
}

impl Walker {
    fn new(is_buy: bool, sl_paisa: Option<i64>, tp_paisa: Option<i64>, policy: FillPolicy) -> Self {
        Self {
            is_buy,
            sl_paisa,
            tp_paisa,
            policy,
            position: None,
            pending_entry: false,
            pending_entry_session: 0,
            pending_exit: false,
        }
    }

    /// (sl_level, tp_level) in paisa for the configured direction — mirrors
    /// `_levels`. BUY: stop below / target above; SELL: stop above / target below.
    ///
    /// Deliberately UNGUARDED raw `±` on the hot path: `|entry_price| ≤ C` (a
    /// boundary-validated bar open) and `0 ≤ d ≤ C`, so `|entry ± d| ≤ 2·C <
    /// i64::MAX` — see the [`MAX_PRICE_PAISA`] derivation (R2-RC-F1).
    fn levels(&self, entry_price: i64) -> (Option<i64>, Option<i64>) {
        if self.is_buy {
            (
                self.sl_paisa.map(|d| entry_price - d),
                self.tp_paisa.map(|d| entry_price + d),
            )
        } else {
            (
                self.sl_paisa.map(|d| entry_price + d),
                self.tp_paisa.map(|d| entry_price - d),
            )
        }
    }

    /// Open a position at `open[idx]` (the next-bar-open entry fill). Mirrors
    /// `_open_position`. No excursion update at open — the entry bar's high/low is
    /// folded by the same-bar `manage_position` call (step 3).
    fn open_position(&mut self, open_paisa: i64, idx: usize) {
        let (sl_level, tp_level) = self.levels(open_paisa);
        self.position = Some(OpenPosition {
            entry_idx: idx,
            entry_price_paisa: open_paisa,
            sl_level_paisa: sl_level,
            tp_level_paisa: tp_level,
            mfe_paisa: 0,
            mae_paisa: 0,
            last_idx: idx,
        });
    }

    /// Fold a `(high, low)` pair into the running MFE/MAE — mirrors
    /// `_update_excursion`. Both stay `>= 0` (max-accumulated from the 0 floor).
    ///
    /// Deliberately UNGUARDED raw `−` on the hot path: every operand is a
    /// boundary-validated bar price (`|x| ≤ C`), so each difference is `≤ 2·C
    /// < i64::MAX`; the running-max compares do no further arithmetic — see
    /// the [`MAX_PRICE_PAISA`] derivation (R2-RC-F1).
    fn update_excursion(pos: &mut OpenPosition, is_buy: bool, high_paisa: i64, low_paisa: i64) {
        let (favourable, adverse) = if is_buy {
            (
                high_paisa - pos.entry_price_paisa,
                pos.entry_price_paisa - low_paisa,
            )
        } else {
            (
                pos.entry_price_paisa - low_paisa,
                high_paisa - pos.entry_price_paisa,
            )
        };
        // `.max()` instead of `if new > acc` — behavior-identical for i64
        // (a tie assigns the same value); removes the tie-equivalent `>`→`>=`
        // cargo-mutants class structurally (DEC-MUTATION-RUNTIME-003; the
        // exact-value excursion tests below + the Python↔Rust parity suites
        // are the net).
        pos.mfe_paisa = pos.mfe_paisa.max(favourable);
        pos.mae_paisa = pos.mae_paisa.max(adverse);
    }

    /// Decide which of SL/TP a bar's `[low, high]` touched (same-bar tie applied)
    /// — mirrors `_resolve_level_hit`. Returns `(is_stop, level_paisa)` or `None`.
    /// Operators are inclusive (touching the level exactly is a hit).
    fn resolve_level_hit(
        &self,
        pos: &OpenPosition,
        high_paisa: i64,
        low_paisa: i64,
    ) -> Option<(bool, i64)> {
        let buy = self.is_buy;
        let stop: Option<(bool, i64)> = pos.sl_level_paisa.and_then(|sl| {
            let hit = if buy {
                low_paisa <= sl
            } else {
                high_paisa >= sl
            };
            if hit { Some((true, sl)) } else { None }
        });
        let target: Option<(bool, i64)> = pos.tp_level_paisa.and_then(|tp| {
            let hit = if buy {
                high_paisa >= tp
            } else {
                low_paisa <= tp
            };
            if hit { Some((false, tp)) } else { None }
        });
        match (stop, target) {
            (Some(s), Some(t)) => Some(if self.policy.same_bar == SameBarTie::SlFirst {
                s
            } else {
                t
            }),
            (Some(s), None) => Some(s),
            (None, t) => t,
        }
    }

    /// Build the trade record + flatten the walker — mirrors `_close`.
    ///
    /// Deliberately UNGUARDED raw `−` on the hot path: `|entry| ≤ C` (a
    /// boundary-validated bar open) and `|exit| ≤ 2·C` (a bar open/close ≤ C,
    /// or an SL/TP level ≤ 2·C from [`Walker::levels`]), so `|pnl| ≤ 3·C <
    /// i64::MAX` — see the [`MAX_PRICE_PAISA`] derivation (R2-RC-F1).
    fn close(&mut self, pos: OpenPosition, exit_price_paisa: i64, exit_idx: usize) -> TradeRecord {
        let pnl = if self.is_buy {
            exit_price_paisa - pos.entry_price_paisa
        } else {
            pos.entry_price_paisa - exit_price_paisa
        };
        self.position = None;
        let holding = holding_bars(pos.entry_idx, exit_idx);
        (
            pnl,
            pos.mfe_paisa,
            pos.mae_paisa,
            holding,
            pos.entry_idx as u64,
            exit_idx as u64,
        )
    }

    /// Process one in-position bar — mirrors `_manage_position`. `pos` is taken by
    /// value (the caller `take()`s it from `self.position`); still-open branches
    /// write it back, closing branches consume it. Precedence is EXACTLY the
    /// Python order.
    fn manage_position(
        &mut self,
        mut pos: OpenPosition,
        open_paisa: i64,
        high_paisa: i64,
        low_paisa: i64,
        idx: usize,
        forced: bool,
        exit_signal: bool,
    ) -> Option<TradeRecord> {
        pos.last_idx = idx;

        // (1) A next-open exit scheduled on a prior bar fills at THIS bar's open —
        // BEFORE forced is checked (pending beats forced).
        if self.pending_exit {
            self.pending_exit = false;
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, open_paisa, idx));
        }

        // (2) Forced 15:20 IST exit overrides SL/TP/signal — fill at the open.
        if forced {
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, open_paisa, idx));
        }

        // (3) Full-range in-position bar: fold its high/low into MFE/MAE.
        Self::update_excursion(&mut pos, self.is_buy, high_paisa, low_paisa);

        // (4) Stop-loss / take-profit (per-leg fill policy).
        if let Some((is_stop, level_paisa)) = self.resolve_level_hit(&pos, high_paisa, low_paisa) {
            let fill = if is_stop {
                self.policy.stop_loss
            } else {
                self.policy.take_profit
            };
            if fill == LevelFill::AtLevel {
                return Some(self.close(pos, level_paisa, idx));
            }
            // NEXT_OPEN — fill on the next bar's open.
            self.position = Some(pos);
            self.pending_exit = true;
            return None;
        }

        // (5) Mode-B signal exit — fill on the next bar's open.
        if exit_signal {
            self.position = Some(pos);
            self.pending_exit = true;
            return None;
        }

        // (6) Still open — persist the mutated excursion + last_idx.
        self.position = Some(pos);
        None
    }

    /// Advance one bar — mirrors `push`. Returns a trade iff a position closed.
    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        open_paisa: i64,
        high_paisa: i64,
        low_paisa: i64,
        idx: usize,
        forced_raw: bool,
        session: i64,
        entry_signal: bool,
        exit_signal: bool,
    ) -> Option<TradeRecord> {
        // forced is only consulted when opening a pending entry or managing an
        // open position (Python skips it on a flat, no-pending bar).
        let need_forced = self.position.is_some() || self.pending_entry;
        let forced = need_forced && forced_raw;

        // (A) Open a pending entry at THIS bar's open — only within the SAME
        // session it armed on (a fill bar on a LATER session is an overnight-gap
        // fill and is DROPPED, R2-OC-2) and never on a forced bar (cannot open
        // at/after the session's forced-exit instant). The pending flag is
        // cleared UNCONDITIONALLY. Mirrors the Python (A) branch exactly.
        if self.position.is_none() && self.pending_entry {
            self.pending_entry = false;
            let same_session = session == self.pending_entry_session;
            if !forced && same_session {
                self.open_position(open_paisa, idx);
            }
        }

        // (B) Manage an open position on this bar (including one just opened in A).
        let mut closed: Option<TradeRecord> = None;
        if let Some(pos) = self.position.take() {
            closed = self.manage_position(
                pos,
                open_paisa,
                high_paisa,
                low_paisa,
                idx,
                forced,
                exit_signal,
            );
        }

        // (C) While flat (and NOT on a bar a trade just closed — the exit bar
        // consumes the entry signal), arm the next entry, recording THIS bar's
        // session — the fill gate for (A). Knowable at bar `idx` (no look-ahead).
        if self.position.is_none() && closed.is_none() && entry_signal {
            self.pending_entry = true;
            self.pending_entry_session = session;
        }

        closed
    }

    /// Close any still-open position at the last managed bar's CLOSE — mirrors
    /// `finish`. A still-pending next-open exit is IGNORED here (closed at CLOSE,
    /// not the would-be open); no excursion fold.
    fn finish(&mut self, close_at: impl Fn(usize) -> i64) -> Option<TradeRecord> {
        let pos = self.position.take()?;
        let exit_price = close_at(pos.last_idx);
        Some(self.close(pos, exit_price, pos.last_idx))
    }
}

/// Inclusive holding length in bars — `exit_idx − entry_idx`, plus one.
///
/// The forward-only walker always closes at or after the entry bar, so
/// `exit_idx >= entry_idx` is an invariant and for every real call this equals
/// the old `exit_idx − entry_idx + 1` byte-for-byte. The subtract uses
/// `saturating_sub` purely as a **defensive guard**: were a future refactor to
/// call `close` with `exit_idx < entry_idx`, the old unsigned subtract would
/// underflow and — under this crate's `overflow-checks = true` plus
/// `panic = "abort"` profile — `abort()` the process. `saturating_sub` instead
/// clamps the difference to zero, yielding a defined minimum holding of one bar
/// and never a panic. Valid inputs are unaffected, so the existing trade-sim
/// tests stay bit-identical.
#[inline]
#[must_use]
fn holding_bars(entry_idx: usize, exit_idx: usize) -> u64 {
    (exit_idx.saturating_sub(entry_idx) as u64) + 1
}

/// Build the policy from the three boolean flags shared by the batch methods.
fn policy_from_flags(sl_next_open: bool, tp_next_open: bool, tp_first: bool) -> FillPolicy {
    FillPolicy {
        stop_loss: if sl_next_open {
            LevelFill::NextOpen
        } else {
            LevelFill::AtLevel
        },
        take_profit: if tp_next_open {
            LevelFill::NextOpen
        } else {
            LevelFill::AtLevel
        },
        same_bar: if tp_first {
            SameBarTie::TpFirst
        } else {
            SameBarTie::SlFirst
        },
    }
}

/// Resident bar window for the Stage-2 trade-sim sweep — OHLC paisa + forced flags
/// + the per-bar condition bitset, held Rust-side so a whole mask grid is
/// simulated without re-marshalling the window per combo.
///
/// See the module docs for the bit-exactness + honest-envelope contracts.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepSim {
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    /// Per-bar session identity — the bar's IST calendar-date ordinal, computed
    /// in Python from bar `i`'s own timestamp (P-06: no look-ahead). A pending
    /// entry only fills on a bar with the SAME value it armed on (R2-OC-2).
    session_idx: Vec<i64>,
    bar_bits: Vec<u64>,
}

impl ResidentSweepSim {
    /// Pure constructor returning a plain `Result<_, String>` (NO `PyErr`), so the
    /// `#[cfg(test)]` suite exercises the length validation WITHOUT pulling Python
    /// C-API symbols into the no-libpython `cargo test` link (mirrors
    /// `python::count_supermasks_scoped`). The `#[new]` wrapper maps the error.
    fn build(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> Result<Self, String> {
        let n = open.len();
        if high.len() != n
            || low.len() != n
            || close.len() != n
            || is_forced.len() != n
            || session_idx.len() != n
            || bar_bits.len() != n
        {
            return Err(format!(
                "ResidentSweepSim: column length mismatch — open={}, high={}, low={}, close={}, \
                 is_forced={}, session_idx={}, bar_bits={} (all must be equal)",
                n,
                high.len(),
                low.len(),
                close.len(),
                is_forced.len(),
                session_idx.len(),
                bar_bits.len(),
            ));
        }
        // R2-RC-F1 overflow boundary: every paisa price must sit inside
        // [-MAX_PRICE_PAISA, MAX_PRICE_PAISA] so the walker's raw i64 add/sub
        // is provably panic-free (see the ceiling's derivation doc). One-time
        // O(N_bars) scan at construction — NOT on the hot loop. `is_forced`
        // (bool), `bar_bits` (u64, only `&`-compared against masks) and
        // `session_idx` (i64 day ordinal, only `==`-compared for the R2-OC-2
        // same-session entry gate) do no arithmetic and need no bound.
        check_price_column("open", &open)?;
        check_price_column("high", &high)?;
        check_price_column("low", &low)?;
        check_price_column("close", &close)?;
        Ok(Self {
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits,
        })
    }

    /// Simulate ONE mask over the resident window — the pure-Rust core both the
    /// PyO3 methods and the `#[cfg(test)]` suite call. Entry signal for bar `i` is
    /// `(bar_bits[i] & mask) == mask`; there is no Mode-B exit-signal stream on the
    /// sweep path (it matches `metrics_for_mask`, which passes none).
    fn simulate_one(
        &self,
        mask: u64,
        is_buy: bool,
        sl: Option<i64>,
        tp: Option<i64>,
        policy: FillPolicy,
    ) -> Vec<TradeRecord> {
        let n = self.open.len();
        let mut walker = Walker::new(is_buy, sl, tp, policy);
        let mut trades: Vec<TradeRecord> = Vec::new();
        for i in 0..n {
            let entry_signal = (self.bar_bits[i] & mask) == mask;
            if let Some(t) = walker.push(
                self.open[i],
                self.high[i],
                self.low[i],
                i,
                self.is_forced[i],
                self.session_idx[i],
                entry_signal,
                false,
            ) {
                trades.push(t);
            }
        }
        if let Some(t) = walker.finish(|idx| self.close[idx]) {
            trades.push(t);
        }
        trades
    }

    /// Pure core of [`Self::simulate_masks`] — validates the SL/TP distances at
    /// the batch boundary (R2-RC-F1, see [`check_level_distance`] /
    /// [`MAX_PRICE_PAISA`]) then runs the serial/parallel sweep. `Result<_,
    /// String>` (NO `PyErr`) so the `#[cfg(test)]` suite exercises both the
    /// reject and compute branches without pulling Python C-API symbols into
    /// the no-libpython `cargo test` link. (The flat batch ABI mirrors the
    /// PyO3 method; `clippy::too_many_arguments` is allowed crate-wide.)
    fn simulate_masks_impl(
        &self,
        masks: &[u64],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
    ) -> Result<Vec<Vec<TradeRecord>>, String> {
        check_level_distance("sl_paisa", sl_paisa)?;
        check_level_distance("tp_paisa", tp_paisa)?;
        let policy = policy_from_flags(sl_next_open, tp_next_open, tp_first);
        let work = (masks.len() as u64).saturating_mul(self.open.len() as u64);
        // Dispatch via the COMMON crate predicate (DEC-MUTATION-RUNTIME-003):
        // both arms are output-identical by design, so the boundary is gated
        // by `dispatch_tests::parallel_dispatch_boundary_is_exclusive`.
        Ok(
            if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
                masks
                    .iter()
                    .map(|&m| self.simulate_one(m, is_buy, sl_paisa, tp_paisa, policy))
                    .collect()
            } else {
                masks
                    .par_iter()
                    .map(|&m| self.simulate_one(m, is_buy, sl_paisa, tp_paisa, policy))
                    .collect()
            },
        )
    }

    /// Pure core of [`Self::count_trades`] — same boundary validation + sweep
    /// as [`Self::simulate_masks_impl`], returning only the per-mask counts.
    fn count_trades_impl(
        &self,
        masks: &[u64],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
    ) -> Result<Vec<u64>, String> {
        check_level_distance("sl_paisa", sl_paisa)?;
        check_level_distance("tp_paisa", tp_paisa)?;
        let policy = policy_from_flags(sl_next_open, tp_next_open, tp_first);
        let work = (masks.len() as u64).saturating_mul(self.open.len() as u64);
        let run = |m: u64| {
            self.simulate_one(m, is_buy, sl_paisa, tp_paisa, policy)
                .len() as u64
        };
        // Same COMMON-predicate dispatch as `simulate_masks_impl`
        // (DEC-MUTATION-RUNTIME-003).
        Ok(
            if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
                masks.iter().map(|&m| run(m)).collect()
            } else {
                masks.par_iter().map(|&m| run(m)).collect()
            },
        )
    }
}

#[pymethods]
impl ResidentSweepSim {
    /// Build the resident buffer. The ONE-TIME `O(N_bars)` Python→Rust copy of the
    /// price columns + forced flags + per-bar session ordinals + bar_bits is paid
    /// here via the `Vec` extracts. `session_idx[i]` is bar `i`'s IST calendar-date
    /// ordinal (the session identity gating pending-entry fills, R2-OC-2). Unequal
    /// column lengths raise a clean `ValueError` (never a panic that would SIGABRT
    /// under `panic=abort`).
    #[new]
    fn new(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> PyResult<Self> {
        Self::build(open, high, low, close, is_forced, session_idx, bar_bits)
            .map_err(PyValueError::new_err)
    }

    /// Number of resident bars (the only exposed view of the buffer).
    #[getter]
    fn n_bars(&self) -> usize {
        self.open.len()
    }

    /// Simulate a BATCH of masks over the resident window → per-mask trade lists.
    ///
    /// `out[j]` is the list of round-trip trades for `masks[j]`, each a tuple
    /// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
    /// `is_buy` selects long (`true`) / short (`false`); `sl_paisa` / `tp_paisa`
    /// are the stop / target distances in paisa (`None` disables that leg). The
    /// three policy flags mirror `FillPolicy`: `sl_next_open` / `tp_next_open` fill
    /// the SL / TP leg at the next bar's open, and `tp_first` breaks a same-bar
    /// SL+TP tie in favour of the target (default `false` = pessimistic SL-first).
    ///
    /// Multi-core (DEC-RUST-RAYON-SUPERMASK-001 convention): masks are independent
    /// (each walks the shared read-only window), so above the work threshold the
    /// batch fans out across rayon's default global pool with an order-preserving
    /// `par_iter().map().collect()` — `out[j]` always aligns with `masks[j]` and
    /// each per-mask trade list is bit-identical to the serial walk (pure integer
    /// arithmetic; Golden Rule #4). Below the threshold it runs serially.
    ///
    /// Overflow boundary (R2-RC-F1): an `sl_paisa` / `tp_paisa` outside
    /// `[0, MAX_PRICE_PAISA]` raises a clean `ValueError` (never a panic →
    /// SIGABRT under `panic = "abort"`) — see [`MAX_PRICE_PAISA`].
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa, sl_next_open=false, tp_next_open=false, tp_first=false))]
    fn simulate_masks(
        &self,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
    ) -> PyResult<Vec<Vec<TradeRecord>>> {
        self.simulate_masks_impl(
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
        )
        .map_err(PyValueError::new_err)
    }

    /// Per-mask trade COUNT only — the throughput-bench fast path.
    ///
    /// Identical state machine to [`Self::simulate_masks`] but returns only the
    /// number of round-trip trades per mask (no per-trade allocation), so a
    /// throughput bench measures the pure mask-eval + trade-walk hot loop without
    /// the `O(Σ trades)` output marshalling. `out[j]` aligns with `masks[j]`.
    ///
    /// Overflow boundary (R2-RC-F1): same SL/TP validation as
    /// [`Self::simulate_masks`] — out-of-range distances raise `ValueError`.
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa, sl_next_open=false, tp_next_open=false, tp_first=false))]
    fn count_trades(
        &self,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
        sl_next_open: bool,
        tp_next_open: bool,
        tp_first: bool,
    ) -> PyResult<Vec<u64>> {
        self.count_trades_impl(
            &masks,
            is_buy,
            sl_paisa,
            tp_paisa,
            sl_next_open,
            tp_next_open,
            tp_first,
        )
        .map_err(PyValueError::new_err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy_default() -> FillPolicy {
        FillPolicy {
            stop_loss: LevelFill::AtLevel,
            take_profit: LevelFill::AtLevel,
            same_bar: SameBarTie::SlFirst,
        }
    }

    /// Build a sim with explicit per-column data, no forced bars, and every bar
    /// in ONE session (test helper — the single-session case is byte-identical
    /// to the pre-session-gate kernel).
    fn sim(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> ResidentSweepSim {
        let n = open.len();
        ResidentSweepSim::build(open, high, low, close, vec![false; n], vec![0; n], bar_bits)
            .expect("equal-length test data")
    }

    // ── holding-length guard (defensive checked-subtract) ──────────────────

    #[test]
    fn holding_bars_normal_is_inclusive() {
        // The invariant case: exit at or after entry → inclusive bar count,
        // byte-identical to the old `exit_idx - entry_idx + 1`.
        assert_eq!(holding_bars(0, 0), 1); // same-bar entry+exit
        assert_eq!(holding_bars(3, 3), 1);
        assert_eq!(holding_bars(2, 5), 4); // 2..=5 inclusive
        assert_eq!(holding_bars(0, 99), 100);
    }

    #[test]
    fn holding_bars_inverted_does_not_underflow() {
        // Defensive: exit_idx < entry_idx (impossible from the public walker, but
        // a future mis-call) must NOT underflow→abort under overflow-checks; it
        // saturates to a defined minimum holding of 1 instead of panicking.
        assert_eq!(holding_bars(5, 2), 1);
        assert_eq!(holding_bars(usize::MAX, 0), 1);
    }

    // ── excursion (MFE/MAE) folding — exact values both directions ─────────

    fn pos_at(entry: i64) -> OpenPosition {
        OpenPosition {
            entry_idx: 0,
            entry_price_paisa: entry,
            sl_level_paisa: None,
            tp_level_paisa: None,
            mfe_paisa: 0,
            mae_paisa: 0,
            last_idx: 0,
        }
    }

    #[test]
    fn update_excursion_buy_exact_mfe_mae() {
        // BUY entered at 100; bar high=130, low=90.
        // favourable = high - entry = 30 ; adverse = entry - low = 10.
        let mut pos = pos_at(100);
        Walker::update_excursion(&mut pos, true, 130, 90);
        assert_eq!(pos.mfe_paisa, 30);
        assert_eq!(pos.mae_paisa, 10);
        // A strictly-smaller bar leaves MFE/MAE unchanged (the running max holds).
        Walker::update_excursion(&mut pos, true, 120, 95);
        assert_eq!(pos.mfe_paisa, 30);
        assert_eq!(pos.mae_paisa, 10);
        // A strictly larger excursion DOES update (kills the `>`->`<`/`==` family).
        Walker::update_excursion(&mut pos, true, 131, 89);
        assert_eq!(pos.mfe_paisa, 31);
        assert_eq!(pos.mae_paisa, 11);
    }

    #[test]
    fn update_excursion_sell_exact_mfe_mae() {
        // SELL entered at 100; bar high=130, low=90.
        // favourable = entry - low = 10 ; adverse = high - entry = 30.
        // (distinguishes the SHORT-branch `-` from `+`/`/` mutants.)
        let mut pos = pos_at(100);
        Walker::update_excursion(&mut pos, false, 130, 90);
        assert_eq!(pos.mfe_paisa, 10);
        assert_eq!(pos.mae_paisa, 30);
    }

    // ── resolve_level_hit — both SL and TP touch (buy + sell) ──────────────

    fn walker_with_pos(is_buy: bool, sl: Option<i64>, tp: Option<i64>) -> (Walker, OpenPosition) {
        let w = Walker::new(is_buy, None, None, policy_default());
        let mut pos = pos_at(100);
        pos.sl_level_paisa = sl;
        pos.tp_level_paisa = tp;
        (w, pos)
    }

    #[test]
    fn resolve_level_hit_buy_sl_and_tp() {
        // BUY: SL hit when low <= sl; TP hit when high >= tp.
        let (w, pos) = walker_with_pos(true, Some(95), Some(110));
        // low touches SL exactly (inclusive >=/<=), high below TP → stop.
        assert_eq!(w.resolve_level_hit(&pos, 105, 95), Some((true, 95)));
        // high touches TP, low above SL → target.
        assert_eq!(w.resolve_level_hit(&pos, 110, 96), Some((false, 110)));
        // neither touched → None.
        assert_eq!(w.resolve_level_hit(&pos, 109, 96), None);
    }

    #[test]
    fn resolve_level_hit_sell_sl_and_tp() {
        // SELL: SL hit when high >= sl; TP hit when low <= tp.
        let (w, pos) = walker_with_pos(false, Some(110), Some(95));
        assert_eq!(w.resolve_level_hit(&pos, 110, 99), Some((true, 110))); // SL
        assert_eq!(w.resolve_level_hit(&pos, 105, 95), Some((false, 95))); // TP
        assert_eq!(w.resolve_level_hit(&pos, 109, 96), None);
    }

    // ── stress: a large window must not panic and is deterministic ─────────

    #[test]
    fn stress_large_window_no_panic_and_stable() {
        // A 20k-bar window with a saw-tooth price + per-bar forced-exit flags +
        // varied entry bits. The whole sweep must complete without tripping the
        // holding/overflow guards, and re-running it is bit-identical (Golden
        // Rule #7 idempotence — the sweep ranking must be reproducible).
        let n = 20_000usize;
        let open: Vec<i64> = (0..n as i64).map(|i| 10_000 + (i % 137) * 5).collect();
        let high: Vec<i64> = open.iter().map(|&o| o + 50).collect();
        let low: Vec<i64> = open.iter().map(|&o| o - 50).collect();
        let close: Vec<i64> = open.iter().map(|&o| o + 10).collect();
        let is_forced: Vec<bool> = (0..n).map(|i| i % 375 == 374).collect();
        // A realistic multi-session shape: one synthetic 375-bar day per session.
        let session_idx: Vec<i64> = (0..n).map(|i| (i / 375) as i64).collect();
        let bar_bits: Vec<u64> = (0..n as u64).map(|i| (i & 0x3) | 0x1).collect();
        let s = ResidentSweepSim::build(open, high, low, close, is_forced, session_idx, bar_bits)
            .expect("equal-length stress window");

        let policy = policy_default();
        let run = || s.simulate_one(0x1, true, Some(200), Some(400), policy);
        let first = run();
        let second = run();
        assert_eq!(first, second, "trade sweep must be deterministic");
        // Every record's holding is >= 1 (the inclusive guard) and exit >= entry.
        for &(_, _, _, holding, entry, exit) in &first {
            assert!(holding >= 1);
            assert!(exit >= entry);
        }
    }

    // ── construction validation (via the pure `build`, no PyErr in tests) ──

    #[test]
    fn build_rejects_length_mismatch() {
        assert!(
            ResidentSweepSim::build(
                vec![1, 2],
                vec![1],
                vec![1, 2],
                vec![1, 2],
                vec![false, false],
                vec![0, 0],
                vec![0, 0]
            )
            .is_err()
        );
    }

    #[test]
    fn build_rejects_each_single_column_mismatch() {
        // ONE column short at a time, all others length 2. Each case must error
        // — this kills the `||`->`&&` mutants on every clause of the length guard
        // (under `&&` a single-column mismatch would wrongly pass).
        let ok2 = || vec![0i64; 2];
        let okf = || vec![false; 2];
        let oks = || vec![0i64; 2];
        let okb = || vec![0u64; 2];
        // open is the reference `n`; mismatching a later column individually.
        assert!(
            ResidentSweepSim::build(ok2(), vec![0], ok2(), ok2(), okf(), oks(), okb()).is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), vec![0], ok2(), okf(), oks(), okb()).is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), vec![0], okf(), oks(), okb()).is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), vec![false], oks(), okb()).is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), okf(), vec![0i64], okb()).is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), okf(), oks(), vec![0u64]).is_err()
        );
    }

    #[test]
    fn build_accepts_equal_lengths_and_reports_n_bars() {
        let r = ResidentSweepSim::build(
            vec![1, 2],
            vec![3, 4],
            vec![0, 1],
            vec![2, 3],
            vec![false, false],
            vec![0, 0],
            vec![0, 0],
        )
        .expect("equal lengths must build");
        assert_eq!(r.n_bars(), 2);
    }

    #[test]
    fn build_empty_is_ok() {
        let r = ResidentSweepSim::build(vec![], vec![], vec![], vec![], vec![], vec![], vec![])
            .expect("empty ok");
        assert_eq!(r.n_bars(), 0);
    }

    // ── empty / degenerate windows ──

    #[test]
    fn empty_window_yields_no_trades() {
        let s = sim(vec![], vec![], vec![], vec![], vec![]);
        assert_eq!(
            s.simulate_one(0, true, None, None, policy_default()),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn single_bar_never_opens_a_trade() {
        // Entry arms on bar 0 but there is no NEXT bar to fill the open against,
        // and finish() ignores pending_entry → zero trades. Mirrors Python.
        let s = sim(vec![100], vec![110], vec![90], vec![105], vec![0xFF]);
        assert_eq!(
            s.simulate_one(0, true, None, None, policy_default()),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn zero_firing_mask_yields_no_trades() {
        let s = sim(
            vec![100, 101, 102],
            vec![100, 101, 102],
            vec![100, 101, 102],
            vec![100, 101, 102],
            vec![1, 1, 1],
        );
        assert_eq!(
            s.simulate_one(1u64 << 63, true, None, None, policy_default()),
            Vec::<TradeRecord>::new()
        );
    }

    // ── basic long round-trip: end-of-data close ──

    #[test]
    fn long_entry_then_end_of_data_close() {
        let s = sim(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 130],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, None, policy_default());
        assert_eq!(out.len(), 1);
        let (pnl, mfe, mae, holding, entry_idx, exit_idx) = out[0];
        assert_eq!(pnl, 30); // close[2]=130 - open[1]=100
        assert_eq!(holding, 2);
        assert_eq!(entry_idx, 1);
        assert_eq!(exit_idx, 2);
        // entry=100. bar1 (110,95): fav=10, adv=5. bar2 (130,118): fav=30, adv=-18.
        assert_eq!(mfe, 30);
        assert_eq!(mae, 5);
    }

    // ── stop-loss / take-profit at level (long) ──

    #[test]
    fn long_stop_loss_at_level() {
        let s = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 95],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, Some(10), None, policy_default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, -10); // fill at sl_level 90, pnl 90-100
        assert_eq!(out[0].5, 2);
    }

    #[test]
    fn long_take_profit_at_level() {
        let s = sim(
            vec![50, 100, 115],
            vec![60, 105, 125],
            vec![40, 98, 113],
            vec![55, 100, 118],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, Some(20), policy_default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 20); // fill at tp_level 120
    }

    // ── same-bar SL+TP tie ──

    #[test]
    fn same_bar_sl_and_tp_tie_policies() {
        let s = sim(
            vec![50, 100, 100],
            vec![60, 105, 115],
            vec![40, 98, 85],
            vec![55, 100, 100],
            vec![0, 0, 0],
        );
        assert_eq!(
            s.simulate_one(0, true, Some(10), Some(10), policy_default())[0].0,
            -10
        );
        let tp_first = FillPolicy {
            same_bar: SameBarTie::TpFirst,
            ..policy_default()
        };
        assert_eq!(
            s.simulate_one(0, true, Some(10), Some(10), tp_first)[0].0,
            10
        );
    }

    // ── short round-trips ──

    #[test]
    fn short_take_profit_at_level() {
        let s = sim(
            vec![50, 100, 85],
            vec![60, 105, 90],
            vec![40, 98, 75],
            vec![55, 100, 82],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, false, None, Some(20), policy_default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 20); // short: entry 100 - tp_level 80
    }

    #[test]
    fn short_stop_loss_at_level() {
        let s = sim(
            vec![50, 100, 108],
            vec![60, 105, 115],
            vec![40, 98, 106],
            vec![55, 100, 112],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, false, Some(10), None, policy_default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, -10); // short: entry 100 - sl_level 110
    }

    // ── forced 15:20 exit ──

    #[test]
    fn forced_exit_overrides_and_fills_at_open() {
        // Entry open[1]=100; sl=50→50, tp=20→120 do NOT trigger on bar1
        // (high110<120, low95>50). bar2 is_forced AND tp would trigger
        // (high130>=120) — but forced overrides → fill at open[2]=125 (≠ the
        // tp_level 120), proving the forced override. pnl = 125-100 = 25.
        let s = ResidentSweepSim::build(
            vec![50, 100, 125],
            vec![60, 110, 130],
            vec![40, 95, 123],
            vec![55, 105, 128],
            vec![false, false, true],
            vec![0, 0, 0],
            vec![0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(0, true, Some(50), Some(20), policy_default());
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 25); // forced fill at open[2]=125, NOT the tp_level 120
        assert_eq!(out[0].5, 2);
    }

    #[test]
    fn forced_bar_suppresses_pending_entry() {
        // mask=1 fires ONLY on bar0 → arms an entry for bar1. bar1 is_forced →
        // the pending entry is dropped, and bar1 does not fire (bit0 clear) so it
        // does not re-arm. No trade. (With mask=0 every bar fires, so bar1 would
        // re-arm and bar2 would open — a different, also-correct path.)
        let s = ResidentSweepSim::build(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 125],
            vec![false, true, false],
            vec![0, 0, 0],
            vec![1, 0, 0],
        )
        .unwrap();
        assert_eq!(
            s.simulate_one(1, true, None, None, policy_default()),
            Vec::<TradeRecord>::new()
        );
    }

    // ── cross-session pending-entry drop (R2-OC-2) ──────────────────────────

    #[test]
    fn cross_session_pending_entry_dropped() {
        // mask=1 fires ONLY on bar1 — the LAST bar of session 0 (e.g. 15:29 IST).
        // bar2 is the FIRST bar of session 1 (next day 09:15, an overnight gap):
        // the pending entry must be DROPPED, never filled at session 1's open.
        // Pre-fix this produced one trade entering at open[2]=11900.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900, 11950],
            vec![10100, 10100, 12000, 12050],
            vec![9900, 9900, 11800, 11850],
            vec![10000, 10000, 11900, 11950],
            vec![false, false, false, false],
            vec![0, 0, 1, 1],
            vec![0, 1, 0, 0],
        )
        .unwrap();
        assert_eq!(
            s.simulate_one(1, true, None, None, policy_default()),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn same_session_pending_entry_still_fills() {
        // The SAME feed shape but with every bar in one session: the pending
        // entry armed on bar1 fills at open[2] as before (the gate only drops
        // CROSS-session fills; kills a `==`→`!=` mutant on the session compare).
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900, 11950],
            vec![10100, 10100, 12000, 12050],
            vec![9900, 9900, 11800, 11850],
            vec![10000, 10000, 11900, 11950],
            vec![false, false, false, false],
            vec![0, 0, 0, 0],
            vec![0, 1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None, policy_default());
        assert_eq!(out.len(), 1);
        let (pnl, _mfe, _mae, holding, entry_idx, exit_idx) = out[0];
        assert_eq!(entry_idx, 2); // fill at open[2]=11900, same session
        assert_eq!(exit_idx, 3); // end-of-data close at close[3]=11950
        assert_eq!(pnl, 50);
        assert_eq!(holding, 2);
    }

    #[test]
    fn cross_session_drop_then_rearm_fills_next_bar() {
        // Signal fires on bar1 (session 0's last bar → dropped at bar2) AND on
        // bar2 (session 1's first bar → legitimately fills at bar3's open):
        // the drop must not eat a genuine next-session signal.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900, 11950, 11960],
            vec![10100, 10100, 12000, 12050, 12060],
            vec![9900, 9900, 11800, 11850, 11860],
            vec![10000, 10000, 11900, 11950, 11960],
            vec![false, false, false, false, false],
            vec![0, 0, 1, 1, 1],
            vec![0, 1, 1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None, policy_default());
        assert_eq!(out.len(), 1);
        let (_pnl, _mfe, _mae, _holding, entry_idx, _exit_idx) = out[0];
        assert_eq!(entry_idx, 3); // opened at bar3 (session 1), NOT bar2
    }

    // ── next-open fills ──

    #[test]
    fn stop_loss_next_open_fill() {
        let s = sim(
            vec![50, 100, 95, 80],
            vec![60, 105, 99, 82],
            vec![40, 98, 85, 78],
            vec![55, 100, 90, 81],
            vec![0, 0, 0, 0],
        );
        let policy = FillPolicy {
            stop_loss: LevelFill::NextOpen,
            ..policy_default()
        };
        let out = s.simulate_one(0, true, Some(10), None, policy);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, -20); // fill at open[3]=80
        assert_eq!(out[0].5, 3);
    }

    #[test]
    fn next_open_pending_exit_takes_precedence_over_forced() {
        let s = ResidentSweepSim::build(
            vec![50, 100, 95, 80],
            vec![60, 105, 99, 82],
            vec![40, 98, 85, 78],
            vec![55, 100, 90, 81],
            vec![false, false, false, true],
            vec![0, 0, 0, 0],
            vec![0, 0, 0, 0],
        )
        .unwrap();
        let policy = FillPolicy {
            stop_loss: LevelFill::NextOpen,
            ..policy_default()
        };
        let out = s.simulate_one(0, true, Some(10), None, policy);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, -20); // pending SL fill at open[3]=80, NOT a forced exit
        assert_eq!(out[0].5, 3);
    }

    #[test]
    fn next_open_pending_exit_at_end_of_data_closes_at_last_close() {
        let s = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 90],
            vec![0, 0, 0],
        );
        let policy = FillPolicy {
            stop_loss: LevelFill::NextOpen,
            ..policy_default()
        };
        let out = s.simulate_one(0, true, Some(10), None, policy);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, -10); // finish closes at close[2]=90, NOT the discarded next-open
        assert_eq!(out[0].5, 2);
    }

    // ── multiple non-overlapping trades from one mask ──

    #[test]
    fn multiple_trades_non_overlapping() {
        let bar_bits = vec![1u64, 0, 0, 1, 0, 0];
        let s = sim(
            vec![100, 100, 100, 100, 100, 100],
            vec![100, 110, 100, 100, 110, 100],
            vec![100, 100, 100, 100, 100, 100],
            vec![100, 100, 100, 100, 100, 100],
            bar_bits,
        );
        let out = s.simulate_one(1, true, None, Some(5), policy_default());
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].0, 5);
        assert_eq!(out[1].0, 5);
    }

    // ── batch parity ──

    #[test]
    fn batch_aligns_with_per_mask() {
        let s = sim(
            vec![100, 100, 100, 100],
            vec![100, 110, 100, 108],
            vec![100, 92, 100, 100],
            vec![100, 100, 100, 100],
            vec![1, 0, 1, 0],
        );
        let masks = vec![0u64, 1u64, 1u64 << 63];
        let batch = s
            .simulate_masks_impl(&masks, true, Some(8), Some(5), false, false, false)
            .expect("in-range SL/TP must pass the boundary");
        for (j, &m) in masks.iter().enumerate() {
            assert_eq!(
                batch[j],
                s.simulate_one(m, true, Some(8), Some(5), policy_default())
            );
        }
    }

    #[test]
    fn count_trades_matches_simulate_masks_lengths() {
        let s = sim(
            vec![100, 100, 100, 100],
            vec![100, 110, 100, 108],
            vec![100, 92, 100, 100],
            vec![100, 100, 100, 100],
            vec![1, 0, 1, 0],
        );
        let masks = vec![0u64, 1u64, 1u64 << 63];
        let counts = s
            .count_trades_impl(&masks, true, Some(8), Some(5), false, false, false)
            .expect("in-range SL/TP must pass the boundary");
        let full = s
            .simulate_masks_impl(&masks, true, Some(8), Some(5), false, false, false)
            .expect("in-range SL/TP must pass the boundary");
        let expected: Vec<u64> = full.iter().map(|t| t.len() as u64).collect();
        assert_eq!(counts, expected);
    }

    #[test]
    fn empty_masks_batch_is_empty() {
        let s = sim(vec![100], vec![100], vec![100], vec![100], vec![0]);
        assert_eq!(
            s.simulate_masks_impl(&[], true, None, None, false, false, false)
                .expect("empty batch ok"),
            Vec::<Vec<TradeRecord>>::new()
        );
        assert_eq!(
            s.count_trades_impl(&[], true, None, None, false, false, false)
                .expect("empty batch ok"),
            Vec::<u64>::new()
        );
    }

    // ── parallel arm parity (crosses PARALLEL_WORK_THRESHOLD) ──

    #[test]
    fn parallel_work_threshold_is_pinned_to_one_mebi_ops() {
        // DEC-MUTATION-RUNTIME-003: pins `1 << 20` against an INDEPENDENT
        // literal — kills the `<<` → `>>` const mutant (`1 >> 20` == 0 would
        // silently route EVERY batch to the rayon arm, breaking the small-N
        // O(1)-canary protection this documented invariant exists for).
        assert_eq!(PARALLEL_WORK_THRESHOLD, 1_048_576);
    }

    #[test]
    fn parallel_arm_matches_serial() {
        let n = 1024usize;
        let open: Vec<i64> = (0..n as i64).map(|i| 1000 + (i % 7)).collect();
        let high: Vec<i64> = open.iter().map(|&o| o + 5).collect();
        let low: Vec<i64> = open.iter().map(|&o| o - 5).collect();
        let close: Vec<i64> = open.clone();
        let bar_bits: Vec<u64> = (0..n as u64)
            .map(|i| (i & 0b111) | (1 << (i % 50)))
            .collect();
        let s = sim(open, high, low, close, bar_bits);
        let masks: Vec<u64> = (0..1100u64).map(|i| (i & 0b1) | (1 << (i % 50))).collect();
        assert!((masks.len() as u64) * (n as u64) >= PARALLEL_WORK_THRESHOLD);
        let batch = s
            .simulate_masks_impl(&masks, true, Some(3), Some(4), false, false, false)
            .expect("in-range SL/TP must pass the boundary");
        let serial: Vec<Vec<TradeRecord>> = masks
            .iter()
            .map(|&m| s.simulate_one(m, true, Some(3), Some(4), policy_default()))
            .collect();
        assert_eq!(batch, serial);
    }

    // ── excursion accumulation over still-open bars ──

    #[test]
    fn excursion_accumulates_over_open_bars() {
        let s = sim(
            vec![50, 100, 100, 100],
            vec![60, 130, 105, 102],
            vec![40, 95, 70, 99],
            vec![55, 100, 100, 101],
            vec![0, 0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, None, policy_default());
        assert_eq!(out.len(), 1);
        let (_pnl, mfe, mae, _h, _e, _x) = out[0];
        // bar1 (130,95): fav=30,adv=5. bar2 (105,70): fav=5,adv=30. bar3 (102,99): fav=2,adv=1.
        assert_eq!(mfe, 30);
        assert_eq!(mae, 30);
    }

    // ── R2-RC-F1 overflow guards: boundary validation + old-abort regressions ──

    /// Shorthand for the ceiling in the guard tests.
    const C: i64 = MAX_PRICE_PAISA;

    #[test]
    fn build_rejects_out_of_range_price_per_column() {
        // OLD-ABORT REPRODUCTION (build side): a ~2^63 paisa operand
        // (i64::MAX ≈ 2^63 − 1) in ANY price column previously flowed into the
        // walker's raw `entry ± d` / `high − entry` arithmetic and SIGABRT'd
        // (exit 134) under `overflow-checks = true` + `panic = "abort"`. The
        // boundary now rejects it with a clean Err (→ ValueError at the FFI
        // edge) before the loop can ever see it. One column at a time, both
        // extremes, so every clause of the guard is exercised.
        let ok = || vec![100i64, 100];
        let okf = || vec![false; 2];
        let oks = || vec![0i64; 2];
        let okb = || vec![0u64; 2];
        for bad in [i64::MAX, i64::MIN, C + 1, -C - 1] {
            let bad_col = vec![100i64, bad];
            for col in 0..4usize {
                let (o, h, l, c) = match col {
                    0 => (bad_col.clone(), ok(), ok(), ok()),
                    1 => (ok(), bad_col.clone(), ok(), ok()),
                    2 => (ok(), ok(), bad_col.clone(), ok()),
                    _ => (ok(), ok(), ok(), bad_col.clone()),
                };
                let err = ResidentSweepSim::build(o, h, l, c, okf(), oks(), okb())
                    .map(|_| ()) // () is Debug; ResidentSweepSim is not
                    .expect_err("out-of-range price must be rejected at build");
                assert!(
                    err.contains("overflow-safe range"),
                    "column {col} value {bad}: message must name the guard: {err}"
                );
            }
        }
    }

    #[test]
    fn build_accepts_exact_ceiling_boundary() {
        // Exactly ±MAX_PRICE_PAISA is VALID (inclusive range) — kills the
        // off-by-one `>`/`>=` mutants on the guard, and proves the ceiling
        // never rejects the extreme in-range input.
        let r = ResidentSweepSim::build(
            vec![C, -C],
            vec![C, -C],
            vec![C, -C],
            vec![C, -C],
            vec![false, false],
            vec![0, 0],
            vec![0, 0],
        )
        .expect("exact-ceiling prices must build");
        assert_eq!(r.n_bars(), 2);
    }

    #[test]
    fn check_level_distance_boundaries() {
        // None (leg disabled) always valid; [0, C] valid inclusive; negative
        // (nonsensical distance) and > C (overflow-prone) rejected.
        assert!(check_level_distance("sl_paisa", None).is_ok());
        assert!(check_level_distance("sl_paisa", Some(0)).is_ok());
        assert!(check_level_distance("sl_paisa", Some(1)).is_ok());
        assert!(check_level_distance("sl_paisa", Some(C)).is_ok());
        assert!(check_level_distance("sl_paisa", Some(-1)).is_err());
        assert!(check_level_distance("sl_paisa", Some(C + 1)).is_err());
        assert!(check_level_distance("tp_paisa", Some(i64::MAX)).is_err());
        assert!(check_level_distance("tp_paisa", Some(i64::MIN)).is_err());
    }

    #[test]
    fn old_abort_huge_sl_tp_now_clean_err() {
        // OLD-ABORT REPRODUCTION (batch-entry side): a valid small window with
        // tp_paisa = i64::MAX (~2^63) previously overflowed `entry + d` in
        // `Walker::levels` the moment a position opened → SIGABRT. Now BOTH
        // batch entries reject it up front with a clean Err (→ ValueError).
        let s = sim(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 130],
            vec![1, 0, 0],
        );
        for bad in [Some(i64::MAX), Some(i64::MIN), Some(-1), Some(C + 1)] {
            assert!(
                s.simulate_masks_impl(&[1], true, bad, None, false, false, false)
                    .is_err(),
                "sl_paisa={bad:?} must be rejected"
            );
            assert!(
                s.simulate_masks_impl(&[1], true, None, bad, false, false, false)
                    .is_err(),
                "tp_paisa={bad:?} must be rejected"
            );
            assert!(
                s.count_trades_impl(&[1], true, bad, bad, false, false, false)
                    .is_err(),
                "count_trades sl/tp={bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn boundary_extremes_compute_cleanly_without_panic() {
        // The derivation's TEETH: with every input at the ceiling, the worst
        // intermediates actually occur — `entry + d = -C + C = 0` (tp level),
        // `entry - d = -C - C = -2C` (sl level), excursion `C - (-C) = 2C` —
        // and none panics. Entry signal on bar0 → entry at open[1] = -C; bar1
        // spans [-C, C]; TP level 0 is hit at-level → pnl = 0 − (−C) = C.
        let s = sim(
            vec![0, -C, 0],
            vec![0, C, 0],
            vec![0, -C, 0],
            vec![0, 0, C],
            vec![1, 0, 0],
        );
        let out = s
            .simulate_masks_impl(&[1], true, Some(C), Some(C), false, false, false)
            .expect("exact-ceiling SL/TP must pass the boundary");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].len(), 1);
        let (pnl, mfe, mae, holding, entry_idx, exit_idx) = out[0][0];
        assert_eq!(pnl, C); // TP fill at level 0, entry −C
        assert_eq!(mfe, 2 * C); // high C − entry −C = 2·C — the max intermediate
        assert_eq!(mae, 0);
        assert_eq!(holding, 1);
        assert_eq!(entry_idx, 1);
        assert_eq!(exit_idx, 1);
    }

    #[test]
    fn golden_valid_inputs_identical_through_validated_path() {
        // Regression golden: the exact `long_stop_loss_at_level` scenario run
        // through the NEW validated batch entry emits byte-identical trades
        // (the guard must not perturb any in-range computation).
        let s = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 95],
            vec![0, 0, 0],
        );
        let out = s
            .simulate_masks_impl(&[0], true, Some(10), None, false, false, false)
            .expect("in-range inputs must pass");
        // Entry open[1]=100; bar1 folds (105,98) → mfe 5 / mae 2; bar2 folds
        // (99,85) → mfe 5 / mae 15, then low 85 ≤ sl_level 90 → SL at-level
        // fill: pnl = 90 − 100 = −10; holding 1..=2 inclusive = 2.
        assert_eq!(out, vec![vec![(-10i64, 5i64, 15i64, 2u64, 1u64, 2u64)]]);
        let counts = s
            .count_trades_impl(&[0], true, Some(10), None, false, false, false)
            .expect("in-range inputs must pass");
        assert_eq!(counts, vec![1]);
    }
}
