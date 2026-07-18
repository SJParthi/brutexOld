"""Kernel-vs-oracle differential properties for the stats tails.

DEC-METRICS-RUST-STATS-002. Each property drives the PUBLIC module function
under ``BRUTEX_STATS_RUST=0`` (the retained pure-Python oracle) and ``=1``
(the fno_math kernel path) over hostile hypothesis corpora — ±0.0,
subnormal-scale and 1e±300 magnitudes, branch-point neighbourhoods — and
asserts BIT equality (``struct.pack('<d', …)``) of every float output plus
exception parity where the domain raises. Burn-in is reset per example so
every ON call genuinely exercises the kernel AND the burn-in compare (a
kernel drift latches + fails the engaged assertion — the anti-vacuous law).

Requires the ``fno_math`` wheel (CI builds it); a wheel-free environment
fails loudly rather than passing vacuously.
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.deflated_sharpe import (
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from brutex.metrics.pbo import pbo_probability
from brutex.metrics.wfo import WFOScheme, wfo_pass_rate
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs
from structlog.typing import EventDict

_BITS = struct.Struct("<d")


def _bits(value: float) -> bytes:
    return _BITS.pack(value)


def _fresh() -> None:
    rust_stats.reset_self_check_for_tests()
    rust_stats.reset_observability_memo_for_tests()


#: Hostile-but-finite float pool: signed zeros, subnormal-scale, huge and
#: ordinary magnitudes (validation refuses non-finite BEFORE the seam, so
#: NaN/inf belong to the exception-parity cases, not the value corpus).
_HOSTILE_FINITE = st.one_of(
    st.sampled_from([0.0, -0.0, 5e-324, -5e-324, 1e-300, -1e-300, 1e300, -1e300, 1.5, -2.25]),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
)


def _run_both(fn: str, call: object, *, expect_engaged: bool = True) -> None:
    """Drive *call* under both flags; assert bit/exception parity + ON
    engagement (kernel refusals would silently drop engagement — asserted
    so a wheel-less/vacuous run FAILS). ``expect_engaged=False`` covers the
    shapes that legitimately never dispatch (the ``n_trials == 1`` early
    return sits BEFORE the seam)."""
    import os

    assert callable(call)
    _fresh()
    os.environ[rust_stats.STATS_RUST_ENV] = "0"
    off_exc: BaseException | None = None
    off_val: object = None
    try:
        off_val = call()
    except (ValueError, ArithmeticError) as exc:  # StatisticsError ⊂ ValueError
        off_exc = exc
    _fresh()
    os.environ[rust_stats.STATS_RUST_ENV] = "1"
    on_exc: BaseException | None = None
    on_val: object = None
    logs: list[EventDict] = []
    try:
        with capture_logs() as logs:
            on_val = call()
    except (ValueError, ArithmeticError) as exc:
        on_exc = exc
    finally:
        os.environ[rust_stats.STATS_RUST_ENV] = "0"
    if off_exc is not None or on_exc is not None:
        assert type(off_exc) is type(on_exc), f"exception parity broke: {off_exc!r} vs {on_exc!r}"
        assert str(off_exc) == str(on_exc)
        return
    assert isinstance(off_val, float) and isinstance(on_val, float)
    assert _bits(on_val) == _bits(off_val), f"{fn}: ON {on_val!r} != OFF {off_val!r}"
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    if expect_engaged:
        assert engaged, f"{fn}: the ON arm never engaged (vacuous-pass trap)"


@settings(max_examples=300, deadline=None)
@given(
    observed=_HOSTILE_FINITE,
    benchmark=_HOSTILE_FINITE,
    sample_length=st.integers(min_value=2, max_value=10**7),
    skew=_HOSTILE_FINITE,
    kurtosis=_HOSTILE_FINITE,
)
def test_psr_on_off_bit_equal(
    observed: float, benchmark: float, sample_length: int, skew: float, kurtosis: float
) -> None:
    _run_both(
        "psr",
        lambda: probabilistic_sharpe_ratio(observed, benchmark, sample_length, skew, kurtosis),
    )


@settings(max_examples=300, deadline=None)
@given(
    n_trials=st.one_of(
        st.integers(min_value=2, max_value=10**7),
        st.integers(min_value=2, max_value=2**62),
        st.just(1),
    ),
    variance=st.one_of(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
        st.sampled_from([0.0, 5e-324, 1e-300, 1e300, float("inf")]),
    ),
)
def test_expected_max_sharpe_on_off_bit_equal(n_trials: int, variance: float) -> None:
    # inf variance is validation-legal (`< 0.0` is False) and flows to an
    # inf SR_0 on both arms; astronomical n_trials exercises the
    # StatisticsError exception-parity arm; n_trials == 1 returns 0.0
    # BEFORE the seam (never dispatches — engagement not expected there).
    _run_both(
        "sr_max",
        lambda: expected_max_sharpe_ratio(n_trials, variance),
        expect_engaged=n_trials != 1,
    )


@settings(max_examples=100, deadline=None)
@given(
    n_splits_half=st.integers(min_value=1, max_value=3),
    n_strat=st.integers(min_value=2, max_value=5),
    scale=st.sampled_from([1.0, 1e-8, 1e150]),
    data=st.data(),
)
def test_pbo_on_off_bit_equal(
    n_splits_half: int, n_strat: int, scale: float, data: st.DataObject
) -> None:
    import os

    n_splits = n_splits_half * 2
    rows = n_splits * 2  # two observations per block
    cell = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
    performance = [[data.draw(cell) * scale for _ in range(n_strat)] for _ in range(rows)]
    _fresh()
    os.environ[rust_stats.STATS_RUST_ENV] = "0"
    off = pbo_probability(performance, n_splits=n_splits)
    _fresh()
    os.environ[rust_stats.STATS_RUST_ENV] = "1"
    try:
        with capture_logs() as logs:
            on = pbo_probability(performance, n_splits=n_splits)
    finally:
        os.environ[rust_stats.STATS_RUST_ENV] = "0"
    assert any(e["event"] == "rust_stats.engaged" and e["fn"] == "pbo" for e in logs)
    assert _bits(on.pbo) == _bits(off.pbo)
    assert on.n_combinations == off.n_combinations
    for got, want in zip(on.logits, off.logits, strict=True):
        assert _bits(got) == _bits(want)


@settings(max_examples=100, deadline=None)
@given(
    n=st.integers(min_value=40, max_value=400),
    scale=st.sampled_from([1.0, 1e-6, 1e12, 1e300]),
    min_sharpe=st.one_of(st.just(0.0), st.floats(min_value=-3.0, max_value=3.0, allow_nan=False)),
    scheme=st.sampled_from(list(WFOScheme)),
    data=st.data(),
)
def test_wfo_on_off_bit_equal(
    n: int, scale: float, min_sharpe: float, scheme: WFOScheme, data: st.DataObject
) -> None:
    # 1e300-scale values overflow the centered squares to inf on BOTH arms
    # (the sharpe fail-safe None) — parity must hold through the guard. The
    # oracle's numpy `(arr - mean) ** 2` emits a RuntimeWarning on that
    # overflow, which the repo's `filterwarnings = ["error"]` would promote
    # — suppressed HERE only (the warning is numpy's, not a regression; the
    # VALUES are the parity subject and both arms agree on the fail-safe).
    import os
    import warnings

    cell = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
    returns = np.array([data.draw(cell) * scale for _ in range(n)], dtype=np.float64)
    train = max(4, n // 4)
    test = max(2, n // 8)
    step = max(1, n // 10)

    def call() -> float:
        return wfo_pass_rate(
            returns, scheme, train_size=train, test_size=test, step=step, min_sharpe=min_sharpe
        )

    _fresh()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        os.environ[rust_stats.STATS_RUST_ENV] = "0"
        off = call()
        _fresh()
        os.environ[rust_stats.STATS_RUST_ENV] = "1"
        try:
            with capture_logs() as logs:
                on = call()
        finally:
            os.environ[rust_stats.STATS_RUST_ENV] = "0"
    assert any(e["event"] == "rust_stats.engaged" and e["fn"] == "wfo" for e in logs)
    assert _bits(on) == _bits(off)


@pytest.mark.parametrize(
    "z_seed",
    [0.84375, 1.25, 1.0 / 0.35, 6.0, 2.0**-28, 0.0],
)
def test_psr_erf_branch_boundaries_bit_equal(z_seed: float) -> None:
    """ULP-bracket the erf branch boundaries THROUGH the public PSR:
    thresholds engineered so z/sqrt2 lands within a few ULP of each libm
    branch point (the same-symbol law leaves no room for drift — this pin
    proves it per platform)."""
    for k in range(-3, 4):
        z = z_seed * math.sqrt(2.0)
        target = z
        for _ in range(abs(k)):
            target = math.nextafter(target, math.inf if k > 0 else -math.inf)
        # observed = target / sqrt(n-1) with benchmark 0, skew 0, kurt 1 →
        # radicand exactly 1.0 → z == observed * sqrt(n-1).
        observed = target / math.sqrt(4 - 1)
        _run_both(
            "psr",
            lambda obs=observed: probabilistic_sharpe_ratio(obs, 0.0, 4, 0.0, 1.0),
        )
