"""O(1) benchmarks for the u128 TWO-LIMB numpy ORACLE path (`combo_mask`).

DEC-O1-CLAIMS-HONESTY-001 bench pin. ``test_engine_combo_mask_o1.py`` pins the
u64 scalar path only (its dense bar is 52-bit); since DEC-VOCAB-U128-CODEGEN-001
the vocabulary mask carries u128 headroom. Since DEC-ENUM-KERNEL-U128-001 a
mask OR bar with a bit >= 64 routes the production ``count_supermasks`` to the
u128 RUST kernel (pinned by ``test_engine_combo_mask_wide_kernel_o1.py``);
the bit-exact two-limb numpy reference benched HERE
(``count_supermasks_reference`` → ``_split_limbs``/``_count_two_limb`` — two
u64 ANDs + two compares per bar) is retained as the parity ORACLE, and its
per-bar cost stays pinned so an oracle regression cannot silently rot the
parity suites. These benches pin the >=64-bit oracle path:

* ``evaluate_mask`` with u128 operands — per-eval latency flat vs how many
  evals run (a constant AND/CMP on a bounded-width (<=128-bit) Python int);
* ``count_supermasks_reference`` — per-BAR amortised cost flat at 10k vs 1k
  bars (the per-call cost is O(bars) by design: one limb-split + one
  vectorised pass; the O(1) claim is per bar).

Both ratios route through :func:`tests.bench._o1_tiers.cache_tier_ceiling`
(DEC-O1-CACHE-TIER-001 measured 3× allowance) exactly like the u64 sibling:
the op is constant, but the 10k working set crosses a CPU cache tier — a
cache-cliff, not an algorithmic regression; a genuine O(n)-per-op wrapper
still blows well past 3×. Shared CI is byte-unchanged (5× default). Operand
WIDTHS are identical across both bases so the ratio times the workload size,
never a limb-count difference.

NON-GOAL (stated per Golden Rule #10): ``ResidentBarBuffer``'s wide branch is
deliberately NOT benched here — since DEC-ENUM-KERNEL-U128-001 it serves the
resident u128 Rust kernel, pinned in its own file
(``test_engine_combo_mask_wide_kernel_o1.py``).
"""

from __future__ import annotations

import time
from typing import Final

from brutex.engine.combo_mask import (
    U64_MASK,
    count_supermasks,
    count_supermasks_reference,
    evaluate_mask,
)

from tests.bench._o1_tiers import cache_tier_ceiling

_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 10_000

#: Dense 120-bit truth vector — bits set in BOTH u64 limbs (bit >= 64 forces
#: the two-limb route; MAX_VOCAB_BITS is 128).
_BAR_U128: Final[int] = (1 << 120) - 1
#: A u128 sub-mask with required bits in BOTH limbs (> U64_MASK by construction).
#: High limb occupies bits 64-79; low limb occupies bits 0-11.
_MASK_U128: Final[int] = (0b1010_1100_1111_0001 << 64) | 0b1111_0000_1111


def _u128_masks(n: int) -> list[int]:
    """*n* GENUINELY DISTINCT satisfiable u128 sub-masks of the dense bar.

    ``i << 16`` occupies bits 16..29 for n <= 10k — clean of the fixed mask's
    low-limb bits (0-11) and below bit 64 — so every value is distinct by
    construction, every mask keeps ``_MASK_U128``'s high-limb (>=64) bits set
    (two-limb routing guaranteed), and every mask is a sub-mask of the dense
    120-bit bar (always satisfiable).
    """
    return [_MASK_U128 | (i << 16) for i in range(n)]


class TestU128Routing:
    """Correctness/teeth smokes — the fixtures genuinely exercise the u128 path."""

    def test_operands_exceed_u64(self) -> None:
        """Every benched operand routes past the u64 kernel (bit >= 64 present).

        Shrinking either constant below bit 64 would silently re-route to the
        u64 kernel and hollow out the bench — this fails loudly instead. The
        `len(set(...))` pin proves the generator's distinctness claim.
        """
        assert _BAR_U128 > U64_MASK
        assert _MASK_U128 > U64_MASK
        assert all(m > U64_MASK for m in _u128_masks(64))
        assert len(set(_u128_masks(_LARGE))) == _LARGE

    def test_evaluate_mask_two_limb_truth(self) -> None:
        """u128 AND-semantics: sub-mask satisfies; an extra hi-limb bit fails."""
        assert evaluate_mask(_BAR_U128, _MASK_U128) is True
        # Bit 121 sits OUTSIDE the dense 120-bit bar → not a superset.
        assert evaluate_mask(_BAR_U128, _MASK_U128 | (1 << 121)) is False

    def test_public_count_routes_to_two_limb_bit_exact(self) -> None:
        """Public ``count_supermasks`` with a bit >= 64 == the oracle, bit-exact.

        (Since DEC-ENUM-KERNEL-U128-001 the public route serves the u128 Rust
        kernel; the equality against the two-limb oracle is the parity law.)

        Mixed bars: a satisfying superset + the exactly-satisfying mask itself
        + a zero bar → a non-trivial count of 2 on BOTH the public route and
        the two-limb reference.
        """
        bars = [_BAR_U128, _MASK_U128, 0]
        assert (
            count_supermasks(bars, [_MASK_U128])
            == count_supermasks_reference(bars, [_MASK_U128])
            == [2]
        )


class TestEvaluateMaskU128IsO1:
    def test_evaluate_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        result = benchmark(lambda: evaluate_mask(_BAR_U128, _MASK_U128))
        assert result is True

    def test_evaluate_u128_ratio_is_o1(self) -> None:
        """Per-eval u128 latency is flat at 10k evals vs 1k evals (O(1))."""

        def _time(n: int) -> float:
            masks = _u128_masks(n)
            for m in masks[:50]:  # warm
                evaluate_mask(_BAR_U128, m)
            t0 = time.perf_counter_ns()
            for m in masks:
                evaluate_mask(_BAR_U128, m)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1e-9)
        assert ratio < cache_tier_ceiling(5.0), (
            f"evaluate_mask u128 per-eval ratio {ratio:.2f}× (10k vs 1k masks) "
            f"exceeds the ceiling — the u128 two-limb AND/CMP may have regressed "
            f"from O(1). t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )


class TestTwoLimbCountIsO1:
    def test_count_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        bars = [_BAR_U128] * _SMALL
        result = benchmark(lambda: count_supermasks_reference(bars, [_MASK_U128]))
        assert result == [_SMALL]

    def test_two_limb_per_bar_ratio_is_o1(self) -> None:
        """Per-BAR two-limb count cost is flat at 10k bars vs 1k bars.

        The per-CALL cost is O(bars) by design (one limb-split + one vectorised
        pass) — the O(1) claim pinned here is the AMORTISED per-bar cost.
        """

        def _time(n: int) -> float:
            bars = [_BAR_U128] * n
            count_supermasks_reference(bars[:50], [_MASK_U128])  # warm
            t0 = time.perf_counter_ns()
            count_supermasks_reference(bars, [_MASK_U128])
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1e-9)
        assert ratio < cache_tier_ceiling(5.0), (
            f"count_supermasks_reference per-bar ratio {ratio:.2f}× (10k vs 1k "
            f"bars) exceeds the ceiling — the two-limb per-bar cost may have "
            f"regressed from O(1). t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )
