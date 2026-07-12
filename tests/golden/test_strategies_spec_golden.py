"""Golden pins for :mod:`brutex.strategies.spec` — DEC-SPEC-IDENTITY-WIRING-001.

Three frozen contracts:

1. **The 57-byte v1 canonical encode** of the fixture spec, byte-for-byte —
   the SAME hex literal pinned natively in
   ``rust/trading-core/tests/spec_canonical_golden.rs``, asserted here
   through the ACTUAL wheel binding: the wheel == native byte-parity proof
   for the identity surface. Any drift silently orphans every persisted
   spec run-identity, so a change must be a deliberate
   ``SPEC_SCHEMA_VERSION`` bump, never an in-place v1 edit.
2. **The spec run-identity** for the fixture under pinned context strings —
   freezing the whole projection (canonical bytes → ``"spec:"`` hex
   strategy_id → 9-field BLAKE3 ``combo_hash``).
3. **The symbol-id assignment table**, BOTH directions — APPEND-ONLY
   forever: a renumber or an in-place label rename fails here by design.
"""

from __future__ import annotations

from brutex.strategies.fib_definitions import Direction
from brutex.strategies.spec import (
    SYMBOL_IDS,
    StrategySpec,
    spec_run_id,
    symbol_label_for,
)
from brutex.strategies.types import FibAnchorKind

#: The FROZEN v1 encode of the fixture spec — segment by segment (all LE):
#: version 1 (u32) ‖ mask 0x1f (u128) ‖ Long (u8 0) ‖ pivot 50 (i64) ‖
#: pdh_pdl 10 (i64) ‖ fib 25 (i64) ‖ prior_n 3 (u64) ‖ symbol_id 1 (u32).
#: MUST equal ``FIXTURE_CANONICAL_HEX`` in ``spec_canonical_golden.rs``.
FIXTURE_CANONICAL_HEX = (
    "01000000"
    "1f000000000000000000000000000000"
    "00"
    "3200000000000000"
    "0a00000000000000"
    "1900000000000000"
    "0300000000000000"
    "01000000"
)


def fixture_spec() -> StrategySpec:
    return StrategySpec(
        condition_mask=0x1F,
        direction=Direction.LONG,
        pivot_proximity_bps=50,
        pdh_pdl_proximity_bps=10,
        fib_proximity_bps=25,
        prior_n=3,
        symbol_id=1,
    )


def test_canonical_bytes_frozen_and_wheel_equals_native_literal() -> None:
    raw = fixture_spec().canonical_bytes()
    assert len(raw) == 57
    assert raw.hex() == FIXTURE_CANONICAL_HEX


def test_spec_run_id_frozen() -> None:
    rid = spec_run_id(
        fixture_spec(),
        tf="15m",
        mode="B",
        sl_paisa=None,
        tp_paisa=None,
        start_equity_paisa=10_000_000_00,
        fib_anchor=FibAnchorKind.PREV_DAY,
        data_snapshot_hash="a" * 64,
        indicator_version="ind-v1",
        code_commit_sha="b" * 40,
    )
    # Frozen 2026-07-02 on the real fno_math BLAKE3 kernel. A change here
    # means the identity projection moved — a reproducibility break unless
    # it rides a deliberate, ledgered schema bump (Golden Rule #4/#7).
    assert rid == "13214cd1727ae1f4f601b30c5313feb4dc6ccfb0aa6fb085507266695d5eab95"


def test_symbol_id_table_frozen_both_directions() -> None:
    # Row 4 appended by DEC-FUTURES-ADDRESSING-PIN-001 (the ADR-0012
    # NIFTY-futures SERIES label — the #1138 [C16] reservation discharged).
    assert dict(SYMBOL_IDS) == {
        "NSE-NIFTY": 1,
        "NSE-BANKNIFTY": 2,
        "BSE-SENSEX": 3,
        "NSE-NIFTY-FUT": 4,
    }
    assert symbol_label_for(1) == "NSE-NIFTY"
    assert symbol_label_for(2) == "NSE-BANKNIFTY"
    assert symbol_label_for(3) == "BSE-SENSEX"
    assert symbol_label_for(4) == "NSE-NIFTY-FUT"
