"""Unit tests for :mod:`brutex.metrics.pbo` — PBO via CSCV.

Known-answer assertions on hand-constructed matrices (a strategy that
dominates in- and out-of-sample → PBO = 0; an anti-generalising matrix where
the IS-best is always the OOS-worst → PBO = 1) plus explicit edge-case
coverage (zero-variance chunk, rank ties, custom evaluator, and every
validation raise) so the module reaches 100% line + 100% branch coverage.

Formula source cited in the module docstring (Bailey, Borwein, López de
Prado & Zhu 2017, CSCV).
"""

from __future__ import annotations

import math

import pytest
from brutex.metrics.pbo import PBOResult, pbo_probability, sharpe_from_moments

#: A dominant-strategy matrix: column 0 has Sharpe 2 in both sub-matrices,
#: column 1 has Sharpe −3. With S=2 the IS-best (col 0) is also OOS-best in
#: both symmetric splits → PBO = 0.
_DOMINANT = [[3.0, -2.0], [1.0, -1.0], [3.0, -1.0], [1.0, -2.0]]

#: An anti-generalising matrix: whichever column wins in-sample is the worst
#: out-of-sample in both splits → PBO = 1.
_ANTI = [[3.0, -3.0], [1.0, -1.0], [-3.0, 3.0], [-1.0, 1.0]]


# ── sharpe_from_moments ─────────────────────────────────────────────


def test_sharpe_from_moments_positive() -> None:
    """[3, 1] → sum 4, sumsq 10, n 2 → mean 2, var 1 → Sharpe 2."""
    assert sharpe_from_moments(4.0, 10.0, 2) == 2.0


def test_sharpe_from_moments_negative() -> None:
    """[-3, -1] → mean −2, var 1 → Sharpe −2 (sign is preserved)."""
    assert sharpe_from_moments(-4.0, 10.0, 2) == -2.0


def test_sharpe_from_moments_zero_variance_returns_zero() -> None:
    """A flat chunk ([2, 2] → var 0) yields the documented 0.0 convention."""
    assert sharpe_from_moments(4.0, 8.0, 2) == 0.0


# ── pbo_probability — known answers ─────────────────────────────────


def test_pbo_dominant_strategy_is_zero() -> None:
    """A strategy dominating IS and OOS in every split → PBO = 0."""
    result = pbo_probability(_DOMINANT, n_splits=2)
    assert isinstance(result, PBOResult)
    assert result.pbo == 0.0
    assert result.n_splits == 2
    assert result.n_combinations == 2  # C(2, 1)
    assert result.n_strategies == 2
    assert len(result.logits) == 2
    assert all(lam > 0.0 for lam in result.logits)


def test_pbo_anti_generalising_is_one() -> None:
    """IS-best always OOS-worst → PBO = 1; logits are ln(1/2) = −ln 2."""
    result = pbo_probability(_ANTI, n_splits=2)
    assert result.pbo == 1.0
    assert result.logits == pytest.approx((-math.log(2.0), -math.log(2.0)))


def test_pbo_is_a_probability_for_s4() -> None:
    """A larger deterministic S=4 matrix yields a PBO in [0, 1]."""
    matrix = [[float((t * 7 + n * 3) % 5 - 2) for n in range(4)] for t in range(8)]
    result = pbo_probability(matrix, n_splits=4)
    assert 0.0 <= result.pbo <= 1.0
    assert result.n_combinations == 6  # C(4, 2)


def test_pbo_handles_oos_ties() -> None:
    """Tied OOS performances use average ranks (no crash, valid probability).

    Columns 1 and 2 are identical, so OOS ties occur and the ``equal > 1``
    branch of the rank computation is exercised.
    """
    matrix = [
        [3.0, 1.0, 1.0],
        [1.0, 1.0, 1.0],
        [2.0, 1.0, 1.0],
        [0.0, 1.0, 1.0],
    ]
    result = pbo_probability(matrix, n_splits=2)
    assert 0.0 <= result.pbo <= 1.0
    assert result.n_strategies == 3


def test_pbo_accepts_custom_evaluator() -> None:
    """A custom moment-based evaluator (mean only) is honoured (DYNAMIC rule)."""

    def mean_only(total: float, total_sq: float, count: int) -> float:
        return total / count

    result = pbo_probability(_DOMINANT, n_splits=2, evaluator=mean_only)
    # Column 0 still has the higher mean in every chunk → still PBO 0.
    assert result.pbo == 0.0


# ── pbo_probability — validation raises ─────────────────────────────


def test_pbo_rejects_splits_below_two() -> None:
    """``n_splits < 2`` is rejected (first operand of the guard)."""
    with pytest.raises(ValueError, match="even integer >= 2"):
        pbo_probability(_DOMINANT, n_splits=1)


def test_pbo_rejects_odd_splits() -> None:
    """An odd ``n_splits`` is rejected (second operand of the guard)."""
    with pytest.raises(ValueError, match="even integer >= 2"):
        pbo_probability([[0.0, 0.0]] * 9, n_splits=3)


def test_pbo_rejects_empty_matrix() -> None:
    """An empty performance matrix is rejected."""
    with pytest.raises(ValueError, match="at least one row"):
        pbo_probability([], n_splits=2)


