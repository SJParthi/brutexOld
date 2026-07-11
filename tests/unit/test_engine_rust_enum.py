"""Unit suite for :mod:`brutex.engine.rust_enum` — the ranged enum-kernel seam.

DEC-SWEEP-ENUM-RUST-001 (MIGRATION.md #1738), reworked to the design-of-record
``enumerate_prefilter`` surface. This file alone must hold the module at 100%
line + branch (the ci.yml per-module gate), so every fallback-reason branch,
every domain-validation refusal arm, the width-keyed shadow-window state
machine, the MID-TUPLE CONTINUATION contract and both driver belts are driven
here — each against the REAL compiled kernel where one exists, and against
surgical fakes where the branch is a defence the real kernel cannot trip.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import fno_math
import pytest
from brutex.engine import rust_enum
from brutex.engine.combo_mask import count_combos, count_supermasks, iter_masks_k_of_bits
from brutex.engine.rust_enum import RustEnumResult
from brutex.vocabulary import CONFLICT_PAIRS, has_conflict
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

#: The REAL compiled classes, captured BEFORE any monkeypatching so poison
#: wrappers can delegate to the genuine kernel.
_REAL_U64 = fno_math.ResidentSweepEnum
_REAL_U128 = fno_math.ResidentSweepEnumU128

#: A small mixed bar window: bits 0-3 fire with decreasing frequency.
_BARS: list[int] = [0b1111, 0b0111, 0b0011, 0b0001, 0b0000] * 4

#: One REAL production conflict pair (both bits < 60) — used to build tuples
#: whose every mask conflicts.
_A_PAIR: tuple[int, int] = min(CONFLICT_PAIRS)


def _count_hits(masks: Sequence[int]) -> list[int]:
    """The seam's injected Stage-1B counter over the fixture window."""
    return count_supermasks(_BARS, masks)


def _oracle_kept(bits: Sequence[int], k: int, min_hits: int) -> list[int]:
    """Today's spine, verbatim: enumerate → conflict prune → min-hits filter."""
    survivors = [m for m in iter_masks_k_of_bits(bits, k) if not has_conflict(m)]
    if min_hits > 0 and survivors:
        hits = _count_hits(survivors)
        return [m for m, h in zip(survivors, hits, strict=True) if h >= min_hits]
    return survivors


def _call(
    bits: Sequence[int],
    k: int,
    *,
    min_hits: int = 0,
    bar_bits: Sequence[int] = _BARS,
    digest_path: bool = True,
) -> RustEnumResult | None:
    return rust_enum.enum_prefilter_via_kernel(
        bits,
        k,
        min_hits=min_hits,
        bar_bits=bar_bits,
        count_hits=_count_hits,
        digest_path=digest_path,
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Fresh flag/env/shadow/latch/memo state per test (process-global cells)."""
    monkeypatch.delenv(rust_enum.SWEEP_RUST_ENUM_ENV, raising=False)
    monkeypatch.delenv(rust_enum.ENUM_WORK_CELLS_ENV, raising=False)
    rust_enum.reset_enum_self_check_for_tests()
    rust_enum.reset_observability_memo_for_tests()
    yield
    rust_enum.reset_enum_self_check_for_tests()
    rust_enum.reset_observability_memo_for_tests()


@pytest.fixture
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "1")


# ────── the flag ──────


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, value)
    assert rust_enum.sweep_rust_enum_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "No", " OFF "])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, value)
    assert rust_enum.sweep_rust_enum_enabled() is False


def test_flag_unset_is_off_ships_inert() -> None:
    """Default-OFF is the ONE flip constant — the kernel ships inert."""
    assert rust_enum.SWEEP_RUST_ENUM_DEFAULT is False
    assert rust_enum.sweep_rust_enum_enabled() is False


@pytest.mark.parametrize("value", ["", "   "])
def test_flag_empty_follows_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, value)
    assert rust_enum.sweep_rust_enum_enabled() is rust_enum.SWEEP_RUST_ENUM_DEFAULT


def test_flag_unrecognised_is_off_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd ENABLE attempt is VISIBLE (warn-once), never silently inert."""
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "0n")
    with capture_logs() as logs:
        assert rust_enum.sweep_rust_enum_enabled() is False
        assert rust_enum.sweep_rust_enum_enabled() is False
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(warns) == 1
    assert warns[0]["reason"] == "flag_unrecognised"


