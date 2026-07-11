"""Hypothesis ON≡OFF equivalence for the Rust CPCV fold kernel (slice 6).

DEC-METRICS-RUST-STATS-001 PR-1. Drives adversarial returns series through
:func:`brutex.metrics.cpcv.cpcv_pass_rate` with ``BRUTEX_STATS_RUST=0`` then
``=1`` and asserts:

* the returned RATE is BIT-equal (``struct.pack('<d', …)`` — never approx;
  the rate is ``repr``-serialized into cascade-report bytes downstream);
* the integer ``n_pass`` is EXACTLY equal at the seam level (the
  count-exactness law — a single fold flip moves a cascade verdict at the
  0.5 threshold);
* the ON arm genuinely ENGAGED the Rust kernel (anti-vacuous-pass law: CI
  always builds the wheel via ``pip install ./rust/fno-math``; a wheel-free
  environment fails HERE loudly — install the wheel — never passes
  vacuously);
* no burn-in autorevert fired (bit-equality alone cannot see a burn-in
  mismatch — the seam ships the oracle value on mismatch by design, so the
  latch state is the observable).

Adversarial input classes: near-zero-mean folds (the sign of the fold mean
IS the pass verdict at ``min_sharpe=0``), exact-constant series (zero
variance → undefined folds), 1e300-scale overflow (the inf/NaN fail-safe
must flow IEEE-identically), and the tiny ``n == n_groups`` boundary
(singleton groups). ``min_sharpe`` is drawn from floats ∪ UNBOUNDED ints —
including non-float-representable ints past 2^53 (the refuter-round-1
exactness-leak class) and ints past 2^1024 whose ``float()`` raises
OverflowError (the refuter-round-2 crash class) — the seam's exact-type
domain gate refuses both to the oracle — and ±10^5000, past CPython's
int→str digit limit as well (the refuter-round-3 refusal-context crash
class: the pre-fix corpus capped at 401 digits). A second hypothesis test
widens the threshold to the EXOTIC type corpus (bools, numpy scalars, an
ordering-reversed float subclass, a hostile-metaclass-``__name__`` foreign
object, >4300-digit ints, Decimals, NaN/inf) under the round-2
invariant: ON≡OFF ALWAYS — a bit-equal value or an IDENTICAL exception
type + message (the oracle serves both arms with the ORIGINAL object's own
semantics). The strict-``>`` boundary at an ACHIEVED fold Sharpe is pinned
deterministically below.
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal
from itertools import combinations
from typing import TYPE_CHECKING

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import (
    _combine_moments,  # pyright: ignore[reportPrivateUsage]
    _fold_pass_count,  # pyright: ignore[reportPrivateUsage]
    _group_bounds,  # pyright: ignore[reportPrivateUsage]
    _group_moments,  # pyright: ignore[reportPrivateUsage]
    cpcv_pass_rate,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

if TYPE_CHECKING:
    import numpy.typing as npt


@st.composite
def _cases(draw: st.DrawFn) -> tuple[list[float], int, int, float]:
    """(returns, n_groups, n_test_groups, min_sharpe) — adversarial classes."""
    s = draw(st.integers(min_value=2, max_value=8))
    k = draw(st.integers(min_value=1, max_value=s - 1))
    # n == s is the singleton-group boundary (count-1 groups → undefined
    # k=1 folds); larger n exercises multi-observation merges.
    n = draw(st.integers(min_value=s, max_value=64))
    kind = draw(st.sampled_from(["general", "near_zero", "constant", "huge"]))
    if kind == "general":
        vals = draw(
            st.lists(
                st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
                min_size=n,
                max_size=n,
            )
        )
    elif kind == "near_zero":
        # Fold means within a few ULP of zero — the sign-flip territory the
        # STRUCTURAL claim must hold on.
        vals = draw(
            st.lists(
                st.floats(min_value=-1e-12, max_value=1e-12, allow_nan=False),
                min_size=n,
                max_size=n,
            )
        )
    elif kind == "constant":
        c = draw(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False))
        vals = [c] * n
    else:
        # 1e300-scale: squared deviations / merge terms overflow to inf —
        # the fail-safe non-pass must be IEEE-identical on both arms.
        vals = draw(
            st.lists(
                st.sampled_from([1e300, -1e300, 2e300, 5e299, 0.0]),
                min_size=n,
                max_size=n,
            )
        )
    # min_sharpe over floats ∪ UNBOUNDED ints ∪ bools (refuter rounds 1+2):
    # the ON≡OFF law must hold for EVERY type-legal threshold. Ints past
    # 2^53 (2^53+3, 2^60+1) are not float-representable and ints past
    # ~2^1024 (10^400, ±2^1200-band draws) make ``float()`` raise
    # OverflowError — the seam's exact-type domain gate refuses BOTH to the
    # oracle (never a rounded FFI compare, never an ON-arm crash);
    # value-exact ints (0, ±1, 2^53) and bools stay kernel-served.
    # ±10^5000 (refuter round 3): past sys.get_int_max_str_digits() too —
    # the refusal's own warn-context repr must be digit-limit-safe (the
    # pre-fix corpus capped at 401 digits and missed the crash).
    min_sharpe: float = draw(
        st.one_of(
            st.sampled_from([0.0, 0.5, -0.5]),
            st.sampled_from([0, 1, -1, 2**53, 2**53 + 3, -(2**53 + 3), 2**60 + 1, 10**400]),
            st.sampled_from([10**5000, -(10**5000)]),
            st.integers(min_value=-(2**60), max_value=2**60),
            st.integers(min_value=2**1024, max_value=2**1200),
            st.integers(min_value=-(2**1200), max_value=-(2**1024)),
            st.booleans(),
        )
    )
    return vals, s, k, min_sharpe


def _kernel_gate_engages(min_sharpe: float) -> bool:
    """Mirror of the seam's exact-TYPE engagement gate (the expected-
    engagement predicate): exact-type ``float`` (non-NaN) or exact-type
    ``int``/``bool`` whose ``float()`` image is value-exact — anything
    else (incl. an OverflowError-raising huge int) is a refusal."""
    # ``type[object]`` mirrors the seam's own widened annotation: the param
    # is ANNOTATED float but the numeric tower delivers ints/bools at
    # runtime — the widening keeps the int/bool arm reachable for mypy.
    ms_type: type[object] = type(min_sharpe)
    if ms_type is float:
        return not math.isnan(min_sharpe)
    if ms_type is int or ms_type is bool:
        try:
            return float(min_sharpe) == min_sharpe
        except OverflowError:
            return False
    return False


def _rate_under_flag(
    flag: str, returns: list[float], s: int, k: int, min_sharpe: float, *, burn_in: bool
) -> tuple[float, bool]:
    """Run ``cpcv_pass_rate`` under *flag*; return (rate, engaged)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rust_stats.STATS_RUST_ENV, flag)
        rust_stats.reset_observability_memo_for_tests()
        remaining = None if burn_in else 0
        rust_stats.reset_self_check_for_tests("cpcv", remaining=remaining)
        with capture_logs() as logs, np.errstate(all="ignore"):
            rate = cpcv_pass_rate(returns, n_groups=s, n_test_groups=k, min_sharpe=min_sharpe)
        engaged = any(e["event"] == "rust_stats.engaged" for e in logs)
        # A burn-in mismatch would ship the oracle value (rates equal!) and
        # LATCH — so the latch state is the mismatch observable.
        assert not rust_stats.kernel_disabled("cpcv"), "burn-in autorevert fired: kernel≠oracle"
    return rate, engaged


