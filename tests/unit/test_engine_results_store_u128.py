"""u128 two-column mask persistence for the results store (DEC-VOCAB-U128-CODEGEN-001).

PR-1 widened the strategy mask u64 → u128. A u128 does not fit one signed Parquet
``Int64``, so the mask is persisted as TWO ``Int64`` limb columns
(``mask_bits_lo`` / ``mask_bits_hi``) via an unsigned↔signed offset and reassembled
on read. These tests pin the limb encode/decode bijection, a round-trip of a mask
with bits ``>= 64`` set (the headroom the old single-column ``Int64`` could never
hold), the ``MASK_BITS_MAX`` boundary, and the unreadable-store guard.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import (
    MASK_BITS_MAX,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    _decode_mask,
    _encode_limb,
)
from brutex.vocabulary.bits import TOTAL_CONDITIONS


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(mask_bits: int, *, run_id: str) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=1_000,
        gross_profit_paisa=1_500,
        gross_loss_paisa=500,
        max_drawdown_paisa=250,
        expectancy_paisa=25,
        total_trades=40,
        winning_trades=25,
        losing_trades=15,
        sharpe_ratio=1.5,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=0.625,
        max_drawdown_pct=12.5,
        avg_entry_price=Decimal("14020.50"),
        avg_exit_price=Decimal("14100.25"),
    )


@pytest.mark.parametrize(
    "mask",
    [
        0,
        0b101,
        (1 << 51),  # top legacy bit
        (1 << 52),  # first new (layout-only) bit
        (1 << 59),  # last defined bit (TOTAL_CONDITIONS-1)
        MASK_BITS_MAX,  # every defined bit set
    ],
)
def test_limb_encode_decode_is_a_bijection(mask: int) -> None:
    """``_decode_mask(_encode_limb(lo), _encode_limb(hi)) == mask`` for every mask."""
    lo = _encode_limb(mask, hi=False)
    hi = _encode_limb(mask, hi=True)
    # Each limb fits a signed int64.
    assert -(2**63) <= lo <= 2**63 - 1
    assert -(2**63) <= hi <= 2**63 - 1
    assert _decode_mask(lo, hi) == mask


def test_mask_above_64_bits_round_trips_through_parquet(tmp_path: Path) -> None:
    """A mask with a bit >= 52 (and the full u128 domain) round-trips bit-for-bit.

    The single-column signed ``Int64`` of the pre-u128 store could hold at most
    bits 0..62; the two-column limb split holds the whole defined vocabulary
    (bits 0..TOTAL_CONDITIONS-1), proving the >=53-bit headroom survives the lake.
    """
    pytest.importorskip("polars")
    high_mask = (1 << 59) | (1 << 52) | (1 << 51) | 0b101  # spans below + above bit 52
    assert high_mask <= MASK_BITS_MAX
    store = ResultsStore(path=tmp_path / "r.parquet")
    store.append(_row(high_mask, run_id=_rid(1)))
    store.append(_row(0, run_id=_rid(2)))  # the empty mask, both limbs at -2**63
    store.flush()

    reloaded = ResultsStore.load(tmp_path / "r.parquet")
    by_id = {row.run_id: row for row in reloaded.rows()}
    assert by_id[_rid(1)].mask_bits == high_mask
    assert by_id[_rid(2)].mask_bits == 0


def test_mask_bits_max_is_the_full_vocabulary() -> None:
    """``MASK_BITS_MAX`` is every defined condition bit set (grow-only count)."""
    assert MASK_BITS_MAX == (1 << TOTAL_CONDITIONS) - 1


def test_mask_above_max_is_rejected() -> None:
    """A bit >= TOTAL_CONDITIONS (undefined / reserved) is rejected at construction."""
    with pytest.raises(ResultsStoreError, match="out of range"):
        _row(1 << TOTAL_CONDITIONS, run_id=_rid(3))


def test_unreadable_store_raises(tmp_path: Path) -> None:
    """A corrupt / non-Parquet file raises a clean ``ResultsStoreError`` on load."""
    pytest.importorskip("polars")
    bad = tmp_path / "garbage.parquet"
    bad.write_bytes(b"not a parquet file at all")
    with pytest.raises(ResultsStoreError, match="unreadable results store"):
        ResultsStore.load(bad)