# ────── the work-cells knob ──────


def test_enum_cells_unset_and_blank_serve_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert rust_enum._resolve_enum_work_cells() == rust_enum.DEFAULT_ENUM_WORK_CELLS
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "   ")
    assert rust_enum._resolve_enum_work_cells() == rust_enum.DEFAULT_ENUM_WORK_CELLS


def test_enum_cells_invalid_warns_once_and_serves_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #1608 invalid-numeric-knob precedent: loud + default, never a crash."""
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "lots")
    with capture_logs() as logs:
        assert rust_enum._resolve_enum_work_cells() == rust_enum.DEFAULT_ENUM_WORK_CELLS
        assert rust_enum._resolve_enum_work_cells() == rust_enum.DEFAULT_ENUM_WORK_CELLS
    assert len([e for e in logs if e["event"] == "sweep_rust_enum.enum_cells_invalid"]) == 1


def test_enum_cells_clamps_both_directions_with_one_warn_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The design §3 override clamps: a typo'd 2**44 can never hold the GIL
    for minutes; a degenerate 1 can never make a one-mask-per-call stream."""
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, str(2**44))
    with capture_logs() as logs:
        assert rust_enum._resolve_enum_work_cells() == rust_enum.ENUM_WORK_CELLS_UPPER_CLAMP
    clamps = [e for e in logs if e["event"] == "sweep_rust_enum.work_cap_clamped"]
    assert clamps[0]["direction"] == "high"
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "1")
    with capture_logs() as logs:
        assert rust_enum._resolve_enum_work_cells() == rust_enum.ENUM_WORK_CELLS_LOWER_CLAMP
    clamps = [e for e in logs if e["event"] == "sweep_rust_enum.work_cap_clamped"]
    assert clamps[0]["direction"] == "low"


def test_enum_cells_valid_value_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, str(1 << 20))
    with capture_logs() as logs:
        assert rust_enum._resolve_enum_work_cells() == 1 << 20
    assert not logs


# ────── shadow window + latch state ──────


def test_shadow_window_defaults_open_per_width() -> None:
    assert rust_enum.enum_shadow_pending("u64")
    assert rust_enum.enum_shadow_pending("u128")


def test_reset_enum_self_check_sets_the_named_states() -> None:
    """The design's full state-setter: closed / cap-near / per-width."""
    rust_enum.reset_enum_self_check_for_tests(width="u64", masks_remaining=0)
    assert not rust_enum.enum_shadow_pending("u64")
    assert rust_enum.enum_shadow_pending("u128")  # untouched width stays open
    rust_enum.reset_enum_self_check_for_tests()
    assert rust_enum.enum_shadow_pending("u64")


def test_disable_kernel_latches_once_per_width_and_logs_once() -> None:
    with capture_logs() as logs:
        rust_enum.disable_kernel("u64", "self_check_mismatch")
        rust_enum.disable_kernel("u64", "another_reason")  # no-op repeat
    errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert len(errors) == 1
    assert errors[0]["reason"] == "self_check_mismatch"
    assert rust_enum.kernel_disabled("u64")
    assert not rust_enum.kernel_disabled("u128")  # per-width latch (design §5)


def test_warn_fallback_once_is_per_reason() -> None:
    with capture_logs() as logs:
        rust_enum.warn_fallback_once("reason_a")
        rust_enum.warn_fallback_once("reason_a")
        rust_enum.warn_fallback_once("reason_b")
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert [w["reason"] for w in warns] == ["reason_a", "reason_b"]


# ────── kernel-class resolution ──────


def test_resolve_kernel_class_returns_the_real_classes() -> None:
    assert rust_enum._resolve_kernel_class("u64") is _REAL_U64
    assert rust_enum._resolve_kernel_class("u128") is _REAL_U128


def test_resolve_kernel_wheel_missing_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert rust_enum._resolve_kernel_class("u64") is None
        assert rust_enum._resolve_kernel_class("u64") is None
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(warns) == 1
    assert warns[0]["reason"] == "wheel_missing"


def test_resolve_kernel_wheel_stale_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(fno_math, "ResidentSweepEnum")
    with capture_logs() as logs:
        assert rust_enum._resolve_kernel_class("u64") is None
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert warns[0]["reason"] == "wheel_stale"
    assert warns[0]["missing"] == "ResidentSweepEnum"


# ────── input pre-validation ──────


