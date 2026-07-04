"""Memory-slope CI gate for the columnar survivor store (DEC-SWEEP-STREAMING-RANK-001).

Turns the honest-complexity claim — **O(kept) columnar retention at a bounded,
flat bytes-per-entry slope** — into a measured, deterministic invariant:

* the EXACT owned payload (:meth:`SurvivorColumns.owned_nbytes` — array
  payload + ``None``/gate bit-sets; no allocator noise, so this gate cannot
  flake) per kept entry stays ≤ a pinned ceiling at three sizes, and
* the per-entry figure is FLAT across a 64× size spread (the slope gate: a
  regression that starts retaining O(kept²) or per-entry object state fails
  loudly).

Ceilings derive from the documented arithmetic: 80 B/entry array payload +
2 packed bits without the overfit columns; 104 B + 4 bits with them. The pin
is deliberately a hair above (81 / 106) to absorb only the sub-byte bit-set
rounding at tiny sizes — NOT a tolerance for growth.

HONEST scope (Golden Rule #10): this measures the columns' logical payload,
not process RSS (``array`` growth slack + CPython object headers are excluded
— small constants); it is a memory gate, not a wall-clock gate, so it is
immune to shared-CI timing noise.
"""

from __future__ import annotations

import pytest
from brutex.engine.sweep_stream import SurvivorColumns

from tests.fakes.metrics_fixtures import make_result_metrics

_SIZES = (1_000, 8_000, 64_000)

#: Payload ceilings (bytes/entry): the documented arithmetic + bit-set rounding.
_CEILING_BASE = 81.0
_CEILING_OVERFIT = 106.0


def _bytes_per_entry(n: int, *, with_overfit: bool) -> float:
    columns = SurvivorColumns(with_overfit=with_overfit)
    metrics = make_result_metrics()
    for index in range(n):
        columns.append(index, metrics)
    return columns.owned_nbytes() / n


@pytest.mark.parametrize(
    ("with_overfit", "ceiling"),
    [(False, _CEILING_BASE), (True, _CEILING_OVERFIT)],
    ids=["base", "overfit"],
)
def test_owned_bytes_per_entry_is_bounded_and_flat(with_overfit: bool, ceiling: float) -> None:
    per_entry = [_bytes_per_entry(n, with_overfit=with_overfit) for n in _SIZES]
    for n, figure in zip(_SIZES, per_entry, strict=True):
        assert figure <= ceiling, f"{figure} B/entry at n={n} exceeds the {ceiling} pin"
    # FLAT slope: the largest and smallest per-entry figures agree to < 1%
    # (the only legitimate variation is the ≤ 1-byte bit-set rounding).
    assert max(per_entry) / min(per_entry) < 1.01


def test_owned_nbytes_is_exact_not_estimated() -> None:
    """The watermark figure is exact payload arithmetic — pinned at one size."""
    n = 1_000
    columns = SurvivorColumns(with_overfit=True)
    metrics = make_result_metrics()
    for index in range(n):
        columns.append(index, metrics)
    arrays = 13 * 8 * n  # 2 mask limbs + sharpe + 7 terms + trades + skew + kurt
    bit_sets = 4 * (n // 8)  # gate + sharpe/skew/kurt None bits (n divisible by 8)
    assert columns.owned_nbytes() == arrays + bit_sets
