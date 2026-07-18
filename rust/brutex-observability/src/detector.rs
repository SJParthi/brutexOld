//! Laws A / B / C — the three pure detector decision laws.
//!
//! Bit-for-bit mirrors of the Python oracles (the facade's live parity
//! sources), transcribed token-for-token where the host language allows:
//!
//! * **Law A** — [`loop_watchdog_check_once`]: the tiered
//!   OK→WARNED→CRITICAL→recovered event-loop stall state machine
//!   (`brutex/observability/loop_watchdog.py::_check_once_python`).
//! * **Law B** — [`watchdog_decide`]: the per-worker HEALTHY↔HUNG decision
//!   (`brutex/observability/watchdog.py::_worker_decide_python`).
//! * **Law C** — [`sla_decide`]: the SLA predicate + hysteresis edge
//!   (`brutex/observability/sla.py::_sla_decide_python`).
//!
//! ## Internal sharing (the honesty ruling)
//!
//! B and C genuinely share the two-state edge law (`edge = new != prev`) —
//! ONE private [`two_state_edge`] hosts it. Law A's tiered machine shares
//! NOTHING material with B/C — it gets its own function; a forced "shared
//! hysteresis core" abstraction over all three would be dishonest and is
//! deliberately NOT built.
//!
//! ## The argument-order `max` mirror
//!
//! Python's `max(0.0, d)` returns `d` iff `d > 0.0`, else the FIRST operand
//! `0.0` — so `d = NaN` yields `0.0` (the comparison is False) and
//! `d = -0.0` yields `+0.0`. The Rust mirror is the explicit
//! `if d > 0.0 { d } else { 0.0 }` — NOT `f64::max` (identical here, but the
//! explicit form documents the Python argument-order law and is
//! signed-zero/NaN exact by construction).

/// The law-A `episodes + 1` overflow refusal message (names the field — the
/// facade's degrade-set `ValueError` carries it verbatim to the WARN log).
pub const EPISODES_OVERFLOW_MSG: &str =
    "episodes counter overflow: episodes + 1 exceeds u64::MAX on the warn edge";

/// The ONE law arithmetic fault: the warn-edge `episodes + 1` increment
/// overflowing u64 (`checked_add` — module docs). Crate-wide integer-op
/// audit (this fix's scope): this is the ONLY overflow-capable integer op in
/// the laws — every other detector op is IEEE f64 (never panics) and
/// stamp.rs holds only string compares plus a bounded `usize → i64` cast
/// (index ≤ 5, lossless). Maps to a `PyValueError` at the FFI edge, a
/// facade degrade-set member: the oracle ships the honest value (Python
/// ints are unbounded — `u64::MAX + 1` is a fine Python episodes count).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EpisodesOverflow;

impl core::fmt::Display for EpisodesOverflow {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(EPISODES_OVERFLOW_MSG)
    }
}

impl core::error::Error for EpisodesOverflow {}

/// The three loop-watchdog tier tokens (mirrors `loop_watchdog.TIER_*`).
pub const TIER_OK: &str = "ok";
/// The WARNED tier token.
pub const TIER_WARNED: &str = "warned";
/// The CRITICAL tier token.
pub const TIER_CRITICAL: &str = "critical";

/// The SLA EQUALS epsilon — the identical literal to `sla._EPS` (same f64).
pub const SLA_EPS: f64 = 1e-9;

/// One law-A decision — the new state scalars plus the edge flags
/// (the scalar decomposition of `loop_watchdog.WatchdogVerdict`).
#[derive(Debug, Clone, PartialEq)]
pub struct LoopVerdict {
    /// New `state.tier` — always one of the three tier tokens.
    pub tier: &'static str,
    /// New `state.stall_started_beat`.
    pub stall_started_beat: f64,
    /// New `state.last_logged_at`.
    pub last_logged_at: f64,
    /// New `state.last_alerted_at`.
    pub last_alerted_at: f64,
    /// New `state.episodes` (WARN edges — feeds the /metrics counter).
    pub episodes: u64,
    /// The sampled heartbeat lag (never negative; NaN diff clamps to 0.0).
    pub lag_s: f64,
    /// Episode start — WARN log + episode counter.
    pub warn_edge: bool,
    /// ERROR log + Telegram.
    pub critical_edge: bool,
    /// Periodic re-log while stalled (non-edge samples only).
    pub relog: bool,
    /// Periodic re-alert while CRITICAL persists (non-edge samples only).
    pub realert: bool,
    /// Recovery — INFO with `stall_total_s`.
    pub recovered: bool,
    /// `max(0.0, last_beat - stall_started_beat)` on recovery, else 0.0.
    pub stall_total_s: f64,
}