def test_validated_unique_bits_normalises_sorted_unique() -> None:
    assert rust_enum._validated_unique_bits([5, 0, 3, 5, 0], 2) == (0, 3, 5)
    assert rust_enum._validated_unique_bits([], 0) == ()


@pytest.mark.parametrize(
    ("bits", "k"),
    [
        ([0, 1.5], 1),  # non-int entry
        (["1"], 1),  # str entry
        ([-1, 3], 1),  # negative position
        ([128], 1),  # >= MAX_VOCAB_BITS
        ([0, 1], 3),  # k > n_unique
        ([0, 1], -1),  # k < 0
    ],
)
def test_validated_unique_bits_refuses_silently(bits: list[int], k: int) -> None:
    """Silent ``None`` — the untouched generator owns the canonical raise."""
    with capture_logs() as logs:
        assert rust_enum._validated_unique_bits(bits, k) is None
    assert not logs


def test_bool_bits_are_accepted_as_ints() -> None:
    """bool is an int subclass and shifts like one — generator parity."""
    assert rust_enum._validated_unique_bits([True, False, 3], 2) == (0, 1, 3)


# ────── per-range domain validation ──────


def _real_range(
    bits: Sequence[int], k: int, min_hits: int = 0
) -> tuple[tuple[int, int, int, bool, int, list[int]], int, int]:
    """One REAL whole-range kernel call + its (count, allowed_mask) context."""
    core = _REAL_U64(list(_BARS))
    unique = sorted(set(bits))
    raw = core.enumerate_prefilter(unique, k, sorted(CONFLICT_PAIRS), min_hits, 0, 2**63, 2**63)
    allowed = 0
    for pos in unique:
        allowed |= 1 << pos
    return raw, count_combos(k, len(unique)), allowed


def test_valid_range_result_accepts_a_real_range() -> None:
    for min_hits in (0, 2):
        raw, count, allowed = _real_range([0, 1, 2, 3, 4], 2, min_hits)
        assert rust_enum._valid_range_result(
            raw, start_rank=0, count=count, min_hits=min_hits, k=2, allowed_mask=allowed
        )


def test_valid_range_result_refuses_every_forged_arm() -> None:
    """Each refusal arm of the domain gate, forged from a REAL valid range."""
    raw, count, allowed = _real_range([0, 1, 2, 3, 4], 2, 0)
    enumerated, pruned, next_rank, exhausted, hits, kept = raw
    assert kept, "fixture must keep at least two masks"

    def bad(candidate: object) -> None:
        assert not rust_enum._valid_range_result(
            candidate, start_rank=0, count=count, min_hits=0, k=2, allowed_mask=allowed
        )

    bad("junk")  # not a tuple
    bad(raw[:5])  # wrong arity
    bad((float(enumerated), pruned, next_rank, exhausted, hits, kept))  # non-int
    bad((True, pruned, next_rank, exhausted, hits, kept))  # bool masquerade
    bad((enumerated, pruned, next_rank, 1, hits, kept))  # exhausted not bool
    bad((enumerated, pruned, next_rank, exhausted, hits, tuple(kept)))  # kept not list
    bad((enumerated + 1, pruned, count + 1, exhausted, hits, kept))  # next_rank > count
    bad((enumerated + 1, pruned, next_rank, exhausted, hits, kept))  # position law
    bad((enumerated, -1, next_rank, exhausted, hits, kept))  # pruned < 0
    bad((enumerated, enumerated + 1, next_rank, exhausted, hits, kept))  # pruned > enum
    # MIN-HITS SHORT-CIRCUIT law, arm 1 (min_hits == 0): hits must be 0 and
    # kept must be EXACTLY the conflict-free count.
    bad((enumerated, pruned, next_rank, exhausted, 1, kept))
    bad((enumerated, pruned + 1, next_rank, exhausted, hits, kept))  # kept != cf
    # First/last kept-mask sanity (spot checks only — O(1)).
    bad((enumerated, pruned, next_rank, exhausted, hits, ["0b11", *kept[1:]]))  # non-int
    bad((enumerated, pruned, next_rank, exhausted, hits, [True, *kept[1:]]))  # bool
    bad((enumerated, pruned, next_rank, exhausted, hits, [0b111, *kept[1:]]))  # popcount
    bad((enumerated, pruned, next_rank, exhausted, hits, [*kept[:-1], 1 << 40]))  # outside
    conflicted = (1 << _A_PAIR[0]) | (1 << _A_PAIR[1])
    assert not rust_enum._valid_range_result(
        (1, 0, 1, True, 0, [conflicted]),
        start_rank=0,
        count=1,
        min_hits=0,
        k=2,
        allowed_mask=conflicted,
    )  # spot mask carries a production conflict pair


