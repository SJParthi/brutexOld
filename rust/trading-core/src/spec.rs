//! Strategy-as-DATA: a `StrategySpec` declaration plus a single
//! [`evaluate`] orchestrator that **wraps the frozen [`evaluate_atoms`] kernel**
//! (PR-4b sub-PR 1, DEC-STRATEGYSPEC-CORE-001 / ADR-0012).
//!
//! The thesis (backtest == live BY CONSTRUCTION): a strategy is a *declaration*
//! — a required-condition `u128` mask (the SAME vocabulary as [`crate::bits`]) +
//! a [`Direction`] + the proximity tolerances + the prior-bar look-back + an
//! opaque instrument token — and [`evaluate`] is a THIN, ADDITIVE orchestrator
//! that calls the frozen [`evaluate_atoms`] for the bar's bitmask and applies the
//! single superset test `(bar_bits & mask) == mask`. It NEVER touches the kernel's
//! arithmetic, so the frozen 400-case `vocab_golden_vectors.json` answer-key stays
//! byte-identical and the spec path is bit-consistent with the engine path.
//!
//! Dependency-free for TickVault: `serde` is runtime-OPTIONAL behind the `serde`
//! feature; the `condition_mask` serialises as a HEX STRING (a `u128` in JSON
//! would lose precision past 2⁵³), matching the `vocab_golden_vectors.json`
//! `expected_mask` hex convention.

use crate::bits::IMPLEMENTED_MASK;
use crate::evaluator::{self, IndicatorSnapshot, PriorBarHistory, ProximityParams};

/// The strategy-mask schema version. Bumped (with a recorded decision ID) on any
/// change to [`StrategySpec::canonical_bytes`]'s field layout, so a serialized
/// spec is never silently mis-decoded by a newer/older engine.
pub const SPEC_SCHEMA_VERSION: u32 = 1;

/// Trade direction. `Long`/`Short` ONLY — "HOLD" is the absence of a spec, never
/// a direction. `#[repr(u8)]` so the discriminant is the stable wire/FFI value
/// (`Long = 0`, `Short = 1`).
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    /// Enter long on a fire.
    Long = 0,
    /// Enter short on a fire.
    Short = 1,
}

impl Direction {
    /// The stable `u8` wire/FFI discriminant (`Long = 0`, `Short = 1`).
    #[must_use]
    pub const fn as_u8(self) -> u8 {
        self as u8
    }

    /// Map a `u8` wire/FFI discriminant back to a [`Direction`]; any value other
    /// than 0/1 is rejected (so a garbage FFI byte never silently becomes a
    /// direction).
    ///
    /// # Errors
    /// Returns `Err` with a precise message for any `value` ∉ {0, 1}.
    pub fn from_u8(value: u8) -> Result<Self, String> {
        match value {
            0 => Ok(Direction::Long),
            1 => Ok(Direction::Short),
            other => Err(format!(
                "direction must be 0 (Long) or 1 (Short), got {other}"
            )),
        }
    }
}

/// A backtest-or-live strategy declaration over the shared condition vocabulary.
///
/// Validated at construction ([`StrategySpec::new`]): `condition_mask` may only
/// require IMPLEMENTED bits (`condition_mask & !IMPLEMENTED_MASK == 0`), so a spec
/// can never demand a reserved/un-evaluated bit (which the kernel never sets, so
/// such a spec could never fire — a silent dead strategy).
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StrategySpec {
    /// Entry required-bits SUPERSET over the same `u128` condition vocabulary
    /// as [`crate::bits`] (66 declared positions; only IMPLEMENTED bits may be
    /// required). The spec fires on a bar iff EVERY set bit here is also
    /// set in the bar's `evaluate_atoms` mask. Serialized as a HEX STRING
    /// (`condition_mask` as a JSON number loses precision past bit 52).
    #[cfg_attr(feature = "serde", serde(with = "crate::spec::mask_hex"))]
    pub condition_mask: u128,

    /// Long/Short. HOLD is "no spec", never a direction.
    pub direction: Direction,

    /// The `near_*` proximity tolerances (bps). Embedded on the spec (not a side
    /// arg) so one declaration FULLY determines `bar_bits` — the same spec yields
    /// the same fire decision for a given bar in both repos.
    pub proximity: ProximityParams,

    /// Group-6 prior-bar look-back length. On the spec because it changes WHICH
    /// bits fire (the prior bullish/bearish flags themselves stay a runtime
    /// input via [`PriorBarHistory`]).
    pub prior_n: usize,

    /// OPAQUE, vocabulary-free instrument token. Does NOT affect [`evaluate`]'s
    /// arithmetic AT ALL (symbol is a CONTEXT label, never a condition input);
    /// it exists only so a serialized spec is self-describing and run-identity
    /// can namespace by it. The human-readable symbol string + universe
    /// validation live entirely in the Python/host layer — the crate holds no
    /// symbol table, so it stays repo-agnostic.
    pub symbol_id: u32,
}