@settings(max_examples=150)
@given(case=_cases(), burn_in=st.booleans())
def test_on_off_rate_bit_equal_and_count_exact(
    case: tuple[list[float], int, int, float], burn_in: bool
) -> None:
    returns, s, k, min_sharpe = case
    # The exact-type gate's engagement predicate (refuter rounds 1+2): a
    # value-exact exact-type threshold engages; an inexact int (past 2^53),
    # an OverflowError-raising huge int, or a foreign type is domain-refused
    # to the oracle. (Computing ``float(min_sharpe)`` directly here would
    # itself crash on the huge-int draws — the gate mirror guards it.)
    exact = _kernel_gate_engages(min_sharpe)
    off_rate, off_engaged = _rate_under_flag("0", returns, s, k, min_sharpe, burn_in=burn_in)
    on_rate, on_engaged = _rate_under_flag("1", returns, s, k, min_sharpe, burn_in=burn_in)
    assert not off_engaged
    # Anti-vacuous-pass law wherever the kernel CAN legally engage.
    assert on_engaged == exact
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)

    # Count-exactness at the seam: the integer n_pass, not just the rate.
    with np.errstate(all="ignore"):
        arr = np.asarray(returns, dtype=np.float64)
        count, mean, m2 = _group_moments(arr, _group_bounds(len(returns), s))
        oracle_count = _fold_pass_count(count, mean, m2, s, k, min_sharpe)
        rust_stats.reset_self_check_for_tests("cpcv", remaining=0)
        kernel_count = rust_stats.cpcv_pass_count_via_kernel(
            count,
            mean,
            m2,
            k,
            min_sharpe,
            n_total=math.comb(s, k),
            oracle=lambda: oracle_count,
        )
    if exact:
        assert kernel_count == oracle_count
    else:
        # The domain guard refused the kernel — the CALLER's oracle serves.
        assert kernel_count is None


