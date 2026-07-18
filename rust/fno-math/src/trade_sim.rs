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
//! which it does EXACTLY. For the same `(bars, mask, direction, sl, tp)` it
//! emits byte-identical trades to `simulate_trades(...)` under the ONE
//! hardcoded worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001); a golden
//! cross-check asserts this field-by-field over hypothesis-generated inputs.
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
//! ## No cross-session OPEN position (the session_gap guard)
//!
//! The open-position twin of the entry gate
//! (DEC-TRADESIM-OPEN-SESSION-GUARD-001): a position never survives a
//! `session_idx` change either. On the first pushed bar whose session differs
//! from the position's entry session — the prior session's stored bars ended
//! before any forced-exit-window bar (a session-tail data hole / stitched-
//! series roll gap) — the walker closes the position retroactively at the
//! PRIOR session's last stored bar's ADVERSE extreme
//! (`pos.last_exit_basis_paisa` at `pos.last_idx` — the `finish()` pricing:
//! no excursion fold from the boundary bar), clears any pending next-open
//! exit (subsumed), and the
//! boundary bar never arms a new entry (the step-(C) `closed.is_none()` gate).
//! Mirrors the Python walker's (A2) guard bit-for-bit; the Python detail sink
//! names the close `session_gap` — the kernel's `TradeRecord` carries no
//! reason, so the numerics ARE the parity surface.
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

use crate::mask_word::MaskWord;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// One emitted round-trip trade. Field order matches the Python `ClosedTrade`
/// numeric payload; `entry_idx` / `exit_idx` are bar indices the Python wrapper
/// maps to `entry_ts` / `exit_ts` via the bar timestamps. `direction` is a
/// per-batch constant (not stored per trade).
///
/// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
pub type TradeRecord = (i64, i64, i64, u64, u64, u64);

