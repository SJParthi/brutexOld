"""Unit tests for the flag-gated Rust sweep-enumeration seam (rust_enum.py).

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738). Covers the env-flag resolution,
the kernel binding (wheel-missing / wheel-stale / kernel-error /
refused-result fallbacks + the engaged-once signal), the silent pre-validation
(the flag-invariant error contract), the count ceiling, the domain validation,
the burn-in self-check + autorevert latch, and the observability memo — every
branch, for the strict 100% line+branch gate. The kernel-vs-Python-oracle
bit-equality is pinned here at the seam level (plus hypothesis in
``tests/property`` and the sweep golden end-to-end).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import fno_math
import pytest
from brutex.engine.combo_mask import count_combos, iter_masks_k_of_bits
from brutex.engine.rust_enum import (
    _CONFLICT_PAIRS_SORTED,  # pyright: ignore[reportPrivateUsage]
    ENUM_KERNEL_MAX_MASKS,
    FULL_CHECK_ENUMERATED,
    PREFIX_CHECK_SURVIVORS,
    SELF_CHECK_CALLS,
    SWEEP_RUST_ENUM_DEFAULT,
    SWEEP_RUST_ENUM_ENV,
    RustEnumResult,
    _burn_in_passes,  # pyright: ignore[reportPrivateUsage] — the seam's own suite
    _resolve_kernel,  # pyright: ignore[reportPrivateUsage]
    _valid_enum_result,  # pyright: ignore[reportPrivateUsage]
    _validated_unique_bits,  # pyright: ignore[reportPrivateUsage]
    consume_self_check_sample,
    disable_kernel,
    enum_survivors_via_kernel,
    kernel_disabled,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    self_check_pending,
    sweep_rust_enum_enabled,
    warn_fallback_once,
)
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Sequence

import brutex.engine.rust_enum as rust_enum_module

#: A conflict-free bit palette (no CONFLICT_PAIRS member has both bits here).
_CLEAN_BITS = [0, 2, 5, 7]

#: The full production vocabulary.
_VOCAB_BITS = list(range(60))


def _oracle(bits: Sequence[int], k: int) -> list[int]:
    """The live Python oracle: conflict-free masks in enumeration order."""
    return [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test observes first-occurrence log events, a fresh burn-in budget,
    an unlatched kernel and an UNSET flag (explicitly armed per test)."""
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()
    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")


# ── flag resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  On  "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, value)
    assert sweep_rust_enum_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "  Off  "])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, value)
    assert sweep_rust_enum_enabled() is False


def test_flag_unset_is_off_ships_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is OFF — the standing default-OFF contract for new Rust
    kernels (the default-ON flip is a SEPARATE later ADR, the ADR-0017
    precedent). Flip = the ONE ``SWEEP_RUST_ENUM_DEFAULT`` constant."""
    monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
    assert sweep_rust_enum_enabled() is False
    assert SWEEP_RUST_ENUM_DEFAULT is False


@pytest.mark.parametrize("value", ["", "   "])
def test_flag_empty_follows_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, value)
    assert sweep_rust_enum_enabled() is SWEEP_RUST_ENUM_DEFAULT


@pytest.mark.parametrize("value", ["0n", "enable", "garbage", "2"])
def test_flag_unrecognised_is_off_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A typo'd ENABLE attempt must be VISIBLE, never silently inert.

    Under a default-OFF kernel the unrecognised arm coincides with the
    default, but the warn-once ``flag_unrecognised`` stays: an operator who
    typed ``0n`` believing they armed the kernel must see the typo named.
    """
    monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, value)
    with capture_logs() as logs:
        assert sweep_rust_enum_enabled() is False
        assert sweep_rust_enum_enabled() is False  # warn-once, still OFF
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "flag_unrecognised"
    assert events[0]["value"] == value


# ── burn-in / latch primitives ───────────────────────────────────────────