impl StrategySpec {
    /// Construct a validated spec.
    ///
    /// # Errors
    /// Returns `Err` iff `condition_mask` requires a bit OUTSIDE
    /// [`IMPLEMENTED_MASK`] (a reserved/un-evaluated bit the kernel never sets) —
    /// a spec that could only ever be a silent dead strategy is refused up front
    /// rather than shipped.
    pub fn new(
        condition_mask: u128,
        direction: Direction,
        proximity: ProximityParams,
        prior_n: usize,
        symbol_id: u32,
    ) -> Result<Self, String> {
        let unimplemented = condition_mask & !IMPLEMENTED_MASK;
        if unimplemented != 0 {
            return Err(format!(
                "condition_mask requires unimplemented/reserved bits \
                 ({unimplemented:#034x}); only bits in IMPLEMENTED_MASK \
                 ({IMPLEMENTED_MASK:#034x}) may be required"
            ));
        }
        Ok(StrategySpec {
            condition_mask,
            direction,
            proximity,
            prior_n,
            symbol_id,
        })
    }

    /// Deterministic little-endian encode of
    /// `condition_mask ‖ direction ‖ proximity ‖ prior_n ‖ symbol_id` in a FIXED
    /// order, prefixed by [`SPEC_SCHEMA_VERSION`].
    ///
    /// The COMMON spec-axis fingerprint both repos feed into the run-identity
    /// bit-identically: the SAME spec yields the SAME bytes on every platform
    /// (all fixed-width LE, no padding, no host-endian leak), so two repos /
    /// two runs that mean the same strategy hash identically (Golden Rule #4/#7).
    /// `prior_n` is encoded as a fixed `u64` (not the platform-sized `usize`) so
    /// the bytes are identical on a 32-bit and a 64-bit target.
    #[must_use]
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(4 + 16 + 1 + 24 + 8 + 4);
        out.extend_from_slice(&SPEC_SCHEMA_VERSION.to_le_bytes());
        out.extend_from_slice(&self.condition_mask.to_le_bytes());
        out.push(self.direction.as_u8());
        out.extend_from_slice(&self.proximity.pivot_proximity_bps.to_le_bytes());
        out.extend_from_slice(&self.proximity.pdh_pdl_proximity_bps.to_le_bytes());
        out.extend_from_slice(&self.proximity.fib_proximity_bps.to_le_bytes());
        out.extend_from_slice(&(self.prior_n as u64).to_le_bytes());
        out.extend_from_slice(&self.symbol_id.to_le_bytes());
        out
    }
}

/// The per-bar verdict of [`evaluate`]: the bar's full condition bitmask, whether
/// the spec FIRED (its required bits are a subset of the bar's), and the
/// direction to act on if it did.
///
/// A NEW minimal struct — NOT `brutex.strategies.types.Signal` (which carries
/// `symbol`/`strength`/`reason` and is strategy-engine-level); keeping it distinct
/// preserves the dependency-free crate. It carries NO entry/exit price levels (the
/// candle lake stores neither IV nor levels — fabricating them would violate
/// Golden Rule #1; pricing is sourced downstream in the options layer).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Signal {
    /// The bar's full `evaluate_atoms` condition bitmask (the bar reproduces its
    /// own mask — bit-identical to the frozen 400-case golden).
    pub bar_bits: u128,
    /// `true` iff EVERY bit required by the spec is set in `bar_bits`.
    pub fires: bool,
    /// The spec's direction (echoed so a downstream consumer needs only the
    /// `Signal`).
    pub direction: Direction,
}

/// Evaluate a [`StrategySpec`] against one bar.
///
/// A THIN orchestrator: it asks the FROZEN [`evaluate_atoms`] kernel for the bar's
/// bitmask, then applies the single superset test `(bar_bits & mask) == mask`. It
/// NEVER touches the kernel's arithmetic, so the frozen 400-case `expected_mask`
/// golden stays byte-identical and the spec path is bit-consistent with the engine
/// path (`combo_mask.evaluate_mask`'s `(bar & mask) == mask`). O(1) per bar (a
/// `u128` AND is two `u64` ANDs — NOT an O(1)-total claim).
#[must_use]
pub fn evaluate(
    spec: &StrategySpec,
    snapshot: &IndicatorSnapshot,
    prior: &PriorBarHistory,
) -> Signal {
    let bar_bits = evaluator::evaluate_atoms(snapshot, &spec.proximity, prior);
    Signal {
        bar_bits,
        fires: (bar_bits & spec.condition_mask) == spec.condition_mask,
        direction: spec.direction,
    }
}

/// serde adapter: serialise a `u128` mask as a lowercase 32-nibble hex STRING
/// (a JSON number would lose precision past 2⁵³) and parse it back. Matches the
/// `vocab_golden_vectors.json` `expected_mask` convention, so a spec fixture and
/// the atom fixture share ONE hex encoding (Golden Rule #11 COMMON).
#[cfg(feature = "serde")]
pub mod mask_hex {
    use serde::de::{Deserialize, Deserializer, Error};
    use serde::ser::Serializer;

