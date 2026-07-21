//! Resident Stage-2 trade-simulation kernel — the 1-MINUTE execution-grid walk in Rust.
//!
//! DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563) + DEC-FILL-EXEC-GRID-1M-001
//! (the PR-B 1-minute execution grid). The Rust port of the Python
//! `brutex.engine.trade_sim.TradeSimWalker` state machine, fused with the
//! per-mask entry-signal projection, over a **resident 1-minute EXECUTION
//! window**. Decision-TF bars contribute ONLY their Stage-1A `bar_bits`
//! signals, projected onto their next-1-minute fill bars through the
//! precomputed `arm_source` column (built Python-side by
//! `brutex.engine.exec_grid` — the derive-in-Python / consume-in-Rust
//! discipline; NO timestamps cross the FFI). All pricing, excursion folds,
//! SL/TP detection and the forced/end-of-data/session-gap laws run per
//! 1-minute bar.
//!
//! ## Why a Rust port
//!
//! The per-combo sweep walks the window ~33 billion times; the interpreter
//! overhead per bar dominates the pure-Python walk. Moving the per-bar state
//! machine into a branch-predictable Rust loop over resident `Vec<i64>` price
//! columns removes that tax; the **flat-skip** jump table (`next_arm`,
//! design §1.5) additionally bounds the per-mask cost by
//! `O(#arm-points + Σ minutes-in-position + #trades)` instead of `O(N_1m)` —
//! while the walker is FLAT nothing can happen until the next 1m bar with an
//! arm point (excursions fold only in-position; forced/session flags are only
//! consulted with a position or a due entry), so armless flat spans are
//! jumped. Behavior-identical to the full walk (parity-pinned; the Python
//! walker deliberately does NOT flat-skip — it is the independence oracle).
//!
//! ## Bit-exactness (Golden Rule #4)
//!
//! The price→paisa conversion (`_price_to_paisa`, `ROUND_HALF_UP`), the
//! session-aware forced-exit detection (`is_forced_exit_bar` at `tf = 1`),
//! the per-bar IST session identity AND the signal→fill mapping (the
//! `exec_grid` close-instant law with its same-session / selector /
//! collision gates) stay in Python — the single source of truth (CLAUDE.md
//! §11 COMMON) — and their pre-computed results (`exec_open/high/low/close`
//! paisa + `exec_is_forced` + `exec_session_idx` + `arm_source`) are handed
//! to this kernel. The kernel reproduces only the Python state machine's
//! branch order, which it does EXACTLY: for the same inputs it emits
//! byte-identical trades to `simulate_trades(...)` under the ONE hardcoded
//! worst-case 1m-grid fill law; a golden cross-check asserts this
//! field-by-field over hypothesis-generated dual-stream inputs.
//!
//! ## The fill laws reproduced here (normative source: the design §2)
//!
//! * **Entry** — a 1m bar `j` with `arm_source[j] = N >= 0` and
//!   `is_superset(bar_bits[N], mask)` fills at bar `j`'s ADVERSE extreme
//!   (BUY high / SELL low); SL/TP levels anchor on that actual fill. Gates:
//!   never on a forced 1m bar, never on a bar a trade just closed on (the
//!   session-gate + selector + collision gates were baked into `arm_source`
//!   at build).
//! * **Same-bar deferral (§2.4)** — the entry bar folds its full range into
//!   MFE/MAE but NEVER exits; SL/TP detection begins on its successor.
//! * **SL/TP per 1m bar** — detection on the 1m `[low, high]` (inclusive);
//!   fill = adverse-max-vs-level on THAT bar (`min(level, low)` BUY /
//!   `max(level, high)` SELL — the gap-open law); one bar spanning BOTH
//!   levels resolves the STOP first, always.
//! * **Mode-B signal exit** — `exit_due` on a managed bar closes AT that bar
//!   (the mapping already did the next-1m timing) at its adverse extreme
//!   with the legacy `(open, open)` fold; `exit_due` on the trade's OWN
//!   entry bar defers to the successor via `pending_exit` (the same-bar
//!   deferral). The sweep path passes no exit signals.
//! * **Forced session-close** — a forced 1m bar closes at its adverse
//!   extreme, `(open, open)` fold.
//! * **End of data / session gap** — close at the last stored 1m bar's
//!   exit basis: its ADVERSE extreme, **unless that bar IS the trade's own
//!   entry bar**, where the basis is the bar's CLOSE (the B3 terminal
//!   carve-out — the carried `last_exit_basis_paisa` starts as the entry
//!   bar's close and is refreshed to each managed bar's adverse extreme).
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! * Construction is `O(N_1m)` — the one-time copy of the price columns +
//!   flags + arm/jump tables + the decision-grid bar_bits, paid ONCE.
//! * [`ResidentSweepSim::simulate_masks`] is `O(N_masks · N_1m)` worst-case
//!   compute (a dense always-in-position mask walks every minute) +
//!   `O(Σ trades)` output; the flat-skip table bounds SPARSE masks by
//!   `O(#arm-points + Σ in-position minutes + #trades)`. Per-bar work is
//!   O(1); claiming O(1) total is a physical impossibility and is FORBIDDEN
//!   (Golden Rule #10).

use crate::mask_word::MaskWord;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// One emitted round-trip trade. Field order matches the Python `ClosedTrade`
/// numeric payload; `entry_idx` / `exit_idx` are **1-minute** bar indices the
/// Python wrapper maps to `entry_ts` / `exit_ts` via the EXECUTION-stream
/// timestamps (DEC-FILL-EXEC-GRID-1M-001 §7.2 — `holding_bars` is a 1M-BAR
/// count). `direction` is a per-batch constant (not stored per trade).
///
/// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
pub type TradeRecord = (i64, i64, i64, u64, u64, u64);

/// Why a position closed — the kernel twin of the Python `_ExitReason` enum
/// (DEC-METRICS-LOSSRATE-FIRSTCLASS-001, the Stage-B exit-reason counter
/// wire). Discriminants are the positions of the append-only Python
/// `EXIT_REASONS` vocabulary (`brutex/engine/trade_sim.py`) — `stop_loss`,
/// `take_profit`, `forced_1520`, `signal`, `end_of_data`, `session_gap` —
/// so `reason as usize` indexes the per-mask counter array and the FFI
/// counts tuple positionally, exactly like the Python walker's counters.
/// The kernel already KNEW each close's reason at every `close` call site
/// (the branch that reached it); this enum only names it so the counter
/// can be accumulated O(1)/trade — the emitted `TradeRecord` is UNTOUCHED.
/// Under the worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001) a same-bar
/// SL/TP tie resolves STOP-first, so the tie feeds the `stop_loss` counter —
/// the pessimistic attribution is the counted truth by construction.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum ExitReason {
    StopLoss = 0,
    TakeProfit = 1,
    Forced = 2,
    Signal = 3,
    EndOfData = 4,
    SessionGap = 5,
}

/// Number of exit-reason vocabulary members (the counter-array width).
const N_EXIT_REASONS: usize = 6;

/// Per-mask exit-reason counters, one `u64` per `EXIT_REASONS` member in
/// vocabulary order: `(stop_loss, take_profit, forced_1520, signal,
/// end_of_data, session_gap)`. Additive FFI output beside the existing
/// per-mask trade batch (the additive-twin precedent,
/// DEC-SWEEP-EXIT-INDEX-FASTPATH-001): the `TradeRecord` 6-tuple carries NO
/// reason, so the counters are the whole per-mask reason surface.
/// Overflow-free by construction — one increment per closed trade, and a
/// window holds at most `N_bars` trades (`usize` ≤ `u64`).
pub type ExitReasonCounts = (u64, u64, u64, u64, u64, u64);

/// Mutable per-trade state while a position is open (int64 paisa). Mirrors the
/// Python `_OpenPosition`; `last_bar` is replaced by `last_idx` +
/// `last_exit_basis_paisa` since the kernel holds resident price columns.
#[derive(Clone, Copy)]
struct OpenPosition {
    entry_idx: usize,
    entry_price_paisa: i64,
    /// The `session_idx` of the entry fill 1m bar — the open-position session
    /// guard closes the position the moment a pushed bar's session differs
    /// (mirrors the Python `_OpenPosition.entry_day_ist`).
    entry_session: i64,
    sl_level_paisa: Option<i64>,
    tp_level_paisa: Option<i64>,
    mfe_paisa: i64,
    mae_paisa: i64,
    last_idx: usize,
    /// The terminal-close basis at `last_idx` (paisa) — the end-of-data /
    /// session-gap retroactive fill price. Initialised to the ENTRY bar's
    /// CLOSE (the B3 terminal same-bar carve-out: a terminal close ON the
    /// entry bar prices at its close, never its adverse extreme — the
    /// full-range phantom-loss firewall) and refreshed by `manage_position`
    /// to each managed bar's ADVERSE extreme (BUY low / SELL high — the
    /// worst-case law; the Python twin keeps the whole `last_bar` and
    /// resolves at close time via `_last_bar_exit_paisa`).
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
/// * [`Walker::levels`] — `entry ± d` with `|entry| ≤ C` (a bar extreme) and
///   `0 ≤ d ≤ C` → `|level| ≤ 2·C`.
/// * [`Walker::update_excursion`] — `high − entry` / `entry − low` (or the
///   `(open, open)` fold) with both operands `|x| ≤ C` → magnitude `≤ 2·C`;
///   the running-max compares fold no further arithmetic.
/// * [`Walker::close`] — `pnl = exit − entry` where `exit` is a bar price
///   (`≤ C`) or an SL/TP level (`≤ 2·C`) → `|pnl| ≤ 3·C`.
///
/// The worst intermediate is therefore `3 · (i64::MAX / 8) < i64::MAX`
/// (3 operands × max magnitude `C`, with `8·C ≤ i64::MAX`) — provably
/// panic-free. The ceiling is ≈ 1.15·10¹⁸ paisa = ₹1.15·10¹⁶, astronomically
/// above any real Indian price in paisa (SENSEX ≈ 8·10⁶), so NO valid input
/// is ever rejected. `arm_source` / `session_idx` do equality/index work
/// only — no arithmetic, no bound.
const MAX_PRICE_PAISA: i64 = i64::MAX / 8;

/// Validate one boundary price column against [`MAX_PRICE_PAISA`]. Pure
/// (`Result<_, String>`, NO `PyErr`) so the no-libpython `cargo test` link
/// exercises every branch; the PyO3 wrappers map the error to `ValueError`.
/// `label` names the raising pyclass (`"ResidentSweepSim"` /
/// `"ResidentSweepSimU128"`) so both width variants emit self-naming
/// messages through the ONE shared guard.
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

/// Pure-Rust faithful port of the 1m-grid `TradeSimWalker` — a forward-only
/// per-bar state machine over the EXECUTION stream. No look-ahead: at bar `i`
/// it reads only `open/high/low/close[i]`, `is_forced[i]`, `session_idx[i]`
/// and the due-fill projections derived from `arm_source[i]` + the resident
/// decision-grid `bar_bits`.
struct Walker {
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
    position: Option<OpenPosition>,
    /// True iff a Mode-B exit that landed on the trade's OWN entry 1m bar was
    /// deferred to the bar's successor (the §2.4 same-bar deferral — the ONLY
    /// remaining next-bar scheduler; every other Mode-B fill closes directly
    /// on its mapped `exit_due` bar). The sweep path never feeds exit
    /// signals, so this stays false there — retained for the 1:1
    /// state-machine mirror of the Python walker's Mode-B law.
    pending_exit: bool,
    /// Per-reason close counters in `EXIT_REASONS` order — incremented once
    /// per `close` (O(1)/trade), read out as [`ExitReasonCounts`]. A pure
    /// OBSERVER of the walk: no branch reads it, so the emitted trades are
    /// byte-identical with the counters present (the existing suite is the
    /// frozen-behaviour gate — DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
    exit_counts: [u64; N_EXIT_REASONS],
}

impl Walker {
    fn new(is_buy: bool, sl_paisa: Option<i64>, tp_paisa: Option<i64>) -> Self {
        Self {
            is_buy,
            sl_paisa,
            tp_paisa,
            position: None,
            pending_exit: false,
            exit_counts: [0; N_EXIT_REASONS],
        }
    }