def test_valid_range_result_min_hits_positive_law() -> None:
    """Arm 2: hits_counted must equal the conflict-free count exactly — the
    design's mandated seam pin on the short-circuit law (a guard-deleting
    kernel mutant flips this value and dies here)."""
    raw, count, allowed = _real_range([0, 1, 2, 3, 4], 2, 3)
    enumerated, pruned, next_rank, exhausted, hits, kept = raw
    assert hits == enumerated - pruned

    def check(candidate: object) -> bool:
        return rust_enum._valid_range_result(
            candidate, start_rank=0, count=count, min_hits=3, k=2, allowed_mask=allowed
        )

    assert check(raw)
    assert not check((enumerated, pruned, next_rank, exhausted, 0, kept))  # count skipped
    assert not check((enumerated, pruned, next_rank, exhausted, hits - 1, kept))
    too_many = [*kept, *([3] * (enumerated - pruned - len(kept) + 1))]
    assert not check((enumerated, pruned, next_rank, exhausted, hits, too_many))


# ────── the python window / continuation helpers ──────


def test_python_window_and_continuation_compose_to_the_whole_stream() -> None:
    bits, k, min_hits = [0, 1, 2, 3, 4], 2, 2
    gen = iter(iter_masks_k_of_bits(bits, k))
    w_enum, w_pruned, w_kept = rust_enum._python_window(gen, 4, min_hits, _count_hits)
    c_enum, c_pruned, c_kept = rust_enum._python_continuation(gen, min_hits, _count_hits)
    assert w_enum == 4
    assert w_enum + c_enum == 10  # C(5, 2)
    assert w_kept + c_kept == _oracle_kept(bits, k, min_hits)
    assert w_pruned + c_pruned == 10 - len(_oracle_kept(bits, k, 0))


def test_python_window_all_conflicting_skips_the_count() -> None:
    """An all-conflicted window has NO conflict-free masks — the min-hits
    filter's ``and conflict_free`` guard short-circuits (no count call)."""
    a, b = _A_PAIR
    gen = iter(iter_masks_k_of_bits([a, b], 2))

    def _must_not_count(masks: Sequence[int]) -> NoReturn:
        raise AssertionError("count_hits must not run on an empty window")

    assert rust_enum._python_window(gen, 1, 5, _must_not_count) == (1, 1, [])


def test_python_continuation_min_hits_zero_keeps_conflict_free() -> None:
    gen = iter(iter_masks_k_of_bits([0, 1, 2], 2))
    enum, pruned, kept = rust_enum._python_continuation(gen, 0, _count_hits)
    assert enum == 3
    assert kept == _oracle_kept([0, 1, 2], 2, 0)
    assert pruned == 3 - len(kept)


def test_consume_shadow_floor_unmet_warns_once() -> None:
    """The 256-range cap tripping with the mask floor unmet is the genuine
    degenerate-stream signal (design §5 audit C51)."""
    rust_enum.reset_enum_self_check_for_tests(
        width="u64", masks_remaining=10**9, ranges_cap_remaining=1
    )
    with capture_logs() as logs:
        rust_enum._consume_shadow("u64", 100)
        rust_enum._consume_shadow("u64", 100)
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.enum_shadow_floor_unmet"]
    assert len(warns) == 1
    assert warns[0]["width"] == "u64"
    assert not rust_enum.enum_shadow_pending("u64")


# ────── enum_prefilter_via_kernel — the driver ──────