def test_self_check_budget_counts_down_and_clamps() -> None:
    reset_self_check_for_tests(remaining=2)
    assert self_check_pending() is True
    consume_self_check_sample()
    assert self_check_pending() is True
    consume_self_check_sample()
    assert self_check_pending() is False
    consume_self_check_sample()  # clamped — never goes negative
    assert self_check_pending() is False


def test_reset_restores_the_full_burn_in_budget() -> None:
    reset_self_check_for_tests(remaining=0)
    assert self_check_pending() is False
    reset_self_check_for_tests()
    assert self_check_pending() is True


def test_disable_kernel_latches_once_and_logs_once() -> None:
    assert kernel_disabled() is False
    with capture_logs() as logs:
        disable_kernel("self_check_mismatch", k=2)
        disable_kernel("some_other_reason")  # no-op — latch already set
    assert kernel_disabled() is True
    events = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert len(events) == 1
    assert events[0]["reason"] == "self_check_mismatch"
    reset_self_check_for_tests()
    assert kernel_disabled() is False


def test_warn_fallback_once_is_per_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    with capture_logs() as logs:
        warn_fallback_once("reason_a", detail=1)
        warn_fallback_once("reason_a", detail=2)
        warn_fallback_once("reason_b")
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert [e["reason"] for e in events] == ["reason_a", "reason_b"]


# ── _resolve_kernel ──────────────────────────────────────────────────────


def test_resolve_kernel_returns_the_real_symbol() -> None:
    kernel = _resolve_kernel()
    assert kernel is fno_math.sweep_enum_conflict_free


def test_resolve_kernel_wheel_missing_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fno_math", None)  # import raises ImportError
    with capture_logs() as logs:
        assert _resolve_kernel() is None
        assert _resolve_kernel() is None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "wheel_missing"


def test_resolve_kernel_wheel_stale_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = types.ModuleType("fno_math")  # imports fine, lacks the slice-1 symbol
    monkeypatch.setitem(sys.modules, "fno_math", stale)
    with capture_logs() as logs:
        assert _resolve_kernel() is None
        assert _resolve_kernel() is None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "wheel_stale"


# ── _validated_unique_bits (the FM-13 silent pre-validation) ─────────────


def test_validated_unique_bits_normalises_sorted_unique() -> None:
    assert _validated_unique_bits([5, 0, 3, 5, 0], 2) == (0, 3, 5)
    assert _validated_unique_bits([], 0) == ()
    assert _validated_unique_bits([127], 1) == (127,)


@pytest.mark.parametrize(
    ("bits", "k"),
    [
        ([-1, 3], 1),  # negative position
        ([0, 128], 1),  # position at the u128 ceiling
        ([0, 1], 3),  # k beyond n_unique
        ([5, 5], 2),  # duplicates collapse BEFORE the k check
        ([0, 1], -1),  # negative k
        ([1.5, 3], 1),  # non-int entry → the generator owns the failure
    ],
)
def test_validated_unique_bits_refuses_silently(bits: list[int], k: int) -> None:
    """Every violation returns ``None`` with NO raise and NO warn — the
    untouched Python generator then raises its canonical error (FM-13)."""
    with capture_logs() as logs:
        assert _validated_unique_bits(bits, k) is None
    assert logs == []


def test_bool_bits_are_accepted_as_ints() -> None:
    """``bool`` is an int subclass and shifts like one — the generator
    accepts it, so the pre-validation must too (parity, not strictness)."""
    assert _validated_unique_bits([True, 3], 1) == (True, 3)


# ── _valid_enum_result (the refuse-poison domain gate) ───────────────────

_ALLOWED_057 = (1 << 0) | (1 << 5) | (1 << 7)


def test_valid_enum_result_accepts_a_consistent_triple() -> None:
    assert _valid_enum_result(([33, 129, 160], 3, 0), 3, 2, _ALLOWED_057) is True
    # Empty survivors with a fully-pruned count is also consistent.
    assert _valid_enum_result(([], 3, 3), 3, 2, _ALLOWED_057) is True