    /// (sl_level, tp_level) in paisa for the configured direction — mirrors
    /// `_levels`. BUY: stop below / target above; SELL: stop above / target below.
    ///
    /// Deliberately UNGUARDED raw `±` on the hot path: `|entry_price| ≤ C` (a
    /// boundary-validated bar extreme) and `0 ≤ d ≤ C`, so `|entry ± d| ≤ 2·C <
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

    /// Open a position at `entry_fill_paisa` on 1m bar `idx` — the caller
    /// resolves the fill as the bar's ADVERSE extreme (BUY high / SELL low —
    /// DEC-FILL-WORST-CASE-ONLY-001 on the 1m grid). Mirrors
    /// `_open_position`: the SL/TP levels anchor on the ACTUAL entry fill.
    /// `session` is the fill bar's `session_idx` (the open-position guard's
    /// key); `close_basis_paisa` is the entry bar's CLOSE — the B3 terminal
    /// carve-out basis: a terminal (end-of-data / session-gap) close landing
    /// ON the entry bar prices at its close, never its adverse extreme. The
    /// caller folds the entry bar's full range into MFE/MAE right after
    /// (the §2.4 same-bar deferral: the entry bar folds but never exits).
    fn open_position(
        &mut self,
        entry_fill_paisa: i64,
        idx: usize,
        session: i64,
        close_basis_paisa: i64,
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
            last_exit_basis_paisa: close_basis_paisa,
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
        // pessimistic tie, hardcoded — DEC-FILL-WORST-CASE-ONLY-001).
        stop.or(target)
    }

    /// Build the trade record + flatten the walker — mirrors `_close`.
    ///
    /// `reason` names WHY the position closed — every call site already sat
    /// on the deciding branch, so threading it costs nothing; the ONLY use
    /// is the per-reason counter increment (a pure observer — the returned
    /// record is byte-identical to the pre-Stage-B `close`).
    ///
    /// Deliberately UNGUARDED raw `−` on the hot path: `|entry| ≤ C` (a
    /// boundary-validated bar extreme) and `|exit| ≤ 2·C` (a bar price ≤ C,
    /// or an SL/TP level ≤ 2·C from [`Walker::levels`]), so `|pnl| ≤ 3·C <
    /// i64::MAX` — see the [`MAX_PRICE_PAISA`] derivation (R2-RC-F1).
    fn close(
        &mut self,
        pos: OpenPosition,
        exit_price_paisa: i64,
        exit_idx: usize,
        reason: ExitReason,
    ) -> TradeRecord {
        let pnl = if self.is_buy {
            exit_price_paisa - pos.entry_price_paisa
        } else {
            pos.entry_price_paisa - exit_price_paisa
        };
        self.position = None;
        self.exit_counts[reason as usize] += 1;
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

    /// The accumulated per-reason close counters in `EXIT_REASONS` order.
    fn exit_counts_tuple(&self) -> ExitReasonCounts {
        let c = self.exit_counts;
        (c[0], c[1], c[2], c[3], c[4], c[5])
    }

    /// Process one in-position 1m bar STRICTLY AFTER the entry bar — mirrors
    /// `_manage_position` (the entry bar itself is fully handled at open: the
    /// §2.4 same-bar deferral). `pos` is taken by value (the caller `take()`s
    /// it from `self.position`); still-open branches write it back, closing
    /// branches consume it. Precedence per managed bar: Mode-B signal fill
    /// (a pending entry-bar deferral OR a directly-due mapped `exit_due`) →
    /// forced → SL/TP → still open. `exit_basis_paisa` is the bar's
    /// direction-resolved ADVERSE extreme (resolved once in `push`) — the
    /// terminal-close basis refresh.
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
        exit_due: bool,
    ) -> Option<TradeRecord> {
        // Mirrors Python `_manage_position`'s top-of-body `last_idx/last_bar`
        // refresh — the (A2) guard reads the PRE-update state, so it runs in
        // `push` BEFORE this method is entered. A managed bar's terminal
        // basis is its ADVERSE extreme (the entry bar's stays its CLOSE —
        // the B3 carve-out set at open).
        pos.last_idx = idx;
        pos.last_exit_basis_paisa = exit_basis_paisa;

        // The signal/forced exit fill: the bar's adverse extreme — BUY exits
        // (sells) at the LOW, SELL exits (covers) at the HIGH
        // (DEC-FILL-WORST-CASE-ONLY-001). The excursion fold on those arms
        // stays the legacy (open, open) — only the fill PRICE is worst-case.
        let adverse_fill = if self.is_buy { low_paisa } else { high_paisa };

        // (1) A Mode-B signal exit fills on THIS bar — either the mapped
        // `exit_due` fill (the exec-grid mapping already did the next-1m
        // timing) or a pending entry-bar-deferred one. Precedence over
        // forced mirrors the historical pending-beats-forced ladder. Counted
        // under `Signal` — the only reason a Mode-B pending/due exit can carry
        // (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
        if self.pending_exit || exit_due {
            self.pending_exit = false;
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, adverse_fill, idx, ExitReason::Signal));
        }

        // (2) Forced session-close exit overrides SL/TP.
        if forced {
            Self::update_excursion(&mut pos, self.is_buy, open_paisa, open_paisa);
            return Some(self.close(pos, adverse_fill, idx, ExitReason::Forced));
        }

        // (3) Full-range in-position bar: fold its high/low into MFE/MAE.
        Self::update_excursion(&mut pos, self.is_buy, high_paisa, low_paisa);

        // (4) Stop-loss / take-profit — detection AND fill on THIS 1m bar
        // (design §3b.1 option (b): the 1m path IS the monitor). The fill is
        // the adverse-max law WITH the level: BUY exit = min(level, bar low)
        // — a gap-through bar fills at the extreme while a bar entirely PAST
        // the level in the trade's favour stays clamped AT the level (the
        // gap-open law, design B5); SELL exit = max(level, bar high).
        // Bounds: |level| <= 2·C and |low|/|high| <= C, so the min/max stays
        // <= 2·C — the MAX_PRICE_PAISA derivation is unchanged. The fill law
        // no longer branches on `is_stop` — only the exit-reason COUNTER reads
        // it (a pure observer: the same-bar SL/TP tie is stop-first, so the
        // tie is counted `stop_loss` — the pessimistic attribution IS the
        // counted truth; DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
        if let Some((is_stop, level_paisa)) = self.resolve_level_hit(&pos, high_paisa, low_paisa) {
            let reason = if is_stop {
                ExitReason::StopLoss
            } else {
                ExitReason::TakeProfit
            };
            let exit_fill = if self.is_buy {
                level_paisa.min(low_paisa)
            } else {
                level_paisa.max(high_paisa)
            };
            return Some(self.close(pos, exit_fill, idx, reason));
        }

        // (5) Still open — persist the mutated excursion + last_idx/basis.
        self.position = Some(pos);
        None
    }

    /// Advance one 1-MINUTE bar — mirrors `push_1m`. Returns a trade iff a
    /// position closed. `entry_due` / `exit_due` are the mapped due-fill
    /// projections (`arm_source[idx] = N >= 0` AND the mask/exit signal live
    /// on decision bar `N`) — the caller computes them; the walker never
    /// re-times signals. `close_paisa` is bar `idx`'s CLOSE — the B3
    /// terminal-carve-out basis carried onto a position opened on this bar.
    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        open_paisa: i64,
        high_paisa: i64,
        low_paisa: i64,
        close_paisa: i64,
        idx: usize,
        forced_raw: bool,
        session: i64,
        entry_due: bool,
        exit_due: bool,
    ) -> Option<TradeRecord> {
        // forced is only consulted when filling a due entry or managing an
        // open position (Python skips it on a flat, no-fill bar).
        let need_forced = self.position.is_some() || entry_due;
        let forced = need_forced && forced_raw;

        // THIS bar's direction-resolved terminal basis — what an
        // end-of-data / session-gap close against this MANAGED bar fills at:
        // the adverse extreme (the Python twin resolves lazily from the kept
        // `last_bar`; the entry bar's own basis is its CLOSE — B3).
        let exit_basis_paisa = if self.is_buy { low_paisa } else { high_paisa };

        // (A2)/(B) An open position first: the session-boundary guard, else
        // ordinary management. The (A) entry fill runs AFTER — gated on
        // `closed.is_none()`, so a boundary/exit bar never opens (the
        // earliest re-fill is a later bar — the §2.2 pinned law re-keyed).
        let mut closed: Option<TradeRecord> = None;
        if let Some(pos) = self.position.take() {
            if session != pos.entry_session {
                // (A2) Session-boundary guard — DEC-TRADESIM-OPEN-SESSION-
                // GUARD-001 on the 1m stream. Close at the PRIOR session's
                // last stored 1m bar's carried basis (the finish() idiom —
                // pre-update `last_exit_basis_paisa` / `last_idx`; the B3
                // close basis when that bar is the trade's own entry bar); a
                // pending Mode-B exit is subsumed (cleared). A just-opened
                // position carries THIS bar's session, so it can never trip
                // the guard; the inequality compare fires exactly once even
                // across a multi-session hole. The Python detail sink names
                // this close `session_gap`; the record's numerics are the
                // parity surface, and the counter attributes it under the
                // SAME name (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
                self.pending_exit = false;
                closed = Some(self.close(
                    pos,
                    pos.last_exit_basis_paisa,
                    pos.last_idx,
                    ExitReason::SessionGap,
                ));
            } else {
                closed = self.manage_position(
                    pos,
                    open_paisa,
                    high_paisa,
                    low_paisa,
                    exit_basis_paisa,
                    idx,
                    forced,
                    exit_due,
                );
            }
        }

        // (A) Fill a due entry on THIS bar at its ADVERSE extreme (BUY high /
        // SELL low; the SL/TP levels anchor on that actual fill) — never on a
        // bar a trade just closed on, never on a forced 1m bar (cannot open
        // at/after the session's forced-exit instant). The same-session /
        // selector / collision gates were baked into `arm_source` at build.
        // §2.4 same-bar deferral: the entry bar folds its full range into
        // MFE/MAE here but NEVER exits — management begins on its successor;
        // a Mode-B `exit_due` on the entry bar defers via `pending_exit`.
        if self.position.is_none() && closed.is_none() && entry_due && !forced {
            let is_buy = self.is_buy;
            let entry_fill = if is_buy { high_paisa } else { low_paisa };
            self.open_position(entry_fill, idx, session, close_paisa);
            if let Some(pos) = self.position.as_mut() {
                Self::update_excursion(pos, is_buy, high_paisa, low_paisa);
            }
            if exit_due {
                self.pending_exit = true;
            }
        }

        closed
    }

    /// Close any still-open position at the last stored bar's carried basis —
    /// mirrors `finish` (the bar's adverse extreme, or its CLOSE when it is
    /// the trade's own entry bar — the B3 terminal carve-out). A
    /// still-pending Mode-B exit is IGNORED here (closed at the basis, not a
    /// would-be successor fill); no excursion fold.
    fn finish(&mut self) -> Option<TradeRecord> {
        let pos = self.position.take()?;
        Some(self.close(
            pos,
            pos.last_exit_basis_paisa,
            pos.last_idx,
            ExitReason::EndOfData,
        ))
    }
}