def test_pbo_rejects_single_strategy() -> None:
    """A single-column matrix cannot be ranked — rejected."""
    with pytest.raises(ValueError, match=">= 2 strategies"):
        pbo_probability([[1.0], [2.0]], n_splits=2)


def test_pbo_rejects_ragged_matrix() -> None:
    """A ragged matrix (unequal row lengths) is rejected."""
    with pytest.raises(ValueError, match="ragged"):
        pbo_probability([[1.0, 2.0], [3.0]], n_splits=2)


def test_pbo_rejects_indivisible_row_count() -> None:
    """A row count that is not a multiple of ``n_splits`` is rejected."""
    with pytest.raises(ValueError, match="must be a positive multiple"):
        pbo_probability([[1.0, 2.0]] * 5, n_splits=2)


# ── Non-finite cell rejection (DEC-METRICS-NONFINITE-GUARD-001) ──────
#
# A NaN/inf cell would fabricate a finite mid-rank (the ranking compares the
# IS-best's OOS Sharpe, which a non-finite input silently corrupts). The
# explicit finiteness sweep rejects it, matching the module's validation
# philosophy. The bad cell is placed at [1][2] (not the origin) to prove the
# sweep scans the whole matrix, not just the first cell.


def test_pbo_rejects_nan_cell() -> None:
    """A NaN cell anywhere in the matrix is rejected with its coordinates."""
    matrix = [
        [0.1, 0.2, 0.3],
        [0.1, 0.2, float("nan")],
        [0.1, 0.2, 0.3],
        [0.1, 0.2, 0.3],
    ]
    with pytest.raises(ValueError, match=r"non-finite entry at \[1\]\[2\]"):
        pbo_probability(matrix, n_splits=2)


def test_pbo_rejects_inf_cell() -> None:
    """An inf cell anywhere in the matrix is rejected with its coordinates."""
    matrix = [
        [0.1, 0.2, 0.3],
        [0.1, 0.2, float("inf")],
        [0.1, 0.2, 0.3],
        [0.1, 0.2, 0.3],
    ]
    with pytest.raises(ValueError, match=r"non-finite entry at \[1\]\[2\]"):
        pbo_probability(matrix, n_splits=2)


# ═════════════════════════════════════════════════════════════════════════
# DEC-METRICS-RUST-STATS-002 — the Rust dispatch arms (flag ON / custom
# evaluator refusal / fallthrough / flag-OFF bomb-kernel pin).
# ═════════════════════════════════════════════════════════════════════════

import struct  # noqa: E402  (appended PR-2 section)

import fno_math  # noqa: E402
from brutex.metrics import rust_stats  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_stats_seam() -> None:
    """Fresh burn-in budgets, latches and log memo per test."""
    rust_stats.reset_self_check_for_tests()
    rust_stats.reset_observability_memo_for_tests()


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


def _matrix() -> list[list[float]]:
    return [[float((i * j + 3) % 11 - 5) * 0.01 for j in range(1, 6)] for i in range(64)]


def test_flag_on_engages_bit_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = pbo_probability(_matrix(), n_splits=8)
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    with capture_logs() as logs:
        on = pbo_probability(_matrix(), n_splits=8)
    assert any(e["event"] == "rust_stats.engaged" and e["fn"] == "pbo" for e in logs), (
        "the ON arm must engage the Rust kernel (anti-vacuous-pass law)"
    )
    assert _bits(on.pbo) == _bits(off.pbo)
    assert on.n_combinations == off.n_combinations == 70
    assert len(on.logits) == len(off.logits)
    for got, want in zip(on.logits, off.logits, strict=True):
        assert _bits(got) == _bits(want)


def test_flag_on_custom_evaluator_refuses_to_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom evaluator is not transcribed — the seam refuses and the
    Python loop serves, identical to OFF."""

    def evaluator(total: float, total_sq: float, count: int) -> float:
        return total / count  # plain mean, not Sharpe

    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = pbo_probability(_matrix(), n_splits=4, evaluator=evaluator)
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    with capture_logs() as logs:
        on = pbo_probability(_matrix(), n_splits=4, evaluator=evaluator)
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == ["custom_evaluator"]
    assert _bits(on.pbo) == _bits(off.pbo)
    for got, want in zip(on.logits, off.logits, strict=True):
        assert _bits(got) == _bits(want)


def test_flag_on_latched_kernel_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    rust_stats.disable_kernel("pbo", "self_check_mismatch")
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off = pbo_probability(_matrix(), n_splits=4)
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    on = pbo_probability(_matrix(), n_splits=4)
    assert _bits(on.pbo) == _bits(off.pbo)


def test_flag_off_never_touches_the_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb-kernel spy: flag OFF is byte-identical AND crate-untouched."""
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")

    def _bomb(*args: object) -> tuple[int, list[float]]:
        raise AssertionError("flag-OFF must never call the Rust kernel")

    monkeypatch.setattr(fno_math, "stats_pbo_cscv", _bomb)
    result = pbo_probability(_matrix(), n_splits=4)
    assert 0.0 <= result.pbo <= 1.0