def test_strict_gt_boundary_at_achieved_fold_sharpe() -> None:
    """``min_sharpe`` set EXACTLY at an achieved fold Sharpe: the strict-``>``
    predicate makes that fold a NON-pass on BOTH arms, bit-equal."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]
    s, k = 4, 2
    arr = np.asarray(returns, dtype=np.float64)
    count, mean, m2 = _group_moments(arr, _group_bounds(len(returns), s))
    # Pick a real achieved fold Sharpe as the threshold.
    achieved = _combine_moments(count, mean, m2, (0, 1))
    assert achieved is not None
    off_rate, _ = _rate_under_flag("0", returns, s, k, achieved, burn_in=True)
    on_rate, on_engaged = _rate_under_flag("1", returns, s, k, achieved, burn_in=True)
    assert on_engaged
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)
    # The threshold fold itself does not pass (strict >) but one ulp below
    # it does — pinned through BOTH arms.
    below = math.nextafter(achieved, -math.inf)
    off_b, _ = _rate_under_flag("0", returns, s, k, below, burn_in=True)
    on_b, _ = _rate_under_flag("1", returns, s, k, below, burn_in=True)
    assert struct.pack("<d", on_b) == struct.pack("<d", off_b)
    assert on_b > on_rate


# ── ULP-boundary discriminators at the RATE level (refuter round 5) ──────
# The pre-round-5 blind spot: `test_strict_gt_boundary_at_achieved_fold_
# sharpe` uses 8 samples / 4 groups → n_b = 2 (a power of 2 — multiply/
# divide by 2^k is exact, so an algebraically-equal fold-op-order swap in
# the KERNEL is bit-invisible there), and a random threshold lands within
# 1 ULP of a fold Sharpe with probability ~1e-16 — a 1-ULP fold-order
# mutant WHEEL passed all 52 pre-round-5 Python-side tests. These twins use
# NON-power-of-2 group counts whose fold arithmetic is proven
# rounding-ACTIVE in-test, with the threshold nextafter-bracketed at an
# achieved fold Sharpe, so the ON arm's steady-state (raw-kernel) rate
# flips against the OFF (oracle) rate on ANY ≥1-ULP kernel fold drift.


def _witness_reorder_fold(
    count: npt.NDArray[np.intp],
    mean: npt.NDArray[np.float64],
    m2: npt.NDArray[np.float64],
    combo: tuple[int, ...],
) -> float | None:
    """The op-order WITNESS (`delta * n_b / combined` → `delta / combined *
    n_b` in the mean update — the mutant that survived the pre-round-5
    battery), used ONLY to prove a fixture's fold arithmetic rounds
    differently under reordering; never an oracle."""
    n_total = 0
    mean_total = 0.0
    m2_total = 0.0
    for gid in combo:
        n_b = int(count[gid])
        mean_b = float(mean[gid])
        m2_b = float(m2[gid])
        combined = n_total + n_b
        delta = mean_b - mean_total
        mean_total += delta / combined * n_b  # the ONE reorder under test
        m2_total += m2_b + delta * delta * n_total * n_b / combined
        n_total = combined
    if n_total < 2:
        return None
    variance = m2_total / n_total
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    return mean_total / math.sqrt(variance)


def _first_rounding_active_fold(
    count: npt.NDArray[np.intp],
    mean: npt.NDArray[np.float64],
    m2: npt.NDArray[np.float64],
    s: int,
    k: int,
) -> float:
    """First lexicographic fold Sharpe the reorder witness MOVES — raises
    when the fixture is not rounding-active (a defanged fixture must fail
    loudly, never pin vacuously)."""
    for combo in combinations(range(s), k):
        s_star = _combine_moments(count, mean, m2, combo)
        if s_star is None:
            continue
        if _witness_reorder_fold(count, mean, m2, combo) != s_star:
            return s_star
    raise AssertionError("fixture is not rounding-active — the ULP bracket would be vacuous")


def _assert_ulp_bracket_on_off_bit_equal(returns: list[float], s: int, k: int) -> None:
    """Drive both arms at (S*, nextafter(S*, -inf)) around an achieved,
    rounding-active fold Sharpe; steady state so the ON arm serves the
    kernel's RAW answer (a burn-in mismatch would ship the oracle value —
    rates equal — and the helper's latch assert catches that instead)."""
    arr = np.asarray(returns, dtype=np.float64)
    count, mean, m2 = _group_moments(arr, _group_bounds(len(returns), s))
    s_star = _first_rounding_active_fold(count, mean, m2, s, k)
    below = math.nextafter(s_star, -math.inf)
    off_at, off_engaged = _rate_under_flag("0", returns, s, k, s_star, burn_in=False)
    on_at, on_engaged = _rate_under_flag("1", returns, s, k, s_star, burn_in=False)
    assert not off_engaged
    assert on_engaged  # never vacuous — the kernel genuinely served
    assert struct.pack("<d", on_at) == struct.pack("<d", off_at)
    off_below, _ = _rate_under_flag("0", returns, s, k, below, burn_in=False)
    on_below, on_below_engaged = _rate_under_flag("1", returns, s, k, below, burn_in=False)
    assert on_below_engaged
    assert struct.pack("<d", on_below) == struct.pack("<d", off_below)
    # The bracket is ACHIEVED: the bracketed fold itself flips between the
    # two thresholds (strict `>` at S* → non-pass; one ULP below → pass).
    assert on_below > on_at