/// Inclusive holding length in 1-minute bars — `exit_idx − entry_idx`, plus one.
///
/// The forward-only walker always closes at or after the entry bar
/// (`exit_idx == entry_idx` is the LEGAL B3 terminal degenerate → holding 1).
/// The subtract uses `saturating_sub` purely as a **defensive guard**: were a
/// future refactor to call `close` with `exit_idx < entry_idx`, the unsigned
/// subtract would underflow and — under this crate's `overflow-checks = true`
/// plus `panic = "abort"` profile — `abort()` the process. `saturating_sub`
/// instead clamps the difference to zero, yielding a defined minimum holding
/// of one bar and never a panic.
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
// at the top of this file repoints the import).

/// The width-INDEPENDENT resident columns — the 1-MINUTE execution grid's
/// OHLC paisa + forced flags + session ordinals + the `arm_source` projection
/// column + its derived flat-skip jump table. Shared verbatim by both kernel
/// widths; only the DECISION-grid `bar_bits` vector (held next to this struct
/// in each pyclass) is width-typed.
struct SweepColumns {
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    /// Per-1m-bar session identity — the bar's IST calendar-date ordinal,
    /// computed in Python from bar `i`'s own timestamp (P-06: no look-ahead).
    /// Equality-compared ONLY (the open-position session-gap guard).
    session_idx: Vec<i64>,
    /// Per-1m-bar arm point: the DECISION index whose next-1m fill instant
    /// lands at this bar (the exec-grid collision WINNER — earliest-armed),
    /// else `-1`. Strictly increasing over its non-negative entries (WINNERS
    /// only — collision losers were dropped + counted at map build,
    /// design §1.2 B4); every non-negative entry indexes the decision-grid
    /// `bar_bits`.
    arm_source: Vec<i64>,
    /// Flat-skip jump table (design §1.5): `next_arm[j]` = the smallest
    /// `k >= j` with `arm_source[k] >= 0`, else `n`. Derived from
    /// `arm_source` at build (mask-independent, once per resident window);
    /// while the walker is FLAT nothing can happen until the next arm point,
    /// so armless flat spans jump straight to it — behavior-identical to the
    /// full walk (parity-pinned).
    next_arm: Vec<usize>,
}

/// Build + validate the width-independent columns — the ONE length-mismatch
/// guard, R2-RC-F1 price-boundary scan AND `arm_source` validation both
/// kernels ride. `label` names the constructing pyclass; `bar_bits_len` is
/// the width-typed DECISION-grid vector's length (the vector itself stays
/// with the caller) — every non-negative `arm_source` entry must index it.
#[allow(clippy::too_many_arguments)]
fn build_columns(
    label: &str,
    open: Vec<i64>,
    high: Vec<i64>,
    low: Vec<i64>,
    close: Vec<i64>,
    is_forced: Vec<bool>,
    session_idx: Vec<i64>,
    arm_source: Vec<i64>,
    bar_bits_len: usize,
) -> Result<SweepColumns, String> {
    let n = open.len();
    if high.len() != n
        || low.len() != n
        || close.len() != n
        || is_forced.len() != n
        || session_idx.len() != n
        || arm_source.len() != n
    {
        return Err(format!(
            "{label}: column length mismatch — open={}, high={}, low={}, close={}, \
             is_forced={}, session_idx={}, arm_source={} (all must be equal)",
            n,
            high.len(),
            low.len(),
            close.len(),
            is_forced.len(),
            session_idx.len(),
            arm_source.len(),
        ));
    }
    // arm_source discipline (design §1.3): -1 or a valid DECISION index;
    // strictly increasing over the non-negative entries (each decision bar
    // maps to at most one fill bar — the column stores collision WINNERS
    // only). Violations are a corrupt map, refused with a clean ValueError.
    let mut prev: i64 = -1;
    for (j, &src) in arm_source.iter().enumerate() {
        if src < -1 || (src >= 0 && (src as usize) >= bar_bits_len) {
            return Err(format!(
                "{label}: arm_source[{j}] = {src} outside [-1, {bar_bits_len})"
            ));
        }
        if src >= 0 {
            if src <= prev {
                return Err(format!(
                    "{label}: arm_source not strictly increasing over its non-negative \
                     entries at index {j} ({src} <= {prev})"
                ));
            }
            prev = src;
        }
    }
    // R2-RC-F1 overflow boundary: every paisa price must sit inside
    // [-MAX_PRICE_PAISA, MAX_PRICE_PAISA] so the walker's raw i64 add/sub
    // is provably panic-free (see the ceiling's derivation doc). One-time
    // O(N_1m) scan at construction — NOT on the hot loop. `is_forced`
    // (bool), `bar_bits` (u64/u128, only superset-compared against masks),
    // `session_idx` (equality only) and `arm_source` (index only) do no
    // arithmetic and need no bound.
    check_price_column(label, "open", &open)?;
    check_price_column(label, "high", &high)?;
    check_price_column(label, "low", &low)?;
    check_price_column(label, "close", &close)?;
    // Flat-skip jump table — one backward pass (design §1.5).
    let mut next_arm = vec![n; n];
    let mut nxt = n;
    for j in (0..n).rev() {
        if arm_source[j] >= 0 {
            nxt = j;
        }
        next_arm[j] = nxt;
    }
    Ok(SweepColumns {
        open,
        high,
        low,
        close,
        is_forced,
        session_idx,
        arm_source,
        next_arm,
    })
}

/// Simulate ONE mask over a resident window AND report its per-reason exit
/// counters — the width-generic pure-Rust core every batch path rides
/// (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B over the 1m-execution-grid
/// dual-feed walk, DEC-FILL-EXEC-GRID-1M-001). The entry projection for 1m bar
/// `i` is `arm_source[i] >= 0 && W::is_superset(bar_bits[arm_source[i]], mask)`
/// — the ONLY width-touching line; there is no Mode-B exit-signal stream on the
/// sweep path (it matches `metrics_for_mask`, which passes none). The counters
/// are a pure observer of the ONE walk — [`simulate_one_core`] delegates here
/// and drops them, so the counted and uncounted surfaces can never drift.
///
/// FLAT-SKIP (design §1.5): while the walker is flat, armless spans jump via
/// `next_arm` — a flat push with no due entry is provably a no-op (no
/// position → no excursion fold, no session/forced consult, no state
/// change), so the jump is behavior-identical to the full walk
/// (parity-pinned by `flat_skip_matches_naive_full_walk`). In-position spans
/// walk every minute (the SL/TP law).
// The loop indexes SEVEN parallel per-bar sequences by the same `i` (six
// columns + the width-typed bar_bits); a 7-way iterator zip would obscure the
// walk, so the index form is kept deliberately.
#[allow(clippy::needless_range_loop)]
fn simulate_one_counted_core<W: MaskWord>(
    cols: &SweepColumns,
    bar_bits: &[W],
    mask: W,
    is_buy: bool,
    sl: Option<i64>,
    tp: Option<i64>,
) -> (Vec<TradeRecord>, ExitReasonCounts) {
    let n = cols.open.len();
    let mut walker = Walker::new(is_buy, sl, tp);
    let mut trades: Vec<TradeRecord> = Vec::new();
    let mut i = 0usize;
    while i < n {
        if walker.position.is_none() {
            i = cols.next_arm[i];
            if i >= n {
                break;
            }
        }
        let src = cols.arm_source[i];
        let entry_due = src >= 0 && W::is_superset(bar_bits[src as usize], mask);
        if let Some(t) = walker.push(
            cols.open[i],
            cols.high[i],
            cols.low[i],
            cols.close[i],
            i,
            cols.is_forced[i],
            cols.session_idx[i],
            entry_due,
            false,
        ) {
            trades.push(t);
        }
        i += 1;
    }
    if let Some(t) = walker.finish() {
        trades.push(t);
    }
    let counts = walker.exit_counts_tuple();
    (trades, counts)
}

