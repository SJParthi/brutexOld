#![no_main]
//! Fuzz target for the strategy-as-DATA surface (`src/spec.rs`,
//! DEC-STRATEGYSPEC-CORE-001) — the ONLY trading-core boundary that consumes
//! genuinely UNTRUSTED external bytes/strings:
//!
//! * `serde_json` deserialization of a [`StrategySpec`] (behind the crate's
//!   `serde` feature), including the `mask_hex` u128 hex-string parser;
//! * [`Direction::from_u8`] — the FFI/wire byte decoder;
//! * [`StrategySpec::new`] — the IMPLEMENTED_MASK admission validator.
//!
//! Asserted contracts:
//!
//! 1. **No panic** parsing ARBITRARY bytes as a JSON `StrategySpec`, and no
//!    panic parsing an arbitrary string via the `mask_hex` path.
//! 2. **Round-trip** — any successfully parsed spec re-serializes and
//!    re-parses to an equal value with identical `canonical_bytes`.
//! 3. **`canonical_bytes` determinism + fixed layout** — two calls agree, the
//!    length is the documented 57 bytes (4+16+1+24+8+4), and the
//!    `SPEC_SCHEMA_VERSION` prefix leads.
//! 4. **`Direction::from_u8` totality** — `Ok` iff the byte is 0/1, and the
//!    round-trip `as_u8` is the identity.
//! 5. **`StrategySpec::new` admission** — accepts a mask iff it requires no
//!    bit outside `IMPLEMENTED_MASK` (a reserved-bit spec is refused, never a
//!    silent dead strategy).

use libfuzzer_sys::fuzz_target;
use trading_core::bits::IMPLEMENTED_MASK;
use trading_core::{Direction, ProximityParams, SPEC_SCHEMA_VERSION, StrategySpec};

/// The documented `canonical_bytes` layout: version u32 ‖ mask u128 ‖
/// direction u8 ‖ 3×bps i64 ‖ prior_n u64 ‖ symbol_id u32.
const CANONICAL_LEN: usize = 4 + 16 + 1 + 24 + 8 + 4;

fn read_u64(data: &[u8], off: usize) -> u64 {
    let mut b = [0u8; 8];
    for (k, slot) in b.iter_mut().enumerate() {
        *slot = data.get(off + k).copied().unwrap_or(0);
    }
    u64::from_le_bytes(b)
}

fuzz_target!(|data: &[u8]| {
    // ── 1+2. arbitrary bytes → JSON parse → round-trip ──
    if let Ok(spec) = serde_json::from_slice::<StrategySpec>(data) {
        let json = serde_json::to_string(&spec).expect("a parsed spec must re-serialize");
        let back: StrategySpec =
            serde_json::from_str(&json).expect("a re-serialized spec must re-parse");
        assert_eq!(back, spec, "serde round-trip must be lossless");
        assert_eq!(
            back.canonical_bytes(),
            spec.canonical_bytes(),
            "round-trip must preserve the canonical fingerprint"
        );
    }

    // ── 1. the mask_hex string parser on an ARBITRARY string ──
    // Embed the (lossy-decoded) fuzz string as the condition_mask value via a
    // JSON Value, so escaping is handled and ONLY the hex parser is stressed.
    let hex_candidate = String::from_utf8_lossy(data);
    let value = serde_json::json!({
        "condition_mask": hex_candidate,
        "direction": "Long",
        "proximity": {
            "pivot_proximity_bps": 50,
            "pdh_pdl_proximity_bps": 10,
            "fib_proximity_bps": 25,
        },
        "prior_n": 0,
        "symbol_id": 0,
    });
    let parsed = serde_json::from_value::<StrategySpec>(value); // must not panic
    if let Ok(spec) = parsed {
        // A parse can only succeed when the string was valid base-16 in u128
        // (mask_hex delegates to from_str_radix — they must agree exactly).
        assert_eq!(
            spec.condition_mask,
            u128::from_str_radix(&hex_candidate, 16)
                .expect("mask_hex accepted a string from_str_radix rejects"),
            "mask_hex must agree with u128::from_str_radix"
        );
    }

    // ── 4. Direction::from_u8 totality over every byte ──
    let dir_byte = data.first().copied().unwrap_or(0);
    match Direction::from_u8(dir_byte) {
        Ok(d) => {
            assert!(dir_byte <= 1, "from_u8 accepted a byte outside {{0, 1}}");
            assert_eq!(d.as_u8(), dir_byte, "as_u8 must round-trip");
        }
        Err(msg) => {
            assert!(dir_byte > 1, "from_u8 rejected a valid byte {dir_byte}");
            assert!(!msg.is_empty(), "the rejection must carry a message");
        }
    }

    // ── 5. StrategySpec::new admission + 3. canonical_bytes layout ──
    let mask = (u128::from(read_u64(data, 1)) << 64) | u128::from(read_u64(data, 9));
    let proximity = ProximityParams {
        pivot_proximity_bps: (read_u64(data, 17) % 10_001) as i64,
        pdh_pdl_proximity_bps: (read_u64(data, 25) % 10_001) as i64,
        fib_proximity_bps: (read_u64(data, 33) % 10_001) as i64,
    };
    let prior_n = (read_u64(data, 41) % 9) as usize;
    let symbol_id = read_u64(data, 49) as u32;
    let direction = if dir_byte & 1 == 0 {
        Direction::Long
    } else {
        Direction::Short
    };
    match StrategySpec::new(mask, direction, proximity, prior_n, symbol_id) {
        Ok(spec) => {
            assert_eq!(
                mask & !IMPLEMENTED_MASK,
                0,
                "new() accepted a mask with reserved bits"
            );
            let bytes = spec.canonical_bytes();
            assert_eq!(bytes.len(), CANONICAL_LEN, "canonical layout drifted");
            assert_eq!(
                &bytes[..4],
                &SPEC_SCHEMA_VERSION.to_le_bytes(),
                "the schema-version prefix must lead"
            );
            assert_eq!(bytes, spec.canonical_bytes(), "canonical_bytes must be deterministic");
        }
        Err(msg) => {
            assert_ne!(
                mask & !IMPLEMENTED_MASK,
                0,
                "new() rejected an all-implemented mask: {msg}"
            );
        }
    }
});