@pytest.mark.parametrize(
    "forged",
    [
        [1, 2, 3],  # not a tuple
        (1, 2),  # wrong arity
        ((1, 2, 3), 3, 0),  # survivors not a list
        ([33], True, 0),  # bool masquerading as enumerated
        ([33], 3, True),  # bool masquerading as conflict_pruned
        ([33], 3.0, 2),  # non-int enumerated
        ([33], 3, "2"),  # non-int conflict_pruned
        ([33], 4, 3),  # enumerated != count
        ([33, 129, 160, 36], 3, -1),  # negative conflict_pruned
        ([33], 3, 1),  # tally inconsistency (3 - 1 != 1)
        (["33", 129], 3, 1),  # non-int FIRST survivor
        ([33, "160"], 3, 1),  # non-int LAST survivor
        ([True, 129], 3, 1),  # bool masquerading as a mask
        ([33, 1 << 9], 3, 1),  # LAST survivor outside the allowed mask
        ([1 << 9, 33], 3, 1),  # FIRST survivor outside the allowed mask
        ([33, 161], 3, 1),  # wrong popcount (161 = bits 0+5+7, 3 bits for k=2)
        ([3, 129], 3, 1),  # conflicting survivor (bits 0+1 = CONFLICT pair)
    ],
)
def test_valid_enum_result_refuses_forged_shapes(forged: object) -> None:
    allowed = _ALLOWED_057 | 0b11  # admit bits 0/1 so the conflict case is reachable
    assert _valid_enum_result(forged, 3, 2, allowed) is False


# ── _burn_in_passes ──────────────────────────────────────────────────────


def test_burn_in_full_compare_passes_and_consumes() -> None:
    survivors = _oracle(_CLEAN_BITS, 2)
    before = self_check_pending()
    assert _burn_in_passes(_CLEAN_BITS, 2, survivors, count_combos(2, 4)) is True
    assert before is True
    assert kernel_disabled() is False


def test_burn_in_full_compare_catches_tail_divergence() -> None:
    """The FULL arm compares whole lists — a dropped tail or an extra element
    mismatches even when the head agrees."""
    survivors = _oracle(_CLEAN_BITS, 2)[:-1]  # dropped tail
    with capture_logs() as logs:
        assert _burn_in_passes(_CLEAN_BITS, 2, survivors, count_combos(2, 4)) is False
    assert kernel_disabled() is True
    events = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert len(events) == 1
    assert events[0]["reason"] == "self_check_mismatch"


def test_burn_in_prefix_compare_checks_only_the_head_region() -> None:
    """Above FULL_CHECK_ENUMERATED only the first PREFIX_CHECK_SURVIVORS
    elements are compared (the honest envelope: tail divergence past the
    prefix is the property/golden suites' layer)."""
    oracle = _oracle(_VOCAB_BITS, 3)
    beyond_full = FULL_CHECK_ENUMERATED + 1
    # A CORRUPT TAIL past the prefix window passes the prefix arm …
    corrupt_tail = [*oracle[:PREFIX_CHECK_SURVIVORS], 0xDEAD]
    assert _burn_in_passes(_VOCAB_BITS, 3, corrupt_tail, beyond_full) is True
    assert kernel_disabled() is False
    # … while a corrupt HEAD (order flip inside the window) is caught.
    flipped = list(oracle)
    flipped[0], flipped[1] = flipped[1], flipped[0]
    assert _burn_in_passes(_VOCAB_BITS, 3, flipped, beyond_full) is False
    assert kernel_disabled() is True


def test_burn_in_prefix_compare_catches_a_short_kernel_list() -> None:
    """A kernel list SHORTER than the prefix window mismatches by length."""
    oracle = _oracle(_VOCAB_BITS, 3)
    assert _burn_in_passes(_VOCAB_BITS, 3, oracle[:100], FULL_CHECK_ENUMERATED + 1) is False
    assert kernel_disabled() is True


# ── enum_survivors_via_kernel — the decision ladder ──────────────────────