def test_off_arm_is_silent_and_structurally_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF is the deliberate default state — no warn, no kernel resolve."""

    def _must_not_resolve(width: str) -> NoReturn:
        raise AssertionError("the OFF arm must never resolve the kernel")

    monkeypatch.setattr(rust_enum, "_resolve_kernel_class", _must_not_resolve)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    assert not logs


@pytest.mark.usefixtures("_on")
def test_legacy_dedup_path_warns_and_falls_back() -> None:
    """The design's engagement-condition listing order: the digest-path
    condition is named FIRST — a legacy tuple with the flag ON warns
    ``legacy_dedup_path`` (once), never a later condition."""
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2, digest_path=False) is None
        assert _call([0, 1, 2], 2, digest_path=False) is None
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert len(warns) == 1
    assert warns[0]["reason"] == "legacy_dedup_path"
    assert not [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]


@pytest.mark.usefixtures("_on")
def test_invalid_input_returns_none_silently() -> None:
    with capture_logs() as logs:
        assert _call([0, 1.5], 1) is None  # type: ignore[list-item]
    assert not logs


@pytest.mark.usefixtures("_on")
def test_latched_width_short_circuits_with_warn_once() -> None:
    rust_enum.disable_kernel("u64", "self_check_mismatch")
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert warns[0]["reason"] == "kernel_disabled"


@pytest.mark.usefixtures("_on")
def test_wheel_missing_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "wheel_missing"


@pytest.mark.usefixtures("_on")
def test_count_overflow_falls_back() -> None:
    """C(128, 64) exceeds the u64 rank domain — refused wrapper-side."""
    with capture_logs() as logs:
        assert _call(list(range(128)), 64) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "count_overflow"


@pytest.mark.usefixtures("_on")
def test_min_hits_negative_is_served_as_zero() -> None:
    """``min_hits <= 0`` is the exact ``if cfg.min_hits > 0`` spine law."""
    result = _call([0, 1, 2, 3, 4], 2, min_hits=-5)
    assert result is not None
    assert result.kept == _oracle_kept([0, 1, 2, 3, 4], 2, 0)
    assert result.hits_counted == 0  # the short-circuit law held


@pytest.mark.usefixtures("_on")
def test_kernel_served_tuple_matches_the_oracle_across_min_hits() -> None:
    bits = [0, 1, 2, 3, 4, 5]
    for min_hits in (0, 1, 3, 20, 10**6):
        rust_enum.reset_enum_self_check_for_tests()
        result = _call(bits, 3, min_hits=min_hits)
        assert result is not None
        assert result.kept == _oracle_kept(bits, 3, min_hits)
        assert result.enumerated == count_combos(3, 6)
        assert result.evaluated == len(_oracle_kept(bits, 3, 0))
        expected_hits = result.evaluated if min_hits > 0 else 0
        assert result.hits_counted == expected_hits


@pytest.mark.usefixtures("_on")
def test_u128_width_serves_bits_beyond_64() -> None:
    result = _call([0, 64, 65], 1, min_hits=0)
    assert result is not None
    assert result.kept == [1, 1 << 64, 1 << 65]
    assert result.enumerated == 3


@pytest.mark.usefixtures("_on")
def test_k_zero_yields_the_single_empty_mask() -> None:
    result = _call([], 0)
    assert result is not None
    assert result.kept == [0]
    assert (result.enumerated, result.conflict_pruned) == (1, 0)


@pytest.mark.usefixtures("_on")
def test_bar_value_past_the_width_is_a_kernel_error_fallback() -> None:
    """A u64-width tuple over a bar value >= 2**64: PyO3's conversion
    OverflowError degrades to the Python loop (never a crash)."""
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2, bar_bits=[1 << 64]) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "kernel_error"


@pytest.mark.usefixtures("_on")
def test_construction_memory_error_re_raises_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OomCtor:
        """Ctor that OOMs — the global-stop class must re-raise bare."""

        def __init__(self, bar_bits: list[int]) -> None:
            raise MemoryError("enumerate_prefilter: allocation of planes failed")

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _OomCtor)
    with pytest.raises(MemoryError):
        _call([0, 1, 2], 2)


@pytest.mark.usefixtures("_on")
def test_unexpected_ctor_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WeirdCtor:
        """Ctor raising outside the narrow boundary set — must propagate."""

        def __init__(self, bar_bits: list[int]) -> None:
            raise RuntimeError("not a boundary error")

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _WeirdCtor)
    with pytest.raises(RuntimeError):
        _call([0, 1, 2], 2)


class _FakeCore:
    """Configurable fake resident core for the driver's defence branches."""

    def __init__(self, bar_bits: list[int]) -> None:
        self._n = len(bar_bits)

    def n_bars(self) -> int:
        return self._n

    def enumerate_prefilter(self, *args: object) -> object:
        raise AssertionError("override per test")


