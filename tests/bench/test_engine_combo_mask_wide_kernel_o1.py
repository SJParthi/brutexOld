"""O(1) + measured benches for the u128 Rust enum/count kernel (`combo_mask`).

DEC-ENUM-KERNEL-U128-001 bench pins. ``test_engine_combo_mask_o1.py`` pins the
u64 scalar path and ``test_engine_combo_mask_u128_o1.py`` pins the two-limb
numpy ORACLE; this file pins the production **u128 Rust kernel** the >=64-bit
dispatch now serves (``fno_math.count_supermasks_u128s`` /
``fno_math.ResidentBarsU128`` via ``ResidentBarBuffer``):

* the per-MASK amortised cost of a resident wide-window batch count is flat at
  10k vs 1k masks (the O(1)-per-mask claim; the per-call cost is
  O(N_masks · N_bars) by design — the pin is the amortised per-mask cost over
  a FIXED resident window);
* a MEASURED numpy-oracle vs Rust-wide-kernel comparison on a representative
  >=64-bit grid, recorded honestly — printed at run time, never an asserted
  speedup (Golden Rule #10; the M4-Pro figure rides bench-m4pro.yml's
  whole-dir shared tier later, exactly like the u128_o1 sibling — neither
  file is in the strict-tier explicit list).

## Measured sandbox figures (2026-07-17, shared-CI-class sandbox, py3.12,
## release wheel; best-of-5 per path; 94,000 dense-120-bit bars x 1,000
## two-limb masks, bit-identical outputs verified)

* numpy two-limb oracle (``count_supermasks_reference``):  179.5 ms/call
* Rust u128 kernel, free (re-marshals bars per call):       27.7 ms/call (6.5x)
* Rust u128 kernel, resident (``ResidentBarsU128``):        20.0 ms/call (9.0x)

The ratios are the HONEST sandbox observation for THIS workload only — noisy
shared hardware, no SLA claimed, and the bench below asserts NOTHING about
them (it re-measures + prints; the only assertions are bit-equality teeth and
the O(1)-per-mask ratio through the house two-tier ceiling).

Both ratio pins route through :func:`tests.bench._o1_tiers.cache_tier_ceiling`
(DEC-O1-CACHE-TIER-001 measured 3x allowance) exactly like the u64 sibling:
the per-mask op is constant, but the 10k mask working set crosses a CPU cache
tier — a cache-cliff, not an algorithmic regression; a genuine O(n)-per-op
wrapper still blows well past the ceiling.
"""

from __future__ import annotations

import time
from typing import Final

import fno_math
from brutex.engine.combo_mask import (
    KERNEL_RUST_U128,
    U64_MASK,
    ResidentBarBuffer,
    count_supermasks_reference,
)

from tests.bench._o1_tiers import cache_tier_ceiling

_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 10_000

#: Resident window size for the ratio pin — small enough that the 10k-mask leg
#: stays inside the shared-CI time budget, large enough to be a real scan.
_N_BARS: Final[int] = 2_000

#: Dense 120-bit truth vector — bits set in BOTH u64 limbs (bit >= 64 forces
#: the wide dispatch; MAX_VOCAB_BITS is 128).
_BAR_U128: Final[int] = (1 << 120) - 1
#: A u128 sub-mask with required bits in BOTH limbs (> U64_MASK by construction).
_MASK_U128: Final[int] = (0b1010_1100_1111_0001 << 64) | 0b1111_0000_1111

#: The measured-comparison grid (mirrors the recorded module-docstring run at
#: a CI-budget-friendly scale; the docstring figures used 94k bars).
_CMP_N_BARS: Final[int] = 20_000
_CMP_N_MASKS: Final[int] = 200


def _u128_masks(n: int) -> list[int]:
    """*n* GENUINELY DISTINCT satisfiable u128 sub-masks of the dense bar.

    ``i << 16`` occupies bits 16..29 for n <= 10k — clean of the fixed mask's
    low-limb bits (0-11) and below bit 64 — so every value is distinct by
    construction, every mask keeps ``_MASK_U128``'s high-limb (>=64) bits set
    (wide dispatch guaranteed), and every mask is a sub-mask of the dense
    120-bit bar (always satisfiable).
    """
    return [_MASK_U128 | (i << 16) for i in range(n)]