def test_off_arm_is_structurally_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF returns ``None`` before ANY kernel resolution or logging."""

    def _must_not_resolve() -> None:
        raise AssertionError("flag-OFF arm must never resolve the kernel")

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", _must_not_resolve)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    assert logs == []


def test_latched_kernel_short_circuits_with_warn_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    disable_kernel("self_check_mismatch")
    reset_observability_memo_for_tests()  # observe the fallback fresh
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "kernel_disabled"


def test_wheel_missing_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    assert [e["reason"] for e in logs if e["event"] == "sweep_rust_enum.fallback"] == [
        "wheel_missing"
    ]


@pytest.mark.parametrize(
    ("bits", "k"),
    [([-1], 1), ([0, 128], 1), ([0, 1], 5), ([0, 1], -1)],
)
def test_invalid_input_returns_none_silently_flag_on(
    monkeypatch: pytest.MonkeyPatch, bits: list[int], k: int
) -> None:
    """FM-13: invalid input never raises or warns HERE even with the flag ON;
    the untouched generator raises the canonical ``ValueError``."""
    _arm(monkeypatch)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(bits, k) is None
    assert logs == []
    with pytest.raises(ValueError, match="iter_masks_k_of_bits"):
        next(iter_masks_k_of_bits(bits, k))


def test_count_ceiling_falls_back_to_the_lazy_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    monkeypatch.setattr(rust_enum_module, "ENUM_KERNEL_MAX_MASKS", 5)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None  # C(4,2)=6 > 5
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "count_ceiling"
    assert events[0]["count"] == 6
    assert events[0]["ceiling"] == 5


@pytest.mark.parametrize("exc", [ValueError("boom"), TypeError("boom"), OverflowError("boom")])
def test_kernel_error_narrow_except_falls_back(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _arm(monkeypatch)

    def _raising_kernel(*args: object) -> object:
        raise exc

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _raising_kernel)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "kernel_error"
    assert repr(exc) in str(events[0]["error"])


def test_memory_error_re_raises_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001) — never swallowed."""
    _arm(monkeypatch)

    def _oom_kernel(*args: object) -> object:
        raise MemoryError("survivor buffer")

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _oom_kernel)
    with pytest.raises(MemoryError, match="survivor buffer"):
        enum_survivors_via_kernel(_CLEAN_BITS, 2)


def test_unexpected_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine code bug still raises (CLAUDE.md §4 — no broad excepts)."""
    _arm(monkeypatch)

    def _buggy_kernel(*args: object) -> object:
        raise RuntimeError("kernel bug")

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _buggy_kernel)
    with pytest.raises(RuntimeError, match="kernel bug"):
        enum_survivors_via_kernel(_CLEAN_BITS, 2)


def test_refused_result_logs_error_once_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)

    def _lying_kernel(*args: object) -> object:
        return ([33], 999, 0)  # enumerated != count_combos — FM-6

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _lying_kernel)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_result_refused"]
    assert len(events) == 1  # ERROR-once (memoised like the warn family)


def test_burn_in_mismatch_autoreverts_then_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain-consistent but ORDER-wrong result is caught by the burn-in,
    latches the kernel OFF, and every later call short-circuits."""
    _arm(monkeypatch)
    oracle = _oracle(_CLEAN_BITS, 2)
    wrong_order = [oracle[1], oracle[0], *oracle[2:]]  # set-equal, order-flipped

    def _reordered_kernel(*args: object) -> object:
        return (wrong_order, count_combos(2, 4), count_combos(2, 4) - len(oracle))

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _reordered_kernel)
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    assert kernel_disabled() is True
    reverts = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert len(reverts) == 1
    assert reverts[0]["reason"] == "self_check_mismatch"
    # The NEXT call never reaches the kernel again this process.
    with capture_logs() as logs:
        assert enum_survivors_via_kernel(_CLEAN_BITS, 2) is None
    assert [e["reason"] for e in logs if e["event"] == "sweep_rust_enum.fallback"] == [
        "kernel_disabled"
    ]