    /// Serialize a `u128` as a zero-padded 32-nibble lowercase hex string.
    ///
    /// # Errors
    /// Propagates the serializer's own error (infallible for a `str`).
    pub fn serialize<S>(value: &u128, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&format!("{value:032x}"))
    }

    /// Parse a hex string back into a `u128`.
    ///
    /// # Errors
    /// Fails iff the string is not valid base-16 (or overflows `u128`).
    pub fn deserialize<'de, D>(deserializer: D) -> Result<u128, D::Error>
    where
        D: Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;
        u128::from_str_radix(&s, 16)
            .map_err(|e| D::Error::custom(format!("invalid hex mask {s:?}: {e}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::indicators::fib::FibLevels;
    use crate::indicators::market_structure::MarketStructureSignal;
    use crate::indicators::pivots::PivotLevels;

    fn proximity() -> ProximityParams {
        ProximityParams {
            pivot_proximity_bps: 50,
            pdh_pdl_proximity_bps: 10,
            fib_proximity_bps: 25,
            swing_proximity_bps: 10,
        }
    }

    /// A minimal in-range snapshot. EMA bits are warm (Some) so a mask requiring
    /// them is reachable; the exact bits are exercised by the golden test.
    fn snapshot(close: i64) -> IndicatorSnapshot {
        IndicatorSnapshot {
            tod_seconds: 10 * 3600, // mid-morning
            open_paisa: 1_000_000,
            high_paisa: 1_010_000,
            low_paisa: 990_000,
            close_paisa: close,
            ema20_paisa: Some(995_000),
            ema200_paisa: Some(990_000),
            pivots: PivotLevels {
                p_paisa: 1_000_000,
                r1_paisa: 1_005_000,
                r2_paisa: 1_010_000,
                r3_paisa: 1_015_000,
                s1_paisa: 995_000,
                s2_paisa: 990_000,
                s3_paisa: 985_000,
                pdh_paisa: 1_010_000,
                pdl_paisa: 990_000,
                pdc_paisa: 1_000_000,
                r4_paisa: 0,
                s4_paisa: 0,
                r5_paisa: 1_025_000,
                s5_paisa: 975_000,
            },
            fib: FibLevels {
                retrace_level_at_pdh_paisa: 1_010_000,
                retrace_23_paisa: 1_005_280,
                retrace_38_paisa: 1_002_360,
                retrace_50_paisa: 1_000_000,
                retrace_62_paisa: 997_640,
                retrace_78_paisa: 994_280,
                retrace_level_at_pdl_paisa: 990_000,
                // Bullish retracement ladder — unread by the evaluator
                // (DEC-FIB-BULLISH-RETRACE-001), zeroed like the extensions.
                retrace_bull_23_paisa: 0,
                retrace_bull_38_paisa: 0,
                retrace_bull_62_paisa: 0,
                retrace_bull_78_paisa: 0,
                ext_up_127_paisa: 0,
                ext_up_162_paisa: 0,
                ext_up_200_paisa: 0,
                ext_up_262_paisa: 0,
                ext_down_127_paisa: 0,
                ext_down_162_paisa: 0,
                ext_down_200_paisa: 0,
                ext_down_262_paisa: 0,
                pdh_paisa: 0,
                pdl_paisa: 0,
                range_paisa: 0,
                ext_up_424_paisa: 0,
                ext_down_424_paisa: 0,
            },
            day_high_paisa: 1_010_000,
            day_low_paisa: 990_000,
            open_of_day_paisa: 1_000_000,
            market_structure: MarketStructureSignal::default(),
            last_swing_high_paisa: None,
            last_swing_low_paisa: None,
        }
    }

    fn prior() -> PriorBarHistory {
        PriorBarHistory {
            prior_n: 0,
            flags: vec![],
        }
    }

    // ── Direction mapping ──────────────────────────────────────────────────
    #[test]
    fn direction_u8_round_trips() {
        assert_eq!(Direction::Long.as_u8(), 0);
        assert_eq!(Direction::Short.as_u8(), 1);
        assert_eq!(Direction::from_u8(0).unwrap(), Direction::Long);
        assert_eq!(Direction::from_u8(1).unwrap(), Direction::Short);
    }

    #[test]
    fn direction_from_u8_rejects_garbage() {
        let err = Direction::from_u8(2).unwrap_err();
        assert!(err.contains("must be 0 (Long) or 1 (Short)"), "got: {err}");
        assert!(Direction::from_u8(255).is_err());
    }

    // ── Construction validation ────────────────────────────────────────────
    #[test]
    fn new_accepts_a_mask_of_implemented_bits() {
        let spec = StrategySpec::new(IMPLEMENTED_MASK, Direction::Long, proximity(), 3, 7)
            .expect("an all-implemented mask is valid");
        assert_eq!(spec.condition_mask, IMPLEMENTED_MASK);
        assert_eq!(spec.direction, Direction::Long);
        assert_eq!(spec.prior_n, 3);
        assert_eq!(spec.symbol_id, 7);
    }

    #[test]
    fn new_accepts_an_empty_mask() {
        // A zero mask is a vacuous "always fires" spec — legal (every bit it
        // requires, namely none, is trivially present).
        let spec =
            StrategySpec::new(0, Direction::Short, proximity(), 0, 0).expect("zero is valid");
        assert_eq!(spec.condition_mask, 0);
    }

    #[test]
    fn new_rejects_a_reserved_bit() {
        // Bit 74 is the first undeclared/reserved bit (TOTAL_CONDITIONS == 74,
        // DEC-FIB-TEMPLATE-EXPRESS-001).
        let reserved = 1u128 << 74;
        let err = StrategySpec::new(reserved, Direction::Long, proximity(), 1, 0).unwrap_err();
        assert!(err.contains("unimplemented/reserved"), "got: {err}");
        // Bit 66 is a declared RESERVED placeholder (the gap-midpoint
        // re-stack fillers, bits 66-68) — equally refused.
        let placeholder = 1u128 << 66;
        assert!(StrategySpec::new(placeholder, Direction::Long, proximity(), 1, 0).is_err());
        // Bit 64 (Supertrend) is DECLARED but layout-only — equally refused.
        let declared_only = 1u128 << 64;
        assert!(StrategySpec::new(declared_only, Direction::Long, proximity(), 1, 0).is_err());
        // Bit 52 (VWAP) is TOMBSTONED — refused too (the wipe).
        let tombstone = 1u128 << 52;
        assert!(StrategySpec::new(tombstone, Direction::Long, proximity(), 1, 0).is_err());
    }

    #[test]
    fn new_rejects_a_mask_mixing_implemented_and_reserved() {
        let mixed = IMPLEMENTED_MASK | (1u128 << 100);
        assert!(StrategySpec::new(mixed, Direction::Long, proximity(), 1, 0).is_err());
    }

    // ── canonical_bytes determinism ────────────────────────────────────────
    #[test]
    fn canonical_bytes_is_deterministic() {
        let spec = StrategySpec::new(0xDEAD_BEEF, Direction::Short, proximity(), 4, 42).unwrap();
        assert_eq!(spec.canonical_bytes(), spec.canonical_bytes());
        // The version prefix leads.
        assert_eq!(
            &spec.canonical_bytes()[..4],
            &SPEC_SCHEMA_VERSION.to_le_bytes()
        );
    }

    #[test]
    fn canonical_bytes_distinguishes_every_field() {
        let base = StrategySpec::new(0xAA, Direction::Long, proximity(), 2, 1).unwrap();
        let by_mask = StrategySpec::new(0xAB, Direction::Long, proximity(), 2, 1).unwrap();
        let by_dir = StrategySpec::new(0xAA, Direction::Short, proximity(), 2, 1).unwrap();
        let by_prior = StrategySpec::new(0xAA, Direction::Long, proximity(), 3, 1).unwrap();
        let by_symbol = StrategySpec::new(0xAA, Direction::Long, proximity(), 2, 2).unwrap();
        let by_prox = StrategySpec::new(
            0xAA,
            Direction::Long,
            ProximityParams {
                pivot_proximity_bps: 51,
                pdh_pdl_proximity_bps: 10,
                fib_proximity_bps: 25,
                swing_proximity_bps: 10,
            },
            2,
            1,
        )
        .unwrap();
        let b = base.canonical_bytes();
        assert_ne!(b, by_mask.canonical_bytes());
        assert_ne!(b, by_dir.canonical_bytes());
        assert_ne!(b, by_prior.canonical_bytes());
        assert_ne!(b, by_symbol.canonical_bytes());
        assert_ne!(b, by_prox.canonical_bytes());
    }

    /// Bit 63 (`narrow_cpr_day`) is the u64 SIGN bit — cheap insurance that a
    /// mask/bar_bits word with it set survives the canonical-bytes encode
    /// unchanged (everything here is u128/unsigned; a signed-i64 round-trip
    /// anywhere in the encode would corrupt exactly this bit).
    #[test]
    fn bit_63_mask_round_trips_canonical_bytes_unchanged() {
        let mask = (1u128 << 63) | 0b1010;
        assert_eq!(mask & IMPLEMENTED_MASK, mask, "bit 63 must be implemented");
        let spec = StrategySpec::new(mask, Direction::Long, proximity(), 2, 9).unwrap();
        let bytes = spec.canonical_bytes();
        // The mask occupies bytes 4..20 little-endian — recover it verbatim.
        let mut le = [0u8; 16];
        le.copy_from_slice(&bytes[4..20]);
        assert_eq!(u128::from_le_bytes(le), mask);
        // And the encode is stable across calls (no sign-dependent drift).
        assert_eq!(spec.canonical_bytes(), bytes);
    }

    // ── evaluate superset semantics (thin-wrapper proof) ───────────────────
    #[test]
    fn evaluate_fires_when_required_bits_are_a_subset() {
        let snap = snapshot(1_000_000);
        let bar = evaluator::evaluate_atoms(&snap, &proximity(), &prior());
        // The bar's own mask is trivially a subset of itself → fires.
        let spec = StrategySpec::new(bar, Direction::Long, proximity(), 0, 0).unwrap();
        let sig = evaluate(&spec, &snap, &prior());
        assert_eq!(sig.bar_bits, bar, "the wrapper must echo evaluate_atoms");
        assert!(sig.fires);
        assert_eq!(sig.direction, Direction::Long);
    }

    #[test]
    fn evaluate_does_not_fire_when_a_required_bit_is_absent() {
        let snap = snapshot(1_000_000);
        let bar = evaluator::evaluate_atoms(&snap, &proximity(), &prior());
        // Pick an IMPLEMENTED bit the bar does NOT have and require it.
        let absent = IMPLEMENTED_MASK & !bar;
        assert_ne!(absent, 0, "the test fixture must leave some bit clear");
        let want = absent & absent.wrapping_neg(); // lowest absent bit
        let spec = StrategySpec::new(want, Direction::Short, proximity(), 0, 0).unwrap();
        let sig = evaluate(&spec, &snap, &prior());
        assert_eq!(sig.bar_bits, bar);
        assert!(!sig.fires);
        assert_eq!(sig.direction, Direction::Short);
    }

    #[test]
    fn evaluate_empty_mask_always_fires() {
        let snap = snapshot(1_000_000);
        let spec = StrategySpec::new(0, Direction::Long, proximity(), 0, 0).unwrap();
        assert!(evaluate(&spec, &snap, &prior()).fires);
    }

    // ── serde round-trip (hex mask) ────────────────────────────────────────
    #[cfg(feature = "serde")]
    #[test]
    fn serde_round_trips_via_hex_mask() {
        // (Bit 53 in the original mask moved to bit 54 when 52-53 were
        // TOMBSTONED — a spec may only require IMPLEMENTED bits.)
        let spec = StrategySpec::new(
            0x0000_0000_0000_0000_0642_1014_8000_a01a,
            Direction::Short,
            proximity(),
            3,
            99,
        )
        .unwrap();
        let json = serde_json::to_string(&spec).expect("serialize");
        // The mask is a HEX STRING, never a JSON number.
        assert!(
            json.contains("\"0000000000000000064210148000a01a\""),
            "mask must serialise as zero-padded hex string, got: {json}"
        );
        let back: StrategySpec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(back, spec);
        assert_eq!(back.canonical_bytes(), spec.canonical_bytes());
    }

    #[cfg(feature = "serde")]
    #[test]
    fn serde_bit_63_mask_hex_round_trips_unchanged() {
        // The u64-sign-bit insurance, serde half: a spec whose mask carries
        // bit 63 serialises through the `mask_hex` STRING adapter and parses
        // back bit-identically (no signedness/precision loss).
        let mask = (1u128 << 63) | (1u128 << 60) | 1;
        let spec = StrategySpec::new(mask, Direction::Short, proximity(), 1, 4).unwrap();
        let json = serde_json::to_string(&spec).expect("serialize");
        assert!(
            json.contains("\"00000000000000009000000000000001\""),
            "hex string must carry bit 63 verbatim, got: {json}"
        );
        let back: StrategySpec = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(back.condition_mask, mask);
        assert_eq!(back.canonical_bytes(), spec.canonical_bytes());
    }

    #[cfg(feature = "serde")]
    #[test]
    fn serde_rejects_a_non_hex_mask() {
        let bad = r#"{"condition_mask":"zzzz","direction":"Long","proximity":{"pivot_proximity_bps":1,"pdh_pdl_proximity_bps":1,"fib_proximity_bps":1},"prior_n":0,"symbol_id":0}"#;
        let err = serde_json::from_str::<StrategySpec>(bad).unwrap_err();
        assert!(err.to_string().contains("invalid hex mask"), "got: {err}");
    }
}