class TestWideKernelRouting:
    """Correctness/teeth smokes — the fixtures genuinely exercise the wide kernel."""

    def test_operands_exceed_u64(self) -> None:
        """Every benched operand routes past the u64 kernel (bit >= 64 present)."""
        assert _BAR_U128 > U64_MASK
        assert _MASK_U128 > U64_MASK
        assert all(m > U64_MASK for m in _u128_masks(64))
        assert len(set(_u128_masks(_LARGE))) == _LARGE

    def test_resident_buffer_serves_wide_bars_on_rust_kernel(self) -> None:
        """The benched buffer is genuinely Rust-u128-backed (the observable)."""
        buf = ResidentBarBuffer([_BAR_U128] * 8)
        assert buf.kernel_kind == KERNEL_RUST_U128
        assert buf.wide_kernel_active is True
        assert buf.count_supermasks([_MASK_U128, 0]) == [8, 8]

    def test_wide_kernel_bit_exact_vs_oracle(self) -> None:
        """Kernel == two-limb numpy oracle on a mixed non-trivial fixture."""
        bars = [_BAR_U128, _MASK_U128, 1 << 70, 0]
        masks = [*_u128_masks(64), 0, _MASK_U128]
        assert list(fno_math.count_supermasks_u128s(bars, masks)) == (
            count_supermasks_reference(bars, masks)
        )


class TestWideKernelCountIsO1PerMask:
    def test_count_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        buf = ResidentBarBuffer([_BAR_U128] * 64)
        result = benchmark(lambda: buf.count_supermasks([_MASK_U128]))
        assert result == [64]

    def test_wide_per_mask_ratio_is_o1(self) -> None:
        """Per-MASK amortised wide-kernel cost is flat at 10k vs 1k masks.

        The per-CALL cost is O(N_masks · N_bars) by design (a fused branchless
        scan per mask over the FIXED resident window) — the O(1) claim pinned
        here is the AMORTISED per-mask cost: doubling the mask count must not
        super-linearly grow the batch, or the masks-outer loop has regressed.
        """
        buf = ResidentBarBuffer([_BAR_U128] * _N_BARS)
        assert buf.kernel_kind == KERNEL_RUST_U128  # teeth: genuinely wide

        def _time(n: int) -> float:
            masks = _u128_masks(n)
            buf.count_supermasks(masks[:50])  # warm
            t0 = time.perf_counter_ns()
            buf.count_supermasks(masks)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1e-9)
        assert ratio < cache_tier_ceiling(5.0), (
            f"resident u128 kernel per-mask ratio {ratio:.2f}x (10k vs 1k masks) "
            f"exceeds the ceiling — the wide count may have regressed from "
            f"O(1) per mask. t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )


class TestWideKernelVsNumpyOracleMeasured:
    def test_measured_comparison_recorded_not_asserted(self) -> None:
        """MEASURE the oracle vs the wide kernel on a representative grid.

        Prints the run's own numbers (visible under ``pytest -s``; the
        module docstring records the 2026-07-17 sandbox figures — 179.5 ms
        numpy vs 27.7 ms free-kernel vs 20.0 ms resident at 94k x 1k). The
        ONLY assertions are bit-equality (the comparison must compare equal
        work) and each path completing — NO speed relationship is asserted
        (Golden Rule #10: shared runners are noisy; the M4-Pro tier is where
        absolute figures get gated, and no gate is claimed here).
        """
        bars = [(_BAR_U128 >> (i % 7)) | (1 << 70) for i in range(_CMP_N_BARS)]
        masks = _u128_masks(_CMP_N_MASKS)

        def _best_of(fn: object, repeats: int = 3) -> tuple[float, list[int]]:
            best = float("inf")
            out: list[int] = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                out = list(fn(bars, masks))  # type: ignore[operator]
                best = min(best, time.perf_counter() - t0)
            return best, out

        t_numpy, out_numpy = _best_of(count_supermasks_reference)
        t_rust, out_rust = _best_of(fno_math.count_supermasks_u128s)
        resident = fno_math.ResidentBarsU128(bars)
        t_res = float("inf")
        out_res: list[int] = []
        for _ in range(3):
            t0 = time.perf_counter()
            out_res = list(resident.count_supermasks(masks))
            t_res = min(t_res, time.perf_counter() - t0)

        assert out_numpy == out_rust == out_res  # equal work, bit-identical
        print(
            f"\nwide-count measured ({_CMP_N_BARS} bars x {_CMP_N_MASKS} masks, "
            f"best-of-3): numpy={t_numpy * 1e3:.1f}ms "
            f"rust-free={t_rust * 1e3:.1f}ms rust-resident={t_res * 1e3:.1f}ms "
            f"(observed, not asserted)"
        )
