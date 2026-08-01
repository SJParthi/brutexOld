"""Unit tests for :mod:`brutex.metrics.wfo` (walk-forward pass-rates).

Targets 100% line + branch coverage (Golden Rule #12): walk generation
for all three schemes, the no-look-ahead bounds, the embargo gap, the
minimum-train defaulting, every validation guard, and the three
``wfo_pass_rate`` walk outcomes (pass / fail / undefined).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
from brutex.metrics.wfo import (
    WFOPassRates,
    WFOScheme,
    WFOWalk,
    walk_forward_splits,
    wfo_pass_rate,
    wfo_pass_rates,
)

# A 600-bar series with a small positive drift, used across the pass-rate tests.
_RNG = np.random.default_rng(42)
_DRIFT = _RNG.normal(0.001, 0.01, 600)

_WIN = {"train_size": 200, "test_size": 50, "step": 25}


class TestSchemeEnum:
    def test_values(self) -> None:
        assert {s.value for s in WFOScheme} == {"rolling", "anchored", "expanding"}


class TestWalkGeneration:
    def test_rolling_window_is_fixed_length(self) -> None:
        walks = walk_forward_splits(600, WFOScheme.ROLLING, **_WIN)
        assert walks  # non-empty
        for w in walks:
            assert w.train_stop - w.train_start == 200
            assert w.test_stop - w.test_start == 50

    def test_rolling_first_walk(self) -> None:
        walks = walk_forward_splits(600, WFOScheme.ROLLING, **_WIN)
        assert walks[0] == WFOWalk(train_start=0, train_stop=200, test_start=200, test_stop=250)

    def test_anchored_train_starts_at_origin_and_grows(self) -> None:
        walks = walk_forward_splits(600, WFOScheme.ANCHORED, **_WIN)
        assert all(w.train_start == 0 for w in walks)
        lengths = [w.train_stop - w.train_start for w in walks]
        assert lengths == sorted(lengths)  # non-decreasing
        assert lengths[0] == 200  # anchored initial = train_size

    def test_expanding_starts_from_smaller_minimum(self) -> None:
        walks = walk_forward_splits(600, WFOScheme.EXPANDING, **_WIN)
        assert all(w.train_start == 0 for w in walks)
        # default min_train_size = train_size // 2 = 100 → first test at 100.
        assert walks[0].test_start == 100
        assert walks[0].train_stop == 100

    def test_expanding_explicit_min_train(self) -> None:
        walks = walk_forward_splits(600, WFOScheme.EXPANDING, min_train_size=300, **_WIN)
        assert walks[0].test_start == 300

    def test_too_short_series_yields_no_walks(self) -> None:
        assert walk_forward_splits(100, WFOScheme.ROLLING, **_WIN) == []

    def test_walk_index_arrays(self) -> None:
        w = WFOWalk(train_start=0, train_stop=3, test_start=3, test_stop=5)
        assert w.train.tolist() == [0, 1, 2]
        assert w.test.tolist() == [3, 4]


class TestNoLookAhead:
    @pytest.mark.parametrize("scheme", list(WFOScheme))
    def test_train_strictly_before_test(self, scheme: WFOScheme) -> None:
        walks = walk_forward_splits(600, scheme, **_WIN)
        assert walks
        for w in walks:
            assert w.train_stop <= w.test_start  # the WFO invariant
            assert int(w.train.max()) < int(w.test.min())

    @pytest.mark.parametrize("scheme", list(WFOScheme))
    def test_embargo_inserts_gap(self, scheme: WFOScheme) -> None:
        walks = walk_forward_splits(600, scheme, embargo=10, **_WIN)
        assert walks
        for w in walks:
            assert w.test_start - w.train_stop == 10


class TestPassRate:
    def test_in_unit_interval(self) -> None:
        rate = wfo_pass_rate(_DRIFT, WFOScheme.ROLLING, **_WIN)
        assert 0.0 <= rate <= 1.0

    def test_all_positive_windows_pass(self) -> None:
        returns = np.arange(1.0, 601.0)  # strictly increasing → every window up
        assert wfo_pass_rate(returns, WFOScheme.ROLLING, **_WIN) == 1.0

    def test_all_negative_windows_fail(self) -> None:
        returns = -np.arange(1.0, 601.0)
        assert wfo_pass_rate(returns, WFOScheme.ROLLING, **_WIN) == 0.0

    def test_single_sample_window_is_undefined_fail(self) -> None:
        # test_size=1 → every test window has one sample → Sharpe undefined.
        returns = np.arange(1.0, 601.0)
        rate = wfo_pass_rate(returns, WFOScheme.ROLLING, train_size=200, test_size=1, step=25)
        assert rate == 0.0

    def test_partial_rate(self) -> None:
        # first 300 strongly up, last 300 strongly down → mixed walks.
        returns = np.concatenate([np.arange(1.0, 301.0), -np.arange(1.0, 301.0)])
        rate = wfo_pass_rate(returns, WFOScheme.ROLLING, **_WIN)
        assert 0.0 < rate < 1.0

    def test_raises_when_no_walks(self) -> None:
        with pytest.raises(ValueError, match="no walks"):
            wfo_pass_rate(np.arange(1.0, 100.0), WFOScheme.ROLLING, **_WIN)

    def test_full_series_validated_even_outside_windows(self) -> None:
        # A NaN in the (unused) train region must still be rejected up front.
        returns = np.arange(1.0, 601.0)
        returns[5] = np.nan  # index 5 is in the first walk's train window
        with pytest.raises(ValueError, match="finite"):
            wfo_pass_rate(returns, WFOScheme.ROLLING, **_WIN)

    def test_determinism(self) -> None:
        assert wfo_pass_rate(_DRIFT, WFOScheme.ANCHORED, **_WIN) == wfo_pass_rate(
            _DRIFT, WFOScheme.ANCHORED, **_WIN
        )


class TestPassRatesTriple:
    def test_returns_all_three(self) -> None:
        rates = wfo_pass_rates(_DRIFT, **_WIN)
        assert isinstance(rates, WFOPassRates)
        for value in (rates.rolling, rates.anchored, rates.expanding):
            assert 0.0 <= value <= 1.0

    def test_matches_individual_calls(self) -> None:
        rates = wfo_pass_rates(_DRIFT, **_WIN)
        assert rates.rolling == wfo_pass_rate(_DRIFT, WFOScheme.ROLLING, **_WIN)
        assert rates.anchored == wfo_pass_rate(_DRIFT, WFOScheme.ANCHORED, **_WIN)
        assert rates.expanding == wfo_pass_rate(_DRIFT, WFOScheme.EXPANDING, **_WIN)

    def test_propagates_too_short_error(self) -> None:
        with pytest.raises(ValueError, match="no walks"):
            wfo_pass_rates(np.arange(1.0, 100.0), **_WIN)


class TestValidation:
    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"train_size": 0, "test_size": 50, "step": 25}, "train_size must be >= 1"),
            ({"train_size": 200, "test_size": 0, "step": 25}, "test_size must be >= 1"),
            ({"train_size": 200, "test_size": 50, "step": 0}, "step must be >= 1"),
            (
                {"train_size": 200, "test_size": 50, "step": 25, "min_train_size": 0},
                "min_train_size must be >= 1",
            ),
            (
                {"train_size": 200, "test_size": 50, "step": 25, "embargo": -1},
                "embargo must be >= 0",
            ),
        ],
    )
    def test_bad_window_params(self, kwargs: dict[str, int], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            walk_forward_splits(600, WFOScheme.ROLLING, **kwargs)

    def test_bad_n_samples(self) -> None:
        with pytest.raises(ValueError, match="n_samples must be >= 1"):
            walk_forward_splits(0, WFOScheme.ROLLING, **_WIN)

    def test_pass_rate_rejects_bad_series(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            wfo_pass_rate([], WFOScheme.ROLLING, **_WIN)


# ═════════════════════════════════════════════════════════════════════════
# DEC-METRICS-RUST-STATS-002 — the Rust dispatch arms (flag ON / NaN
# threshold refusal / fallthrough / flag-OFF bomb-kernel pin).
# ═════════════════════════════════════════════════════════════════════════

import struct  # noqa: E402  (appended PR-2 section)

import fno_math  # noqa: E402
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused  # noqa: E402
from brutex.metrics import rust_stats  # noqa: E402
from brutex.metrics.wfo import _walk_pass_count  # noqa: E402  # pyright: ignore[reportPrivateUsage]
from structlog.testing import capture_logs  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_stats_seam() -> None:
    """Fresh burn-in budgets, latches and log memo per test."""
    rust_stats.reset_self_check_for_tests()
    rust_stats.reset_observability_memo_for_tests()


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


def _series() -> npt.NDArray[np.float64]:
    return np.array([0.01 * ((i * 13 % 17) - 8) for i in range(200)], dtype=np.float64)


def test_flag_on_engages_bit_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = wfo_pass_rate(_series(), WFOScheme.ROLLING, train_size=50, test_size=20, step=10)
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    with capture_logs() as logs:
        on = wfo_pass_rate(_series(), WFOScheme.ROLLING, train_size=50, test_size=20, step=10)
    assert any(e["event"] == "rust_stats.engaged" and e["fn"] == "wfo" for e in logs), (
        "the ON arm must engage the Rust kernel (anti-vacuous-pass law)"
    )
    assert _bits(on) == _bits(off)


def test_flag_on_all_three_schemes_bit_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = wfo_pass_rates(_series(), train_size=60, test_size=25, step=15, embargo=3)
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    on = wfo_pass_rates(_series(), train_size=60, test_size=25, step=15, embargo=3)
    for field in ("rolling", "anchored", "expanding"):
        assert _bits(getattr(on, field)) == _bits(getattr(off, field)), field


def test_flag_on_nan_threshold_refuses_to_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NaN min_sharpe refuses (the exact-type threshold law) — the oracle's
    ``> NaN`` all-non-pass semantics own the call on BOTH arms."""
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = wfo_pass_rate(
        _series(), WFOScheme.ROLLING, train_size=50, test_size=20, step=10, min_sharpe=float("nan")
    )
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    with capture_logs() as logs:
        on = wfo_pass_rate(
            _series(),
            WFOScheme.ROLLING,
            train_size=50,
            test_size=20,
            step=10,
            min_sharpe=float("nan"),
        )
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == ["arg_inexact"]
    assert _bits(on) == _bits(off) and off == 0.0


