"""Unit tests for :mod:`brutex.metrics.cpcv` (CPCV splits + pass-rate).

Targets 100% line + branch coverage of the module (Golden Rule #12 /
``docs/STANDARDS_100PCT.md``): split-count correctness, the purge +
embargo row removal, every validation guard, the ``sharpe_of_returns``
edge cases (single sample, zero variance), and the three ``cpcv_pass_rate``
fold outcomes (pass / fail / undefined).

Whitebox module: the non-finite-variance regression imports the private
moment helpers (``_combine_moments`` / ``_group_bounds`` / ``_group_moments``)
to exercise the cross-group merge guard directly, so the module-level
``reportPrivateUsage`` warning is silenced (the established convention for
whitebox tests, DEC-P0-017).
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import math
from typing import ClassVar

import numpy as np
import pytest
from brutex.metrics.cpcv import (
    DEFAULT_N_GROUPS,
    DEFAULT_N_TEST_GROUPS,
    CPCVSplit,
    _combine_moments,
    _group_bounds,
    _group_moments,
    cpcv_pass_rate,
    cpcv_splits,
    sharpe_of_returns,
)


class TestSplitCount:
    def test_default_is_c_16_8(self) -> None:
        """Defaults reproduce the spec's S=16, C(16,8)=12,870 splits (line 387)."""
        assert DEFAULT_N_GROUPS == 16
        assert DEFAULT_N_TEST_GROUPS == 8
        splits = cpcv_splits(64)
        assert len(splits) == math.comb(16, 8) == 12_870

    @pytest.mark.parametrize(
        ("n_groups", "n_test_groups"),
        [(2, 1), (4, 2), (5, 1), (6, 3), (8, 4)],
    )
    def test_count_equals_binomial(self, n_groups: int, n_test_groups: int) -> None:
        n_samples = n_groups * 3
        splits = cpcv_splits(n_samples, n_groups=n_groups, n_test_groups=n_test_groups)
        assert len(splits) == math.comb(n_groups, n_test_groups)

    def test_split_is_frozen_dataclass(self) -> None:
        split = cpcv_splits(4, n_groups=2, n_test_groups=1)[0]
        assert isinstance(split, CPCVSplit)
        with pytest.raises(AttributeError):
            split.test = np.array([0])  # type: ignore[misc]  # frozen


class TestSplitStructure:
    def test_complementary_without_purge(self) -> None:
        """label_span=0, embargo=0 → train and test partition the index set."""
        splits = cpcv_splits(12, n_groups=4, n_test_groups=2)
        all_idx = set(range(12))
        for s in splits:
            test = set(s.test.tolist())
            train = set(s.train.tolist())
            assert test.isdisjoint(train)
            assert test | train == all_idx

    def test_test_groups_recorded_in_order(self) -> None:
        splits = cpcv_splits(12, n_groups=4, n_test_groups=2)
        # itertools.combinations order: first split is (0, 1).
        assert splits[0].test_groups == (0, 1)
        assert all(list(s.test_groups) == sorted(s.test_groups) for s in splits)

    def test_uneven_group_partition(self) -> None:
        """11 samples / 4 groups → sizes [3, 3, 3, 2] (array_split rule)."""
        splits = cpcv_splits(11, n_groups=4, n_test_groups=1)
        sizes = {s.test_groups[0]: s.test.size for s in splits}
        assert sizes == {0: 3, 1: 3, 2: 3, 3: 2}

    def test_even_group_partition(self) -> None:
        splits = cpcv_splits(12, n_groups=4, n_test_groups=1)
        assert {s.test.size for s in splits} == {3}

    def test_arrays_are_read_only(self) -> None:
        split = cpcv_splits(4, n_groups=2, n_test_groups=1)[0]
        with pytest.raises(ValueError, match="read-only"):
            split.test[0] = 999
        with pytest.raises(ValueError, match="read-only"):
            split.train[0] = 999

    def test_determinism(self) -> None:
        a = cpcv_splits(20, n_groups=5, n_test_groups=2, label_span=2, embargo=1)
        b = cpcv_splits(20, n_groups=5, n_test_groups=2, label_span=2, embargo=1)
        assert [s.test_groups for s in a] == [s.test_groups for s in b]
        for sa, sb in zip(a, b, strict=True):
            assert np.array_equal(sa.train, sb.train)
            assert np.array_equal(sa.test, sb.test)


