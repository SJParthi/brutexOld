//! The rate-bridge-cache MEMO decision core — pure, clockless (Wave 3H).
//!
//! The TWO decision laws of `brutex/dashboard/rate_bridge_cache.py` the
//! port design names (§2.3 — "decision-kernel-only, thread core
//! PERMANENT-PYTHON"), transcribed token-for-token:
//!
//! * [`memo_serve_arm`] — the three-arm serve decision of
//!   `NonBlockingFileMemoBase._serve_bounded`: given the PRE-COMPUTED age
//!   (`None` = never probed; the caller reads its monotonic clock ONCE and
//!   clamps `max(0.0, now - probed_at)` — clocks NEVER cross the FFI), the
//!   TTL and the single-flight flag, name the arm the Python oracle takes.
//!   The lock, the deadline guard, the daemon and the `_inflight` clearing
//!   law all stay Python; the seam additionally FORCES the follower arm
//!   whenever `_inflight` is set (a poisoned kernel must never spawn a
//!   second refresh — the fail-safe belt lives Python-side).
//! * [`token_revalidates`] — the token-compare revalidation law of
//!   `RateBridgeCache._refresh_body`: an unchanged NON-`None` content token
//!   revalidates without re-reading; a changed / first / `None`
//!   ("unknown — if uncertain, fetch") token re-reads.  An erroring stat is
//!   NEVER trusted.
//!
//! Comparisons mirror the oracle's IEEE-754 semantics exactly: the TTL fast
//! path takes strict `age < ttl` guarded by `ttl > 0` — a NaN TTL fails
//! `> 0` (never caches), a NaN age fails `<` (never caches), `ttl == 0`
//! disables the fast path (every poll revalidates), and an age EXACTLY at
//! the TTL is over it (pinned by the frozen golden's boundary rows).

/// Arm 0 — the zero-syscall TTL fast path (serve the memo, labelled cached).
pub const MEMO_ARM_CACHED_HIT: u8 = 0;
/// Arm 1 — a refresh is in flight: serve last-known, labelled unverified
/// (the non-blocking single-flight — NEVER wait).
pub const MEMO_ARM_FOLLOWER: u8 = 1;
/// Arm 2 — this caller owns the guard-bounded refresh.
pub const MEMO_ARM_OWNER: u8 = 2;

/// The three-arm serve decision — pure + clockless.
///
/// `age_s` = `None` when no refresh ever completed, else the caller's
/// clamped `max(0.0, now - probed_at)`; `ttl_s` as constructed (any f64 —
/// the env resolvers refuse junk, but an explicitly-constructed cache may
/// carry `0`, negative, `inf` or NaN and the law is total over them);
/// `inflight` = the single-flight flag read under the caller's lock.
#[must_use]
pub fn memo_serve_arm(age_s: Option<f64>, ttl_s: f64, inflight: bool) -> u8 {
    if let Some(age) = age_s {
        // The oracle's compound guard, order preserved:
        // `age is not None and self._ttl_s > 0 and age < self._ttl_s`.
        if ttl_s > 0.0 && age < ttl_s {
            return MEMO_ARM_CACHED_HIT;
        }
    }
    if inflight {
        return MEMO_ARM_FOLLOWER;
    }
    MEMO_ARM_OWNER
}

/// The token revalidation law — `token is not None and token == prior`.
///
/// `true` = keep the memo, skip the read (the file is byte-unchanged:
/// atomic-replace publisher ⇒ same `mtime_ns:size:ino` = same content);
/// `false` = re-read (first probe, changed token, or an erroring stat whose
/// `None` is never trusted).  String equality is codepoint equality on both
/// arms (tokens are ASCII `mtime:size:ino` triples in production).
#[must_use]
pub fn token_revalidates(token: Option<&str>, prior: Option<&str>) -> bool {
    matches!((token, prior), (Some(t), Some(p)) if t == p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cold_age_never_caches() {
        assert_eq!(memo_serve_arm(None, 2.0, false), MEMO_ARM_OWNER);
        assert_eq!(memo_serve_arm(None, 2.0, true), MEMO_ARM_FOLLOWER);
        assert_eq!(memo_serve_arm(None, f64::INFINITY, false), MEMO_ARM_OWNER);
    }

    #[test]
    fn ttl_boundary_is_strict_less_than() {
        let eps = 2f64.powi(-10);
        assert_eq!(
            memo_serve_arm(Some(2.0 - eps), 2.0, false),
            MEMO_ARM_CACHED_HIT
        );
        assert_eq!(memo_serve_arm(Some(2.0), 2.0, false), MEMO_ARM_OWNER); // exactly at = over
        assert_eq!(memo_serve_arm(Some(2.0 + eps), 2.0, false), MEMO_ARM_OWNER);
    }

    #[test]
    fn ttl_zero_or_negative_disables_the_fast_path() {
        assert_eq!(memo_serve_arm(Some(0.0), 0.0, false), MEMO_ARM_OWNER);
        assert_eq!(memo_serve_arm(Some(0.0), -1.0, false), MEMO_ARM_OWNER);
        assert_eq!(memo_serve_arm(Some(0.0), 0.0, true), MEMO_ARM_FOLLOWER);
    }

    #[test]
    fn ttl_wins_over_inflight() {
        // The oracle checks the TTL fast path BEFORE the single-flight flag.
        assert_eq!(memo_serve_arm(Some(1.0), 2.0, true), MEMO_ARM_CACHED_HIT);
    }

    #[test]
    fn nan_arms_fall_through_ieee_style() {
        assert_eq!(memo_serve_arm(Some(1.0), f64::NAN, false), MEMO_ARM_OWNER); // NaN > 0 false
        assert_eq!(memo_serve_arm(Some(f64::NAN), 2.0, false), MEMO_ARM_OWNER); // NaN < ttl false
        assert_eq!(memo_serve_arm(Some(f64::NAN), 2.0, true), MEMO_ARM_FOLLOWER);
    }

    #[test]
    fn infinite_ttl_always_caches_a_probed_age() {
        assert_eq!(
            memo_serve_arm(Some(1.0e18), f64::INFINITY, false),
            MEMO_ARM_CACHED_HIT
        );
    }

    #[test]
    fn token_law_never_trusts_none() {
        assert!(!token_revalidates(None, None));
        assert!(!token_revalidates(None, Some("t1")));
        assert!(!token_revalidates(Some("t1"), None));
        assert!(token_revalidates(Some("t1"), Some("t1")));
        assert!(!token_revalidates(Some("t1"), Some("t2")));
        assert!(token_revalidates(Some(""), Some(""))); // equal empties revalidate
        assert!(!token_revalidates(Some(""), Some("t1")));
        assert!(token_revalidates(Some("μ:1"), Some("μ:1"))); // codepoint equality
    }
}