def test_flag_on_latched_kernel_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The degrade-to-oracle contract, pinned in the mode that still HAS one.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` default-ON,
    and a LATCHED kernel is Class B — the crate answered but its RESULT is
    structurally refused — so under the shipped default this fall-back RAISES
    rather than quietly serving the retained Python tail.  The degrade
    machinery is not deleted; it stays reachable through the documented
    ``BRUTEX_RUST_STRICT=0`` kill switch, and THAT is the mode this test
    describes.  The strict-ON refusal for the same input is pinned by
    :func:`test_latched_kernel_refuses_under_strict` below.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    rust_stats.disable_kernel("wfo", "self_check_mismatch")
    on = wfo_pass_rate(_series(), WFOScheme.ANCHORED, train_size=50, test_size=20, step=10)
    import numpy as np

    arr = np.asarray(_series(), dtype=np.float64)
    walks = walk_forward_splits(
        int(arr.size), WFOScheme.ANCHORED, train_size=50, test_size=20, step=10
    )
    assert _bits(on) == _bits(_walk_pass_count(arr, walks, 0.0) / len(walks))


def test_latched_kernel_refuses_under_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict-ON refuses the latch itself, BEFORE it is ever written.

    That ordering is why the fallthrough arm above is structurally
    unreachable under the shipped default: the process can never get INTO
    the latched state.  The refusal must name the facade and the documented
    kill switch so the operator can act on the message alone.
    """
    monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    with pytest.raises(RustFallbackRefused, match="self_check_mismatch") as excinfo:
        rust_stats.disable_kernel("wfo", "self_check_mismatch")
    text = str(excinfo.value)
    assert "brutex.metrics.rust_stats" in text
    assert f"{RUST_STRICT_ENV}=0" in text
    # ...and because the raise preceded the latch write, the kernel still serves.
    served = wfo_pass_rate(_series(), WFOScheme.ANCHORED, train_size=50, test_size=20, step=10)
    assert 0.0 <= served <= 1.0


def test_flag_off_never_touches_the_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb-kernel spy: flag OFF is byte-identical AND crate-untouched."""
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")

    def _bomb(*args: object) -> int:
        raise AssertionError("flag-OFF must never call the Rust kernel")

    monkeypatch.setattr(fno_math, "stats_wfo_pass_count", _bomb)
    rate = wfo_pass_rate(_series(), WFOScheme.EXPANDING, train_size=50, test_size=20, step=10)
    assert 0.0 <= rate <= 1.0