@pytest.mark.usefixtures("_on")
def test_bars_length_mismatch_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The §1.3 F27 seam invariant: an n_bars()-lying core is refused."""

    class _Lying(_FakeCore):
        def n_bars(self) -> int:
            return self._n + 1

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Lying)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "bars_length_mismatch"


@pytest.mark.usefixtures("_on")
def test_range_call_value_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Raising(_FakeCore):
        def enumerate_prefilter(self, *args: object) -> NoReturn:
            raise ValueError("enumerate_prefilter: boundary")

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Raising)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "kernel_error"


@pytest.mark.usefixtures("_on")
def test_range_call_memory_error_re_raises_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Oom(_FakeCore):
        def enumerate_prefilter(self, *args: object) -> NoReturn:
            raise MemoryError("enumerate_prefilter: allocation of kept masks failed")

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Oom)
    with pytest.raises(MemoryError):
        _call([0, 1, 2], 2)


@pytest.mark.usefixtures("_on")
def test_refused_range_logs_error_once_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Junk(_FakeCore):
        def enumerate_prefilter(self, *args: object) -> str:
            return "junk"

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Junk)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
        assert _call([0, 1, 2], 2) is None
    errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_result_refused"]
    assert len(errors) == 1


@pytest.mark.usefixtures("_on")
def test_enum_no_progress_belt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-exhausted zero-progress return (structurally unreachable under
    the kernel's guaranteed-progress law) trips the driver belt."""

    class _Stuck(_FakeCore):
        def enumerate_prefilter(self, *args: Any) -> tuple[int, int, int, bool, int, list[int]]:
            return (0, 0, 0, False, 0, [])

    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _Stuck)
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "enum_no_progress"


class _EarlyExhaust:
    """Wraps the REAL kernel but lies ``exhausted`` after the first range —
    the total-reconciliation belt's teeth (the §1.4 driver assert)."""

    def __init__(self, bar_bits: list[int]) -> None:
        self._real = _REAL_U64(bar_bits)

    def n_bars(self) -> int:
        return self._real.n_bars()

    def enumerate_prefilter(self, *args: Any) -> tuple[int, int, int, bool, int, list[int]]:
        enumerated, pruned, next_rank, _, hits, kept = self._real.enumerate_prefilter(*args)
        return (enumerated, pruned, next_rank, True, hits, kept)


@pytest.mark.usefixtures("_on")
def test_enum_total_mismatch_belt_catches_an_early_exhaust_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kernel lying "exhausted" mid-space passes the per-range gate (the
    exhausted flag is deliberately not cross-checked there) and dies at the
    loop-exit TOTAL-RECONCILIATION belt vs the driver's own count_combos."""
    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _EarlyExhaust)
    # Force multiple ranges so the first range's next_rank < count: cells at
    # the lower clamp over C(60, 3) = 34,220 > the 4096-mask range floor.
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "1")
    rust_enum.reset_enum_self_check_for_tests(masks_remaining=0)  # past burn-in
    with capture_logs() as logs:
        assert _call(list(range(60)), 3) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "enum_total_mismatch"


@pytest.mark.usefixtures("_on")
def test_multi_range_tuple_is_shadowed_and_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    """C(60, 3) at the clamped cells floor runs ~9 ranges; the shadow window
    stays open ACROSS ranges (mask-count-floored, design §5) and the result
    equals the oracle exactly."""
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "1")  # clamps up: 4096-mask ranges
    bits = list(range(60))
    result = _call(bits, 3, min_hits=2)
    assert result is not None
    assert result.kept == _oracle_kept(bits, 3, 2)
    assert result.enumerated == count_combos(3, 60)
    # The whole 34,220-mask tuple fits inside the 2^16 shadow floor: every
    # range was shadowed, so the floor counter dropped by exactly the tuple.
    assert (
        rust_enum._SHADOW_MASKS_REMAINING["u64"]
        == rust_enum.SWEEP_ENUM_SELF_CHECK_MASKS - result.enumerated
    )


@pytest.mark.usefixtures("_on")
def test_shadow_window_closes_mid_tuple_and_later_ranges_run_unshadowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rust_enum.ENUM_WORK_CELLS_ENV, "1")
    rust_enum.reset_enum_self_check_for_tests(width="u64", masks_remaining=5000)
    bits = list(range(60))
    result = _call(bits, 3)
    assert result is not None
    assert result.kept == _oracle_kept(bits, 3, 0)
    assert not rust_enum.enum_shadow_pending("u64")  # closed mid-tuple