/// The Python `max(0.0, d)` mirror — argument-order exact (module docs).
#[inline]
fn clamp_non_negative(d: f64) -> f64 {
    if d > 0.0 { d } else { 0.0 }
}

/// Law A — one PURE loop-watchdog decision (`(now, last_beat, state) → verdict`).
///
/// Token-for-token mirror of `_check_once_python` (loop_watchdog.py). Laws:
///
/// * `lag = max(0.0, now - last_beat)` — argument-order `max` mirror
///   (NaN → 0.0, −0.0 → +0.0).
/// * Thresholds are `>=` (a lag exactly AT the threshold trips); `lag >= NaN`
///   is False → OK tier; no epsilon anywhere.
/// * The `tier` compare is OPAQUE string equality — a junk token (not one of
///   the three) behaves as "previously stalled, non-critical"; NO validation
///   is added (adding one would CHANGE behavior). The RETURNED tier is
///   always one of the three tokens by construction.
/// * A starved monitor may observe the lag jumping straight past
///   `critical_after_s` — that sample fires the warn edge AND the critical
///   edge together (one episode, both flags).
/// * `relog` fires only on a NON-edge stalled sample ≥ `relog_every_s` after
///   the last logged sample; `realert` only at CRITICAL target on a
///   non-critical-edge sample ≥ `realert_every_s` after the last alert.
/// * Recovery (target back at OK from a stalled tier) resets to a fresh
///   state (three stamps 0.0, episodes CARRIED) and reports
///   `stall_total_s = max(0.0, last_beat - stall_started_beat)`.
/// * `episodes + 1` on the warn edge is `checked_add` — a `u64::MAX` input
///   returns `Err(EpisodesOverflow)` instead of panicking/wrapping (the
///   pre-fix overflow-checks panic crossed the FFI as a PER-EXTENSION
///   `PanicException` no name-based belt home could resolve — the escape
///   this refusal kills at the root); the FFI edge maps it to `ValueError`
///   and the facade's degrade set serves the ORACLE value, which is honest —
///   Python ints are unbounded, so the oracle computes `u64::MAX + 1` fine.
///   Every in-u64-domain call returns `Ok`.
#[allow(clippy::too_many_arguments)]
pub fn loop_watchdog_check_once(
    now: f64,
    last_beat: f64,
    tier: &str,
    stall_started_beat: f64,
    last_logged_at: f64,
    last_alerted_at: f64,
    episodes: u64,
    warn_after_s: f64,
    critical_after_s: f64,
    relog_every_s: f64,
    realert_every_s: f64,
) -> Result<LoopVerdict, EpisodesOverflow> {
    let lag = clamp_non_negative(now - last_beat);
    let target = if lag >= critical_after_s {
        TIER_CRITICAL
    } else if lag >= warn_after_s {
        TIER_WARNED
    } else {
        TIER_OK
    };

    if target == TIER_OK {
        if tier == TIER_OK {
            // OK→OK: the Python law returns the caller's state UNCHANGED
            // (object passthrough there; value passthrough here).
            return Ok(LoopVerdict {
                tier: TIER_OK,
                stall_started_beat,
                last_logged_at,
                last_alerted_at,
                episodes,
                lag_s: lag,
                warn_edge: false,
                critical_edge: false,
                relog: false,
                realert: false,
                recovered: false,
                stall_total_s: 0.0,
            });
        }
        let total = clamp_non_negative(last_beat - stall_started_beat);
        return Ok(LoopVerdict {
            tier: TIER_OK,
            stall_started_beat: 0.0,
            last_logged_at: 0.0,
            last_alerted_at: 0.0,
            episodes,
            lag_s: lag,
            warn_edge: false,
            critical_edge: false,
            relog: false,
            realert: false,
            recovered: true,
            stall_total_s: total,
        });
    }

    let warn_edge = tier == TIER_OK;
    let critical_edge = target == TIER_CRITICAL && tier != TIER_CRITICAL;
    let relog = !warn_edge && !critical_edge && now - last_logged_at >= relog_every_s;
    let realert =
        target == TIER_CRITICAL && !critical_edge && now - last_alerted_at >= realert_every_s;
    let episodes_next = if warn_edge {
        // The ONE overflow-capable integer op in the laws — checked, never
        // a panic across the FFI, never a silent wrap (module docs).
        episodes.checked_add(1).ok_or(EpisodesOverflow)?
    } else {
        episodes
    };
    Ok(LoopVerdict {
        tier: target,
        stall_started_beat: if warn_edge {
            last_beat
        } else {
            stall_started_beat
        },
        last_logged_at: if warn_edge || critical_edge || relog {
            now
        } else {
            last_logged_at
        },
        last_alerted_at: if critical_edge || realert {
            now
        } else {
            last_alerted_at
        },
        episodes: episodes_next,
        lag_s: lag,
        warn_edge,
        critical_edge,
        relog,
        realert,
        recovered: false,
        stall_total_s: 0.0,
    })
}