def test_post_burn_in_steady_state_skips_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the budget exhausted a (hypothetically wrong) consistent result is
    served WITHOUT the oracle compare — the documented honest envelope."""
    _arm(monkeypatch)
    reset_self_check_for_tests(remaining=0)
    oracle = _oracle(_CLEAN_BITS, 2)
    wrong_order = [oracle[1], oracle[0], *oracle[2:]]

    def _reordered_kernel(*args: object) -> object:
        return (wrong_order, count_combos(2, 4), count_combos(2, 4) - len(oracle))

    monkeypatch.setattr(rust_enum_module, "_resolve_kernel", lambda: _reordered_kernel)
    result = enum_survivors_via_kernel(_CLEAN_BITS, 2)
    assert result is not None
    assert result.survivors == wrong_order  # steady state trusts the domain gate
    assert kernel_disabled() is False


def test_engaged_logs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    with capture_logs() as logs:
        first = enum_survivors_via_kernel(_CLEAN_BITS, 2)
        second = enum_survivors_via_kernel(_CLEAN_BITS, 2)
    assert first is not None
    assert second is not None
    events = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    assert len(events) == 1


# ── real-kernel parity pins (the literal-pin layer) ──────────────────────


def test_doctest_pin_bits_0_3_5_k2(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([0, 3, 5], 2)
    assert result is not None
    assert result.survivors == [9, 33, 40]
    assert (result.enumerated, result.conflict_pruned) == (3, 0)


def test_input_order_invariance(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([5, 0, 3], 2)
    assert result is not None
    assert result.survivors == [9, 33, 40]


def test_duplicates_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([5, 5, 5], 1)
    assert result is not None
    assert result.survivors == [32]
    assert (result.enumerated, result.conflict_pruned) == (1, 0)


def test_k_zero_yields_the_single_empty_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    for bits in ([7, 2], []):
        reset_self_check_for_tests()
        result = enum_survivors_via_kernel(bits, 0)
        assert result is not None
        assert result.survivors == [0]
        assert (result.enumerated, result.conflict_pruned) == (1, 0)


def test_lex_order_witness_is_not_numeric_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Position-lex on bits {0,2,5,7} at k=2: mask 129 (0+7) precedes 36
    (2+5) — a numeric-ascending (Gosper) kernel fails this pin (FM-1)."""
    _arm(monkeypatch)
    result = enum_survivors_via_kernel(_CLEAN_BITS, 2)
    assert result is not None
    assert result.survivors == [5, 33, 129, 36, 132, 160]


def test_k_equals_n_unique_fully_conflicting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bits {0, 1} at k=2: the single mask 3 carries the (0, 1) CONFLICT pair
    — empty survivors, pruned 1."""
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([0, 1], 2)
    assert result is not None
    assert result.survivors == []
    assert (result.enumerated, result.conflict_pruned) == (1, 1)


@pytest.mark.parametrize("k", [1, 2, 3])
def test_production_vocabulary_full_list_equality(monkeypatch: pytest.MonkeyPatch, k: int) -> None:
    """The 60-bit production vocabulary with the REAL conflict pairs: exact
    list equality (SET ∧ ORDER) + the counter laws, k = 1..3."""
    _arm(monkeypatch)
    result = enum_survivors_via_kernel(_VOCAB_BITS, k)
    oracle = _oracle(_VOCAB_BITS, k)
    assert result is not None
    assert result.survivors == oracle
    assert result.enumerated == count_combos(k, 60)
    assert result.conflict_pruned == result.enumerated - len(oracle)
    if k >= 2:
        assert result.conflict_pruned > 0  # anti-vacuous: the prune genuinely fires


def test_u128_bits_beyond_64(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([63, 64, 127], 2)
    assert result is not None
    assert result.survivors == _oracle([63, 64, 127], 2)
    assert result.survivors[-1] == (1 << 64) | (1 << 127)


def test_bool_bits_parity_with_the_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    result = enum_survivors_via_kernel([True, 3], 1)
    assert result is not None
    assert result.survivors == _oracle([True, 3], 1) == [2, 8]


def test_exhaustive_small_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every n <= 7, 0 <= k <= n over the REAL conflict pairs (mirrors the
    ``test_engine_combo_mask.py`` exhaustive-matrix idiom)."""
    _arm(monkeypatch)
    for n in range(8):
        bits = list(range(n))
        for k in range(n + 1):
            reset_self_check_for_tests()
            result = enum_survivors_via_kernel(bits, k)
            assert result is not None, f"n={n} k={k}"
            assert result.survivors == _oracle(bits, k), f"n={n} k={k}"
            assert result.enumerated == count_combos(k, n), f"n={n} k={k}"


