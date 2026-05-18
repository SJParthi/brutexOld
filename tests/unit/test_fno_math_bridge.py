# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
"""Unit tests for the PyO3 ⇆ Python bridge (DEC-P0-050 / PR-K).

The `fno_math` Python module is built from `rust/fno-math/` via maturin
and installed alongside `brutex` in CI. These tests verify that the
bridge round-trips correctly under the abi3-py312 stable-ABI wheel
build pipeline.

The file-level pyright pragma above silences the missing-stub
diagnostics — V1 ships the wheel WITHOUT a bundled `fno_math.pyi`
(mixed Python+Rust layout complicates the build). A future PR can
add proper stubs via `[tool.maturin] python-source` once the kernel
grows beyond trivial primitives. Runtime correctness is verified by
this test file; static-type confidence ratchets later.

Future Stage 1B AND-mask kernel + blake3 hash + Bloom bit-density
vectorisation will register their `#[pyfunction]` entries in
`rust/fno-math/src/python.rs` and add coverage here.
"""

from __future__ import annotations

import fno_math


def test_version_is_a_string() -> None:
    """`fno_math.__version__` must be a non-empty semver string."""
    version = fno_math.__version__
    assert isinstance(version, str)
    assert len(version) > 0
    # Look like semver (x.y.z); not exhaustive.
    parts = version.split(".")
    assert len(parts) >= 2


class TestAddU64:
    """`add_u64` is the sanity-check primitive proving the round-trip."""

    def test_simple_addition(self) -> None:
        assert fno_math.add_u64(2, 3) == 5

    def test_zero_identity(self) -> None:
        assert fno_math.add_u64(0, 42) == 42
        assert fno_math.add_u64(42, 0) == 42

    def test_large_values(self) -> None:
        # 2^32 + 2^32 = 2^33 — fits in u64 (well below 2^64-1).
        assert fno_math.add_u64(2**32, 2**32) == 2**33


class TestBitcountU64s:
    """`bitcount_u64s` is the foundational popcount primitive."""

    def test_empty_list_returns_zero(self) -> None:
        assert fno_math.bitcount_u64s([]) == 0

    def test_zero_returns_zero(self) -> None:
        assert fno_math.bitcount_u64s([0]) == 0

    def test_all_ones_returns_64(self) -> None:
        # 0xFFFF_FFFF_FFFF_FFFF = u64::MAX has all 64 bits set.
        assert fno_math.bitcount_u64s([0xFFFFFFFFFFFFFFFF]) == 64

    def test_single_bit_returns_one(self) -> None:
        assert fno_math.bitcount_u64s([1]) == 1
        assert fno_math.bitcount_u64s([1 << 63]) == 1

    def test_multi_value_sum(self) -> None:
        # 0xFF (8 bits) + 0x0F (4 bits) + 0x100 (1 bit) = 13.
        assert fno_math.bitcount_u64s([0xFF, 0x0F, 0x100]) == 13

    def test_aligned_with_python_bit_count(self) -> None:
        """Cross-check against Python's `int.bit_count()` reference."""
        values = [0xDEADBEEF, 0xCAFEBABE, 0xFEEDFACE, 0x1234567890ABCDEF]
        expected = sum(v.bit_count() for v in values)
        assert fno_math.bitcount_u64s(values) == expected

    def test_large_batch(self) -> None:
        """1000-element batch — exercises iteration without overflow."""
        # Each value has exactly 1 bit set → total == 1000.
        values = [1 << (i % 64) for i in range(1000)]
        expected = sum(v.bit_count() for v in values)
        assert fno_math.bitcount_u64s(values) == expected


class TestBitandCountU64s:
    """`bitand_count_u64s` — Stage 1B AND-mask kernel (DEC-P0-051 / PR-L)."""

    def test_basic_two_element_pair(self) -> None:
        # 0xFF & 0x0F = 0x0F → popcount = 4
        # 0xF0 & 0xFF = 0xF0 → popcount = 4
        # Total = 8
        assert fno_math.bitand_count_u64s([0xFF, 0xF0], [0x0F, 0xFF]) == 8

    def test_all_ones_against_all_ones(self) -> None:
        # u64::MAX & u64::MAX = u64::MAX → 64 bits × 3 elements = 192
        assert (
            fno_math.bitand_count_u64s(
                [0xFFFFFFFFFFFFFFFF] * 3,
                [0xFFFFFFFFFFFFFFFF] * 3,
            )
            == 192
        )

    def test_anything_anded_with_zero_is_zero(self) -> None:
        assert fno_math.bitand_count_u64s([0xFF] * 5, [0] * 5) == 0

    def test_empty_lists_return_zero(self) -> None:
        assert fno_math.bitand_count_u64s([], []) == 0

    def test_disjoint_masks_zero(self) -> None:
        # 0x0F & 0xF0 = 0 — disjoint bits
        assert fno_math.bitand_count_u64s([0x0F], [0xF0]) == 0

    def test_length_mismatch_raises_value_error(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="length mismatch"):
            fno_math.bitand_count_u64s([1, 2, 3], [4, 5])

    def test_length_mismatch_empty_vs_nonempty(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="length mismatch"):
            fno_math.bitand_count_u64s([], [1])

    def test_aligned_with_python_bit_count(self) -> None:
        """Cross-check the kernel against the Python reference."""
        a = [0xDEADBEEF, 0xCAFEBABE, 0xFEEDFACE, 0x1234567890ABCDEF]
        b = [0xFFFFFFFF, 0x0F0F0F0F, 0xAAAAAAAA, 0x5555555555555555]
        expected = sum((x & y).bit_count() for x, y in zip(a, b, strict=True))
        assert fno_math.bitand_count_u64s(a, b) == expected

    def test_large_batch_matches_python_reference(self) -> None:
        """1000-element AND-mask sweep — cross-check against Python."""
        a = [i * 0x123456789 for i in range(1000)]
        b = [(i + 1) * 0x987654321 for i in range(1000)]
        # Coerce to u64 range for comparison.
        a_u64 = [x & 0xFFFFFFFFFFFFFFFF for x in a]
        b_u64 = [x & 0xFFFFFFFFFFFFFFFF for x in b]
        expected = sum((x & y).bit_count() for x, y in zip(a_u64, b_u64, strict=True))
        assert fno_math.bitand_count_u64s(a_u64, b_u64) == expected
