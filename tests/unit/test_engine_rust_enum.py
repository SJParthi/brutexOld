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

import os
import re
import subprocess
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
    """Fresh flag/env/shadow/latch/memo state per test (process-global cells).

    Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) an unset flag reveals the ON
    default: ON-arm tests use ``@pytest.mark.usefixtures("_on")`` and the OFF
    arm explicitly arms ``"0"`` — never the unset default."""
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


def test_flag_unset_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-ON is the ONE flip constant (DEC-RUST-DEFAULT-ON-WAVE-001, #2215):
    the kernel is ENGAGED by default. delenv removes the conftest belt."""
    monkeypatch.delenv(rust_enum.SWEEP_RUST_ENUM_ENV, raising=False)
    assert rust_enum.SWEEP_RUST_ENUM_DEFAULT is True
    assert rust_enum.sweep_rust_enum_enabled() is True


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
    for minutes; a degenerate 1 clamps UP to 2^16 — which bounds the
    one-mask-per-call stream only for windows up to ~2^16 bars (the honest
    deep-window residual is pinned by TestLowerClampDeepWindowEnvelope)."""
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


class TestLowerClampDeepWindowEnvelope:
    """Review-round-3 honesty pin (DEC-SWEEP-ENUM-RUST-001, #1738): the lower
    work-cells clamp does NOT rule out the one-mask-per-call stream for a
    window DEEPER than the clamp (the min-hits-blind charging rule bills
    n_bars cells per mask and is checked AFTER each mask), so the written
    disclosure must be scoped — and the degenerate stream itself must stay
    correctness-invariant (extra FFI round-trips only, never a wrong list).
    """

    def test_disclosure_is_scoped_never_an_absolute_guarantee(self) -> None:
        """Doc-pin: the false absolute 'can never produce a one-mask-per-call
        stream' claim stays gone; the honest scoped wording stays present."""
        source = Path(rust_enum.__file__).read_text(encoding="utf-8")
        flat = " ".join(source.split())
        assert re.search(r"can never[^.]*one-mask-per-call", flat) is None
        assert "only for windows up to ~2^16 bars" in flat
        assert "one mask per range" in flat

    def test_deep_window_min_knob_streams_one_mask_per_call_cap_invariant(self) -> None:
        """A window deeper than the clamp at the minimum knob enumerates
        exactly ONE mask per kernel call — and walking that degenerate stream
        to exhaustion yields the SAME kept list, SET AND ORDER, as one
        default-cap call (the disclosure's correctness-invariance claim)."""
        clamp = rust_enum.ENUM_WORK_CELLS_LOWER_CLAMP
        deep_bars = [0b1111] * (clamp + 4096)
        core = _REAL_U64(deep_bars)
        bits = list(range(7))
        first = core.enumerate_prefilter(bits, 2, [], 0, 0, 4096, clamp)
        assert first[0] == 1  # the disclosed one-mask-per-call shape
        assert first[3] is False
        kept: list[int] = []
        rank = 0
        for _ in range(64):
            enumerated, _pruned, rank, exhausted, _hits, batch = core.enumerate_prefilter(
                bits, 2, [], 0, rank, 4096, clamp
            )
            assert enumerated <= 1
            kept.extend(batch)
            if exhausted:
                break
        else:  # pragma: no cover - loud failure shape only
            pytest.fail("min-knob stream never exhausted")
        full = core.enumerate_prefilter(bits, 2, [], 0, 0, 4096, rust_enum.DEFAULT_ENUM_WORK_CELLS)
        assert full[3] is True
        assert kept == list(full[5])


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
    # The 74-bit u128 vocabulary (DEC-VOCAB-U128-CODEGEN-001) carries conflict
    # pairs with bits >= 64 that the u64 monomorphisation structurally rejects
    # (``require a < b < 64``). This helper exercises the u64 kernel over
    # sub-64 tuples, so it feeds only the u64-addressable pairs — the exact
    # mirror of the production ``enum_prefilter_via_kernel`` width-bound filter
    # (semantically identical: a pair with a bit >= 64 can never be co-set by a
    # mask over these bits).
    u64_pairs = [(a, b) for (a, b) in sorted(CONFLICT_PAIRS) if b < 64]
    raw = core.enumerate_prefilter(unique, k, u64_pairs, min_hits, 0, 2**63, 2**63)
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
    """Flag OFF (the explicit kill switch) — no warn, no kernel resolve. Since
    DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the default is ON, so OFF is armed
    explicitly ``"0"`` (an unset flag would engage the kernel)."""
    monkeypatch.setenv(rust_enum.SWEEP_RUST_ENUM_ENV, "0")

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
def test_bar_high_bits_outside_the_tuple_are_projected_away_exactly() -> None:
    """DEC-VOCAB-U128-CODEGEN-001 projection: with a 74-bit vocab a per-bar
    condition bitmask routinely sets bits >= 64 even when the tuple's condition
    bits are all < 64. Those high bits sit OUTSIDE the tuple's ``allowed_mask``,
    so the seam projects each bar onto ``allowed_mask`` before the u64 kernel —
    ``mask & bar == mask & (bar & allowed_mask)`` is a bit-identity for every
    ``mask <= allowed_mask``, so the kernel serves the EXACT Python-oracle result
    (no fallback), and the u64 fast path survives the wider vocab."""
    bar = (1 << 64) | 0b101  # bit 64 (outside the tuple) + bits 0 and 2 (inside)
    with capture_logs() as logs:
        result = _call([0, 1, 2], 2, min_hits=1, bar_bits=[bar])
    # No degrade: the projection made the u64 core hold a valid value.
    fallbacks = [e for e in logs if e["event"] == "sweep_rust_enum.fallback"]
    assert fallbacks == []
    assert result is not None
    # Bit-identical to the oracle, which ANDs each mask against the FULL bar.
    assert result.kept == _oracle_kept([0, 1, 2], 2, 1)


@pytest.mark.usefixtures("_on")
def test_malformed_bar_projection_degrades_to_the_python_loop() -> None:
    """A malformed bar (a value whose ``&`` refuses) degrades to the Python loop
    exactly like a PyO3 conversion failure — never an uncaught escape."""
    with capture_logs() as logs:
        assert _call([0, 1, 2], 2, bar_bits=[object()]) is None  # type: ignore[list-item]
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

    def test_default_is_true(self) -> None:
        assert rust_enum.SWEEP_RUST_ENUM_DEFAULT is True

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

    def test_twin_run_dispatch_inputs_are_consumed(self) -> None:
        """Round-1 review MINOR: every declared workflow_dispatch input must be
        wired into the job — a dead input silently ignores the operator's
        window override on the real-lake byte-identity merge gate.
        """
        wf = (self._ROOT / ".github/workflows/rust-enum-twin-run.yml").read_text(encoding="utf-8")
        declared = re.findall(r"^      (\w+):\s*$", wf, flags=re.MULTILINE)
        assert set(declared) >= {"twin_start", "twin_end"}
        for name in declared:
            assert f"${{{{ inputs.{name} }}}}" in wf, f"dispatch input {name!r} is never consumed"
        # ...and they must land on the exact env names the script's own
        # ${TWIN_*:-default} readers consume (rust_enum_twin_run.sh).
        assert "TWIN_START: ${{ inputs.twin_start }}" in wf
        assert "TWIN_END: ${{ inputs.twin_end }}" in wf

    def test_fno_math_shard_dial_narrated_against_mains_6_shard_baseline(self) -> None:
        """Round-5 review MINOR: the shipped fno-math shard dial vs main is
        6 -> 8 — fno-math was NEVER 7 shards on main (the 7-shard matrix is
        the SEPARATE trading-core job's); the first-published "7->8 taken
        AGAIN" comment was a merge-reconcile artifact from the branch's
        pre-rework local 7-shard config. The committed comment AND the
        ledger must narrate the real baseline (Golden Rule #10 — the
        recorded rationale must match the shipped diff).

        Round-6 review MINOR: the round-5 correction was INCOMPLETE — it
        missed the THIRD narrative site, ``rust/fno-math/.cargo/mutants.toml``
        (the primary scope-config file), which kept the phantom
        "ceil(803/7) / 6->7 dial / 7 -> 8" narration verbatim while this pin
        read only mutation.yml + MIGRATION.md, so CI stayed green with two
        committed files disagreeing about the same dial. The pin now reads
        mutants.toml too and refuses the phantom tokens there (spaced AND
        unspaced forms), so the third site can never re-drift.
        """
        wf = (self._ROOT / ".github/workflows/mutation.yml").read_text(encoding="utf-8")
        # The applied dial itself (unchanged by this correction).
        assert "--shard ${{ matrix.shard }}/8" in wf
        assert "ceil(803/8)=101" in wf
        # The honest baseline narration + the real 6-shard per-shard figure.
        assert "6->8 vs main's" in wf
        assert "ceil(803/6)=134" in wf
        # The phantom 7-shard fno-math waypoints must never return.
        assert "7->8" not in wf
        assert "ceil(803/7)" not in wf
        # Round 6: the scope-config file's count block must carry the SAME
        # 6-shard-baseline narrative — never the phantom waypoints.
        toml = (self._ROOT / "rust/fno-math/.cargo/mutants.toml").read_text(encoding="utf-8")
        assert "6 -> 8 vs main's" in toml
        assert "ceil(803/6) = 134" in toml
        assert "ceil(803/8) = 101" in toml
        for phantom in ("ceil(803/7)", "6->7", "6 -> 7", "7->8", "7 -> 8"):
            assert phantom not in toml, f"phantom shard-dial token {phantom!r} in mutants.toml"
        # ...and the ledger entry #1738 must carry the same correction.
        ledger = (self._ROOT / "docs/MIGRATION.md").read_text(encoding="utf-8")
        entry = re.search(r"^1738\. .*$", ledger, flags=re.MULTILINE)
        assert entry is not None, "MIGRATION.md entry #1738 not found"
        assert "6 → 8" in entry.group(0)
        assert "7 → 8 shards" not in entry.group(0)

    def test_twin_run_actions_are_sha_pinned(self) -> None:
        """Round-5 review MINOR: every ``uses:`` in the twin-run workflow must
        be SHA-pinned (the repo convention — 63/33/19 pinned sibling refs).

        This job runs on the self-hosted M4 Pro with the REAL production lake
        mounted; a mutable tag (``@v7``) re-point — or a compromised
        first-party tag — would execute attacker-controlled action code on
        the most sensitive runner in the repo, exactly the exposure the
        SHA-pin convention exists to close. No zizmor/actionlint SHA-pin
        gate exists, so this pin is the teeth.
        """
        wf = (self._ROOT / ".github/workflows/rust-enum-twin-run.yml").read_text(encoding="utf-8")
        refs = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", wf, flags=re.MULTILINE)
        assert refs, "no `uses:` refs found — the workflow shape moved, repin"
        for ref in refs:
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref), (
                f"action ref {ref!r} is not SHA-pinned (40-hex) — the twin-run "
                "job runs self-hosted with the real lake mounted; pin it like "
                "the sibling workflows (`@<sha> # vN`)"
            )

    def test_coverage_gate_comment_carries_no_stale_size_figure(self) -> None:
        """Round-1 review NIT: the pre-rework '151 stmts, 66 branches' figure
        must never return — the gate enforces ``--cov-fail-under=100``; a
        frozen size count in a comment only drifts.
        """
        ci = (self._ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "151 stmts, 66 branches" not in ci

    def test_work_cells_comment_labels_0257_carried_not_measured(self) -> None:
        """Round-2 review NIT: 0.257 ns/(mask·bar) is the design pack's CARRIED
        batch-FFI ``count_supermasks`` constant (combo_counts.md §3.2), never a
        measurement of this branch — the branch's measured fused rates are
        0.077/0.125. The work-cells knob comment must keep that provenance
        (Golden Rule #10: measured-vs-carried is exactly the distinction the
        honesty discipline turns on).
        """
        src = (self._ROOT / "brutex/engine/rust_enum.py").read_text(encoding="utf-8")
        assert "0.257" in src, "the work-cells sizing rationale went missing — repin"
        assert "measured 0.257" not in src
        assert "carried 0.257" in src


class TestTwinRunEngagementGate:
    """Round-2 review MINOR: the real-data twin-run MERGE GATE must refuse a
    vacuous/masked PASS.

    Byte-identity alone is trivially true whenever the ON arm silently served
    the Python spine — a stale/absent wheel falls back with one warn-once
    (``wheel_missing``/``wheel_stale``), and a shadow-burn-in mismatch SHIPS
    THE PYTHON RESULT then latches the width off (``kernel_autoreverted``) —
    so the pre-fix ``cmp``-only verdict could print PASS while proving nothing
    about the kernel (or while the kernel had a REAL caught bug). These tests
    drive the actual script with a stub CLI that writes byte-identical
    artifacts on both arms and emits controlled kernel events on the ON arm.
    """

    _ROOT = Path(__file__).resolve().parents[2]
    _SCRIPT = _ROOT / "scripts/ci/rust_enum_twin_run.sh"

    #: The stub "python" the script invokes as ``$PYTHON -m brutex sweep-all``:
    #: writes ONE identical artifact under --results-root on both arms, and on
    #: the ON arm prints $STUB_ON_ARM_LOG to stderr (where structlog writes).
    _STUB = """#!/usr/bin/env bash
results=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--results-root" ]; then results="$a"; fi
  prev="$a"
done
mkdir -p "$results"
printf 'twin-artifact\\n' > "$results/store.parquet"
if [ "${BRUTEX_SWEEP_RUST_ENUM:-}" = "1" ] && [ -n "${STUB_ON_ARM_LOG:-}" ]; then
  printf '%s\\n' "$STUB_ON_ARM_LOG" >&2
fi
exit 0
"""

    def _run(self, tmp_path: Path, on_arm_log: str) -> subprocess.CompletedProcess[str]:
        stub = tmp_path / "stub-python"
        stub.write_text(self._STUB, encoding="utf-8")
        stub.chmod(0o755)
        lake = tmp_path / "lake"
        lake.mkdir()
        env = dict(os.environ)
        env.pop("BRUTEX_SWEEP_RUST_ENUM", None)  # the script owns both arms
        env["TWIN_PYTHON"] = str(stub)
        env["STUB_ON_ARM_LOG"] = on_arm_log
        env["TMPDIR"] = str(tmp_path)  # scratch stays inside the test tmp dir
        # cwd=tmp_path so the script's relative .venv/bin/python probe misses
        # and TWIN_PYTHON is honoured.
        return subprocess.run(  # fixed repo script; stub inputs only
            ["bash", str(self._SCRIPT), str(lake)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def test_pass_requires_engaged_and_clean_markers(self, tmp_path: Path) -> None:
        """The healthy arm — engaged logged, no markers — still PASSes."""
        proc = self._run(tmp_path, "2026-07-12 [info] sweep_rust_enum.engaged width=u64")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "TWIN-RUN PASS" in proc.stdout
        assert "engagement gate" in proc.stdout

    def test_vacuous_pass_refused_when_kernel_never_engaged(self, tmp_path: Path) -> None:
        """FAILS-BEFORE: identical bytes + zero engaged event (the stale/absent
        wheel shape — every tuple Python-served) must be a loud FAIL, never a
        PASS that proves nothing.
        """
        proc = self._run(tmp_path, "")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "TWIN-RUN PASS" not in proc.stdout
        assert "never logged sweep_rust_enum.engaged" in proc.stderr

    def test_masked_autorevert_refused_even_with_identical_bytes(self, tmp_path: Path) -> None:
        """FAILS-BEFORE: a shadow-burn-in mismatch ships the Python result
        (bytes match!) and logs kernel_autoreverted — a REAL kernel bug the
        pre-fix cmp-only gate could never fail on.
        """
        proc = self._run(
            tmp_path,
            "sweep_rust_enum.engaged width=u64\n"
            "sweep_rust_enum.kernel_autoreverted width=u64 rank=0",
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "TWIN-RUN PASS" not in proc.stdout
        assert "fallback/divergence marker" in proc.stderr
        assert "kernel_autoreverted" in proc.stderr

    def test_wheel_fallback_reason_refused(self, tmp_path: Path) -> None:
        """A warn-once ``sweep_rust_enum.fallback`` (reason=wheel_stale et al.)
        alongside a later engaged event still fails the marker gate — those
        tuples were Python-served, so the compare is partially masked.
        """
        proc = self._run(
            tmp_path,
            "sweep_rust_enum.fallback reason=wheel_stale missing=ResidentSweepEnum\n"
            "sweep_rust_enum.engaged width=u64",
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "fallback/divergence marker" in proc.stderr
        assert "wheel_stale" in proc.stderr

    def test_advisory_shadow_floor_event_does_not_trip_the_gate(self, tmp_path: Path) -> None:
        """``enum_shadow_floor_unmet`` is ADVISORY (a small k=2 tuple cannot
        reach the 2^16-mask shadow floor) and fires on the pinned sub-grid —
        it must never fail the engagement gate.
        """
        proc = self._run(
            tmp_path,
            "sweep_rust_enum.enum_shadow_floor_unmet width=u64 shadowed=1770\n"
            "sweep_rust_enum.engaged width=u64",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "TWIN-RUN PASS" in proc.stdout