# ── constants + pairs-as-data pins (mutation hardening) ──────────────────


class TestMutationHardening:
    """Pin every behavioural literal so the mutmut loop has zero survivors."""

    def test_env_name(self) -> None:
        assert SWEEP_RUST_ENUM_ENV == "BRUTEX_SWEEP_RUST_ENUM"

    def test_default_is_false(self) -> None:
        assert SWEEP_RUST_ENUM_DEFAULT is False

    def test_ceiling_is_2_pow_24(self) -> None:
        assert ENUM_KERNEL_MAX_MASKS == 1 << 24
        # The production worst case C(60, 5) fits under the ceiling.
        assert count_combos(5, 60) == 5_461_512 <= ENUM_KERNEL_MAX_MASKS

    def test_self_check_calls(self) -> None:
        assert SELF_CHECK_CALLS == 16

    def test_prefix_and_full_check_bounds(self) -> None:
        assert PREFIX_CHECK_SURVIVORS == 4096
        assert FULL_CHECK_ENUMERATED == 65_536

    def test_conflict_pairs_cross_as_sorted_data(self) -> None:
        """The FFI pair table is EXACTLY ``sorted(CONFLICT_PAIRS)`` — never
        re-minted, never filtered Python-side (FM-5)."""
        assert tuple(sorted(CONFLICT_PAIRS)) == _CONFLICT_PAIRS_SORTED
        assert len(_CONFLICT_PAIRS_SORTED) == len(CONFLICT_PAIRS)

    def test_result_dataclass_is_frozen(self) -> None:
        result = RustEnumResult(survivors=[1], enumerated=1, conflict_pruned=0)
        with pytest.raises(AttributeError):
            result.enumerated = 2  # type: ignore[misc]


# ── workflow wiring pins ─────────────────────────────────────────────────


class TestWorkflowWiring:
    """Pin this module into its dedicated per-module coverage + mutation gates.

    Deterministic pins (the ``TestMutationWorkflowWiring`` precedent) that
    fail the unit suite if the module:suite pairing is ever silently dropped
    from the workflows — an unwired gate must never masquerade as an active
    one (Golden Rule #10). These pin the WIRING only; the gates' teeth are
    the workflows' own runs.
    """

    _CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    _MUTATION = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_dedicated_100pct_coverage_gate_wired(self) -> None:
        text = self._CI.read_text(encoding="utf-8")
        assert "--cov=brutex.engine.rust_enum" in text
        assert "tests/unit/test_engine_rust_enum.py" in text

    def test_paired_in_the_mutmut_loop(self) -> None:
        text = self._MUTATION.read_text(encoding="utf-8")
        assert '"brutex/engine/rust_enum.py:tests/unit/test_engine_rust_enum.py:"' in text, (
            "rust_enum.py is not wired into mutation.yml's per-module mutmut loop"
        )

    def test_rust_kernel_in_the_cargo_mutants_scope(self) -> None:
        toml = Path(__file__).resolve().parents[2] / "rust" / "fno-math" / ".cargo" / "mutants.toml"
        assert '"src/enum_kernel.rs"' in toml.read_text(encoding="utf-8"), (
            "enum_kernel.rs is not in the cargo-mutants examine_globs scope"
        )