class TestPurgeAndEmbargo:
    def test_overlapping_label_is_purged(self) -> None:
        """No-look-ahead: a train row whose label overlaps a test label is purged.

        Groups of 3 over 12 samples; test = group 1 = indices [3,4,5].
        With label_span=1, train index 2's label [2,3] overlaps test index
        3's label [3,4] → index 2 MUST be purged from train.
        """
        # test_groups=(1,) is the 2nd single-group split.
        split = cpcv_splits(12, n_groups=4, n_test_groups=1, label_span=1)[1]
        assert split.test_groups == (1,)
        assert split.test.tolist() == [3, 4, 5]
        # index 2 (label [2,3]) overlaps test index 3 (label [3,4]) → purged.
        assert 2 not in split.train.tolist()
        # index 6 (label [6,7]) overlaps test index 5 (label [5,6]) → purged.
        assert 6 not in split.train.tolist()
        # index 1 (label [1,2]) does NOT overlap → survives.
        assert 1 in split.train.tolist()

    def test_embargo_removes_following_rows(self) -> None:
        """Embargo drops the buffer immediately AFTER each test block."""
        # test = group 1 = [3,4,5]; no label span, embargo=2.
        split = cpcv_splits(12, n_groups=4, n_test_groups=1, embargo=2)[1]
        assert split.test.tolist() == [3, 4, 5]
        # rows 6,7 (within 2 bars after block end 5) are embargoed.
        assert 6 not in split.train.tolist()
        assert 7 not in split.train.tolist()
        # row 8 is outside the embargo → survives.
        assert 8 in split.train.tolist()
        # embargo is forward-only: rows before the block are untouched.
        assert {0, 1, 2}.issubset(set(split.train.tolist()))

    def test_combined_purge_embargo_exact(self) -> None:
        """Hand-derived exact train sets for n=12, S=4, k=2, span=1, embargo=1."""
        splits = cpcv_splits(12, n_groups=4, n_test_groups=2, label_span=1, embargo=1)
        by_groups = {s.test_groups: s.train.tolist() for s in splits}
        # (0,1): block [0,5] → exclude [max(0,-1), min(11,7)] = [0,7] → train [8..11].
        assert by_groups[(0, 1)] == [8, 9, 10, 11]
        # (0,2): blocks [0,2]→excl[0,4] and [6,8]→excl[5,10] → train [11].
        assert by_groups[(0, 2)] == [11]
        # (2,3): block [6,11] → exclude [5, min(11,13)] = [5,11] → train [0..4].
        assert by_groups[(2, 3)] == [0, 1, 2, 3, 4]

    def test_heavy_purge_can_empty_train(self) -> None:
        """A wide embargo can purge the entire remaining train set."""
        split = cpcv_splits(6, n_groups=3, n_test_groups=1, embargo=10)[0]
        assert split.test.tolist() == [0, 1]
        assert split.train.size == 0