@pytest.mark.usefixtures("_on")
def test_post_burn_in_steady_state_never_builds_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_enum.reset_enum_self_check_for_tests(masks_remaining=0)

    def _must_not_enumerate(bits: Sequence[int], k: int) -> NoReturn:
        raise AssertionError("the closed shadow window must not build the oracle")

    monkeypatch.setattr(rust_enum, "iter_masks_k_of_bits", _must_not_enumerate)
    result = _call([0, 1, 2, 3, 4], 2)
    assert result is not None
    assert result.kept == _oracle_kept([0, 1, 2, 3, 4], 2, 0)


class _DropOneKept:
    """Domain-VALID poison: silently drops one kept mask from the first range
    with SELF-CONSISTENT tallies (the drop re-bucketed as conflict-pruned) —
    the design's in-range kept-set divergence class."""

    def __init__(self, bar_bits: list[int]) -> None:
        self._real = _REAL_U64(bar_bits)
        self._poisoned = False

    def n_bars(self) -> int:
        return self._real.n_bars()

    def enumerate_prefilter(self, *args: Any) -> tuple[int, int, int, bool, int, list[int]]:
        enumerated, pruned, next_rank, exhausted, hits, kept = self._real.enumerate_prefilter(*args)
        if not self._poisoned and kept:
            self._poisoned = True
            return (enumerated, pruned + 1, next_rank, exhausted, hits, kept[1:])
        return (enumerated, pruned, next_rank, exhausted, hits, kept)


@pytest.mark.usefixtures("_on")
def test_shadow_mismatch_autoreverts_and_ships_the_python_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MID-TUPLE CONTINUATION contract: on a shadow mismatch the width
    latches (one autorevert ERROR), the Python window ships, the remainder
    rides today's enumeration, and the RESULT is bit-identical to the pure
    Python spine — the poison never reaches the caller."""
    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _DropOneKept)
    bits = [0, 1, 2, 3, 4, 5]
    with capture_logs() as logs:
        result = _call(bits, 3)
    reverted = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
    assert len(reverted) == 1
    assert reverted[0]["reason"] == "self_check_mismatch"
    assert result is not None
    assert result.kept == _oracle_kept(bits, 3, 0)
    assert result.enumerated == count_combos(3, 6)
    assert result.evaluated == len(_oracle_kept(bits, 3, 0))
    assert rust_enum.kernel_disabled("u64")
    # The NEXT tuple short-circuits on the latch (warn-once).
    with capture_logs() as logs:
        assert _call(bits, 3) is None
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks[0]["reason"] == "kernel_disabled"


@pytest.mark.usefixtures("_on")
def test_mixed_provenance_total_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive belt on the mixed path: if the continuation cannot reconcile
    the driver's count_combos total (only reachable when the oracle stream
    itself is broken — simulated by truncating it), the tuple is refused to
    the spine, never returned short."""
    monkeypatch.setattr(fno_math, "ResidentSweepEnum", _DropOneKept)

    def _truncated(bits: Sequence[int], k: int) -> Iterator[int]:
        return iter(list(iter_masks_k_of_bits(bits, k))[:-3])

    monkeypatch.setattr(rust_enum, "iter_masks_k_of_bits", _truncated)
    with capture_logs() as logs:
        assert _call([0, 1, 2, 3, 4, 5], 3) is None
    warns = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert warns[0]["reason"] == "enum_total_mismatch"
    assert warns[0]["provenance"] == "mixed"


@pytest.mark.usefixtures("_on")
def test_engaged_logs_once_per_process() -> None:
    with capture_logs() as logs:
        assert _call([0, 1, 2, 3, 4], 2) is not None
        assert _call([0, 1, 2, 3, 4], 2) is not None
    engaged = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
    assert len(engaged) == 1
    assert engaged[0]["width"] == "u64"


@pytest.mark.usefixtures("_on")
def test_all_conflicting_tuple_keeps_nothing_with_min_hits() -> None:
    """k == n over one REAL conflict pair: the single mask prunes; the fused
    count never runs (no conflict-free masks) — hits_counted == 0 even at
    min_hits > 0 (the law's degenerate arm)."""
    a, b = _A_PAIR
    result = _call([a, b], 2, min_hits=5)
    assert result is not None
    assert result.kept == []
    assert (result.enumerated, result.conflict_pruned, result.hits_counted) == (1, 1, 0)


