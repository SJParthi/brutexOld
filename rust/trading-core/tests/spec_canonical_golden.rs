//! The CANONICAL-ENCODE golden: `StrategySpec::canonical_bytes` is FROZEN
//! byte-for-byte (DEC-SPEC-IDENTITY-WIRING-001, building on
//! DEC-STRATEGYSPEC-CORE-001's v1 layout).
//!
//! The 57-byte v1 encode is the COMMON spec-axis fingerprint BOTH repos feed
//! into run-identity (BruteX via the PyO3 `spec_canonical_bytes` binding →
//! `combo_hash`; TickVault natively). Any byte-layout drift silently orphans
//! every persisted run-identity, so the EXACT bytes of a fixture spec are
//! pinned here as a hex literal — a deliberate, reviewed failure on any change
//! (the fix is a `SPEC_SCHEMA_VERSION` bump + a new pinned literal, never an
//! in-place v1 edit). The SAME literal is pinned wheel-side in
//! `tests/golden/test_strategies_spec_golden.py`, proving wheel == native.

use trading_core::{Direction, ProximityParams, SPEC_SCHEMA_VERSION, StrategySpec};

/// The frozen fixture: mask `0x1f` (bits 0-4), Long, 50/10/25 bps, prior_n 3,
/// symbol_id 1 (BruteX's `NSE-NIFTY`).
fn fixture_spec() -> StrategySpec {
    StrategySpec::new(
        0x1f,
        Direction::Long,
        ProximityParams {
            pivot_proximity_bps: 50,
            pdh_pdl_proximity_bps: 10,
            fib_proximity_bps: 25,
            gap_mid_proximity_bps: 10,
            swing_proximity_bps: 10,
        },
        3,
        1,
    )
    .expect("fixture spec is valid")
}

/// The FROZEN v1 encode of [`fixture_spec`], segment by segment (all LE):
/// version `1` (u32) ‖ mask `0x1f` (u128) ‖ direction Long (u8 `0`) ‖
/// pivot 50 (i64) ‖ pdh_pdl 10 (i64) ‖ fib 25 (i64) ‖ prior_n 3 (u64) ‖
/// symbol_id 1 (u32) — 4+16+1+24+8+4 = 57 bytes = 114 hex chars.
const FIXTURE_CANONICAL_HEX: &str = concat!(
    "01000000",                         // SPEC_SCHEMA_VERSION = 1, u32 LE
    "1f000000000000000000000000000000", // condition_mask = 0x1f, u128 LE
    "00",                               // Direction::Long = 0, u8
    "3200000000000000",                 // pivot_proximity_bps = 50, i64 LE
    "0a00000000000000",                 // pdh_pdl_proximity_bps = 10, i64 LE
    "1900000000000000",                 // fib_proximity_bps = 25, i64 LE
    "0300000000000000",                 // prior_n = 3, u64 LE
    "01000000",                         // symbol_id = 1, u32 LE
);

fn to_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn fixture_encode_is_frozen_byte_for_byte() {
    assert_eq!(SPEC_SCHEMA_VERSION, 1, "v1 layout pinned by this golden");
    let bytes = fixture_spec().canonical_bytes();
    assert_eq!(bytes.len(), 57);
    assert_eq!(to_hex(&bytes), FIXTURE_CANONICAL_HEX);
}

#[test]
fn each_field_perturbation_moves_the_encode() {
    // Per-field distinctness AGAINST THE PINNED LITERAL (the #1027 unit test
    // proves pairwise distinctness in-memory; this pins it to the frozen hex,
    // localising WHICH byte region moved).
    let base = FIXTURE_CANONICAL_HEX.to_string();
    let prox = ProximityParams {
        pivot_proximity_bps: 50,
        pdh_pdl_proximity_bps: 10,
        fib_proximity_bps: 25,
        gap_mid_proximity_bps: 10,
        swing_proximity_bps: 10,
    };
    let variants = [
        StrategySpec::new(0x1e, Direction::Long, prox, 3, 1).unwrap(),
        StrategySpec::new(0x1f, Direction::Short, prox, 3, 1).unwrap(),
        StrategySpec::new(
            0x1f,
            Direction::Long,
            ProximityParams {
                pivot_proximity_bps: 51,
                ..prox
            },
            3,
            1,
        )
        .unwrap(),
        StrategySpec::new(
            0x1f,
            Direction::Long,
            ProximityParams {
                pdh_pdl_proximity_bps: 11,
                ..prox
            },
            3,
            1,
        )
        .unwrap(),
        StrategySpec::new(
            0x1f,
            Direction::Long,
            ProximityParams {
                fib_proximity_bps: 26,
                ..prox
            },
            3,
            1,
        )
        .unwrap(),
        StrategySpec::new(0x1f, Direction::Long, prox, 4, 1).unwrap(),
        StrategySpec::new(0x1f, Direction::Long, prox, 3, 2).unwrap(),
    ];
    for (i, variant) in variants.iter().enumerate() {
        let hex = to_hex(&variant.canonical_bytes());
        assert_eq!(hex.len(), 114, "variant {i} stays 57 bytes");
        assert_ne!(hex, base, "variant {i} must move the encode");
        assert_eq!(&hex[..8], "01000000", "variant {i} keeps the v1 prefix");
    }
}

#[test]
fn gap_mid_bps_is_deliberately_not_encoded() {
    // The FROZEN v1 law (DEC-GAP-MIDPOINT-CONDITIONS-001): `canonical_bytes`
    // encodes exactly THREE proximity bps — a differing `gap_mid_proximity_bps`
    // leaves the encode BYTE-IDENTICAL (every pre-gap run_id preserved). BruteX
    // carries a swept non-default value in run identity via the default-omit
    // `canonical_trade_params` key instead; growing this layout requires a
    // coordinator-brokered SPEC_SCHEMA_VERSION bump.
    let base = fixture_spec();
    let gap_variant = StrategySpec::new(
        0x1f,
        Direction::Long,
        ProximityParams {
            pivot_proximity_bps: 50,
            pdh_pdl_proximity_bps: 10,
            fib_proximity_bps: 25,
            gap_mid_proximity_bps: 25,
            swing_proximity_bps: 10,
        },
        3,
        1,
    )
    .unwrap();
    assert_eq!(to_hex(&gap_variant.canonical_bytes()), FIXTURE_CANONICAL_HEX);
    assert_eq!(gap_variant.canonical_bytes(), base.canonical_bytes());
}

#[test]
fn swing_proximity_bps_is_not_in_the_frozen_encode() {
    // DEC-FIB-TEMPLATE-EXPRESS-001: `ProximityParams` gained
    // `swing_proximity_bps`, but the spec axis carries NO swing knob — the
    // FROZEN 57-byte v1 canonical encode deliberately does NOT serialize the
    // field (a spec-axis swing sweep would need its own DEC + a
    // SPEC_SCHEMA_VERSION bump). Pinned: a non-default swing value leaves the
    // encode byte-identical to the frozen literal.
    let swung = StrategySpec::new(
        0x1f,
        Direction::Long,
        ProximityParams {
            pivot_proximity_bps: 50,
            pdh_pdl_proximity_bps: 10,
            fib_proximity_bps: 25,
            gap_mid_proximity_bps: 10,
            swing_proximity_bps: 25,
        },
        3,
        1,
    )
    .expect("valid spec");
    assert_eq!(to_hex(&swung.canonical_bytes()), FIXTURE_CANONICAL_HEX);
}