def test_ulp_bracket_non_pow2_group_counts_rate_level() -> None:
    """Non-power-of-2 group sizes (20 samples / 3 groups → 7/7/6) with the
    threshold ULP-bracketed at an achieved fold Sharpe — the deterministic
    rate-level discriminator that kills fold-op-order kernel drift in the
    shipped wheel (fail-against-mutant-wheel proven — MIGRATION.md #1728
    round-5 record)."""
    returns = [
        1.2e-05, 0.002987, -0.002741, -0.008906, -0.004547,
        -0.009916, 0.000601, 0.013402, -0.004922, -0.006205,
        0.004898, 0.003569, 0.001054, -0.009305, -0.000293,
        0.006953, -0.013442, -0.004576, -0.019012, -0.012895,
    ]  # fmt: skip
    _assert_ulp_bracket_on_off_bit_equal(returns, 3, 2)


def test_ulp_bracket_production_shape_non_pow2_counts() -> None:
    """The production-shape twin: S=16, k=8 over 150 samples → group sizes
    10 and 9 (both non-power-of-2, unlike the 512-sample sibling whose
    32-per-group shape is reorder-exact) — the C(16,8)=12,870-fold path
    under a ULP-bracketed threshold."""
    rng = np.random.default_rng(20260711)
    returns = np.round(rng.normal(0.0, 0.01, 150), 6).tolist()
    _assert_ulp_bracket_on_off_bit_equal(returns, 16, 8)


class _ReversedFloat(float):
    """Refuter-round-2 D2 attack class: a type-legal float subclass whose
    ordering is REVERSED while equality stays honest — it defeats any
    value-exactness-only guard, so the exact-type gate must refuse it."""

    def __gt__(self, other: float) -> bool:
        return float.__lt__(self, other)

    def __lt__(self, other: float) -> bool:
        return float.__gt__(self, other)

    def __ge__(self, other: float) -> bool:
        return float.__le__(self, other)

    def __le__(self, other: float) -> bool:
        return float.__ge__(self, other)


class _HostileNameMeta(type):
    """Refuter-round-3 R3-2 attack class: a metaclass whose ``__name__`` is
    a raising property — the foreign refusal's type-name fetch must bypass
    the metaclass MRO (``_type_name``'s C-level getset), never dispatch it."""

    @property
    def __name__(cls) -> str:  # type: ignore[override]  # the deliberate hostile shape under test
        raise RuntimeError("hostile metaclass __name__")