class TestSharpeOfReturns:
    def test_known_value(self) -> None:
        # returns [1,2,3,4,5]: mean 3, population std sqrt(2).
        result = sharpe_of_returns([1.0, 2.0, 3.0, 4.0, 5.0])
        assert result is not None
        assert result == pytest.approx(3.0 / math.sqrt(2.0))

    def test_single_sample_is_none(self) -> None:
        assert sharpe_of_returns([0.5]) is None

    def test_zero_variance_is_none(self) -> None:
        assert sharpe_of_returns([2.0, 2.0, 2.0]) is None

    def test_negative_mean_is_negative(self) -> None:
        result = sharpe_of_returns([-1.0, -2.0, -3.0])
        assert result is not None
        assert result < 0.0

    def test_accepts_numpy_array(self) -> None:
        arr = np.array([0.1, -0.2, 0.3], dtype=np.float64)
        assert sharpe_of_returns(arr) == pytest.approx(sharpe_of_returns([0.1, -0.2, 0.3]))

    @pytest.mark.parametrize(
        ("bad", "match"),
        [
            ([], "non-empty"),
            ([[1.0, 2.0], [3.0, 4.0]], "1-D"),
            ([1.0, float("nan")], "finite"),
            ([1.0, float("inf")], "finite"),
        ],
    )
    def test_validation_errors(self, bad: object, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            sharpe_of_returns(bad)  # type: ignore[arg-type]

    def test_overflow_variance_returns_none_not_zero(self) -> None:
        """A finite but huge series whose squared deviations overflow → ``None``.

        The inputs are individually finite (so ``_validate_returns`` admits
        them), but ``(arr - mean) ** 2`` overflows to ``inf``; the bare
        ``variance <= 0.0`` guard is ``False`` for ``inf`` and would return a
        wrong ``mean / sqrt(inf) == 0.0``. The non-finite-variance guard
        (DEC-METRICS-NONFINITE-GUARD-001) degrades honestly to ``None``.
        """
        with np.errstate(all="ignore"):
            result = sharpe_of_returns(np.array([1e300, 2e300, 3e300, 4e300]))
        assert result is None


class TestNonFiniteVariance:
    """Overflow-magnitude folds never leak a NaN into ``cpcv_pass_rate``.

    Regression for DEC-METRICS-NONFINITE-GUARD-001: for ``|returns| ~ 1e160``
    the per-group ``M2`` accumulates to ``inf`` and the combined fold variance
    becomes ``NaN`` — which the bare ``variance <= 0.0`` guard misses
    (``nan <= 0`` is ``False``), leaking ``mean / sqrt(nan) == nan`` into the
    pass-rate. The ``not math.isfinite(variance)`` guard makes every such fold
    a non-pass, so the rate stays a finite value in ``[0, 1]``.
    """

    def test_pass_rate_finite_for_overflow_magnitude_series(self) -> None:
        with np.errstate(all="ignore"):
            big = np.array([1e160 * (i + 1) for i in range(96)], dtype=np.float64)
            rate = cpcv_pass_rate(big, n_groups=4, n_test_groups=2)
        assert math.isfinite(rate)
        assert 0.0 <= rate <= 1.0

    def test_combine_moments_returns_none_when_merge_overflows_to_nan(self) -> None:
        """``_combine_moments`` must return ``None`` (not ``NaN``) on a
        non-finite *combined* variance.

        Mutation-survivor regression (DEC-METRICS-INVARIANTS-ROUND2-001):
        the per-group ``M2`` here is exactly ``0`` (each group is constant,
        so ``_group_moments`` produces finite triples), yet two groups whose
        means differ by ``2e160`` make the cross-group merge term
        ``delta * delta * n_total * n_b / combined`` overflow to ``inf`` and
        the combined variance ``NaN`` — a non-finiteness that arises *purely
        inside* :func:`_combine_moments`. The bare ``variance <= 0.0`` guard
        is ``False`` for ``NaN`` and would return ``mean / sqrt(NaN) == NaN``;
        the ``not math.isfinite(variance)`` arm degrades honestly to ``None``.
        Asserting the *exact* ``None`` (rather than only "the pass-rate stays
        finite", which a ``NaN`` Sharpe also satisfies because ``NaN > 0`` is
        ``False``) is what kills the dropped-``isfinite`` mutant.
        """
        big = 1e160
        # Four constant groups (zero within-group variance ⇒ finite group M2),
        # alternating sign so any mixed-sign fold overflows on merge.
        values = [v for mean in (big, -big, big, -big) for v in (mean, mean, mean)]
        arr = np.asarray(values, dtype=np.float64)
        bounds = _group_bounds(arr.size, 4)
        count, mean, m2 = _group_moments(arr, bounds)
        assert bool(np.all(np.isfinite(m2)))  # the overflow is NOT at group level
        with np.errstate(all="ignore"):
            merged = _combine_moments(count, mean, m2, (0, 1))
        assert merged is None


class TestPassRate:
    def test_in_unit_interval(self) -> None:
        rng = np.random.default_rng(7)
        rate = cpcv_pass_rate(rng.normal(0.0, 0.01, 96))
        assert 0.0 <= rate <= 1.0

    def test_all_positive_folds_pass(self) -> None:
        # strictly increasing positive series → every fold mean > 0, var > 0.
        returns = np.arange(1.0, 49.0)
        assert cpcv_pass_rate(returns, n_groups=4, n_test_groups=2) == 1.0

    def test_all_negative_folds_fail(self) -> None:
        # strictly decreasing negative series → every fold Sharpe < 0 (defined).
        returns = -np.arange(1.0, 49.0)
        assert cpcv_pass_rate(returns, n_groups=4, n_test_groups=2) == 0.0

    def test_undefined_folds_count_as_fail(self) -> None:
        # n=2, S=2, k=1 → each fold has a single sample → Sharpe undefined.
        assert cpcv_pass_rate([1.0, 2.0], n_groups=2, n_test_groups=1) == 0.0

    def test_zero_variance_folds_fail(self) -> None:
        # constant series → every fold has zero variance → undefined → fail.
        assert cpcv_pass_rate([3.0] * 12, n_groups=4, n_test_groups=2) == 0.0

    def test_partial_rate_between_zero_and_one(self) -> None:
        # first half strongly up, second half strongly down → mixed folds.
        returns = np.concatenate([np.arange(1.0, 25.0), -np.arange(1.0, 25.0)])
        rate = cpcv_pass_rate(returns, n_groups=6, n_test_groups=3)
        assert 0.0 < rate < 1.0

    def test_min_sharpe_threshold_lowers_rate(self) -> None:
        rng = np.random.default_rng(11)
        returns = rng.normal(0.002, 0.01, 96)
        lenient = cpcv_pass_rate(returns, min_sharpe=0.0)
        strict = cpcv_pass_rate(returns, min_sharpe=5.0)
        assert strict <= lenient

    def test_determinism(self) -> None:
        rng = np.random.default_rng(3)
        returns = rng.normal(0.0, 0.01, 80)
        assert cpcv_pass_rate(returns) == cpcv_pass_rate(returns)


class TestParameterValidation:
    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"n_groups": 1}, "n_groups must be >= 2"),
            ({"n_groups": 99}, "cannot exceed n_samples"),
            ({"n_groups": 4, "n_test_groups": 0}, "n_test_groups must be in"),
            ({"n_groups": 4, "n_test_groups": 4}, "n_test_groups must be in"),
            ({"n_groups": 4, "n_test_groups": 2, "label_span": -1}, "label_span must be >= 0"),
            ({"n_groups": 4, "n_test_groups": 2, "embargo": -1}, "embargo must be >= 0"),
        ],
    )
    def test_splits_reject_bad_params(self, kwargs: dict[str, int], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            cpcv_splits(20, **kwargs)

    def test_pass_rate_rejects_bad_returns(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            cpcv_pass_rate([])

    def test_pass_rate_rejects_bad_groups(self) -> None:
        with pytest.raises(ValueError, match="cannot exceed n_samples"):
            cpcv_pass_rate([1.0, 2.0, 3.0], n_groups=8)


class TestRustStatsDispatch:
    """The DEC-METRICS-RUST-STATS-001 seam dispatch inside ``cpcv_pass_rate``.

    Wheel-independent by design (the seam function is monkeypatched): pins
    that a kernel-served count is ADOPTED (the Python fold loop is skipped),
    that a ``None`` from the seam falls through to the retained-forever
    oracle byte-identically, and that the flag-OFF default never consults
    the seam at all. Kernel-vs-oracle value parity lives in
    ``tests/unit/test_rust_stats.py`` + the property/golden suites.
    """

    _RETURNS: ClassVar[list[float]] = [1.0, 2.0, -3.0, -4.0]  # golden: 1 of 6 folds pass

    def test_flag_off_never_consults_the_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.metrics import rust_stats

        def _forbidden(*args: object, **kwargs: object) -> int:
            raise AssertionError("the seam must not be consulted when the flag is OFF")

        monkeypatch.delenv(rust_stats.STATS_RUST_ENV, raising=False)
        monkeypatch.setattr(rust_stats, "cpcv_pass_count_via_kernel", _forbidden)
        rate = cpcv_pass_rate(self._RETURNS, n_groups=4, n_test_groups=2)
        assert rate == 1.0 / 6.0

    def test_flag_on_kernel_count_is_adopted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A seam-served count feeds the SAME exact division expression; the
        stubbed count of 6 (≠ the oracle's 1) proves the adoption."""
        from brutex.metrics import rust_stats

        seen: dict[str, object] = {}

        def _kernel_stub(*args: object, **kwargs: object) -> int:
            seen["n_total"] = kwargs["n_total"]
            oracle = kwargs["oracle"]
            assert callable(oracle)
            seen["oracle_value"] = oracle()
            return 6

        monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
        monkeypatch.setattr(rust_stats, "cpcv_pass_count_via_kernel", _kernel_stub)
        rate = cpcv_pass_rate(self._RETURNS, n_groups=4, n_test_groups=2)
        assert rate == 1.0  # 6/6 — the stub's count, not the oracle's 1
        assert seen["n_total"] == 6
        # The oracle closure handed to the seam IS the extracted fold loop.
        assert seen["oracle_value"] == 1

    def test_flag_on_seam_none_falls_through_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` (latched / wheel-absent / kernel error / refused result)
        serves the call from the Python fold loop — byte-identical to OFF."""
        from brutex.metrics import rust_stats

        monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
        monkeypatch.setattr(rust_stats, "cpcv_pass_count_via_kernel", lambda *a, **k: None)
        rate = cpcv_pass_rate(self._RETURNS, n_groups=4, n_test_groups=2)
        assert rate == 1.0 / 6.0