/// Mutable per-trade state while a position is open (int64 paisa). Mirrors the
/// Python `_OpenPosition`; `last_bar` is replaced by `last_idx` +
/// `last_exit_basis_paisa` since the kernel holds resident price columns (the
/// `Walker` itself never sees them, so the last managed bar's exit-basis
/// price is carried on the position for the `finish()`-idiom session-boundary
/// close — DEC-TRADESIM-OPEN-SESSION-GUARD-001).
#[derive(Clone, Copy)]
struct OpenPosition {
    entry_idx: usize,
    entry_price_paisa: i64,
    /// The `session_idx` of the entry fill bar — the open-position session
    /// guard closes the position the moment a pushed bar's session differs
    /// (mirrors the Python `_OpenPosition.entry_day_ist`).
    entry_session: i64,
    sl_level_paisa: Option<i64>,
    tp_level_paisa: Option<i64>,
    mfe_paisa: i64,
    mae_paisa: i64,
    last_idx: usize,
    /// The last MANAGED bar's direction-resolved EXIT-BASIS price (paisa) —
    /// its ADVERSE extreme (BUY low / SELL high; the one worst-case law,
    /// DEC-FILL-WORST-CASE-ONLY-001 — the Python twin keeps the whole
    /// `last_bar` and resolves at close time). The retroactive
    /// session-boundary + end-of-data fill price (pre-update state: refreshed
    /// by `manage_position` alongside `last_idx`, so at the guard it still
    /// names the PRIOR session's bar).
    last_exit_basis_paisa: i64,
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
/// `label` names the raising pyclass (`"ResidentSweepSim"` /
/// `"ResidentSweepSimU128"`) so both width variants emit self-naming
/// messages through the ONE shared guard — the u64 message is byte-identical
/// to the pre-generic kernel's (DEC-TRADE-SIM-U128-KERNEL-001).
fn check_price_column(label: &str, name: &str, column: &[i64]) -> Result<(), String> {
    for &v in column {
        if !(-MAX_PRICE_PAISA..=MAX_PRICE_PAISA).contains(&v) {
            return Err(format!(
                "{label}: {name} value {v} out of the overflow-safe range \
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
/// `None` (leg disabled) is always valid. Pure — see [`check_price_column`];
/// same shared-`label` convention (the u64 message stays byte-identical via
/// the [`check_level_distance`] delegator the existing suite pins).
fn check_level_distance_for(label: &str, name: &str, distance: Option<i64>) -> Result<(), String> {
    if let Some(d) = distance {
        if !(0..=MAX_PRICE_PAISA).contains(&d) {
            return Err(format!(
                "{label}: {name} ({d}) out of the overflow-safe range \
                 [0, {MAX_PRICE_PAISA}] paisa"
            ));
        }
    }
    Ok(())
}

/// u64-kernel SL/TP distance guard — thin delegator onto
/// [`check_level_distance_for`] with the historical `"ResidentSweepSim"`
/// label, so the pre-generic message bytes (and the `#[cfg(test)]` suite
/// calling this two-argument form) are preserved verbatim. Test-only since
/// the pyclass impls moved onto the labeled core (same bytes for u64).
#[cfg(test)]
fn check_level_distance(name: &str, distance: Option<i64>) -> Result<(), String> {
    check_level_distance_for("ResidentSweepSim", name, distance)
}

/// Pure-Rust faithful port of `TradeSimWalker` — a forward-only per-bar state
/// machine. No look-ahead: at bar `i` it reads only `open/high/low/close[i]` and
/// `is_forced[i]` (plus the entry signal derived from the resident `bar_bits[i]`).
struct Walker {
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
    position: Option<OpenPosition>,
    pending_entry: bool,
    /// The `session_idx` of the bar the pending entry ARMED on — only meaningful
    /// while `pending_entry` is true. A pending entry may only fill on a bar of
    /// the SAME session; a differing fill-bar session drops it (R2-OC-2).
    /// Mirrors the Python walker's `_pending_entry_day`.
    pending_entry_session: i64,
    /// True iff a next-open exit (Mode-B signal — the only remaining
    /// scheduler now the retired `LevelFill::NextOpen` optimism knob is
    /// deleted, DEC-FILL-WORST-CASE-ONLY-001) is scheduled. The Python
    /// walker stores the reason too, but the emitted trade carries no
    /// reason, so a bool is sufficient + bit-identical. The sweep path
    /// never feeds exit signals, so this stays false there — retained for
    /// the 1:1 state-machine mirror of the Python walker's Mode-B law.
    pending_exit: bool,
}

impl Walker {
    fn new(is_buy: bool, sl_paisa: Option<i64>, tp_paisa: Option<i64>) -> Self {
        Self {
            is_buy,
            sl_paisa,
            tp_paisa,
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

    /// Open a position at `entry_fill_paisa` on bar `idx` — the caller resolves
    /// the fill as the bar's ADVERSE extreme (BUY high / SELL low — the one
    /// worst-case law, DEC-FILL-WORST-CASE-ONLY-001).
    /// Mirrors `_open_position`: the SL/TP levels anchor on the ACTUAL entry
    /// fill. No excursion update at open — the entry bar's high/low is folded
    /// by the same-bar `manage_position` call (step 3). `session` is the fill
    /// bar's `session_idx` (the open-position guard's key); `exit_basis_paisa`
    /// initialises `last_exit_basis_paisa` deterministically — it is never
    /// read before the same-push `manage_position` refreshes it (the guard
    /// cannot fire between open and first manage: `entry_session` equals THIS
    /// bar's session by construction).
    fn open_position(
        &mut self,
        entry_fill_paisa: i64,
        idx: usize,
        session: i64,
        exit_basis_paisa: i64,
    ) {
        let (sl_level, tp_level) = self.levels(entry_fill_paisa);
        self.position = Some(OpenPosition {
            entry_idx: idx,
            entry_price_paisa: entry_fill_paisa,
            entry_session: session,
            sl_level_paisa: sl_level,
            tp_level_paisa: tp_level,
            mfe_paisa: 0,
            mae_paisa: 0,
            last_idx: idx,
            last_exit_basis_paisa: exit_basis_paisa,
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
        // Same bar spans both levels — the STOP fills first, always (the
        // pessimistic tie, hardcoded; the retired `SameBarTie::TpFirst`
        // optimism knob is deleted — DEC-FILL-WORST-CASE-ONLY-001).
        stop.or(target)
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
    /// Python order. `exit_basis_paisa` is the bar's direction-resolved
    /// exit-basis price (the adverse extreme — resolved once in `push`).
    #[allow(clippy::too_many_arguments)]
    fn manage_position(
        &mut self,
        mut pos: OpenPosition,
        open_paisa: i64,
        high_paisa: i64,
        low_paisa: i64,
        exit_basis_paisa: i64,
        idx: usize,
        forced: bool,
        exit_signal: bool,
    ) -> Option<TradeRecord> {
        // Mirrors Python `_manage_position`'s top-of-body `last_idx/last_bar`
        // refresh — the (A2) guard reads the PRE-update state, so it runs in
        // `push` BEFORE this method is entered.
        pos.last_idx = idx;
        pos.last_exit_basis_paisa = exit_basis_paisa;

        // The open-anchored exit fill (pending / forced): the bar's adverse
        // extreme — BUY exits (sells) at the LOW, SELL exits (covers) at the
        // HIGH (DEC-FILL-WORST-CASE-ONLY-001). The excursion fold below
        // stays the legacy (open, open) — only the fill PRICE is worst-case.
        let open_anchored_fill = if self.is_buy { low_paisa } else { high_paisa };

        // (1) A next-open exit scheduled on a prior bar fills on THIS bar —
        // BEFORE forced is checked (pending beats forced).
        if self.pending_exit {
            self.pending_exit = false;
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, open_anchored_fill, idx));
        }

        // (2) Forced 15:20 IST exit overrides SL/TP/signal.
        if forced {
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, open_anchored_fill, idx));
        }

        // (3) Full-range in-position bar: fold its high/low into MFE/MAE.
        Self::update_excursion(&mut pos, self.is_buy, high_paisa, low_paisa);

        // (4) Stop-loss / take-profit — the adverse-max law WITH the level
        // (DEC-FILL-WORST-CASE-ONLY-001): BUY exit = min(level, bar low) — a
        // gap-through fills at the extreme while a bar entirely PAST the
        // level in the trade's favour stays clamped AT the level (never an
        // overstatement vs the at-level fill — the proven long-TP gap-up
        // counterexample); SELL exit = max(level, bar high). Bounds:
        // |level| <= 2·C and |low|/|high| <= C, so the min/max stays
        // <= 2·C — the MAX_PRICE_PAISA derivation is unchanged. (The retired
        // `LevelFill::NextOpen` optimism arm is deleted — no level fill is
        // ever deferred to a later bar.) `_is_stop` survives only as the
        // tuple's discriminant slot — the fill law no longer branches on it.
        if let Some((_is_stop, level_paisa)) = self.resolve_level_hit(&pos, high_paisa, low_paisa) {
            let exit_fill = if self.is_buy {
                level_paisa.min(low_paisa)
            } else {
                level_paisa.max(high_paisa)
            };
            return Some(self.close(pos, exit_fill, idx));
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
    /// `close_paisa` is bar `idx`'s CLOSE — retained on the FFI row shape for
    /// column parity; the bar's EXIT-BASIS price (its adverse extreme —
    /// DEC-FILL-WORST-CASE-ONLY-001) is carried onto the open position per
    /// managed bar, so the (A2) session-boundary guard + `finish` can price
    /// the `finish()`-idiom retroactive close
    /// (DEC-TRADESIM-OPEN-SESSION-GUARD-001).
    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        open_paisa: i64,
        high_paisa: i64,
        low_paisa: i64,
        _close_paisa: i64,
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

        // THIS bar's direction-resolved exit-basis price — what an
        // end-of-data / session-gap close against this bar fills at: the
        // adverse extreme (DEC-FILL-WORST-CASE-ONLY-001; the Python twin
        // resolves lazily from the kept `last_bar`).
        let exit_basis_paisa = if self.is_buy { low_paisa } else { high_paisa };

        // (A) Open a pending entry on THIS bar — at its ADVERSE extreme (BUY
        // high / SELL low; the SL/TP levels then anchor on that actual fill
        // — DEC-FILL-WORST-CASE-ONLY-001) — only within the SAME
        // session it armed on (a fill bar on a LATER session is an overnight-gap
        // fill and is DROPPED, R2-OC-2) and never on a forced bar (cannot open
        // at/after the session's forced-exit instant). The pending flag is
        // cleared UNCONDITIONALLY. Mirrors the Python (A) branch exactly.
        // (A) and (A2) below are mutually exclusive by state invariant: step
        // (C) arms only while flat and (A) clears the flag before opening, so
        // `pending_entry` is false whenever a position is open — one push runs
        // (A) xor (A2), never both (DEC-TRADESIM-OPEN-SESSION-GUARD-001 §2.2).
        if self.position.is_none() && self.pending_entry {
            self.pending_entry = false;
            let same_session = session == self.pending_entry_session;
            if !forced && same_session {
                let entry_fill = if self.is_buy { high_paisa } else { low_paisa };
                self.open_position(entry_fill, idx, session, exit_basis_paisa);
            }
        }

        // (B) Manage an open position on this bar (including one just opened in A).
        let mut closed: Option<TradeRecord> = None;
        if let Some(pos) = self.position.take() {
            if session != pos.entry_session {
                // (A2) Session-boundary guard — DEC-TRADESIM-OPEN-SESSION-GUARD-001.
                // Mirrors the Python walker: close at the PRIOR session's last
                // stored bar's exit basis (the finish() idiom — pre-update
                // `last_exit_basis_paisa` / `last_idx`, no excursion fold from
                // THIS bar); a pending next-open exit is subsumed (cleared — a
                // flat walker never carries a stale pending exit into the new
                // session). A just-opened position (same push, step (A))
                // carries THIS bar's session, so it can never trip the guard;
                // the inequality compare fires exactly once even across a
                // multi-session hole. The Python detail sink names this close
                // `session_gap`; the record's numerics are the parity surface.
                self.pending_exit = false;
                closed = Some(self.close(pos, pos.last_exit_basis_paisa, pos.last_idx));
            } else {
                closed = self.manage_position(
                    pos,
                    open_paisa,
                    high_paisa,
                    low_paisa,
                    exit_basis_paisa,
                    idx,
                    forced,
                    exit_signal,
                );
            }
        }

        // (C) While flat (and NOT on a bar a trade just closed — the exit bar
        // consumes the entry signal), arm the next entry, recording THIS bar's
        // session — the fill gate for (A). Knowable at bar `idx` (no look-ahead).
        // The `closed.is_none()` gate also means a session_gap boundary bar
        // never arms: earliest re-arm is the boundary bar's successor
        // (DEC-TRADESIM-OPEN-SESSION-GUARD-001 §2.2 pinned law).
        if self.position.is_none() && closed.is_none() && entry_signal {
            self.pending_entry = true;
            self.pending_entry_session = session;
        }

        closed
    }

    /// Close any still-open position at the last managed bar's exit basis —
    /// mirrors `finish` (the bar's adverse extreme,
    /// DEC-FILL-WORST-CASE-ONLY-001). A still-pending next-open exit is
    /// IGNORED here (closed at the basis, not the would-be open); no
    /// excursion fold. Every position is managed on its own entry bar (the
    /// same-push (B) call), so `last_exit_basis_paisa` always names
    /// `bars[last_idx]`'s resolved basis.
    fn finish(&mut self) -> Option<TradeRecord> {
        let pos = self.position.take()?;
        Some(self.close(pos, pos.last_exit_basis_paisa, pos.last_idx))
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

// The width-generic MaskWord trait (DEC-TRADE-SIM-U128-KERNEL-001) was
// introduced HERE module-internal and is HOISTED verbatim to the shared
// `crate::mask_word` module (DEC-ENUM-KERNEL-U128-001 — the enum/count
// kernels genericize over the SAME trait; CLAUDE.md §11 COMMON). Pure
// relocation — behaviour byte-identical (`use crate::mask_word::MaskWord`
// at the top of this file repoints the import); this suite passing
// UNMODIFIED is the frozen-behaviour gate.

/// The width-INDEPENDENT resident columns — OHLC paisa + forced flags + the
/// per-bar session ordinals. Shared verbatim by both kernel widths; only the
/// `bar_bits` vector (held next to this struct in each pyclass) is width-typed.
struct SweepColumns {
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    /// Per-bar session identity — the bar's IST calendar-date ordinal, computed
    /// in Python from bar `i`'s own timestamp (P-06: no look-ahead). A pending
    /// entry only fills on a bar with the SAME value it armed on (R2-OC-2).
    session_idx: Vec<i64>,
}

/// Build + validate the width-independent columns — the ONE length-mismatch
/// guard and R2-RC-F1 price-boundary scan both kernels ride. `label` names
/// the constructing pyclass; `bar_bits_len` is the width-typed vector's
/// length (validated here so the mismatch message stays complete, the vector
/// itself stays with the caller). The u64 path's messages are byte-identical
/// to the pre-generic kernel's.
fn build_columns(
    label: &str,
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    session_idx: Vec<i64>,
    bar_bits_len: usize,
) -> Result<SweepColumns, String> {
    let n = open.len();
    if high.len() != n
        || low.len() != n
        || close.len() != n
        || is_forced.len() != n
        || session_idx.len() != n
        || bar_bits_len != n
    {
        return Err(format!(
            "{label}: column length mismatch — open={}, high={}, low={}, close={}, \
             is_forced={}, session_idx={}, bar_bits={} (all must be equal)",
            n,
            high.len(),
            low.len(),
            close.len(),
            is_forced.len(),
            session_idx.len(),
            bar_bits_len,
        ));
    }
    // R2-RC-F1 overflow boundary: every paisa price must sit inside
    // [-MAX_PRICE_PAISA, MAX_PRICE_PAISA] so the walker's raw i64 add/sub
    // is provably panic-free (see the ceiling's derivation doc). One-time
    // O(N_bars) scan at construction — NOT on the hot loop. `is_forced`
    // (bool), `bar_bits` (u64/u128, only `&`-compared against masks) and
    // `session_idx` (i64 day ordinal, compared for equality ONLY — by the
    // R2-OC-2 same-session entry gate's `==` and the open-position
    // session-boundary guard's `!=`, DEC-TRADESIM-OPEN-SESSION-GUARD-001)
    // do no arithmetic and need no bound.
    check_price_column(label, "open", &open)?;
    check_price_column(label, "high", &high)?;
    check_price_column(label, "low", &low)?;
    check_price_column(label, "close", &close)?;
    Ok(SweepColumns {
        open,
        high,
        low,
        close,
        is_forced,
        session_idx,
    })
}

/// Simulate ONE mask over a resident window — the width-generic pure-Rust core
/// both pyclasses (and the `#[cfg(test)]` suite, via the u64 delegator) call.
/// Entry signal for bar `i` is `W::is_superset(bar_bits[i], mask)` — the ONLY
/// width-touching line; there is no Mode-B exit-signal stream on the sweep
/// path (it matches `metrics_for_mask`, which passes none).
// The loop indexes SEVEN parallel per-bar sequences by the same `i` (six
// columns + the width-typed bar_bits); a 7-way iterator zip would obscure the
// walk, so the index form is kept deliberately.
#[allow(clippy::needless_range_loop)]
fn simulate_one_core<W: MaskWord>(
    cols: &SweepColumns,
    bar_bits: &[W],
    mask: W,
    is_buy: bool,
    sl: Option<i64>,
    tp: Option<i64>,
) -> Vec<TradeRecord> {
    let n = cols.open.len();
    let mut walker = Walker::new(is_buy, sl, tp);
    let mut trades: Vec<TradeRecord> = Vec::new();
    for i in 0..n {
        let entry_signal = W::is_superset(bar_bits[i], mask);
        if let Some(t) = walker.push(
            cols.open[i],
            cols.high[i],
            cols.low[i],
            cols.close[i],
            i,
            cols.is_forced[i],
            cols.session_idx[i],
            entry_signal,
            false,
        ) {
            trades.push(t);
        }
    }
    if let Some(t) = walker.finish() {
        trades.push(t);
    }
    trades
}

/// Width-generic core of the batch `simulate_masks` — validates the SL/TP
/// distances at the batch boundary (R2-RC-F1, see [`check_level_distance_for`]
/// / [`MAX_PRICE_PAISA`]) then runs the serial/parallel sweep. `Result<_,
/// String>` (NO `PyErr`) so the `#[cfg(test)]` suite exercises both the
/// reject and compute branches without pulling Python C-API symbols into
/// the no-libpython `cargo test` link. (The flat batch ABI mirrors the
/// PyO3 methods; `clippy::too_many_arguments` is allowed crate-wide.)
fn simulate_masks_core<W: MaskWord>(
    label: &str,
    cols: &SweepColumns,
    bar_bits: &[W],
    masks: &[W],
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
) -> Result<Vec<Vec<TradeRecord>>, String> {
    check_level_distance_for(label, "sl_paisa", sl_paisa)?;
    check_level_distance_for(label, "tp_paisa", tp_paisa)?;
    let work = (masks.len() as u64).saturating_mul(cols.open.len() as u64);
    // Dispatch via the COMMON crate predicate (DEC-MUTATION-RUNTIME-003):
    // both arms are output-identical by design, so the boundary is gated
    // by `dispatch_tests::parallel_dispatch_boundary_is_exclusive`.
    Ok(
        if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
            masks
                .iter()
                .map(|&m| simulate_one_core(cols, bar_bits, m, is_buy, sl_paisa, tp_paisa))
                .collect()
        } else {
            masks
                .par_iter()
                .map(|&m| simulate_one_core(cols, bar_bits, m, is_buy, sl_paisa, tp_paisa))
                .collect()
        },
    )
}

/// Width-generic core of the batch `count_trades` — same boundary validation +
/// sweep as [`simulate_masks_core`], returning only the per-mask counts.
fn count_trades_core<W: MaskWord>(
    label: &str,
    cols: &SweepColumns,
    bar_bits: &[W],
    masks: &[W],
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
) -> Result<Vec<u64>, String> {
    check_level_distance_for(label, "sl_paisa", sl_paisa)?;
    check_level_distance_for(label, "tp_paisa", tp_paisa)?;
    let work = (masks.len() as u64).saturating_mul(cols.open.len() as u64);
    let run = |m: W| simulate_one_core(cols, bar_bits, m, is_buy, sl_paisa, tp_paisa).len() as u64;
    // Same COMMON-predicate dispatch as `simulate_masks_core`
    // (DEC-MUTATION-RUNTIME-003).
    Ok(
        if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
            masks.iter().map(|&m| run(m)).collect()
        } else {
            masks.par_iter().map(|&m| run(m)).collect()
        },
    )
}

/// Resident bar window for the Stage-2 trade-sim sweep — OHLC paisa + forced flags
/// + the per-bar u64 condition bitset, held Rust-side so a whole mask grid is
/// simulated without re-marshalling the window per combo. The production kernel
/// for today's 60-bit vocabulary; thin `W = u64` delegators onto the width-
/// generic [`MaskWord`] core (byte-identical surface + behaviour to the
/// pre-generic kernel — the existing suite is the frozen gate).
///
/// See the module docs for the bit-exactness + honest-envelope contracts.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepSim {
    columns: SweepColumns,
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
        let columns = build_columns(
            "ResidentSweepSim",
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits.len(),
        )?;
        Ok(Self { columns, bar_bits })
    }

    /// Simulate ONE u64 mask over the resident window — thin delegator onto
    /// [`simulate_one_core`] (kept, test-only, so the `#[cfg(test)]` suite's
    /// call surface is unchanged; the batch paths call the core directly).
    #[cfg(test)]
    fn simulate_one(
        &self,
        mask: u64,
        is_buy: bool,
        sl: Option<i64>,
        tp: Option<i64>,
    ) -> Vec<TradeRecord> {
        simulate_one_core(&self.columns, &self.bar_bits, mask, is_buy, sl, tp)
    }

    /// Pure core of [`Self::simulate_masks`] — `W = u64` delegator onto
    /// [`simulate_masks_core`].
    fn simulate_masks_impl(
        &self,
        masks: &[u64],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<Vec<TradeRecord>>, String> {
        simulate_masks_core(
            "ResidentSweepSim",
            &self.columns,
            &self.bar_bits,
            masks,
            is_buy,
            sl_paisa,
            tp_paisa,
        )
    }

    /// Pure core of [`Self::count_trades`] — `W = u64` delegator onto
    /// [`count_trades_core`].
    fn count_trades_impl(
        &self,
        masks: &[u64],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<u64>, String> {
        count_trades_core(
            "ResidentSweepSim",
            &self.columns,
            &self.bar_bits,
            masks,
            is_buy,
            sl_paisa,
            tp_paisa,
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
        self.columns.open.len()
    }

    /// Simulate a BATCH of masks over the resident window → per-mask trade lists.
    ///
    /// `out[j]` is the list of round-trip trades for `masks[j]`, each a tuple
    /// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
    /// `is_buy` selects long (`true`) / short (`false`); `sl_paisa` / `tp_paisa`
    /// are the stop / target distances in paisa (`None` disables that leg).
    /// Fills follow the ONE hardcoded worst-case adverse-max law
    /// (DEC-FILL-WORST-CASE-ONLY-001) — the retired mode flags are deleted
    /// from the FFI.
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
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn simulate_masks(
        &self,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<Vec<TradeRecord>>> {
        self.simulate_masks_impl(&masks, is_buy, sl_paisa, tp_paisa)
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
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn count_trades(
        &self,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<u64>> {
        self.count_trades_impl(&masks, is_buy, sl_paisa, tp_paisa)
            .map_err(PyValueError::new_err)
    }
}

/// Resident bar window for the Stage-2 trade-sim sweep over a **>64-bit**
/// vocabulary — the u128 twin of [`ResidentSweepSim`]
/// (DEC-TRADE-SIM-U128-KERNEL-001). Identical construction/validation/batch
/// surface; the ONLY difference is the mask-word width (`bar_bits` / `masks`
/// cross the FFI boundary as native `Vec<u128>` — probe-verified under pyo3
/// 0.29 `abi3-py312`: round-trip exact, `2**128` and negative ints raise
/// `OverflowError` at the edge). Both widths share the ONE [`MaskWord`]
/// generic core, so the walk semantics cannot drift between them; an
/// anti-drift test pins the two kernels field-by-field equal on u64-fit
/// inputs.
///
/// HONEST envelope (Golden Rule #10): today's production vocabulary is 60
/// bits (`IMPLEMENTED_MASK = (1 << 60) - 1`), so this kernel is FUTURE
/// headroom — reachable in production only when the vocabulary crosses 64
/// bits; until then only tests drive it.
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepSimU128 {
    columns: SweepColumns,
    bar_bits: Vec<u128>,
}

impl ResidentSweepSimU128 {
    /// Pure constructor — the u128 twin of [`ResidentSweepSim::build`]; same
    /// `Result<_, String>` convention so the no-libpython `cargo test` link
    /// exercises the validation. Messages name `ResidentSweepSimU128`.
    fn build(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> Result<Self, String> {
        let columns = build_columns(
            "ResidentSweepSimU128",
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            bar_bits.len(),
        )?;
        Ok(Self { columns, bar_bits })
    }

    /// Simulate ONE u128 mask — test-only delegator (mirrors the u64 twin's
    /// `simulate_one`); the batch paths call the generic core directly.
    #[cfg(test)]
    fn simulate_one(
        &self,
        mask: u128,
        is_buy: bool,
        sl: Option<i64>,
        tp: Option<i64>,
    ) -> Vec<TradeRecord> {
        simulate_one_core(&self.columns, &self.bar_bits, mask, is_buy, sl, tp)
    }

    /// Pure core of [`Self::simulate_masks`] — `W = u128` delegator onto
    /// [`simulate_masks_core`].
    fn simulate_masks_impl(
        &self,
        masks: &[u128],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<Vec<TradeRecord>>, String> {
        simulate_masks_core(
            "ResidentSweepSimU128",
            &self.columns,
            &self.bar_bits,
            masks,
            is_buy,
            sl_paisa,
            tp_paisa,
        )
    }

    /// Pure core of [`Self::count_trades`] — `W = u128` delegator onto
    /// [`count_trades_core`].
    fn count_trades_impl(
        &self,
        masks: &[u128],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<u64>, String> {
        count_trades_core(
            "ResidentSweepSimU128",
            &self.columns,
            &self.bar_bits,
            masks,
            is_buy,
            sl_paisa,
            tp_paisa,
        )
    }
}

#[pymethods]
impl ResidentSweepSimU128 {
    /// Build the resident buffer — same contract as [`ResidentSweepSim::new`]
    /// with `bar_bits` widened to `Vec<u128>`. Unequal column lengths raise a
    /// clean `ValueError`; a `bar_bits` value outside `[0, 2^128)` raises
    /// `OverflowError` at the PyO3 extraction edge (probe-pinned).
    #[new]
    fn new(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        is_forced: Vec<bool>,
        session_idx: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> PyResult<Self> {
        Self::build(open, high, low, close, is_forced, session_idx, bar_bits)
            .map_err(PyValueError::new_err)
    }

    /// Number of resident bars (the only exposed view of the buffer).
    #[getter]
    fn n_bars(&self) -> usize {
        self.columns.open.len()
    }

    /// Simulate a BATCH of u128 masks — the wide twin of
    /// [`ResidentSweepSim::simulate_masks`] (same hardcoded worst-case law,
    /// same serial/parallel threshold, same R2-RC-F1 SL/TP boundary →
    /// `ValueError`).
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn simulate_masks(
        &self,
        masks: Vec<u128>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<Vec<TradeRecord>>> {
        self.simulate_masks_impl(&masks, is_buy, sl_paisa, tp_paisa)
            .map_err(PyValueError::new_err)
    }

    /// Per-mask trade COUNT only — the wide twin of
    /// [`ResidentSweepSim::count_trades`].
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn count_trades(
        &self,
        masks: Vec<u128>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<u64>> {
        self.count_trades_impl(&masks, is_buy, sl_paisa, tp_paisa)
            .map_err(PyValueError::new_err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a sim with explicit per-column data, no forced bars, and every bar
    /// in ONE session (test helper — the single-session case is byte-identical
    /// to the pre-session-gate kernel). Every fill below follows the ONE
    /// hardcoded worst-case adverse-max law (DEC-FILL-WORST-CASE-ONLY-001);
    /// each expected record is HAND-DERIVED in its test's comment.
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
            entry_session: 0,
            sl_level_paisa: None,
            tp_level_paisa: None,
            mfe_paisa: 0,
            mae_paisa: 0,
            last_idx: 0,
            last_exit_basis_paisa: entry,
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
        let w = Walker::new(is_buy, None, None);
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

    #[test]
    fn resolve_level_hit_same_bar_tie_is_stop_first() {
        // The hardcoded pessimistic tie (DEC-FILL-WORST-CASE-ONLY-001; the
        // retired `SameBarTie::TpFirst` optimism knob is DELETED): a bar
        // spanning BOTH levels resolves to the STOP, always — both
        // directions. (Under the adverse-max fill the two legs price at the
        // SAME adverse extreme, so the tie is numerically inert in this
        // kernel — the Python twin still distinguishes the exit REASON,
        // which is where the tie stays observable.)
        let (w, pos) = walker_with_pos(true, Some(95), Some(110));
        assert_eq!(w.resolve_level_hit(&pos, 115, 90), Some((true, 95)));
        let (ws, poss) = walker_with_pos(false, Some(110), Some(95));
        assert_eq!(ws.resolve_level_hit(&poss, 115, 90), Some((true, 110)));
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

        let run = || s.simulate_one(0x1, true, Some(200), Some(400));
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
            s.simulate_one(0, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn single_bar_never_opens_a_trade() {
        // Entry arms on bar 0 but there is no NEXT bar to fill the open against,
        // and finish() ignores pending_entry → zero trades. Mirrors Python.
        let s = sim(vec![100], vec![110], vec![90], vec![105], vec![0xFF]);
        assert_eq!(
            s.simulate_one(0, true, None, None),
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
            s.simulate_one(1u64 << 63, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    // ── basic long round-trip: end-of-data close at the adverse extreme ──

    #[test]
    fn long_entry_then_end_of_data_close_at_adverse_extreme() {
        // Entry = high[1] = 110 (BUY fills at the entry bar's HIGH). Folds
        // from that worst entry: bar1 (110,95) → fav 0 / adv 15; bar2
        // (130,118) → fav 20 / adv 0 → mfe 20, mae 15. End-of-data close at
        // the last bar's LOW (118, the long exit's adverse extreme):
        // pnl = 118 − 110 = 8, holding 1..=2 inclusive = 2.
        let s = sim(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 130],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, None);
        assert_eq!(out, vec![(8i64, 20i64, 15i64, 2u64, 1u64, 2u64)]);
    }

    // ── stop-loss / take-profit fills (long) ──

    #[test]
    fn long_stop_loss_fills_at_bar_low() {
        // Entry = high[1] = 105; sl=10 anchors on the ACTUAL worst fill →
        // level 95. bar1 folds (105,98) → fav 0 / adv 7; no hit (98 > 95).
        // bar2 folds (99,85) → fav 0 / adv 20; hit (85 ≤ 95) → fill =
        // min(level 95, low 85) = 85 (a long SL trigger implies low ≤ level,
        // so the fill is ALWAYS the bar low — the honest gap-through loss):
        // pnl = 85 − 105 = −20.
        let s = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 95],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, Some(10), None);
        assert_eq!(out, vec![(-20i64, 0i64, 20i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn long_stop_loss_gap_through_fills_at_low() {
        // The gap-through teeth on a signal-scoped mask: entry = high[1] =
        // 105 (mask 1 fires only bar0); sl=10 → level 95. bar1 folds
        // (105,98) → fav 0 / adv 7; bar2 gaps through (low 80 ≤ 95) → fill
        // min(95, 80) = 80, never the flattering at-level 95. Folds: bar2
        // (88,80) → fav 0 / adv 25. pnl = 80 − 105 = −25.
        let s = sim(
            vec![100, 100, 85],
            vec![101, 105, 88],
            vec![99, 98, 80],
            vec![100, 100, 82],
            vec![1, 0, 0],
        );
        let out = s.simulate_one(1, true, Some(10), None);
        assert_eq!(out, vec![(-25i64, 0i64, 25i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn long_take_profit_fills_at_clamped_bar_low() {
        // Entry = high[1] = 105; tp=20 → level 125. bar1 folds (105,98) →
        // fav 0 / adv 7; no hit (105 < 125). bar2 folds (125,113) → fav 20 /
        // adv 0 → mfe 20, mae 7. TP triggers (high 125 ≥ 125) but the
        // adverse-max fill is min(level 125, low 113) = 113 — a "take
        // profit" priced at the worst printed sell, the anti-illusion
        // floor: pnl = 113 − 105 = 8, NOT the 20-paisa tp distance.
        let s = sim(
            vec![50, 100, 115],
            vec![60, 105, 125],
            vec![40, 98, 113],
            vec![55, 100, 118],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, Some(20));
        assert_eq!(out, vec![(8i64, 20i64, 7i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn long_tp_gap_up_clamps_at_level_never_overstates() {
        // COUNTEREXAMPLE 1 teeth: entry HIGH[1]=105, tp=20 → level 125. bar2
        // sits ENTIRELY ABOVE the target (low 126 > 125): a naive
        // sell-at-low would fill 126 (pnl 21 — OVERSTATING vs an at-level
        // fill); the adverse-max law fills min(125, 126) = 125 →
        // pnl exactly the 20-paisa tp distance.
        let s = sim(
            vec![100, 100, 130],
            vec![101, 105, 140],
            vec![99, 98, 126],
            vec![100, 100, 135],
            vec![1, 0, 0],
        );
        let out = s.simulate_one(1, true, None, Some(20));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 20); // fill at LEVEL 125, never the bar low 126
    }

    // ── same-bar SL+TP: the stop resolves first (numerically inert fill) ──

    #[test]
    fn same_bar_sl_and_tp_resolves_stop_first() {
        // Entry = high[1] = 105; sl=10 → 95, tp=10 → 115. bar2 spans BOTH
        // (low 85 ≤ 95, high 115 ≥ 115) → the STOP resolves first
        // (hardcoded pessimistic tie). Fill = min(95, 85) = 85 → pnl =
        // 85 − 105 = −20. (Under adverse-max both legs would price at the
        // same bar low here — the tie stays observable via the Python
        // twin's exit REASON; this pin guards the resolver's stop-first
        // law end-to-end.) Folds: bar1 (105,98) → 0/7; bar2 (115,85) →
        // fav 10 / adv 20.
        let s = sim(
            vec![50, 100, 100],
            vec![60, 105, 115],
            vec![40, 98, 85],
            vec![55, 100, 100],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, true, Some(10), Some(10));
        assert_eq!(out, vec![(-20i64, 10i64, 20i64, 2u64, 1u64, 2u64)]);
    }

    // ── short round-trips ──

    #[test]
    fn short_take_profit_fills_at_bar_high() {
        // SHORT entry = low[1] = 98 (a sell fills at the entry bar's LOW);
        // tp=20 → level 78. bar1 folds (105,98) → fav 0 / adv 7; no hit.
        // bar2 folds (90,75) → fav 23 / adv 0 → mfe 23, mae 7. TP triggers
        // (low 75 ≤ 78) but the cover fills at max(level 78, high 90) = 90:
        // pnl = 98 − 90 = 8 — the worst printed cover.
        let s = sim(
            vec![50, 100, 85],
            vec![60, 105, 90],
            vec![40, 98, 75],
            vec![55, 100, 82],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, false, None, Some(20));
        assert_eq!(out, vec![(8i64, 23i64, 7i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn short_tp_gap_down_clamps_at_level() {
        // The short mirror of counterexample 1: entry = low[1] = 95; tp=15 →
        // level 80. bar2 sits ENTIRELY BELOW the level (high 75 < 80) →
        // cover fill = max(80, 75) = 80 (the level, never the better bar
        // high) → pnl = 95 − 80 = 15 exactly. Folds: bar1 (102,95) → fav 0 /
        // adv 7; bar2 (75,70) → fav 25 / adv 0.
        let s = sim(
            vec![100, 100, 74],
            vec![101, 102, 75],
            vec![99, 95, 70],
            vec![100, 100, 72],
            vec![1, 0, 0],
        );
        let out = s.simulate_one(1, false, None, Some(15));
        assert_eq!(out, vec![(15i64, 25i64, 7i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn short_stop_loss_fills_at_bar_high() {
        // SHORT entry = low[1] = 98; sl=10 → level 108. bar1 folds (105,98)
        // → fav 0 / adv 7; no hit (105 < 108). bar2 folds (115,106) → fav 0
        // / adv 17. SL triggers (high 115 ≥ 108) → cover fill =
        // max(108, 115) = 115 (a short SL trigger implies high ≥ level, so
        // the fill is ALWAYS the bar high): pnl = 98 − 115 = −17.
        let s = sim(
            vec![50, 100, 108],
            vec![60, 105, 115],
            vec![40, 98, 106],
            vec![55, 100, 112],
            vec![0, 0, 0],
        );
        let out = s.simulate_one(0, false, Some(10), None);
        assert_eq!(out, vec![(-17i64, 0i64, 17i64, 2u64, 1u64, 2u64)]);
    }

    // ── forced 15:20 exit ──

    #[test]
    fn forced_exit_overrides_and_fills_at_adverse_extreme() {
        // Entry = high[1] = 110; sl=50 → 60, tp=20 → 130: neither triggers
        // on bar1 (low 95 > 60, high 110 < 130). bar2 is_forced AND tp
        // would trigger (high 130 ≥ 130) — but forced overrides → fill at
        // the forced bar's LOW (123), never the tp level; the fold stays
        // the legacy (open, open) = (125, 125) → fav 15 / adv 0.
        // pnl = 123 − 110 = 13; mfe 15, mae 15 (bar1's adv).
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
        let out = s.simulate_one(0, true, Some(50), Some(20));
        assert_eq!(out, vec![(13i64, 15i64, 15i64, 2u64, 1u64, 2u64)]);
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
            s.simulate_one(1, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    // ── cross-session pending-entry drop (R2-OC-2) ──────────────────────────

    #[test]
    fn cross_session_pending_entry_dropped() {
        // mask=1 fires ONLY on bar1 — the LAST bar of session 0 (e.g. 15:29 IST).
        // bar2 is the FIRST bar of session 1 (next day 09:15, an overnight gap):
        // the pending entry must be DROPPED, never filled at session 1's open.
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
            s.simulate_one(1, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn same_session_pending_entry_still_fills() {
        // The SAME feed shape but with every bar in one session: the pending
        // entry armed on bar1 fills on bar2 as before (the gate only drops
        // CROSS-session fills; kills a `==`→`!=` mutant on the session
        // compare). Entry = high[2] = 12000; folds: bar2 (12000,11800) →
        // fav 0 / adv 200; bar3 (12050,11850) → fav 50 / adv 150 → mfe 50,
        // mae 200. End-of-data close at low[3] = 11850 → pnl = −150.
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
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-150i64, 50i64, 200i64, 2u64, 2u64, 3u64)]);
    }

    #[test]
    fn cross_session_drop_then_rearm_fills_next_bar() {
        // Signal fires on bar1 (session 0's last bar → dropped at bar2) AND on
        // bar2 (session 1's first bar → legitimately fills on bar3):
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
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out.len(), 1);
        let (_pnl, _mfe, _mae, _holding, entry_idx, _exit_idx) = out[0];
        assert_eq!(entry_idx, 3); // opened at bar3 (session 1), NOT bar2
    }

    // ── cross-session OPEN-position close (DEC-TRADESIM-OPEN-SESSION-GUARD-001) ──

    #[test]
    fn cross_session_open_position_closes_at_prior_session_adverse_extreme() {
        // Directed hand-computed boundary close: signal bar0 → entry =
        // high[1] = 10120 (session 0). bar1 folds (10120,9950) → fav 0 /
        // adv 170; bar2 folds (10150,9960) → fav 30 / adv 160 → mfe 30,
        // mae 170. bar3 is SESSION 1 → the guard closes retroactively at
        // bar2's exit basis — its LOW (9960, the adverse extreme) — with
        // exit_idx = last_idx = 2 (the finish() idiom): pnl = 9960 − 10120
        // = −160, holding 2. The +1800 gap bar's range never enters pnl or
        // the excursions.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 11900],
            vec![10100, 10120, 10150, 12000],
            vec![9900, 9950, 9960, 11800],
            vec![10000, 10050, 10100, 11900],
            vec![false, false, false, false],
            vec![0, 0, 0, 1],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-160i64, 30i64, 170i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn cross_session_boundary_clears_pending_exit_and_new_position_rides() {
        // Walker-driven (the Mode-B exit-signal stream never crosses the
        // sweep FFI): a Mode-B pending exit armed on session 0's LAST stored
        // bar is SUBSUMED by the boundary close — never filled on session
        // 1's bar — AND the clear has teeth: a NEW session-1 position must
        // ride to finish (a stale pending would close it instantly).
        // Trade 1: entry = high[1] = 10060; folds bar1 (10060,9960) → 0/100,
        // bar2 (10000,9940) → 0/120; bar2 arms the Mode-B exit; bar3 is
        // session 1 → guard closes at bar2's LOW (9940): pnl = 9940 − 10060
        // = −120, exit_idx 2, holding 2. Trade 2: signal bar4 → entry =
        // high[5] = 12020; folds bar5 (12020,11900) → 0/120, bar6
        // (12030,11910) → 10/110 → mfe 10, mae 120; finish() closes at
        // bar6's LOW (11910): pnl = −110, exit_idx 6, holding 2.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, i64, bool, bool); 7] = [
            // (open, high, low, close, session, entry_signal, exit_signal)
            (10000, 10050, 9900, 10000, 0, true, false),
            (10000, 10060, 9960, 10010, 0, false, false),
            (9990, 10000, 9940, 9980, 0, false, true),
            (11900, 12000, 11880, 11900, 1, false, false),
            (11910, 12010, 11890, 11910, 1, true, false),
            (11920, 12020, 11900, 11920, 1, false, false),
            (11930, 12030, 11910, 11940, 1, false, false),
        ];
        let mut trades: Vec<TradeRecord> = Vec::new();
        for (i, &(o, h, l, c, sess, entry, exit)) in bars.iter().enumerate() {
            if let Some(t) = w.push(o, h, l, c, i, false, sess, entry, exit) {
                trades.push(t);
            }
        }
        if let Some(t) = w.finish() {
            trades.push(t);
        }
        assert_eq!(
            trades,
            vec![
                (-120i64, 0i64, 120i64, 2u64, 1u64, 2u64),
                (-110i64, 10i64, 120i64, 2u64, 5u64, 6u64),
            ]
        );
    }

    #[test]
    fn cross_session_boundary_bar_does_not_arm_earliest_rearm_next_bar() {
        // §2.2 pinned law: the boundary bar EMITS the session_gap close, so the
        // step-(C) `closed.is_none()` gate suppresses arming on it — a signal
        // on the boundary bar (bar3) produces NO trade from it; the earliest
        // re-arm is bar i+1 (bar4's signal) and the earliest new fill is bar
        // i+2 (bar5). Trade 1 is the boundary close of the sibling test:
        // (−160, 30, 170, 2, 1, 2).
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 11900, 11910, 11920, 11930],
            vec![10100, 10120, 10150, 12000, 12010, 12020, 12030],
            vec![9900, 9950, 9960, 11800, 11810, 11820, 11830],
            vec![10000, 10050, 10100, 11900, 11910, 11920, 11940],
            vec![false; 7],
            vec![0, 0, 0, 1, 1, 1, 1],
            vec![1, 0, 0, 1, 1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0], (-160i64, 30i64, 170i64, 2u64, 1u64, 2u64));
        let (_pnl, _mfe, _mae, _holding, entry_idx, exit_idx) = out[1];
        assert_eq!(entry_idx, 5); // bar4's signal fills at bar5 — never bar3/bar4
        assert_eq!(exit_idx, 6);
    }

    #[test]
    fn cross_session_boundary_close_equals_finish_numerics() {
        // The finish()-equivalence law: the SAME session-0 prefix closed by
        // (a) the boundary guard when the feed continues into session 1 vs
        // (b) `finish()` when the feed truncates at session 0's last stored
        // bar must emit bit-equal records (same fill price / indices /
        // excursions — suffix independence by construction).
        let prefix_open = vec![10000i64, 10000, 10010];
        let prefix_high = vec![10100i64, 10120, 10150];
        let prefix_low = vec![9900i64, 9950, 9960];
        let prefix_close = vec![10000i64, 10050, 10100];
        let continued = ResidentSweepSim::build(
            [prefix_open.clone(), vec![11900]].concat(),
            [prefix_high.clone(), vec![12000]].concat(),
            [prefix_low.clone(), vec![11800]].concat(),
            [prefix_close.clone(), vec![11900]].concat(),
            vec![false; 4],
            vec![0, 0, 0, 1],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let truncated = ResidentSweepSim::build(
            prefix_open,
            prefix_high,
            prefix_low,
            prefix_close,
            vec![false; 3],
            vec![0, 0, 0],
            vec![1, 0, 0],
        )
        .unwrap();
        let boundary = continued.simulate_one(1, true, None, None);
        let finished = truncated.simulate_one(1, true, None, None);
        assert_eq!(boundary, finished);
        assert_eq!(boundary.len(), 1);
    }

    #[test]
    fn cross_session_backward_session_ordinal_still_closes_boundary() {
        // The (A2) compare is an EQUALITY law: ANY session-ordinal CHANGE
        // closes the open position — either direction, not only forward.
        // `build_columns` admits any i64 `session_idx` (the ordinal is
        // "compared for equality ONLY" — no monotonicity check), so a
        // DECREASING ordinal is API-reachable; production sweep feeds are
        // chronological (Catalog/ReplayFeed sorted), hence LOW — which is
        // exactly why the r8 mutation sweep found the `!=`→`>` mutant (M14)
        // surviving BOTH language nets: no prior test fed a backward session
        // change to an OPEN position. (The Python walker carries the same
        // law's defense-in-depth twin on its IST-date compare.)
        // Signal bar0 → entry = high[1] = 10120 (session 5); bar1 folds
        // (10120,9950) → mfe 0 / mae 170, exit basis = low[1] = 9950. bar2
        // DROPS to session 4 → the guard closes retroactively at 9950 with
        // exit_idx = last_idx = 1 (pnl = 9950 − 10120 = −170, holding 1).
        // Under the `>` mutant 4 > 5 is false, so the backward bar folds
        // into the position and it rides to the end-of-data close at
        // low[3] = 9500 (pnl −620) — the killer.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 9500, 9600],
            vec![10100, 10120, 9550, 9650],
            vec![9900, 9950, 9400, 9500],
            vec![10000, 10110, 9500, 9610],
            vec![false, false, false, false],
            vec![5, 5, 4, 4],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-170i64, 0i64, 170i64, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn same_session_multi_bar_hold_does_not_close_early() {
        // The `!=`→`==` mutant killer on the guard's session compare (the
        // `same_session_pending_entry_still_fills` pattern): a multi-bar hold
        // whose bars ALL share one NON-ZERO session ordinal must ride to the
        // end-of-data close — under the mutated compare (or a session-field
        // stamping bug) the guard would fire on the first managed bar instead.
        // Entry = high[1] = 10120; folds: bar1 (10120,9950) → 0/170; bar2
        // (10150,9960) → 30/160; bar3 (10160,9970) → 40/150 → mfe 40,
        // mae 170. End-of-data close at low[3] = 9970 → pnl = −150.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 10020],
            vec![10100, 10120, 10150, 10160],
            vec![9900, 9950, 9960, 9970],
            vec![10000, 10050, 10100, 10140],
            vec![false; 4],
            vec![5, 5, 5, 5],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-150i64, 40i64, 170i64, 3u64, 1u64, 3u64)]);
    }

    // ── Mode-B pending exits (walker-driven — no exit stream crosses the FFI) ──

    #[test]
    fn mode_b_pending_exit_fills_at_adverse_extreme_fold_unchanged() {
        // A Mode-B exit armed on bar2 fills on bar3 at its LOW (78) while
        // the excursion still folds the legacy (open, open) = (80, 80) —
        // mae reads 25 (from the fold), NOT the 27 the fill implies: the
        // fold logic is deliberately untouched
        // (DEC-FILL-WORST-CASE-ONLY-001). Entry = high[1] = 105; folds:
        // bar1 (105,98) → 0/7; bar2 (104,99) → 0/6; bar3 (80,80) → 0/25.
        // pnl = 78 − 105 = −27, holding 3.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 4] = [
            // (open, high, low, close, entry_signal, exit_signal)
            (100, 100, 100, 100, true, false),
            (100, 105, 98, 100, false, false),
            (100, 104, 99, 100, false, true),
            (80, 82, 78, 81, false, false),
        ];
        let mut trades: Vec<TradeRecord> = Vec::new();
        for (i, &(o, h, l, c, entry, exit)) in bars.iter().enumerate() {
            if let Some(t) = w.push(o, h, l, c, i, false, 0, entry, exit) {
                trades.push(t);
            }
        }
        assert!(w.finish().is_none());
        assert_eq!(trades, vec![(-27i64, 0i64, 25i64, 3u64, 1u64, 3u64)]);
    }

    #[test]
    fn mode_b_pending_exit_at_end_of_data_closes_at_last_bar_extreme() {
        // A Mode-B exit still pending when the feed ends is IGNORED by
        // finish(), which closes at the last managed bar's exit basis — its
        // LOW (85). Entry = high[1] = 105; folds: bar1 (105,98) → 0/7; bar2
        // (99,85) → 0/20. pnl = 85 − 105 = −20, exit_idx 2, holding 2.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 3] = [
            (100, 100, 100, 100, true, false),
            (100, 105, 98, 100, false, false),
            (95, 99, 85, 90, false, true),
        ];
        let mut trades: Vec<TradeRecord> = Vec::new();
        for (i, &(o, h, l, c, entry, exit)) in bars.iter().enumerate() {
            if let Some(t) = w.push(o, h, l, c, i, false, 0, entry, exit) {
                trades.push(t);
            }
        }
        if let Some(t) = w.finish() {
            trades.push(t);
        }
        assert_eq!(trades, vec![(-20i64, 0i64, 20i64, 2u64, 1u64, 2u64)]);
    }

    // ── multiple non-overlapping trades from one mask ──

    #[test]
    fn multiple_trades_non_overlapping() {
        // Entry bars (1, 4) are FLAT at 100 (high == open) so the worst
        // entry is 100; tp=5 → level 105. Trigger bars (2, 5) print
        // high 110 / low 106 → fill = min(105, 106) = 105 → pnl 5 each.
        let bar_bits = vec![1u64, 0, 0, 1, 0, 0];
        let s = sim(
            vec![100, 100, 106, 100, 100, 106],
            vec![100, 100, 110, 100, 100, 110],
            vec![100, 100, 106, 100, 100, 106],
            vec![100, 100, 106, 100, 100, 106],
            bar_bits,
        );
        let out = s.simulate_one(1, true, None, Some(5));
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
            .simulate_masks_impl(&masks, true, Some(8), Some(5))
            .expect("in-range SL/TP must pass the boundary");
        for (j, &m) in masks.iter().enumerate() {
            assert_eq!(batch[j], s.simulate_one(m, true, Some(8), Some(5)));
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
            .count_trades_impl(&masks, true, Some(8), Some(5))
            .expect("in-range SL/TP must pass the boundary");
        let full = s
            .simulate_masks_impl(&masks, true, Some(8), Some(5))
            .expect("in-range SL/TP must pass the boundary");
        let expected: Vec<u64> = full.iter().map(|t| t.len() as u64).collect();
        assert_eq!(counts, expected);
    }

    #[test]
    fn empty_masks_batch_is_empty() {
        let s = sim(vec![100], vec![100], vec![100], vec![100], vec![0]);
        assert_eq!(
            s.simulate_masks_impl(&[], true, None, None)
                .expect("empty batch ok"),
            Vec::<Vec<TradeRecord>>::new()
        );
        assert_eq!(
            s.count_trades_impl(&[], true, None, None)
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
            .simulate_masks_impl(&masks, true, Some(3), Some(4))
            .expect("in-range SL/TP must pass the boundary");
        let serial: Vec<Vec<TradeRecord>> = masks
            .iter()
            .map(|&m| s.simulate_one(m, true, Some(3), Some(4)))
            .collect();
        assert_eq!(batch, serial);
    }

    // ── excursion accumulation over still-open bars ──

    #[test]
    fn excursion_accumulates_over_open_bars() {
        // Entry = high[1] = 130 (the worst fill). Folds from it: bar1
        // (130,95) → fav 0 / adv 35; bar2 (105,70) → fav 0 / adv 60; bar3
        // (102,99) → fav 0 / adv 31 → mfe 0, mae 60.
        let s = sim(
            vec![50, 100, 100, 100],
            vec![60, 130, 105, 102],
            vec![40, 95, 70, 99],
            vec![55, 100, 100, 101],
            vec![0, 0, 0, 0],
        );
        let out = s.simulate_one(0, true, None, None);
        assert_eq!(out.len(), 1);
        let (_pnl, mfe, mae, _h, _e, _x) = out[0];
        assert_eq!(mfe, 0);
        assert_eq!(mae, 60);
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
                s.simulate_masks_impl(&[1], true, bad, None).is_err(),
                "sl_paisa={bad:?} must be rejected"
            );
            assert!(
                s.simulate_masks_impl(&[1], true, None, bad).is_err(),
                "tp_paisa={bad:?} must be rejected"
            );
            assert!(
                s.count_trades_impl(&[1], true, bad, bad).is_err(),
                "count_trades sl/tp={bad:?} must be rejected"
            );
        }
    }

    #[test]
    fn boundary_extremes_compute_cleanly_without_panic() {
        // The derivation's TEETH: with every input at the ceiling, the worst
        // intermediates actually occur and none panics. Entry signal on bar0
        // → entry = high[1] = C (the worst BUY fill); levels: sl = C − C = 0,
        // tp = C + C = 2C (the max level magnitude). bar1 folds (C, −C) →
        // fav 0 / adv 2C (the max excursion intermediate). SL triggers
        // (low −C ≤ 0) → fill = min(0, −C) = −C → pnl = −C − C = −2C
        // (|pnl| ≤ 3C — the derivation's bound), holding 1.
        let s = sim(
            vec![0, -C, 0],
            vec![0, C, 0],
            vec![0, -C, 0],
            vec![0, 0, C],
            vec![1, 0, 0],
        );
        let out = s
            .simulate_masks_impl(&[1], true, Some(C), Some(C))
            .expect("exact-ceiling SL/TP must pass the boundary");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0], vec![(-2 * C, 0i64, 2 * C, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn golden_valid_inputs_identical_through_validated_path() {
        // Regression golden: the exact `long_stop_loss_fills_at_bar_low`
        // scenario run through the validated batch entry emits byte-identical
        // trades (the guard must not perturb any in-range computation).
        let s = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 95],
            vec![0, 0, 0],
        );
        let out = s
            .simulate_masks_impl(&[0], true, Some(10), None)
            .expect("in-range inputs must pass");
        // Entry = high[1] = 105; bar1 folds (105,98) → 0/7; bar2 folds
        // (99,85) → 0/20, then low 85 ≤ sl_level 95 → fill min(95, 85) = 85:
        // pnl = 85 − 105 = −20; holding 1..=2 inclusive = 2.
        assert_eq!(out, vec![vec![(-20i64, 0i64, 20i64, 2u64, 1u64, 2u64)]]);
        let counts = s
            .count_trades_impl(&[0], true, Some(10), None)
            .expect("in-range inputs must pass");
        assert_eq!(counts, vec![1]);
    }

    // ── batch entries price the worst-case law (both widths) ───────────────

    #[test]
    fn batch_entries_price_worst_case_law() {
        // The FFI plumbing pin: the flagless batch entries reproduce the
        // per-mask worst-case walk, and `count_trades` agrees — on BOTH
        // kernel widths (the MaskWord core is shared, so one drift would
        // break the bridge pin too). Values from
        // `long_stop_loss_gap_through_fills_at_low`.
        let s = sim(
            vec![100, 100, 85],
            vec![101, 105, 88],
            vec![99, 98, 80],
            vec![100, 100, 82],
            vec![1, 0, 0],
        );
        let batch = s
            .simulate_masks_impl(&[1], true, Some(10), None)
            .expect("in-range");
        assert_eq!(batch, vec![vec![(-25i64, 0i64, 25i64, 2u64, 1u64, 2u64)]]);
        let counts = s
            .count_trades_impl(&[1], true, Some(10), None)
            .expect("in-range");
        assert_eq!(counts, vec![1]);
        let s128 = sim_u128(
            vec![100, 100, 85],
            vec![101, 105, 88],
            vec![99, 98, 80],
            vec![100, 100, 82],
            vec![1, 0, 0],
        );
        let batch128 = s128
            .simulate_masks_impl(&[1], true, Some(10), None)
            .expect("in-range");
        assert_eq!(batch128, batch);
    }

    // ── ResidentSweepSimU128 (DEC-TRADE-SIM-U128-KERNEL-001) ────────────────

    /// u128 twin of [`sim`] — no forced bars, one session.
    fn sim_u128(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> ResidentSweepSimU128 {
        let n = open.len();
        ResidentSweepSimU128::build(open, high, low, close, vec![false; n], vec![0; n], bar_bits)
            .expect("equal-length test data")
    }

    #[test]
    fn u128_build_rejects_length_mismatch_naming_the_class() {
        let err = ResidentSweepSimU128::build(
            vec![1, 2],
            vec![1],
            vec![1, 2],
            vec![1, 2],
            vec![false, false],
            vec![0, 0],
            vec![0, 0],
        )
        .map(|_| ())
        .expect_err("length mismatch must be rejected");
        assert!(err.contains("ResidentSweepSimU128: column length mismatch"));
    }

    #[test]
    fn u128_build_rejects_out_of_range_price_naming_the_class() {
        // Validation parity with the u64 twin: same R2-RC-F1 boundary, message
        // names the WIDE class (never the u64 sibling's label).
        let err = ResidentSweepSimU128::build(
            vec![100, i64::MAX],
            vec![100, 100],
            vec![100, 100],
            vec![100, 100],
            vec![false, false],
            vec![0, 0],
            vec![0, 0],
        )
        .map(|_| ())
        .expect_err("out-of-range price must be rejected at build");
        assert!(err.contains("overflow-safe range"));
        assert!(err.starts_with("ResidentSweepSimU128:"));
    }

    #[test]
    fn u128_level_distance_validation_parity() {
        // The same SL/TP boundary set the u64 twin rejects, rejected with the
        // wide class's own label on BOTH batch entries.
        let s = sim_u128(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 130],
            vec![1, 0, 0],
        );
        for bad in [Some(i64::MAX), Some(i64::MIN), Some(-1), Some(C + 1)] {
            let sim_err = s
                .simulate_masks_impl(&[1], true, bad, None)
                .map(|_| ())
                .expect_err("sl_paisa out of range must be rejected");
            assert!(sim_err.starts_with("ResidentSweepSimU128:"), "{sim_err}");
            let cnt_err = s
                .count_trades_impl(&[1], true, None, bad)
                .map(|_| ())
                .expect_err("tp_paisa out of range must be rejected");
            assert!(cnt_err.starts_with("ResidentSweepSimU128:"), "{cnt_err}");
        }
        // Exactly the ceiling stays valid (inclusive) — parity with the u64 arm.
        assert!(s.simulate_masks_impl(&[1], true, Some(C), Some(C)).is_ok());
    }

    #[test]
    fn u128_empty_window_and_n_bars() {
        let s = ResidentSweepSimU128::build(vec![], vec![], vec![], vec![], vec![], vec![], vec![])
            .expect("empty ok");
        assert_eq!(s.n_bars(), 0);
        assert_eq!(
            s.simulate_masks_impl(&[], true, None, None)
                .expect("empty batch ok"),
            Vec::<Vec<TradeRecord>>::new()
        );
        let two = sim_u128(vec![1, 2], vec![3, 4], vec![0, 1], vec![2, 3], vec![0, 0]);
        assert_eq!(two.n_bars(), 2);
    }

    #[test]
    fn u128_mask_bit_63_64_127_boundaries() {
        // bar0 carries bits 63+64 (STRADDLES the u64/u128 limb boundary), bar2
        // carries bit 127 (the top bit). Each mask must fire ONLY on its bar:
        //   * (1<<63)|(1<<64) → fires bar0 → entry = high[1] = 110 (the worst
        //     BUY fill); folds bar1 (110,92) → 0/18, bar2 (100,100) → 0/10,
        //     bar3 (108,100) → 0/10 → mfe 0, mae 18; end-of-data close at
        //     low[3] = 100 → pnl −10, holding 3.
        //   * 1<<64 → bar0 is a SUPERSET (bit63 extra) → same trade.
        //   * 1<<127 → fires bar2 → entry = high[3] = 108; fold bar3
        //     (108,100) → 0/8; end-of-data close at low[3] = 100 → pnl −8.
        //   * (1<<63)|(1<<127) → NO bar carries both → zero trades (the
        //     superset test must not truncate either limb).
        let bits: Vec<u128> = vec![(1u128 << 63) | (1u128 << 64), 0, 1u128 << 127, 0];
        let s = sim_u128(
            vec![100, 100, 100, 100],
            vec![100, 110, 100, 108],
            vec![100, 92, 100, 100],
            vec![100, 100, 100, 100],
            bits,
        );
        let straddle = s.simulate_one((1u128 << 63) | (1u128 << 64), true, None, None);
        assert_eq!(straddle, vec![(-10i64, 0i64, 18i64, 3u64, 1u64, 3u64)]);
        let hi_only = s.simulate_one(1u128 << 64, true, None, None);
        assert_eq!(hi_only, straddle); // bar0 is a superset of the lone bit-64 mask
        let top_bit = s.simulate_one(1u128 << 127, true, None, None);
        assert_eq!(top_bit, vec![(-8i64, 0i64, 8i64, 1u64, 3u64, 3u64)]);
        let cross = s.simulate_one((1u128 << 63) | (1u128 << 127), true, None, None);
        assert_eq!(cross, Vec::<TradeRecord>::new());
    }

    #[test]
    fn u128_all_ones_mask_requires_all_ones_bar() {
        // mask = u128::MAX fires ONLY on an all-ones bar; a bar missing just
        // bit 0 (or just bit 127) is NOT a superset — kills any masking/
        // truncation mutant on either limb of the compare.
        let bits: Vec<u128> = vec![u128::MAX, u128::MAX - 1, u128::MAX >> 1, u128::MAX];
        let s = sim_u128(
            vec![100, 100, 100, 100],
            vec![100, 110, 100, 108],
            vec![100, 92, 100, 100],
            vec![100, 101, 100, 100],
            bits,
        );
        let out = s.simulate_one(u128::MAX, true, None, None);
        // Fires bar0 → entry = high[1] = 110; bar1/bar2 not supersets (no
        // re-arm); fires bar3 (all-ones) but the position is open → rides to
        // the end-of-data close at low[3] = 100 → pnl −10; folds as in the
        // boundary test → mfe 0, mae 18.
        assert_eq!(out, vec![(-10i64, 0i64, 18i64, 3u64, 1u64, 3u64)]);
    }

    #[test]
    fn u128_bridge_matches_u64_kernel_on_u64_fit_inputs() {
        // ANTI-DRIFT BRIDGE PIN: on inputs that fit u64, the u128 kernel is
        // field-by-field identical to the u64 kernel (both ride the ONE
        // MaskWord core — this pin makes any future de-sharing loud).
        let open = vec![100i64, 100, 100, 100];
        let high = vec![100i64, 110, 100, 108];
        let low = vec![100i64, 92, 100, 100];
        let close = vec![100i64, 100, 100, 100];
        let bits_u64: Vec<u64> = vec![1, 0, 1, 1 << 63];
        let bits_u128: Vec<u128> = bits_u64.iter().map(|&b| b as u128).collect();
        let s64 = sim(
            open.clone(),
            high.clone(),
            low.clone(),
            close.clone(),
            bits_u64,
        );
        let s128 = sim_u128(open, high, low, close, bits_u128);
        let masks_u64 = vec![0u64, 1, 1 << 63];
        let masks_u128: Vec<u128> = masks_u64.iter().map(|&m| m as u128).collect();
        let out64 = s64
            .simulate_masks_impl(&masks_u64, true, Some(8), Some(5))
            .expect("in-range");
        let out128 = s128
            .simulate_masks_impl(&masks_u128, true, Some(8), Some(5))
            .expect("in-range");
        assert_eq!(out64, out128);
        let cnt64 = s64
            .count_trades_impl(&masks_u64, true, Some(8), Some(5))
            .expect("in-range");
        let cnt128 = s128
            .count_trades_impl(&masks_u128, true, Some(8), Some(5))
            .expect("in-range");
        assert_eq!(cnt64, cnt128);
    }

    #[test]
    fn u128_parallel_arm_matches_serial_with_wide_masks() {
        // rayon out[j] ↔ masks[j] alignment ABOVE the work threshold, with
        // masks/bar_bits genuinely wider than 64 bits.
        let n = 1024usize;
        let open: Vec<i64> = (0..n as i64).map(|i| 1000 + (i % 7)).collect();
        let high: Vec<i64> = open.iter().map(|&o| o + 5).collect();
        let low: Vec<i64> = open.iter().map(|&o| o - 5).collect();
        let close: Vec<i64> = open.clone();
        let bar_bits: Vec<u128> = (0..n as u128)
            .map(|i| (i & 0b111) | (1u128 << (64 + (i % 50) as u32)))
            .collect();
        let s = sim_u128(open, high, low, close, bar_bits);
        let masks: Vec<u128> = (0..1100u128)
            .map(|i| (i & 0b1) | (1u128 << (64 + (i % 50) as u32)))
            .collect();
        assert!((masks.len() as u64) * (n as u64) >= PARALLEL_WORK_THRESHOLD);
        let batch = s
            .simulate_masks_impl(&masks, true, Some(3), Some(4))
            .expect("in-range SL/TP must pass the boundary");
        let serial: Vec<Vec<TradeRecord>> = masks
            .iter()
            .map(|&m| s.simulate_one(m, true, Some(3), Some(4)))
            .collect();
        assert_eq!(batch, serial);
        let counts = s
            .count_trades_impl(&masks, true, Some(3), Some(4))
            .expect("in-range SL/TP must pass the boundary");
        let expected: Vec<u64> = batch.iter().map(|t| t.len() as u64).collect();
        assert_eq!(counts, expected);
    }
}