/// The two-state hysteresis edge law B and C genuinely share (module docs).
#[inline]
fn two_state_edge(prev: bool, new: bool) -> bool {
    new != prev
}

/// Law B — the per-worker HEALTHY↔HUNG decision: `(elapsed, hung, edge)`.
///
/// * `elapsed = now - last_beat` — NO clamp (negative elapsed is legal and
///   preserved; unlike law A there is no `max(0.0, …)` — deliberately NOT
///   "fixed"). IEEE f64 subtraction, signed zeros + NaN production intact.
/// * STRICT `>` — elapsed EXACTLY equal to `timeout_seconds` is NOT hung
///   (contrast law A's `>=`; the two modules deliberately differ).
/// * NaN elapsed (`NaN > t` False) → HEALTHY; if prev was HUNG that IS a
///   recovery edge — no NaN special-casing.
pub fn watchdog_decide(
    now: f64,
    last_beat: f64,
    timeout_seconds: f64,
    prev_hung: bool,
) -> (f64, bool, bool) {
    let elapsed = now - last_beat;
    let hung = elapsed > timeout_seconds;
    (elapsed, hung, two_state_edge(prev_hung, hung))
}

/// The law-C predicate core — mirrors `sla._predicate_holds` exactly.
///
/// * `"greater_than"` → strict `value > threshold`; `"less_than"` → strict
///   `<` (a value exactly AT the threshold never breaches).
/// * ANY other token — including junk — takes the EQUALS arm
///   (`abs(value - threshold) < 1e-9`, strict `<`): the Python law has NO
///   else-raise, so the fallthrough is the law; NEVER a raise/refusal on an
///   unknown token (that would be a behavior change).
/// * NaN value: all three arms False. `abs` matches Python `abs`
///   (`abs(-0.0) = +0.0`, `abs(NaN) = NaN`).
pub fn predicate_holds(predicate: &str, value: f64, threshold: f64) -> bool {
    match predicate {
        "greater_than" => value > threshold,
        "less_than" => value < threshold,
        // EQUALS — float-safe via tiny epsilon (the unknown-token fallthrough).
        _ => (value - threshold).abs() < SLA_EPS,
    }
}

/// Law C — the SLA decision: `(breached, edge)`.
///
/// `breached` per [`predicate_holds`]; `edge = breached != prev_breach` (the
/// shared two-state law — hysteresis: steady state emits nothing).
pub fn sla_decide(predicate: &str, value: f64, threshold: f64, prev_breach: bool) -> (bool, bool) {
    let breached = predicate_holds(predicate, value, threshold);
    (breached, two_state_edge(prev_breach, breached))
}