/// Simulate ONE mask over a resident window — the pre-Stage-B surface, a thin
/// delegator over [`simulate_one_counted_core`] dropping the counters (ONE
/// walk spine; the existing suite passing UNMODIFIED is the frozen-behaviour
/// gate).
fn simulate_one_core<W: MaskWord>(
    cols: &SweepColumns,
    bar_bits: &[W],
    mask: W,
    is_buy: bool,
    sl: Option<i64>,
    tp: Option<i64>,
) -> Vec<TradeRecord> {
    simulate_one_counted_core(cols, bar_bits, mask, is_buy, sl, tp).0
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

/// Width-generic core of the batch `simulate_masks_with_exit_counts` — same
/// boundary validation + serial/parallel sweep as [`simulate_masks_core`],
/// each mask yielding `(trades, exit_reason_counts)` via the ONE counted walk
/// core (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B — the additive-twin
/// precedent: the pre-existing batch surfaces are byte-untouched).
fn simulate_masks_with_exit_counts_core<W: MaskWord>(
    label: &str,
    cols: &SweepColumns,
    bar_bits: &[W],
    masks: &[W],
    is_buy: bool,
    sl_paisa: Option<i64>,
    tp_paisa: Option<i64>,
) -> Result<Vec<(Vec<TradeRecord>, ExitReasonCounts)>, String> {
    check_level_distance_for(label, "sl_paisa", sl_paisa)?;
    check_level_distance_for(label, "tp_paisa", tp_paisa)?;
    let work = (masks.len() as u64).saturating_mul(cols.open.len() as u64);
    // Same COMMON-predicate dispatch as `simulate_masks_core`
    // (DEC-MUTATION-RUNTIME-003); order-preserving on both arms.
    Ok(
        if crate::below_parallel_threshold(work, PARALLEL_WORK_THRESHOLD) {
            masks
                .iter()
                .map(|&m| simulate_one_counted_core(cols, bar_bits, m, is_buy, sl_paisa, tp_paisa))
                .collect()
        } else {
            masks
                .par_iter()
                .map(|&m| simulate_one_counted_core(cols, bar_bits, m, is_buy, sl_paisa, tp_paisa))
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

/// Resident 1-minute execution window for the Stage-2 trade-sim sweep — the
/// 1m OHLC paisa + forced flags + session ordinals + the `arm_source`
/// signal→fill projection + the per-DECISION-bar u64 condition bitset, held
/// Rust-side so a whole mask grid is simulated without re-marshalling the
/// window per combo. The production kernel for today's 60-bit vocabulary;
/// thin `W = u64` delegators onto the width-generic [`MaskWord`] core.
///
/// See the module docs for the fill-law + bit-exactness + honest-envelope
/// contracts (DEC-FILL-EXEC-GRID-1M-001).
#[pyclass(frozen, module = "fno_math")]
pub struct ResidentSweepSim {
    columns: SweepColumns,
    bar_bits: Vec<u64>,
}

impl ResidentSweepSim {
    /// Pure constructor returning a plain `Result<_, String>` (NO `PyErr`), so the
    /// `#[cfg(test)]` suite exercises the validation WITHOUT pulling Python
    /// C-API symbols into the no-libpython `cargo test` link (mirrors
    /// `python::count_supermasks_scoped`). The `#[new]` wrapper maps the error.
    #[allow(clippy::too_many_arguments)]
    fn build(
        exec_open: Vec<i64>,
        exec_high: Vec<i64>,
        exec_low: Vec<i64>,
        exec_close: Vec<i64>,
        exec_is_forced: Vec<bool>,
        exec_session_idx: Vec<i64>,
        arm_source: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> Result<Self, String> {
        let columns = build_columns(
            "ResidentSweepSim",
            exec_open,
            exec_high,
            exec_low,
            exec_close,
            exec_is_forced,
            exec_session_idx,
            arm_source,
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

    /// Pure core of [`Self::simulate_masks_with_exit_counts`] — `W = u64`
    /// delegator onto [`simulate_masks_with_exit_counts_core`].
    fn simulate_masks_with_exit_counts_impl(
        &self,
        masks: &[u64],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<(Vec<TradeRecord>, ExitReasonCounts)>, String> {
        simulate_masks_with_exit_counts_core(
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
    /// Build the resident buffer. The ONE-TIME `O(N_1m)` Python→Rust copy of
    /// the 1-minute price columns + forced flags + session ordinals +
    /// `arm_source` (+ the decision-grid `bar_bits`) is paid here via the
    /// `Vec` extracts (DEC-FILL-EXEC-GRID-1M-001 — the dual-feed
    /// constructor; the decision-grid OHLC columns are NOT kernel inputs).
    /// `arm_source[j]` is 1m bar `j`'s winning decision index or `-1`
    /// (built by `brutex.engine.exec_grid` — same-session/selector/collision
    /// gates applied there). Unequal column lengths, an out-of-range or
    /// non-increasing `arm_source`, or an out-of-envelope price raise a
    /// clean `ValueError` (never a panic that would SIGABRT under
    /// `panic=abort`).
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        exec_open: Vec<i64>,
        exec_high: Vec<i64>,
        exec_low: Vec<i64>,
        exec_close: Vec<i64>,
        exec_is_forced: Vec<bool>,
        exec_session_idx: Vec<i64>,
        arm_source: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> PyResult<Self> {
        Self::build(
            exec_open,
            exec_high,
            exec_low,
            exec_close,
            exec_is_forced,
            exec_session_idx,
            arm_source,
            bar_bits,
        )
        .map_err(PyValueError::new_err)
    }

    /// Number of resident 1-minute EXECUTION bars (the walk window).
    #[getter]
    fn n_bars(&self) -> usize {
        self.columns.open.len()
    }

    /// Simulate a BATCH of masks over the resident window → per-mask trade lists.
    ///
    /// `out[j]` is the list of round-trip trades for `masks[j]`, each a tuple
    /// `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`
    /// — all bar quantities are 1-MINUTE indices/counts. `is_buy` selects
    /// long (`true`) / short (`false`); `sl_paisa` / `tp_paisa` are the stop
    /// / target distances in paisa (`None` disables that leg). Fills follow
    /// the ONE hardcoded worst-case 1m-grid law (DEC-FILL-WORST-CASE-ONLY-001
    /// + DEC-FILL-EXEC-GRID-1M-001).
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

    /// [`Self::simulate_masks`] PLUS per-mask exit-reason counters — the
    /// ADDITIVE counted twin (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
    ///
    /// `out[j]` is `(trades, exit_reason_counts)` for `masks[j]`: the trades
    /// are byte-identical to [`Self::simulate_masks`]'s (both surfaces ride
    /// the ONE counted walk core) and the counts tuple holds one `u64` per
    /// `EXIT_REASONS` member in vocabulary order — `(stop_loss, take_profit,
    /// forced_1520, signal, end_of_data, session_gap)` — with
    /// `sum(counts) == len(trades)` by construction (each close increments
    /// exactly one counter). Same worst-case fill law
    /// (DEC-FILL-WORST-CASE-ONLY-001), serial/parallel dispatch and R2-RC-F1
    /// SL/TP boundary → `ValueError` as the uncounted batch.
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn simulate_masks_with_exit_counts(
        &self,
        masks: Vec<u64>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<(Vec<TradeRecord>, ExitReasonCounts)>> {
        self.simulate_masks_with_exit_counts_impl(&masks, is_buy, sl_paisa, tp_paisa)
            .map_err(PyValueError::new_err)
    }
}

/// Resident 1-minute execution window for the Stage-2 trade-sim sweep over a
/// **>64-bit** vocabulary — the u128 twin of [`ResidentSweepSim`]
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
    #[allow(clippy::too_many_arguments)]
    fn build(
        exec_open: Vec<i64>,
        exec_high: Vec<i64>,
        exec_low: Vec<i64>,
        exec_close: Vec<i64>,
        exec_is_forced: Vec<bool>,
        exec_session_idx: Vec<i64>,
        arm_source: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> Result<Self, String> {
        let columns = build_columns(
            "ResidentSweepSimU128",
            exec_open,
            exec_high,
            exec_low,
            exec_close,
            exec_is_forced,
            exec_session_idx,
            arm_source,
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

    /// Pure core of [`Self::simulate_masks_with_exit_counts`] — `W = u128`
    /// delegator onto [`simulate_masks_with_exit_counts_core`].
    fn simulate_masks_with_exit_counts_impl(
        &self,
        masks: &[u128],
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> Result<Vec<(Vec<TradeRecord>, ExitReasonCounts)>, String> {
        simulate_masks_with_exit_counts_core(
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
    /// with `bar_bits` widened to `Vec<u128>`. Invalid columns raise a clean
    /// `ValueError`; a `bar_bits` value outside `[0, 2^128)` raises
    /// `OverflowError` at the PyO3 extraction edge (probe-pinned).
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        exec_open: Vec<i64>,
        exec_high: Vec<i64>,
        exec_low: Vec<i64>,
        exec_close: Vec<i64>,
        exec_is_forced: Vec<bool>,
        exec_session_idx: Vec<i64>,
        arm_source: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> PyResult<Self> {
        Self::build(
            exec_open,
            exec_high,
            exec_low,
            exec_close,
            exec_is_forced,
            exec_session_idx,
            arm_source,
            bar_bits,
        )
        .map_err(PyValueError::new_err)
    }

    /// Number of resident 1-minute EXECUTION bars (the walk window).
    #[getter]
    fn n_bars(&self) -> usize {
        self.columns.open.len()
    }

    /// Simulate a BATCH of u128 masks — the wide twin of
    /// [`ResidentSweepSim::simulate_masks`] (same hardcoded worst-case 1m-grid
    /// law, same serial/parallel threshold, same R2-RC-F1 SL/TP boundary →
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

    /// Batch trades PLUS per-mask exit-reason counters — the wide twin of
    /// [`ResidentSweepSim::simulate_masks_with_exit_counts`] (same counts
    /// vocabulary order, same `sum(counts) == len(trades)` law).
    #[pyo3(signature = (masks, is_buy, sl_paisa, tp_paisa))]
    fn simulate_masks_with_exit_counts(
        &self,
        masks: Vec<u128>,
        is_buy: bool,
        sl_paisa: Option<i64>,
        tp_paisa: Option<i64>,
    ) -> PyResult<Vec<(Vec<TradeRecord>, ExitReasonCounts)>> {
        self.simulate_masks_with_exit_counts_impl(&masks, is_buy, sl_paisa, tp_paisa)
            .map_err(PyValueError::new_err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The next-bar arm column for an n-bar single-session contiguous window:
    /// `arm_source[j] = j - 1` — decision bar `N` fills at bar `N + 1`,
    /// exactly the tf==1minute degenerate mapping (design §6). `bar_bits`
    /// then shares the window length.
    fn next_bar_arm(n: usize) -> Vec<i64> {
        (0..n).map(|j| j as i64 - 1).collect()
    }

    /// Build a sim with explicit per-column data, no forced bars, every bar in
    /// ONE session and the tf==1minute next-bar arm mapping (test helper).
    /// Every fill below follows the ONE hardcoded worst-case 1m-grid law
    /// (DEC-FILL-EXEC-GRID-1M-001); each expected record is HAND-DERIVED in
    /// its test's comment. NOTE the §2.4 same-bar deferral: the ENTRY bar
    /// folds its range but never exits — expectations below account for it.
    fn sim(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        bar_bits: Vec<u64>,
    ) -> ResidentSweepSim {
        let n = open.len();
        ResidentSweepSim::build(
            open,
            high,
            low,
            close,
            vec![false; n],
            vec![0; n],
            next_bar_arm(n),
            bar_bits,
        )
        .expect("equal-length test data")
    }

    // ── holding-length guard (defensive checked-subtract) ──────────────────

    #[test]
    fn holding_bars_normal_is_inclusive() {
        // The invariant case: exit at or after entry → inclusive bar count.
        // Same-bar entry+exit (holding 1) is the LEGAL B3 terminal
        // degenerate under DEC-FILL-EXEC-GRID-1M-001.
        assert_eq!(holding_bars(0, 0), 1);
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
        // The hardcoded pessimistic tie (DEC-FILL-WORST-CASE-ONLY-001): a bar
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
        // varied entry bits, with the next-bar arm mapping SESSION-GATED
        // (exec_grid never emits a cross-session arm — the build contract).
        // The whole sweep must complete without tripping the holding/overflow
        // guards, and re-running it is bit-identical (Golden Rule #7).
        let n = 20_000usize;
        let open: Vec<i64> = (0..n as i64).map(|i| 10_000 + (i % 137) * 5).collect();
        let high: Vec<i64> = open.iter().map(|&o| o + 50).collect();
        let low: Vec<i64> = open.iter().map(|&o| o - 50).collect();
        let close: Vec<i64> = open.iter().map(|&o| o + 10).collect();
        let is_forced: Vec<bool> = (0..n).map(|i| i % 375 == 374).collect();
        // A realistic multi-session shape: one synthetic 375-bar day per session.
        let session_idx: Vec<i64> = (0..n).map(|i| (i / 375) as i64).collect();
        let arm: Vec<i64> = (0..n)
            .map(|j| {
                if j > 0 && session_idx[j] == session_idx[j - 1] {
                    j as i64 - 1
                } else {
                    -1
                }
            })
            .collect();
        let bar_bits: Vec<u64> = (0..n as u64).map(|i| (i & 0x3) | 0x1).collect();
        let s = ResidentSweepSim::build(
            open,
            high,
            low,
            close,
            is_forced,
            session_idx,
            arm,
            bar_bits,
        )
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
                vec![-1, -1],
                vec![0, 0]
            )
            .is_err()
        );
    }

    #[test]
    fn build_rejects_each_single_column_mismatch() {
        // ONE column short at a time, all others length 2. Each case must error
        // — this kills the `||`->`&&` mutants on every clause of the length
        // guard. `bar_bits` is the DECISION grid and is deliberately NOT in
        // the exec-length law (see `bar_bits_decision_length_is_independent`).
        let ok2 = || vec![0i64; 2];
        let okf = || vec![false; 2];
        let oks = || vec![0i64; 2];
        let oka = || vec![-1i64; 2];
        let okb = || vec![0u64; 2];
        assert!(
            ResidentSweepSim::build(ok2(), vec![0], ok2(), ok2(), okf(), oks(), oka(), okb())
                .is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), vec![0], ok2(), okf(), oks(), oka(), okb())
                .is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), vec![0], okf(), oks(), oka(), okb())
                .is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), vec![false], oks(), oka(), okb())
                .is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), okf(), vec![0i64], oka(), okb())
                .is_err()
        );
        assert!(
            ResidentSweepSim::build(ok2(), ok2(), ok2(), ok2(), okf(), oks(), vec![-1i64], okb())
                .is_err()
        );
    }

    #[test]
    fn bar_bits_decision_length_is_independent() {
        // DEC-FILL-EXEC-GRID-1M-001: bar_bits is the DECISION grid — its
        // length is decoupled from the 1m window (n_tf != n_1m is the NORM
        // on a coarse TF). A 2-decision / 4-exec-bar window builds fine when
        // every non-negative arm indexes the decision grid.
        let r = ResidentSweepSim::build(
            vec![0; 4],
            vec![0; 4],
            vec![0; 4],
            vec![0; 4],
            vec![false; 4],
            vec![0; 4],
            vec![-1, 0, -1, 1],
            vec![1u64, 1],
        )
        .expect("decision grid shorter than exec grid must build");
        assert_eq!(r.n_bars(), 4);
    }

    #[test]
    fn build_rejects_arm_source_out_of_range() {
        // arm entries must be -1 or a valid DECISION index: -2 and n_tf both
        // refuse, naming the offender.
        for bad in [-2i64, 2, 99] {
            let err = ResidentSweepSim::build(
                vec![0; 3],
                vec![0; 3],
                vec![0; 3],
                vec![0; 3],
                vec![false; 3],
                vec![0; 3],
                vec![-1, bad, -1],
                vec![1u64, 1],
            )
            .map(|_| ())
            .expect_err("out-of-range arm_source must be rejected");
            assert!(err.contains("arm_source[1]"), "{err}");
        }
    }

    #[test]
    fn build_rejects_non_increasing_arm_source() {
        // The winner law makes arm_source strictly increasing over its
        // non-negative entries; a duplicate or decreasing pair is a corrupt
        // map and refuses (equal AND decreasing arms both covered).
        for pair in [[1i64, 1], [1, 0]] {
            let err = ResidentSweepSim::build(
                vec![0; 3],
                vec![0; 3],
                vec![0; 3],
                vec![0; 3],
                vec![false; 3],
                vec![0; 3],
                vec![-1, pair[0], pair[1]],
                vec![1u64, 1],
            )
            .map(|_| ())
            .expect_err("non-increasing arm_source must be rejected");
            assert!(err.contains("strictly increasing"), "{err}");
        }
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
            vec![-1, 0],
            vec![0, 0],
        )
        .expect("equal lengths must build");
        assert_eq!(r.n_bars(), 2);
    }

    #[test]
    fn build_empty_is_ok() {
        let r = ResidentSweepSim::build(
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
        )
        .expect("empty ok");
        assert_eq!(r.n_bars(), 0);
    }

    // ── flat-skip jump table ────────────────────────────────────────────────

    #[test]
    fn next_arm_table_is_correct() {
        let r = ResidentSweepSim::build(
            vec![0; 6],
            vec![0; 6],
            vec![0; 6],
            vec![0; 6],
            vec![false; 6],
            vec![0; 6],
            vec![-1, 0, -1, -1, 2, -1],
            vec![1u64, 1, 1],
        )
        .expect("build");
        assert_eq!(r.columns.next_arm, vec![1, 1, 4, 4, 4, 6]);
    }

    #[test]
    fn flat_skip_matches_naive_full_walk() {
        // The §1.5 jump-vs-full-walk parity pin: simulate_one (flat-skip)
        // must be bit-identical to a naive push of EVERY bar. Sparse arms,
        // forced bars, a session boundary and SL/TP legs all present.
        let n = 40usize;
        let open: Vec<i64> = (0..n as i64).map(|i| 1000 + (i % 11) * 3).collect();
        let high: Vec<i64> = open.iter().map(|&o| o + 9).collect();
        let low: Vec<i64> = open.iter().map(|&o| o - 9).collect();
        let close: Vec<i64> = open.iter().map(|&o| o + 2).collect();
        let is_forced: Vec<bool> = (0..n).map(|i| i % 20 == 19).collect();
        let session_idx: Vec<i64> = (0..n).map(|i| (i / 20) as i64).collect();
        // Sparse in-session arms: every 5th bar (skipping session firsts).
        let arm: Vec<i64> = {
            let mut next_dec = 0i64;
            (0..n)
                .map(|j| {
                    if j % 5 == 2 {
                        next_dec += 1;
                        next_dec - 1
                    } else {
                        -1
                    }
                })
                .collect()
        };
        let n_dec = arm.iter().filter(|&&a| a >= 0).count();
        let bar_bits: Vec<u64> = (0..n_dec as u64).map(|i| (i % 3) | 1).collect();
        let s = ResidentSweepSim::build(
            open.clone(),
            high.clone(),
            low.clone(),
            close.clone(),
            is_forced.clone(),
            session_idx.clone(),
            arm.clone(),
            bar_bits.clone(),
        )
        .expect("build");
        for mask in [0u64, 1, 2, 3] {
            let skipped = s.simulate_one(mask, true, Some(4), Some(7));
            // Naive full walk — every bar pushed, no jumps.
            let mut w = Walker::new(true, Some(4), Some(7));
            let mut naive: Vec<TradeRecord> = Vec::new();
            for i in 0..n {
                let src = arm[i];
                let due = src >= 0 && u64::is_superset(bar_bits[src as usize], mask);
                if let Some(t) = w.push(
                    open[i],
                    high[i],
                    low[i],
                    close[i],
                    i,
                    is_forced[i],
                    session_idx[i],
                    due,
                    false,
                ) {
                    naive.push(t);
                }
            }
            if let Some(t) = w.finish() {
                naive.push(t);
            }
            assert_eq!(
                skipped, naive,
                "flat-skip must equal the naive walk (mask {mask})"
            );
        }
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
        // A single bar carries no arm point (arm_source = [-1]) — a signal on
        // decision bar 0 has no stored fill bar → zero trades. Mirrors Python.
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
        // Entry = high[1] = 110 (BUY fills at the entry bar's HIGH). The
        // entry bar folds its own range at open (§2.4 deferral): bar1
        // (110,95) → fav 0 / adv 15; bar2 manages (130,118) → fav 20 /
        // adv 0 → mfe 20, mae 15. End-of-data close at the last MANAGED
        // bar's LOW (118, the long exit's adverse extreme — not the entry
        // bar, so no B3 close basis): pnl = 118 − 110 = 8, holding 2.
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

    // ── B3 terminal carve-out: entry bar IS the last stored bar ─────────────

    #[test]
    fn terminal_close_on_entry_bar_prices_at_close_not_extreme() {
        // The B3 carve-out (design §3.12): mask fires bar0 → entry at bar1
        // (the LAST stored bar) = high[1] = 110; there is no successor, so
        // finish() closes ON the entry bar — at its CLOSE (105), never its
        // adverse extreme (95, which would be the full-range phantom loss).
        // Folds: entry bar (110,95) → fav 0 / adv 15. pnl = 105 − 110 = −5,
        // holding 1 (the legalized exit_idx == entry_idx degenerate).
        let s = sim(
            vec![100, 100],
            vec![101, 110],
            vec![99, 95],
            vec![100, 105],
            vec![1, 0],
        );
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-5i64, 0i64, 15i64, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn terminal_close_on_entry_bar_sell_prices_at_close() {
        // SELL twin of the B3 pin: entry = low[1] = 95; finish() on the
        // entry bar closes at its CLOSE 105 → pnl = 95 − 105 = −10 (never
        // the adverse HIGH 110, which would read −15). Folds (110,95) from
        // entry 95 → fav 0 / adv 15.
        let s = sim(
            vec![100, 100],
            vec![101, 110],
            vec![99, 95],
            vec![100, 105],
            vec![1, 0],
        );
        let out = s.simulate_one(1, false, None, None);
        assert_eq!(out, vec![(-10i64, 0i64, 15i64, 1u64, 1u64, 1u64)]);
    }

    // ── same-bar deferral (§2.4) ────────────────────────────────────────────

    #[test]
    fn entry_bar_level_touch_defers_no_same_bar_exit() {
        // The deferral pin: the entry bar's OWN range pierces the SL level
        // (entry = high[1] = 105, sl=5 → level 100; low[1] = 98 <= 100) but
        // the entry bar NEVER exits — detection begins on bar2, whose low 97
        // also pierces → exit ON bar2 at min(100, 97) = 97 → pnl −8.
        // Folds: entry bar (105,98) → 0/7; bar2 (104,97) → 0/8 → mae 8.
        let s = sim(
            vec![100, 100, 100],
            vec![101, 105, 104],
            vec![99, 98, 97],
            vec![100, 100, 100],
            vec![1, 0, 0],
        );
        let out = s.simulate_one(1, true, Some(5), None);
        assert_eq!(out, vec![(-8i64, 0i64, 8i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn entry_bar_touch_with_no_successor_closes_at_close_basis() {
        // Deferral + B3 composed: the entry bar pierces the SL AND is the
        // last stored bar → no same-bar exit (deferral), no successor →
        // finish() at the entry bar's CLOSE (99): pnl = 99 − 105 = −6.
        let s = sim(
            vec![100, 100],
            vec![101, 105],
            vec![99, 98],
            vec![100, 99],
            vec![1, 0],
        );
        let out = s.simulate_one(1, true, Some(5), None);
        assert_eq!(out, vec![(-6i64, 0i64, 7i64, 1u64, 1u64, 1u64)]);
    }

    // ── stop-loss / take-profit fills (long) ──

    #[test]
    fn long_stop_loss_fills_at_bar_low() {
        // Entry = high[1] = 105; sl=10 anchors on the ACTUAL worst fill →
        // level 95. Entry bar folds (105,98) → fav 0 / adv 7; no touch
        // (98 > 95), nothing deferred. bar2 folds (99,85) → fav 0 / adv 20;
        // hit (85 ≤ 95) → fill = min(level 95, low 85) = 85 (a long SL
        // trigger implies low ≤ level, so the fill is ALWAYS the bar low —
        // the honest gap-through loss): pnl = 85 − 105 = −20.
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
        // 105 (mask 1 fires only bar0); sl=10 → level 95. Entry bar folds
        // (105,98) → fav 0 / adv 7. bar2 gaps through (low 80 ≤ 95) → fill
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
        // Entry = high[1] = 105; tp=20 → level 125. Entry bar folds (105,98)
        // → fav 0 / adv 7; no touch (105 < 125). bar2 folds (125,113) →
        // fav 20 / adv 0 → mfe 20, mae 7. TP triggers (high 125 ≥ 125) but
        // the adverse-max fill is min(level 125, low 113) = 113 — a "take
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
        // COUNTEREXAMPLE 1 teeth (the gap-open law, design B5): entry
        // HIGH[1]=105, tp=20 → level 125. bar2 sits ENTIRELY ABOVE the
        // target (low 126 > 125): a naive sell-at-low would fill 126 (pnl
        // 21 — OVERSTATING vs an at-level fill); the adverse-max law fills
        // min(125, 126) = 125 → pnl exactly the 20-paisa tp distance.
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
        // Entry = high[1] = 105; sl=10 → 95, tp=10 → 115. The entry bar
        // (105,98) touches neither. bar2 spans BOTH (low 85 ≤ 95, high 115
        // ≥ 115) → the STOP resolves first (hardcoded pessimistic tie).
        // Fill = min(95, 85) = 85 → pnl = 85 − 105 = −20. Folds: entry bar
        // → 0/7; bar2 (115,85) → fav 10 / adv 20.
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
        // tp=20 → level 78. Entry bar folds (105,98) → fav 0 / adv 7; no
        // touch. bar2 folds (90,75) → fav 23 / adv 0 → mfe 23, mae 7. TP
        // triggers (low 75 ≤ 78) but the cover fills at max(level 78, high
        // 90) = 90: pnl = 98 − 90 = 8 — the worst printed cover.
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
        // high) → pnl = 95 − 80 = 15 exactly. Folds: entry bar (102,95) →
        // fav 0 / adv 7; bar2 (75,70) → fav 25 / adv 0.
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
        // SHORT entry = low[1] = 98; sl=10 → level 108. Entry bar folds
        // (105,98) → fav 0 / adv 7; no touch (105 < 108). bar2 folds
        // (115,106) → fav 0 / adv 17. SL triggers (high 115 ≥ 108) → cover
        // fill = max(108, 115) = 115 (a short SL trigger implies high ≥
        // level, so the fill is ALWAYS the bar high): pnl = 98 − 115 = −17.
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

    // ── forced session-close exit ──

    #[test]
    fn forced_exit_overrides_and_fills_at_adverse_extreme() {
        // Entry = high[1] = 110; sl=50 → 60, tp=20 → 130: the entry bar
        // (110,95) touches neither (low 95 > 60, high 110 < 130). bar2
        // is_forced AND tp would trigger (high 130 ≥ 130) — but forced
        // overrides → fill at the forced bar's LOW (123), never the tp
        // level; the fold stays the legacy (open, open) = (125, 125) →
        // fav 15 / adv 0. pnl = 123 − 110 = 13; mfe 15, mae 15 (entry adv).
        let s = ResidentSweepSim::build(
            vec![50, 100, 125],
            vec![60, 110, 130],
            vec![40, 95, 123],
            vec![55, 105, 128],
            vec![false, false, true],
            vec![0, 0, 0],
            vec![-1, 0, 1],
            vec![0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(0, true, Some(50), Some(20));
        assert_eq!(out, vec![(13i64, 15i64, 15i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn forced_fill_bar_drops_the_entry() {
        // mask=1 fires ONLY on decision bar0, whose mapped fill bar (bar1)
        // is_forced → the entry is DROPPED (an entry never fills at/after
        // the boundary). No later arm fires (bit0 clear) → zero trades.
        let s = ResidentSweepSim::build(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 125],
            vec![false, true, false],
            vec![0, 0, 0],
            vec![-1, 0, 1],
            vec![1, 0, 0],
        )
        .unwrap();
        assert_eq!(
            s.simulate_one(1, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    // ── armless boundaries (the exec-grid gates, kernel-side view) ──────────

    #[test]
    fn armless_boundary_bar_never_fills() {
        // R2-OC-2 re-keyed: the same-session gate is BAKED into arm_source
        // at map build (a cross-session candidate never becomes an arm).
        // Kernel-side shape: session 0's last decision bar has NO arm point
        // (arm[2] = -1 — the map dropped it), so its signal never fills;
        // the next session's own signal (decision 2, arm at bar3) does.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900, 11950],
            vec![10100, 10100, 12000, 12050],
            vec![9900, 9900, 11800, 11850],
            vec![10000, 10000, 11900, 11950],
            vec![false, false, false, false],
            vec![0, 0, 1, 1],
            vec![-1, 0, -1, 2],
            vec![0, 1, 0, 0],
        )
        .unwrap();
        assert_eq!(
            s.simulate_one(1, true, None, None),
            Vec::<TradeRecord>::new()
        );
    }

    #[test]
    fn same_session_arm_still_fills() {
        // The SAME feed shape but single-session with the next-bar arms: the
        // decision-1 signal fills at bar2 (entry = high[2] = 12000). Folds:
        // entry bar (12000,11800) → fav 0 / adv 200; bar3 (12050,11850) →
        // fav 50 / adv 150 → mfe 50, mae 200. End-of-data close at low[3] =
        // 11850 → pnl = −150.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900, 11950],
            vec![10100, 10100, 12000, 12050],
            vec![9900, 9900, 11800, 11850],
            vec![10000, 10000, 11900, 11950],
            vec![false, false, false, false],
            vec![0, 0, 0, 0],
            vec![-1, 0, 1, 2],
            vec![0, 1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-150i64, 50i64, 200i64, 2u64, 2u64, 3u64)]);
    }

    // ── cross-session OPEN-position close (DEC-TRADESIM-OPEN-SESSION-GUARD-001) ──

    #[test]
    fn cross_session_open_position_closes_at_prior_session_adverse_extreme() {
        // Directed hand-computed boundary close: signal bar0 → entry =
        // high[1] = 10120 (session 0). The entry bar folds (10120,9950) →
        // fav 0 / adv 170; bar2 manages (10150,9960) → fav 30 / adv 160 →
        // mfe 30, mae 170; bar2's terminal basis = its LOW 9960 (a managed
        // bar — not the entry bar, so the adverse-extreme basis). bar3 is
        // SESSION 1 → the guard closes retroactively at 9960 with
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
            vec![-1, 0, 1, -1],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-160i64, 30i64, 170i64, 2u64, 1u64, 2u64)]);
    }

    #[test]
    fn session_gap_on_entry_bar_prices_at_close_basis() {
        // B3 × session_gap: the entry fills on session 0's LAST stored bar
        // (bar1, entry = high 10120); bar2 is session 1 → the guard closes
        // ON the entry bar — at its CLOSE (10050, the B3 basis), never its
        // adverse LOW (9950): pnl = 10050 − 10120 = −70, holding 1. Folds:
        // entry bar (10120,9950) → fav 0 / adv 170.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 11900],
            vec![10100, 10120, 12000],
            vec![9900, 9950, 11800],
            vec![10000, 10050, 11900],
            vec![false, false, false],
            vec![0, 0, 1],
            vec![-1, 0, -1],
            vec![1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-70i64, 0i64, 170i64, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn cross_session_boundary_clears_pending_exit_and_new_position_rides() {
        // Walker-driven (the Mode-B exit-signal stream never crosses the
        // sweep FFI): a Mode-B exit deferred on its OWN entry bar (the §2.4
        // pending arm) is SUBSUMED by the boundary close — never filled on
        // session 1's bar — AND the clear has teeth: a NEW session-1
        // position must ride to finish (a stale pending would close it
        // instantly). Trade 1: entry fills bar1 (high 10060) with exit_due
        // on the SAME bar → pending armed; bar2 is session 1 → guard closes
        // at the ENTRY bar's CLOSE (B3 basis 10010): pnl = 10010 − 10060 =
        // −50, exit_idx 1, holding 1; pending cleared. Trade 2: entry fills
        // bar3 (high 12010); folds (12010,11890) → 0/120; bar4 manages
        // (12020,11900) → 10/110 → mfe 10, mae 120, basis = low 11900;
        // finish() closes at 11900: pnl = −110, exit_idx 4, holding 2.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, i64, bool, bool); 5] = [
            // (open, high, low, close, session, entry_due, exit_due)
            (10000, 10050, 9900, 10000, 0, false, false),
            (10000, 10060, 9960, 10010, 0, true, true),
            (11900, 12000, 11880, 11900, 1, false, false),
            (11910, 12010, 11890, 11910, 1, true, false),
            (11920, 12020, 11900, 11920, 1, false, false),
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
                (-50i64, 0i64, 100i64, 1u64, 1u64, 1u64),
                (-110i64, 10i64, 120i64, 2u64, 3u64, 4u64),
            ]
        );
    }

    #[test]
    fn cross_session_boundary_bar_never_fills_next_arm_does() {
        // §2.2 pinned law re-keyed: the boundary bar EMITS the session_gap
        // close, so the `closed.is_none()` gate suppresses a fill on it — an
        // arm point ON the boundary bar (bar3) opens nothing; the next arm
        // (bar5) fills. Trade 1 = the boundary close of the sibling test:
        // (−160, 30, 170, 2, 1, 2).
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 11900, 11910, 11920, 11930],
            vec![10100, 10120, 10150, 12000, 12010, 12020, 12030],
            vec![9900, 9950, 9960, 11800, 11810, 11820, 11830],
            vec![10000, 10050, 10100, 11900, 11910, 11920, 11940],
            vec![false; 7],
            vec![0, 0, 0, 1, 1, 1, 1],
            vec![-1, 0, 1, 2, -1, 4, -1],
            vec![1, 0, 1, 0, 1, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0], (-160i64, 30i64, 170i64, 2u64, 1u64, 2u64));
        let (_pnl, _mfe, _mae, _holding, entry_idx, exit_idx) = out[1];
        assert_eq!(entry_idx, 5); // decision 4's arm at bar5 — never bar3
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
            vec![-1, 0, 1, -1],
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
            vec![-1, 0, 1],
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
        // closes the open position — either direction, not only forward
        // (the r8 `!=`→`>` mutant class, M14). Signal bar0 → entry =
        // high[1] = 10120 (session 5); the entry bar folds (10120,9950) →
        // mfe 0 / mae 170; its terminal basis is its CLOSE 10110 (B3 — the
        // entry bar IS the last bar the position saw). bar2 DROPS to
        // session 4 → the guard closes retroactively at 10110 with
        // exit_idx = 1 (pnl = 10110 − 10120 = −10, holding 1). Under the
        // `>` mutant 4 > 5 is false, so the backward bar folds into the
        // position and it rides to the end-of-data close — the killer.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 9500, 9600],
            vec![10100, 10120, 9550, 9650],
            vec![9900, 9950, 9400, 9500],
            vec![10000, 10110, 9500, 9610],
            vec![false, false, false, false],
            vec![5, 5, 4, 4],
            vec![-1, 0, -1, -1],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-10i64, 0i64, 170i64, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn same_session_multi_bar_hold_does_not_close_early() {
        // The `!=`→`==` mutant killer on the guard's session compare: a
        // multi-bar hold whose bars ALL share one NON-ZERO session ordinal
        // must ride to the end-of-data close — under the mutated compare
        // (or a session-field stamping bug) the guard would fire on the
        // first managed bar instead. Entry = high[1] = 10120; folds: entry
        // bar (10120,9950) → 0/170; bar2 (10150,9960) → 30/160; bar3
        // (10160,9970) → 40/150 → mfe 40, mae 170. End-of-data close at
        // low[3] = 9970 → pnl = −150.
        let s = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 10020],
            vec![10100, 10120, 10150, 10160],
            vec![9900, 9950, 9960, 9970],
            vec![10000, 10050, 10100, 10140],
            vec![false; 4],
            vec![5, 5, 5, 5],
            vec![-1, 0, 1, 2],
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let out = s.simulate_one(1, true, None, None);
        assert_eq!(out, vec![(-150i64, 40i64, 170i64, 3u64, 1u64, 3u64)]);
    }

    // ── Mode-B exits (walker-driven — no exit stream crosses the FFI) ──

    #[test]
    fn mode_b_exit_due_fills_at_adverse_extreme_fold_unchanged() {
        // A mapped Mode-B exit_due on bar3 closes ON bar3 at its LOW (78)
        // while the excursion still folds the legacy (open, open) = (80, 80)
        // — mae reads 25 (from the fold), NOT the 27 the fill implies: the
        // fold logic is deliberately untouched. Entry fills bar1 (high 105);
        // folds: entry bar (105,98) → 0/7; bar2 (104,99) → 0/6; bar3
        // (80,80) → 0/25. pnl = 78 − 105 = −27, holding 3.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 4] = [
            // (open, high, low, close, entry_due, exit_due)
            (100, 100, 100, 100, false, false),
            (100, 105, 98, 100, true, false),
            (100, 104, 99, 100, false, false),
            (80, 82, 78, 81, false, true),
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
    fn mode_b_exit_due_beats_forced_on_the_same_bar() {
        // Precedence pin (the historical pending-beats-forced ladder): a bar
        // that is BOTH the mapped exit_due bar AND forced closes via the
        // signal arm — numerically identical (same adverse fill, same
        // (open,open) fold), so the record is the parity surface; the
        // Python twin's detail sink reads reason "signal".
        let mut w = Walker::new(true, None, None);
        let mut trades: Vec<TradeRecord> = Vec::new();
        for (i, &(o, h, l, c, forced, entry, exit)) in [
            (100i64, 100i64, 100i64, 100i64, false, true, false),
            (100, 105, 98, 100, false, false, false),
            (99, 101, 97, 100, true, false, true),
        ]
        .iter()
        .enumerate()
        {
            if let Some(t) = w.push(o, h, l, c, i, forced, 0, entry, exit) {
                trades.push(t);
            }
        }
        // Entry fills bar1?? — no: entry_due on bar0 (arm at bar0) opens at
        // bar0 (high 100); folds (100,100) → 0/0; bar1 manages (105,98) →
        // 5/2; bar2: exit_due (and forced) → fold (99,99) → fav 0/adv 1 →
        // close at low 97: pnl = 97 − 100 = −3, mfe 5, mae 2, holding 3.
        assert_eq!(trades, vec![(-3i64, 5i64, 2i64, 3u64, 0u64, 2u64)]);
    }

    #[test]
    fn mode_b_deferred_pending_at_end_of_data_closes_at_close_basis() {
        // An exit_due landing on the trade's OWN entry bar defers via
        // pending_exit (§2.4); with NO successor the pending is IGNORED by
        // finish(), which closes at the entry bar's CLOSE (the B3 basis).
        // Entry fills bar1 (high 105, close 100) with exit_due same bar →
        // pnl = 100 − 105 = −5, exit_idx 1, holding 1.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 2] = [
            (100, 100, 100, 100, false, false),
            (100, 105, 98, 100, true, true),
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
        assert_eq!(trades, vec![(-5i64, 0i64, 7i64, 1u64, 1u64, 1u64)]);
    }

    #[test]
    fn mode_b_deferred_pending_fills_on_the_successor() {
        // The deferral's normal arm: exit_due on the entry bar → pending →
        // fills on the NEXT bar at its adverse extreme with the (open,open)
        // fold. Entry fills bar1 (high 105); pending armed; bar2 closes at
        // low 96 with fold (99,99) → fav 0 / adv 6 → mae 7 (entry bar's).
        // pnl = 96 − 105 = −9, exit_idx 2, holding 2.
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 3] = [
            (100, 100, 100, 100, false, false),
            (100, 105, 98, 100, true, true),
            (99, 101, 96, 100, false, false),
        ];
        let mut trades: Vec<TradeRecord> = Vec::new();
        for (i, &(o, h, l, c, entry, exit)) in bars.iter().enumerate() {
            if let Some(t) = w.push(o, h, l, c, i, false, 0, entry, exit) {
                trades.push(t);
            }
        }
        assert!(w.finish().is_none());
        assert_eq!(trades, vec![(-9i64, 0i64, 7i64, 2u64, 1u64, 2u64)]);
    }

    // ── multiple non-overlapping trades from one mask ──

    #[test]
    fn multiple_trades_non_overlapping() {
        // Entry bars (1, 4) are FLAT at 100 (high == open) so the worst
        // entry is 100 and the entry-bar fold touches nothing; tp=5 →
        // level 105. Trigger bars (2, 5) print high 110 / low 106 → fill =
        // min(105, 106) = 105 → pnl 5 each.
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
        // Entry = high[1] = 130 (the worst fill). Folds from it: entry bar
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
        // OLD-ABORT REPRODUCTION (build side): a ~2^63 paisa operand in ANY
        // price column previously flowed into the walker's raw arithmetic
        // and SIGABRT'd under `overflow-checks = true` + `panic = "abort"`.
        // The boundary now rejects it with a clean Err. One column at a
        // time, both extremes, so every clause of the guard is exercised.
        let ok = || vec![100i64, 100];
        let okf = || vec![false; 2];
        let oks = || vec![0i64; 2];
        let oka = || vec![-1i64; 2];
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
                let err = ResidentSweepSim::build(o, h, l, c, okf(), oks(), oka(), okb())
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
            vec![-1, 0],
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
        // intermediates actually occur and none panics. Entry signal on
        // decision bar0 → entry = high[1] = C (the worst BUY fill); levels:
        // sl = C − C = 0, tp = C + C = 2C (the max level magnitude). The
        // entry bar folds (C, −C) → fav 0 / adv 2C (the max excursion
        // intermediate) — its low −C pierces the SL but the §2.4 deferral
        // never exits the entry bar. bar2 (high 0, low 0) folds → fav −C→0
        // / adv C; SL hit (0 ≤ 0) → fill = min(0, 0) = 0 → pnl = 0 − C = −C
        // (|pnl| ≤ 3C — the derivation's bound), holding 2.
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
        assert_eq!(out[0], vec![(-C, 0i64, 2 * C, 2u64, 1u64, 2u64)]);
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
        // per-mask worst-case 1m walk, and `count_trades` agrees — on BOTH
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

    /// u128 twin of [`sim`] — no forced bars, one session, next-bar arms.
    fn sim_u128(
        open: Vec<i64>,
        high: Vec<i64>,
        low: Vec<i64>,
        close: Vec<i64>,
        bar_bits: Vec<u128>,
    ) -> ResidentSweepSimU128 {
        let n = open.len();
        ResidentSweepSimU128::build(
            open,
            high,
            low,
            close,
            vec![false; n],
            vec![0; n],
            next_bar_arm(n),
            bar_bits,
        )
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
            vec![-1, -1],
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
            vec![-1, -1],
            vec![0, 0],
        )
        .map(|_| ())
        .expect_err("out-of-range price must be rejected at build");
        assert!(err.contains("overflow-safe range"));
        assert!(err.starts_with("ResidentSweepSimU128:"));
    }

    #[test]
    fn u128_build_rejects_bad_arm_naming_the_class() {
        let err = ResidentSweepSimU128::build(
            vec![0; 2],
            vec![0; 2],
            vec![0; 2],
            vec![0; 2],
            vec![false; 2],
            vec![0; 2],
            vec![-1, 7],
            vec![1u128],
        )
        .map(|_| ())
        .expect_err("out-of-range arm must be rejected");
        assert!(err.starts_with("ResidentSweepSimU128:"), "{err}");
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
        let s = ResidentSweepSimU128::build(
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
        )
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
        //     BUY fill); folds entry bar (110,92) → 0/18, bar2 (100,100) →
        //     0/10, bar3 (108,100) → 0/10 → mfe 0, mae 18; end-of-data close
        //     at low[3] = 100 → pnl −10, holding 3.
        //   * 1<<64 → bar0 is a SUPERSET (bit63 extra) → same trade.
        //   * 1<<127 → fires bar2 → entry = high[3] = 108; fold entry bar
        //     (108,100) → 0/8; end-of-data close ON the entry bar → the B3
        //     CLOSE basis (close[3] = 100) → pnl −8, holding 1.
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
        // re-fire); fires bar3 but the position is open → rides to the
        // end-of-data close at low[3] = 100 → pnl −10; folds as in the
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

    // ── Exit-reason counters (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) ──
    // Every expected record below is HAND-DERIVED under the worst-case fill
    // law (DEC-FILL-WORST-CASE-ONLY-001): entry = the fill bar's adverse
    // extreme (BUY high), level fills adverse-clamped, gap/EOD closes at the
    // last managed bar's exit basis.

    /// Counted-batch helper over the u64 sim.
    fn counted(
        s: &ResidentSweepSim,
        masks: &[u64],
        is_buy: bool,
        sl: Option<i64>,
        tp: Option<i64>,
    ) -> Vec<(Vec<TradeRecord>, ExitReasonCounts)> {
        s.simulate_masks_with_exit_counts_impl(masks, is_buy, sl, tp)
            .expect("in-range inputs must pass the boundary")
    }

    #[test]
    fn exit_counts_trades_identical_to_uncounted_batch() {
        // ONE-walk-spine drift pin: the counted twin's trades are byte-equal
        // to `simulate_masks` on identical inputs (both ride
        // `simulate_one_counted_core`), and the counters SUM to each mask's
        // trade count (each close increments exactly one reason).
        let s = sim(
            vec![100, 100, 100, 100, 100, 100],
            vec![100, 110, 100, 100, 110, 100],
            vec![100, 92, 100, 100, 92, 100],
            vec![100, 100, 100, 100, 100, 100],
            vec![1, 0, 0, 1, 0, 0],
        );
        let masks = vec![0u64, 1u64, 1u64 << 63];
        let counted_out = counted(&s, &masks, true, Some(8), Some(5));
        let plain = s
            .simulate_masks_impl(&masks, true, Some(8), Some(5))
            .expect("in-range");
        for (j, (trades, counts)) in counted_out.iter().enumerate() {
            assert_eq!(trades, &plain[j], "mask {j}: counted trades must match");
            let (sl_n, tp_n, forced, signal, eod, gap) = *counts;
            assert_eq!(
                sl_n + tp_n + forced + signal + eod + gap,
                trades.len() as u64,
                "mask {j}: counter sum must equal the trade count"
            );
        }
    }

    #[test]
    fn exit_counts_attribute_each_reason_exactly() {
        // Directed per-reason attribution under the worst-case fill law:
        //   * SL: entry = high[1] = 105, sl=10 → level 95; bar2 low 85 ≤ 95
        //     → stop → (1,0,0,0,0,0).
        //   * TP: entry = high[1] = 105, tp=20 → level 125; bar2 high 125 ≥
        //     125 → target → (0,1,0,0,0,0).
        //   * Forced: entry = high[1] = 110 (sl 50 → 60, tp 20 → 130); bar2
        //     is forced and precedes the would-be TP touch → (0,0,1,0,0,0).
        //   * End of data: no levels → finish() → (0,0,0,0,1,0).
        //   * Session gap: the (A2) boundary close → (0,0,0,0,0,1).
        let sl_sim = sim(
            vec![50, 100, 95],
            vec![60, 105, 99],
            vec![40, 98, 85],
            vec![55, 100, 95],
            vec![0, 0, 0],
        );
        assert_eq!(
            counted(&sl_sim, &[0], true, Some(10), None)[0].1,
            (1, 0, 0, 0, 0, 0)
        );
        let tp_sim = sim(
            vec![50, 100, 115],
            vec![60, 105, 125],
            vec![40, 98, 113],
            vec![55, 100, 118],
            vec![0, 0, 0],
        );
        assert_eq!(
            counted(&tp_sim, &[0], true, None, Some(20))[0].1,
            (0, 1, 0, 0, 0, 0)
        );
        let forced_sim = ResidentSweepSim::build(
            vec![50, 100, 125],
            vec![60, 110, 130],
            vec![40, 95, 123],
            vec![55, 105, 128],
            vec![false, false, true],
            vec![0, 0, 0],
            next_bar_arm(3),
            vec![0, 0, 0],
        )
        .unwrap();
        let forced_out =
            forced_sim.simulate_masks_with_exit_counts_impl(&[0], true, Some(50), Some(20));
        assert_eq!(
            forced_out.expect("in-range")[0].1,
            (0, 0, 1, 0, 0, 0),
            "forced overrides the would-be TP → counted forced_1520"
        );
        let eod_sim = sim(
            vec![50, 100, 120],
            vec![60, 110, 130],
            vec![40, 95, 118],
            vec![55, 105, 130],
            vec![0, 0, 0],
        );
        assert_eq!(
            counted(&eod_sim, &[0], true, None, None)[0].1,
            (0, 0, 0, 0, 1, 0)
        );
        // Session gap: entry = high[1] = 10120; folds bar1 (10120,9950) →
        // 0/170, bar2 (10150,9960) → 30/160 → mfe 30, mae 170; bar3 is
        // session 1 → (A2) closes at bar2's LOW (9960), exit_idx 2:
        // pnl = 9960 − 10120 = −160, holding 2.
        let gap_sim = ResidentSweepSim::build(
            vec![10000, 10000, 10010, 11900],
            vec![10100, 10120, 10150, 12000],
            vec![9900, 9950, 9960, 11800],
            vec![10000, 10050, 10100, 11900],
            vec![false, false, false, false],
            vec![0, 0, 0, 1],
            next_bar_arm(4),
            vec![1, 0, 0, 0],
        )
        .unwrap();
        let gap_out = gap_sim
            .simulate_masks_with_exit_counts_impl(&[1], true, None, None)
            .expect("in-range");
        assert_eq!(
            gap_out[0].0,
            vec![(-160i64, 30i64, 170i64, 2u64, 1u64, 2u64)]
        );
        assert_eq!(gap_out[0].1, (0, 0, 0, 0, 0, 1));
    }

    #[test]
    fn exit_counts_mode_b_pending_fill_attributes_signal() {
        // Walker-driven (the Mode-B exit stream never crosses the sweep FFI):
        // a Mode-B pending exit's next-bar fill is counted under SIGNAL —
        // the arming reason carried by `pending_exit: Option<ExitReason>` —
        // exactly the Python walker's pending-reason attribution. Bars are
        // `mode_b_pending_exit_fills_at_adverse_extreme_fold_unchanged`'s
        // (trade record re-verified identical there).
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, bool, bool); 4] = [
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
        assert_eq!(trades.len(), 1);
        assert_eq!(w.exit_counts_tuple(), (0, 0, 0, 1, 0, 0));
    }

    #[test]
    fn exit_counts_session_gap_subsumes_pending_reason() {
        // The (A2) boundary close SUBSUMES a pending Mode-B exit — the close
        // is counted session_gap, never the stale pending signal (mirrors
        // `cross_session_boundary_clears_pending_exit_and_new_position_rides`,
        // whose bars + trade records these are: trade 1 = the boundary close,
        // trade 2 = the finish() end-of-data close).
        let mut w = Walker::new(true, None, None);
        let bars: [(i64, i64, i64, i64, i64, bool, bool); 7] = [
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
        assert_eq!(trades.len(), 2);
        assert_eq!(
            w.exit_counts_tuple(),
            (0, 0, 0, 0, 1, 1),
            "boundary close = session_gap; the pending signal is subsumed, \
             never counted; the second trade is the end-of-data close"
        );
    }

    #[test]
    fn exit_counts_multiple_trades_accumulate() {
        // Two TP round-trips from one mask → the counter reads 2, matching
        // `multiple_trades_non_overlapping`'s trade census (its fixture
        // verbatim: flat entry bars → worst entry 100, tp=5 → level 105,
        // trigger bars fill min(105, 106) = 105).
        let s = sim(
            vec![100, 100, 106, 100, 100, 106],
            vec![100, 100, 110, 100, 100, 110],
            vec![100, 100, 106, 100, 100, 106],
            vec![100, 100, 106, 100, 100, 106],
            vec![1, 0, 0, 1, 0, 0],
        );
        assert_eq!(
            counted(&s, &[1], true, None, Some(5))[0].1,
            (0, 2, 0, 0, 0, 0)
        );
    }

    #[test]
    fn exit_counts_parallel_arm_matches_serial() {
        // rayon out[j] ↔ masks[j] alignment + counter parity ABOVE the work
        // threshold (the counted twin of `parallel_arm_matches_serial`).
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
        let batch = counted(&s, &masks, true, Some(3), Some(4));
        for (j, &m) in masks.iter().enumerate() {
            let solo = s
                .simulate_masks_with_exit_counts_impl(&[m], true, Some(3), Some(4))
                .expect("in-range");
            assert_eq!(batch[j], solo[0], "mask {j}: parallel != serial");
        }
    }

    #[test]
    fn exit_counts_boundary_rejects_like_uncounted() {
        // The counted batch rides the SAME R2-RC-F1 SL/TP boundary as
        // `simulate_masks` — out-of-range distances reject with the class label.
        let s = sim(
            vec![100, 100],
            vec![100, 100],
            vec![100, 100],
            vec![100, 100],
            vec![1, 0],
        );
        let err = s
            .simulate_masks_with_exit_counts_impl(&[1], true, Some(i64::MAX), None)
            .map(|_| ())
            .expect_err("out-of-range sl_paisa must be rejected");
        assert!(err.starts_with("ResidentSweepSim:"), "{err}");
    }

    #[test]
    fn u128_exit_counts_bridge_matches_u64_kernel() {
        // ANTI-DRIFT BRIDGE PIN (the counted twin of
        // `u128_bridge_matches_u64_kernel_on_u64_fit_inputs`): on u64-fit
        // inputs the two widths' counted batches are field-identical, and
        // the wide class's boundary reject names ITSELF.
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
        let out64 = counted(&s64, &masks_u64, true, Some(8), Some(5));
        let out128 = s128
            .simulate_masks_with_exit_counts_impl(&masks_u128, true, Some(8), Some(5))
            .expect("in-range");
        assert_eq!(out64, out128);
        let err = s128
            .simulate_masks_with_exit_counts_impl(&[1], true, Some(-1), None)
            .map(|_| ())
            .expect_err("negative sl_paisa must be rejected");
        assert!(err.starts_with("ResidentSweepSimU128:"), "{err}");
    }
}
