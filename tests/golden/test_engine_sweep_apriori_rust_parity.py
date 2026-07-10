"""Bit-for-bit parity: the native-Rust Apriori arm vs the FROZEN Python oracle.

DEC-SWEEP-APRIORI-RUST-001 (Slice-1b). The native-Rust frontier kernel
(:func:`brutex.engine.sweep_apriori_rust.generate_apriori_candidates_rust` →
``fno_math.apriori_frontier_u64``) must be **bit-for-bit identical** to the
FROZEN Python generator
:func:`brutex.engine.sweep_apriori.generate_apriori_candidates`
(DEC-SWEEP-APRIORI-TUPLE-001) — same pruned survivor set, same lexicographic-by-
position emission order, same per-level census, same deterministic breaker trip
(decision AND reported count), same typed errors + exact messages.

These goldens drive the Rust arm and the frozen generator over the SAME inputs
and assert equality of the returned list / raised exception / emitted census —
across the oracle suites' fixtures plus fresh hostile pools (sparse interleaved
bits, bit 63, k=1..6, the MIN_TRADES boundary at exactly 30/29).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import pytest
from brutex.engine.combo_mask import count_supermasks
from brutex.engine.sweep_apriori import (
    MAX_MASK_BARS_ENV,
    SWEEP_APRIORI_ENV,
    generate_apriori_candidates,
)
from brutex.engine.sweep_apriori_rust import (
    SWEEP_RUST_APRIORI_ENV,
    generate_apriori_candidates_rust,
)
from brutex.engine.sweep_run import (
    SweepAprioriBudgetError,
    SweepAprioriUnsupportedError,
)

pytest.importorskip("fno_math")

_BITS = [0, 1, 2, 3, 4, 5, 6, 7]


def _synthetic_bar_bits(seed: int, n: int = 500, width: int = 8) -> list[int]:
    rng = np.random.default_rng(seed)
    return [int(x) for x in rng.integers(0, 1 << width, size=n)]


def _both(
    bar_bits: Sequence[int],
    *,
    condition_bits: Sequence[int],
    k: int,
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]] | None = None,
    n_bars: int | None = None,
    root_mod: tuple[int, int] = (0, 1),
    max_mask_bars: int | None = None,
    cell_label: str = "",
) -> tuple[list[int], list[int]]:
    """Return ``(frozen_python, native_rust)`` outputs for the SAME inputs."""
    return (
        generate_apriori_candidates(
            bar_bits,
            condition_bits=condition_bits,
            k=k,
            min_hits=min_hits,
            count_hits=count_hits,
            n_bars=n_bars,
            root_mod=root_mod,
            max_mask_bars=max_mask_bars,
            cell_label=cell_label,
        ),
        generate_apriori_candidates_rust(
            bar_bits,
            condition_bits=condition_bits,
            k=k,
            min_hits=min_hits,
            count_hits=count_hits,
            n_bars=n_bars,
            root_mod=root_mod,
            max_mask_bars=max_mask_bars,
            cell_label=cell_label,
        ),
    )


# ───────────────────── (a) survivor-set + emission-order parity ─────────────────────


@pytest.mark.parametrize("seed", [1, 7, 99])
@pytest.mark.parametrize("min_hits", [0, 100, 250, 400])
def test_full_list_parity_across_oracle_matrix(seed: int, min_hits: int) -> None:
    """The oracle-suite matrix: the WHOLE ordered candidate list is byte-equal."""
    bar_bits = _synthetic_bar_bits(seed)
    for k in range(len(_BITS) + 1):
        py, rs = _both(bar_bits, condition_bits=_BITS, k=k, min_hits=min_hits, n_bars=len(bar_bits))
        assert rs == py, f"seed={seed} min_hits={min_hits} k={k}"


@pytest.mark.parametrize("seed", [3, 5])
def test_parity_with_injected_production_counter(seed: int) -> None:
    """The production seam injects ``_count_hits`` (== ``count_supermasks``); the
    Rust arm counts natively (bit-identical). Both paths must still agree."""
    bar_bits = _synthetic_bar_bits(seed)

    def injected(masks: Sequence[int]) -> list[int]:
        return count_supermasks(bar_bits, masks)

    for k in range(len(_BITS) + 1):
        py, rs = _both(
            bar_bits,
            condition_bits=_BITS,
            k=k,
            min_hits=180,
            count_hits=injected,
            n_bars=len(bar_bits),
        )
        assert rs == py, f"seed={seed} k={k}"


def test_lex_order_differs_from_int_order_matches_frozen() -> None:
    # Interleaved bits (0,3)->9 sorts before (1,2)->6 lexicographically. Both
    # paths must emit the SAME lex-by-position order (NOT ascending-by-int).
    bar_bits = [0b1111] * 40
    py, rs = _both(bar_bits, condition_bits=[0, 1, 2, 3], k=2, min_hits=0)
    assert rs == py == [5, 9, 6, 10]
    assert rs != sorted(rs)


def test_hostile_sparse_interleaved_and_bit63() -> None:
    """Sparse interleaved palette incl. the top bit 63 — the refuter pool."""
    bits = [2, 5, 11, 40, 63]
    hi = 0
    for b in bits:
        hi |= 1 << b
    bar_bits = [hi] * 20
    for k in range(len(bits) + 1):
        py, rs = _both(bar_bits, condition_bits=bits, k=k, min_hits=0, n_bars=len(bar_bits))
        assert rs == py, f"bit-63 interleaved k={k}"


@pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6])
def test_parity_over_k_1_to_6(k: int) -> None:
    """k = 1..6 over a 7-bit palette (some conflict pairs, some free)."""
    bar_bits = _synthetic_bar_bits(11, n=300, width=7)
    bits = [0, 1, 2, 3, 5, 6, 7]  # includes conflict pairs (0,1)/(2,3)
    for min_hits in (0, 50, 150):
        py, rs = _both(bar_bits, condition_bits=bits, k=k, min_hits=min_hits, n_bars=len(bar_bits))
        assert rs == py, f"k={k} min_hits={min_hits}"


def test_min_trades_boundary_exactly_30_survives_29_drops() -> None:
    # Bits 6,7 fire on EXACTLY 30 bars; the k=2 pair survives at min_hits<=30,
    # drops at 31 — on BOTH paths, byte-equal.
    pair = (1 << 6) | (1 << 7)
    bar_bits = [pair] * 30
    for min_hits in (29, 30, 31):
        py, rs = _both(bar_bits, condition_bits=[6, 7], k=2, min_hits=min_hits)
        assert rs == py, f"min_hits={min_hits}"
    # Concretely: 30 survives, 31 drops.
    assert generate_apriori_candidates_rust(bar_bits, condition_bits=[6, 7], k=2, min_hits=30) == [
        pair
    ]
    assert generate_apriori_candidates_rust(bar_bits, condition_bits=[6, 7], k=2, min_hits=31) == []


@pytest.mark.parametrize("big_w", [1, 2, 3, 7])
@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_shard_parity(big_w: int, k: int) -> None:
    bar_bits = _synthetic_bar_bits(11)
    for w in range(big_w):
        py, rs = _both(
            bar_bits,
            condition_bits=_BITS,
            k=k,
            min_hits=0,
            n_bars=len(bar_bits),
            root_mod=(w, big_w),
        )
        assert rs == py, f"shard ({w},{big_w}) k={k}"


# ───────────────────── (b) breaker fixtures ─────────────────────


def test_breaker_k2_trips_at_level_2_not_level_1() -> None:
    # k>=2 tiny budget: level-1 |C_1| is deliberately UNCHECKED, so the trip
    # fires at level 2 (|C_2|), NOT level 1 — identical message on both paths.
    bar_bits = [0b1111_1111] * 20
    with pytest.raises(SweepAprioriBudgetError) as py_ei:
        generate_apriori_candidates(
            bar_bits, condition_bits=[0, 2, 4, 6, 7], k=2, min_hits=0, n_bars=20, max_mask_bars=1
        )
    with pytest.raises(SweepAprioriBudgetError) as rs_ei:
        generate_apriori_candidates_rust(
            bar_bits, condition_bits=[0, 2, 4, 6, 7], k=2, min_hits=0, n_bars=20, max_mask_bars=1
        )
    assert str(rs_ei.value) == str(py_ei.value)
    assert "k=2" in str(rs_ei.value)  # the target level named, not k=1


def test_breaker_k1_tiny_budget_trips_at_level_1() -> None:
    # k==1 IS the target level → its |C_1| is budget-checked; tiny budget trips.
    bar_bits = [0] * 8
    with pytest.raises(SweepAprioriBudgetError) as py_ei:
        generate_apriori_candidates(
            bar_bits, condition_bits=list(range(8)), k=1, min_hits=0, n_bars=8, max_mask_bars=5
        )
    with pytest.raises(SweepAprioriBudgetError) as rs_ei:
        generate_apriori_candidates_rust(
            bar_bits, condition_bits=list(range(8)), k=1, min_hits=0, n_bars=8, max_mask_bars=5
        )
    assert str(rs_ei.value) == str(py_ei.value)
    assert "k=1" in str(rs_ei.value)


def test_breaker_exact_boundary_does_not_trip_on_either_path() -> None:
    # count * n_bars == max_mask_bars EXACTLY → ALLOWED (strict >), both paths
    # return the identical list.
    bar_bits = [(1 << 6) | (1 << 7)] * 10
    py, rs = _both(bar_bits, condition_bits=[6, 7], k=1, min_hits=0, n_bars=10, max_mask_bars=20)
    assert rs == py == [1 << 6, 1 << 7]


def test_breaker_message_byte_equal_default_label() -> None:
    # The full breaker message (default '' cell_label) is byte-equal to frozen.
    bar_bits = [0] * 10
    with pytest.raises(SweepAprioriBudgetError) as py_ei:
        generate_apriori_candidates(
            bar_bits, condition_bits=list(range(8)), k=1, min_hits=0, n_bars=10, max_mask_bars=79
        )
    with pytest.raises(SweepAprioriBudgetError) as rs_ei:
        generate_apriori_candidates_rust(
            bar_bits, condition_bits=list(range(8)), k=1, min_hits=0, n_bars=10, max_mask_bars=79
        )
    assert str(rs_ei.value) == str(py_ei.value)


@pytest.mark.parametrize("stride_env", [None, "1", "2", "100"])
def test_breaker_strided_trip_count_matches_frozen(
    monkeypatch: pytest.MonkeyPatch, stride_env: str | None
) -> None:
    """The reported trip COUNT (running strided count / |C_j|) is byte-equal to
    the frozen oracle across strides — including the production stride."""
    import brutex.engine.sweep_apriori as _apriori

    if stride_env is not None:
        monkeypatch.setattr(_apriori, "_BUDGET_CHECK_STRIDE", int(stride_env))
    # A dense k=2 cell producing many candidates over a tiny budget.
    bar_bits = [0b11_1111_1111] * 20
    bits = list(range(10))
    with pytest.raises(SweepAprioriBudgetError) as py_ei:
        generate_apriori_candidates(
            bar_bits, condition_bits=bits, k=2, min_hits=0, n_bars=20, max_mask_bars=1
        )
    with pytest.raises(SweepAprioriBudgetError) as rs_ei:
        generate_apriori_candidates_rust(
            bar_bits, condition_bits=bits, k=2, min_hits=0, n_bars=20, max_mask_bars=1
        )
    assert str(rs_ei.value) == str(py_ei.value)


# ───────────────────── (c) bits >= 64 loud-error golden ─────────────────────


def test_bits_at_or_above_64_raises_identical_unsupported() -> None:
    with pytest.raises(SweepAprioriUnsupportedError) as py_ei:
        generate_apriori_candidates([1], condition_bits=[0, 64], k=1, min_hits=0)
    with pytest.raises(SweepAprioriUnsupportedError) as rs_ei:
        generate_apriori_candidates_rust([1], condition_bits=[0, 64], k=1, min_hits=0)
    assert str(rs_ei.value) == str(py_ei.value)
    assert "vocab bit 64 >= 64" in str(rs_ei.value)
    assert SWEEP_APRIORI_ENV in str(rs_ei.value)


# ───────────────────── (e) validation ValueError parity ─────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"condition_bits": [-1, 3], "k": 1, "min_hits": 0},  # negative bit
        {"condition_bits": [0, 1, 2], "k": 4, "min_hits": 0},  # k out of range
        {"condition_bits": _BITS, "k": 1, "min_hits": 0, "root_mod": (3, 2)},  # bad root_mod
        {"condition_bits": _BITS, "k": 1, "min_hits": 0, "n_bars": -1},  # negative n_bars
        {"condition_bits": _BITS, "k": 1, "min_hits": 0, "max_mask_bars": -5},  # bad cap
    ],
)
def test_validation_valueerror_messages_byte_equal(kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError) as py_ei:
        generate_apriori_candidates([1], **kwargs)
    with pytest.raises(ValueError) as rs_ei:
        generate_apriori_candidates_rust([1], **kwargs)
    assert str(rs_ei.value) == str(py_ei.value)
    assert type(rs_ei.value) is type(py_ei.value)


# ───────────────────── (f) census parity ─────────────────────


def _capture_census(
    fn: Callable[..., object], bar_bits: Sequence[int], **kwargs: object
) -> list[Any]:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        fn(bar_bits, **kwargs)
    return [e for e in logs if e["event"] == "sweep.apriori.census"]


def test_census_events_byte_equal_success() -> None:
    bar_bits = _synthetic_bar_bits(7, n=200)
    kwargs = {
        "condition_bits": [6, 7, 8],
        "k": 3,
        "min_hits": 5,
        "n_bars": 200,
        "cell_label": "NIFTY:1m",
    }
    py = _capture_census(generate_apriori_candidates, bar_bits, **kwargs)
    rs = _capture_census(generate_apriori_candidates_rust, bar_bits, **kwargs)
    assert rs == py
    assert rs, "census must fire on a clean k=3 build"


def test_census_events_byte_equal_on_trip() -> None:
    # A trip at level 2: level-1 census emitted on both paths, level-2 NOT.
    bar_bits = [0b1111_1111] * 20
    kwargs = {
        "condition_bits": [0, 2, 4, 6, 7],
        "k": 2,
        "min_hits": 0,
        "n_bars": 20,
        "max_mask_bars": 1,
        "cell_label": "c",
    }

    def _run(fn: Callable[..., object]) -> list[Any]:
        from structlog.testing import capture_logs

        with capture_logs() as logs, pytest.raises(SweepAprioriBudgetError):
            fn(bar_bits, **kwargs)
        return [e for e in logs if e["event"] == "sweep.apriori.census"]

    assert _run(generate_apriori_candidates_rust) == _run(generate_apriori_candidates)


# ───────────────────── honest u64 envelope: big-int delegation ─────────────────────


def test_beyond_u64_envelope_delegates_to_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    # max_mask_bars > 2**64 - 1 → the seam delegates to the frozen big-int
    # generator (never touches the kernel); output byte-equal to frozen.
    import fno_math

    called = {"n": 0}
    real = fno_math.apriori_frontier_u64

    def spy(*a, **k):  # type: ignore[no-untyped-def]
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(fno_math, "apriori_frontier_u64", spy)
    bar_bits = _synthetic_bar_bits(1, n=50)
    huge = (1 << 64) + 5  # beyond the u64 envelope
    py = generate_apriori_candidates(
        bar_bits, condition_bits=_BITS, k=2, min_hits=0, max_mask_bars=huge
    )
    rs = generate_apriori_candidates_rust(
        bar_bits, condition_bits=_BITS, k=2, min_hits=0, max_mask_bars=huge
    )
    assert rs == py
    assert called["n"] == 0  # kernel never invoked beyond the envelope


# ───────────────────── the arm env flag ─────────────────────


def test_arm_flag_names() -> None:
    assert SWEEP_RUST_APRIORI_ENV == "BRUTEX_SWEEP_RUST_APRIORI"
    assert MAX_MASK_BARS_ENV == "BRUTEX_SWEEP_APRIORI_MAX_MASK_BARS"