# ────── literal pins (the production vocabulary) ──────


@pytest.mark.usefixtures("_on")
def test_doctest_pin_bits_0_3_5_k2() -> None:
    result = _call([0, 3, 5], 2)
    assert result is not None
    assert result.kept == [9, 33, 40]


@pytest.mark.usefixtures("_on")
def test_lex_order_witness_is_not_numeric_order() -> None:
    """The item-1 counterexample: 9 BEFORE 6 (position-lex, never colex).

    Masks 3 (bits {0,1}) and 12 (bits {2,3}) are pruned by the REAL
    production conflict pairs — the surviving stream still witnesses the
    position-lex order (9 strictly before 6; ascending-numeric would flip
    them). The pair-free full witness [3, 5, 9, 6, 10, 12] is pinned
    in-crate and in the property suite over synthetic pair tables."""
    result = _call([0, 1, 2, 3], 2)
    assert result is not None
    assert result.kept == [5, 9, 6, 10]


@pytest.mark.usefixtures("_on")
@pytest.mark.parametrize("k", [1, 2, 3])
def test_production_vocabulary_full_list_equality(k: int) -> None:
    """The REAL 60-bit vocabulary + REAL conflict pairs: full list equality
    (SET ∧ ORDER) at production shape, both min-hits arms."""
    bits = list(range(60))
    for min_hits in (0, 2):
        rust_enum.reset_enum_self_check_for_tests()
        result = _call(bits, k, min_hits=min_hits)
        assert result is not None
        assert result.kept == _oracle_kept(bits, k, min_hits)


# ────── constants + wiring pins ──────


class TestMutationHardening:
    """Literal pins killing constant/operator mutants in the wrapper."""

    def test_env_names(self) -> None:
        assert rust_enum.SWEEP_RUST_ENUM_ENV == "BRUTEX_SWEEP_RUST_ENUM"
        assert rust_enum.ENUM_WORK_CELLS_ENV == "BRUTEX_SWEEP_CORE_ENUM_CELLS"

    def test_default_is_false(self) -> None:
        assert rust_enum.SWEEP_RUST_ENUM_DEFAULT is False

    def test_work_cell_constants(self) -> None:
        assert rust_enum.DEFAULT_ENUM_WORK_CELLS == 2**28
        assert rust_enum.ENUM_WORK_CELLS_LOWER_CLAMP == 2**16
        assert rust_enum.ENUM_WORK_CELLS_UPPER_CLAMP == 2**30
        assert rust_enum.RANGE_MASKS_FLOOR == 4096
        assert rust_enum.RANGE_MASKS_CAP == 2**20

    def test_shadow_constants(self) -> None:
        assert rust_enum.SWEEP_ENUM_SELF_CHECK_MASKS == 2**16
        assert rust_enum.SWEEP_ENUM_SELF_CHECK_RANGES_CAP == 256

    def test_conflict_pairs_cross_as_sorted_data(self) -> None:
        assert tuple(sorted(CONFLICT_PAIRS)) == rust_enum._CONFLICT_PAIRS_SORTED
        assert len(rust_enum._CONFLICT_PAIRS_SORTED) == len(CONFLICT_PAIRS)

    def test_result_dataclass_is_frozen_and_derives_evaluated(self) -> None:
        result = RustEnumResult(kept=[3], enumerated=10, conflict_pruned=4, hits_counted=6)
        assert result.evaluated == 6
        with pytest.raises(AttributeError):
            result.enumerated = 0  # type: ignore[misc]


class TestWorkflowWiring:
    """The CI/mutation wiring must name this module (drift pins)."""

    _ROOT = Path(__file__).resolve().parents[2]

    def test_dedicated_100pct_coverage_gate_wired(self) -> None:
        ci = (self._ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "--cov=brutex.engine.rust_enum" in ci
        assert "tests/unit/test_engine_rust_enum.py" in ci

    def test_paired_in_the_mutmut_loop(self) -> None:
        wf = (self._ROOT / ".github/workflows/mutation.yml").read_text(encoding="utf-8")
        assert "brutex/engine/rust_enum.py:tests/unit/test_engine_rust_enum.py:" in wf

    def test_rust_kernel_in_the_cargo_mutants_scope(self) -> None:
        toml = (self._ROOT / "rust/fno-math/.cargo/mutants.toml").read_text(encoding="utf-8")
        assert "src/enum_kernel.rs" in toml