class _HostileNameObject(metaclass=_HostileNameMeta):
    """Foreign threshold under the hostile metaclass; its reflected
    ``__lt__`` answers the oracle's compare on both arms identically."""

    def __lt__(self, other: object) -> bool:
        return True


def _exotic_thresholds() -> st.SearchStrategy[object]:
    """The refuter-round-2 exotic threshold corpus (round-3 widened):
    everything a hostile or
    exotic caller could feed ``min_sharpe`` without tripping the type
    checker's numeric tower (plus outright junk-adjacent numerics)."""
    return st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),  # incl. NaN/±inf
        st.integers(),  # UNBOUNDED — rare astronomically-huge draws included
        st.integers(min_value=2**1024, max_value=2**1300),  # float() overflow
        st.integers(min_value=-(2**1300), max_value=-(2**1024)),
        # Round 3: past the int→str digit limit too — the refusal's own
        # warn-context repr must be total; and the hostile-metaclass
        # foreign shape — the type-NAME fetch must be metaclass-safe.
        st.sampled_from([10**5000, -(10**5000)]),
        st.builds(_HostileNameObject),
        st.booleans(),
        st.sampled_from(
            [
                np.float64(0.5),
                np.float64("nan"),
                np.float32(0.25),
                np.int64(3),
                np.int64(2**60 + 255),
                np.int32(-2),
            ]
        ),
        st.builds(_ReversedFloat, st.floats(min_value=-2.0, max_value=2.0, allow_nan=False)),
        st.decimals(allow_nan=True, allow_infinity=True),
        st.sampled_from([Decimal("sNaN"), Decimal("0.1"), Decimal(2**53 + 3)]),
    )


@settings(max_examples=150)
@given(
    ms=_exotic_thresholds(),
    series_kind=st.sampled_from(["mixed", "constant"]),
    burn_in=st.booleans(),
)
def test_on_off_parity_for_exotic_threshold_types(
    ms: object, series_kind: str, burn_in: bool
) -> None:
    """The refuter-round-2 invariant: ON≡OFF ALWAYS — a bit-equal value or
    an IDENTICAL exception (type + message, raised at the same oracle
    site) — for EVERY threshold in the exotic corpus, burn-in and steady
    state, with no false autorevert. The ``constant`` series makes every
    fold undefined so the comparison is never reached (the
    value-vs-exception shape: garbage thresholds must return 0.0 on BOTH
    arms, not raise on one)."""
    returns = (
        [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]
        if series_kind == "mixed"
        else [0.01] * 8
    )

    def _arm(flag: str) -> tuple[str, ...]:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(rust_stats.STATS_RUST_ENV, flag)
            rust_stats.reset_observability_memo_for_tests()
            rust_stats.reset_self_check_for_tests("cpcv", remaining=None if burn_in else 0)
            try:
                with np.errstate(all="ignore"):
                    rate = cpcv_pass_rate(
                        returns,
                        n_groups=4,
                        n_test_groups=2,
                        min_sharpe=ms,  # type: ignore[arg-type]  # the exotic corpus under test
                    )
            except Exception as exc:  # the parity harness compares arbitrary raises
                outcome: tuple[str, ...] = ("exc", type(exc).__name__, str(exc))
            else:
                outcome = ("value", struct.pack("<d", rate).hex())
            assert not rust_stats.kernel_disabled("cpcv"), "false autorevert fired: kernel≠oracle"
            return outcome

    on_outcome = _arm("1")
    off_outcome = _arm("0")
    assert on_outcome == off_outcome


def test_production_shape_s16_k8_bit_equal() -> None:
    """One full production-shape case (S=16, k=8 → 12,870 folds) through
    both arms, burn-in inclusive."""
    rng = np.random.default_rng(20240603)
    returns = rng.normal(0.0, 0.01, 512).tolist()
    off_rate, _ = _rate_under_flag("0", returns, 16, 8, 0.0, burn_in=True)
    on_rate, on_engaged = _rate_under_flag("1", returns, 16, 8, 0.0, burn_in=True)
    assert on_engaged
    assert struct.pack("<d", on_rate) == struct.pack("<d", off_rate)
