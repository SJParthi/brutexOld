"""Unit tests for :mod:`brutex.nightly.decay_math` (LANE W5).

100% line + branch of the born-Rust verdict seam
(DEC-NIGHTLY-DECAY-R48-001 + DEC-CHAMPION-CHALLENGER-001): the flag
matrix, the sequential Python oracle laws, the kernel binding with its
per-process burn-in bit-compare + ship-oracle autorevert latch, the
wheel-missing / kernel-raise degrades, the refuse-poison code gate, and
the Rust ≡ oracle differential (hypothesis) against the REAL wheel.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest
import structlog.testing
from brutex.metrics.lockbox import DEFAULT_MDD_TOLERANCE, DEFAULT_SHARPE_TOLERANCE
from brutex.nightly import decay_math
from brutex.nightly.decay_math import (
    DECAY_MDD_TOLERANCE,
    DECAY_MIN_TRADES,
    DECAY_MIN_TRADES_ENV,
    DECAY_RUST_ENV,
    SELF_CHECK_SAMPLES,
    VERDICT_ABSTAIN_LOW_N,
    VERDICT_ABSTAIN_UNDEFINED,
    VERDICT_DECAYED,
    VERDICT_HEALTHY,
    TrailingFold,
    champion_dominates,
    decay_rust_enabled,
    decay_verdict_token,
    reset_decay_math_for_tests,
    resolve_decay_min_trades,
    trailing_fold,
)
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator

fno_math = pytest.importorskip("fno_math", reason="the born-Rust kernel wheel")


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh latch/memos + a clean env per test (hermeticity)."""
    monkeypatch.delenv(DECAY_RUST_ENV, raising=False)
    monkeypatch.delenv(DECAY_MIN_TRADES_ENV, raising=False)
    reset_decay_math_for_tests()
    yield
    reset_decay_math_for_tests()


def _verdict(
    trailing_sharpe: float | None,
    *,
    discovery_sharpe: float = 1.0,
    trailing_max_dd: float = 0.0,
    discovery_max_dd: float = 0.1,
    n_trades: int = 30,
    min_trades: int = 10,
) -> str:
    return decay_verdict_token(
        trailing_sharpe=trailing_sharpe,
        discovery_sharpe=discovery_sharpe,
        trailing_max_dd=trailing_max_dd,
        discovery_max_dd=discovery_max_dd,
        n_trades=n_trades,
        min_trades=min_trades,
    )


class TestConstants:
    def test_tolerances_are_the_lockbox_constants(self) -> None:
        """The ONE-constant law: imported, never re-typed (0.70× / 1.50×)."""
        assert DECAY_MDD_TOLERANCE == DEFAULT_MDD_TOLERANCE == 0.50
        assert decay_math.DECAY_SHARPE_TOLERANCE == DEFAULT_SHARPE_TOLERANCE == 0.30

    def test_verdict_vocabulary_matches_kernel_codes(self) -> None:
        assert decay_math._VERDICT_BY_CODE == (
            VERDICT_HEALTHY,
            VERDICT_DECAYED,
            VERDICT_ABSTAIN_LOW_N,
            VERDICT_ABSTAIN_UNDEFINED,
        )


class TestFlag:
    def test_unset_defaults_on(self) -> None:
        assert decay_rust_enabled() is True

    def test_blank_defaults_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_RUST_ENV, "   ")
        assert decay_rust_enabled() is True

    def test_truthy_on_falsy_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_RUST_ENV, "yes")
        assert decay_rust_enabled() is True
        monkeypatch.setenv(DECAY_RUST_ENV, "0")
        assert decay_rust_enabled() is False

    def test_junk_fails_safe_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd kill switch must still KILL (the rust_metrics law)."""
        monkeypatch.setenv(DECAY_RUST_ENV, "flase")
        with structlog.testing.capture_logs() as logs:
            assert decay_rust_enabled() is False
            assert decay_rust_enabled() is False
        events = [entry for entry in logs if entry["event"] == "decay_math.flag_unrecognised"]
        assert len(events) == 1  # warn-once


class TestResolveMinTrades:
    def test_default(self) -> None:
        assert resolve_decay_min_trades() == DECAY_MIN_TRADES == 10

    def test_blank_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_MIN_TRADES_ENV, " ")
        assert resolve_decay_min_trades() == DECAY_MIN_TRADES

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_MIN_TRADES_ENV, " 25 ")
        assert resolve_decay_min_trades() == 25

    def test_junk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_MIN_TRADES_ENV, "ten")
        with pytest.raises(ValueError, match=DECAY_MIN_TRADES_ENV):
            resolve_decay_min_trades()

    def test_below_one_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_MIN_TRADES_ENV, "0")
        with pytest.raises(ValueError, match="must be >= 1"):
            resolve_decay_min_trades()


class TestOracleFold:
    """The sequential oracle law (also the kernel's op-order contract)."""

    def test_empty(self) -> None:
        assert decay_math._oracle_trailing_fold([]) == (None, 0.0, 0.0)

    def test_single_sample_no_sharpe(self) -> None:
        assert decay_math._oracle_trailing_fold([0.25]) == (None, 0.0, 0.25)

    def test_flat_exact_series_no_sharpe(self) -> None:
        """0.25 is exactly representable → variance EXACTLY zero → None."""
        assert decay_math._oracle_trailing_fold([0.25, 0.25, 0.25]) == (None, 0.0, 0.75)

    def test_exact_values_hand_derived(self) -> None:
        returns = [0.1, -0.2, 0.3]
        sharpe, max_dd, net = decay_math._oracle_trailing_fold(returns)
        total = 0.1 + -0.2 + 0.3
        assert net == total
        mean = total / 3
        sq = 0.0
        for r in returns:
            sq += (r - mean) * (r - mean)
        assert sharpe == mean / math.sqrt(sq / 3)
        # equity [0.1, -0.1, 0.2] → peak stays 0.1 at index 1 → dd = 0.2
        assert max_dd == 0.1 - (0.1 + -0.2)

    def test_monotonic_down_drawdown(self) -> None:
        _, max_dd, _ = decay_math._oracle_trailing_fold([-0.1, -0.2, -0.3])
        assert max_dd == -0.1 - (-0.1 + -0.2 + -0.3)

    def test_non_finite_variance_returns_none(self) -> None:
        sharpe, _, net = decay_math._oracle_trailing_fold([1e308, -1e308])
        assert sharpe is None  # (x - mean)^2 overflows → non-finite variance
        assert net == 0.0

    def test_nan_poisons_safely(self) -> None:
        sharpe, max_dd, net = decay_math._oracle_trailing_fold([0.1, math.nan, 0.2])
        assert sharpe is None
        assert max_dd == 0.0  # NaN comparisons are false — never NaN > 0.0
        assert math.isnan(net)


class TestOracleVerdict:
    def test_healthy_at_exact_floor(self) -> None:
        """0.7 >= 0.7 exactly — the < boundary (kills < → <=)."""
        assert _verdict(0.7) == VERDICT_HEALTHY

    def test_decayed_one_ulp_below_floor(self) -> None:
        floor = 1.0 * (1.0 - DEFAULT_SHARPE_TOLERANCE)
        assert _verdict(math.nextafter(floor, -math.inf)) == VERDICT_DECAYED

    def test_decayed_above_mdd_ceiling_healthy_at(self) -> None:
        ceiling = 0.1 * (1.0 + DEFAULT_MDD_TOLERANCE)
        above = math.nextafter(ceiling, math.inf)
        assert _verdict(2.0, trailing_max_dd=above) == VERDICT_DECAYED
        assert _verdict(2.0, trailing_max_dd=ceiling) == VERDICT_HEALTHY

    def test_negative_discovery_floor_law(self) -> None:
        """The lockbox formula verbatim — a negative discovery's floor is
        LESS negative than discovery itself (disclosed, no special-casing)."""
        assert _verdict(-1.0, discovery_sharpe=-1.0) == VERDICT_DECAYED

    def test_low_n_abstains_before_everything(self) -> None:
        assert _verdict(-99.0, trailing_max_dd=9.9, n_trades=9) == VERDICT_ABSTAIN_LOW_N
        assert _verdict(-99.0, n_trades=10) == VERDICT_DECAYED  # boundary judges

    def test_undefined_abstains(self) -> None:
        assert _verdict(None) == VERDICT_ABSTAIN_UNDEFINED
        assert _verdict(math.nan) == VERDICT_ABSTAIN_UNDEFINED
        assert _verdict(math.inf) == VERDICT_ABSTAIN_UNDEFINED
        assert _verdict(1.0, trailing_max_dd=math.nan) == VERDICT_ABSTAIN_UNDEFINED

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"min_trades": 0}, "min_trades must be >= 1"),
            ({"sharpe_tolerance": -0.1}, "sharpe_tolerance"),
            ({"sharpe_tolerance": math.nan}, "sharpe_tolerance"),
            ({"mdd_tolerance": -1.0}, "mdd_tolerance"),
            ({"discovery_sharpe": math.nan}, "discovery_sharpe"),
            ({"discovery_max_dd": -0.1}, "discovery_max_dd"),
            ({"discovery_max_dd": math.inf}, "discovery_max_dd"),
        ],
    )
    def test_validation_raises(self, kwargs: dict[str, float], match: str) -> None:
        base: dict[str, object] = {
            "trailing_sharpe": 1.0,
            "discovery_sharpe": 1.0,
            "trailing_max_dd": 0.0,
            "discovery_max_dd": 0.1,
            "n_trades": 30,
            "min_trades": 10,
        }
        base.update(kwargs)
        with pytest.raises(ValueError, match=match):
            decay_verdict_token(**base)  # type: ignore[arg-type]


class TestOracleDomination:
    def test_strictly_higher_sharpe_required(self) -> None:
        common = {"challenger_max_dd": 0.1, "champion_max_dd": 0.1}
        assert champion_dominates(challenger_sharpe=1.1, champion_sharpe=1.0, **common) is True
        assert champion_dominates(challenger_sharpe=1.0, champion_sharpe=1.0, **common) is False

    def test_mdd_no_worse_required(self) -> None:
        assert (
            champion_dominates(
                challenger_sharpe=2.0,
                challenger_max_dd=0.2,
                champion_sharpe=1.0,
                champion_max_dd=0.1,
            )
            is False
        )

    @pytest.mark.parametrize(
        "field",
        ["challenger_sharpe", "challenger_max_dd", "champion_sharpe", "champion_max_dd"],
    )
    def test_non_finite_raises(self, field: str) -> None:
        kwargs = {
            "challenger_sharpe": 1.0,
            "challenger_max_dd": 0.1,
            "champion_sharpe": 0.5,
            "champion_max_dd": 0.1,
        }
        kwargs[field] = math.nan
        with pytest.raises(ValueError, match=field):
            champion_dominates(**kwargs)


class TestKernelParity:
    """The Rust ≡ oracle differential against the REAL wheel (bit-exact)."""

    @given(
        st.lists(
            st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
            max_size=64,
        )
    )
    def test_fold_bit_identical(self, returns: list[float]) -> None:
        kernel = fno_math.decay_trailing_fold(returns)
        oracle = decay_math._oracle_trailing_fold(returns)
        assert decay_math._fold_bits(tuple(kernel)) == decay_math._fold_bits(oracle)

    @given(
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
        st.integers(min_value=0, max_value=40),
    )
    def test_verdict_matches_oracle(
        self,
        trailing_sharpe: float,
        discovery_sharpe: float,
        trailing_max_dd: float,
        discovery_max_dd: float,
        n_trades: int,
    ) -> None:
        args = (
            trailing_sharpe,
            discovery_sharpe,
            trailing_max_dd,
            discovery_max_dd,
            n_trades,
            10,
            DEFAULT_SHARPE_TOLERANCE,
            DEFAULT_MDD_TOLERANCE,
        )
        assert fno_math.decay_verdict(*args) == decay_math._oracle_decay_verdict(*args)

    @given(
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=-5.0, max_value=5.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    )
    def test_domination_matches_oracle(self, cs: float, cm: float, hs: float, hm: float) -> None:
        assert fno_math.champion_dominates(cs, cm, hs, hm) is (
            decay_math._oracle_champion_dominates(cs, cm, hs, hm)
        )

    def test_kernel_validation_errors_are_valueerrors(self) -> None:
        """The pyfunction boundary maps Result::Err → ValueError."""
        with pytest.raises(ValueError, match="sharpe_tolerance"):
            fno_math.decay_verdict(1.0, 1.0, 0.0, 0.1, 30, 10, -1.0, 0.5)
        with pytest.raises(ValueError, match="champion_sharpe"):
            fno_math.champion_dominates(1.0, 0.1, math.nan, 0.1)


class _FakeKernel:
    """A poisoned kernel double (wrong values / raises / bad codes)."""

    def __init__(
        self,
        *,
        fold: tuple[float | None, float, float] | None = None,
        verdict: int | None = None,
        dominates: bool | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.fold = fold
        self.verdict = verdict
        self.dominates = dominates
        self.raise_exc = raise_exc
        self.calls = 0

    def decay_trailing_fold(self, returns: list[float]) -> tuple[float | None, float, float]:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.fold is not None
        return self.fold

    def decay_verdict(self, *args: object) -> int:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.verdict is not None
        return self.verdict

    def champion_dominates(self, *args: object) -> bool:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.dominates is not None
        return self.dominates


class TestSeamDegrades:
    def test_falsy_flag_serves_oracle_without_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_RUST_ENV, "off")

        def _bomb() -> object:
            raise AssertionError("kernel must not be imported with the flag OFF")

        monkeypatch.setattr(decay_math, "_import_kernel", _bomb)
        fold = trailing_fold([0.1, -0.2, 0.3])
        assert isinstance(fold, TrailingFold)
        assert fold.n_trades == 3
        assert _verdict(0.7) == VERDICT_HEALTHY
        assert (
            champion_dominates(
                challenger_sharpe=1.1,
                challenger_max_dd=0.1,
                champion_sharpe=1.0,
                champion_max_dd=0.1,
            )
            is True
        )

    def test_wheel_missing_degrades_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _missing() -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(decay_math, "_import_kernel", _missing)
        with structlog.testing.capture_logs() as logs:
            assert trailing_fold([0.25]).net == 0.25
            assert trailing_fold([0.25]).net == 0.25
        events = [entry for entry in logs if entry["event"] == "decay_math.kernel_fallback"]
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"

    @pytest.mark.parametrize("fn", ["fold", "verdict", "dominates"])
    def test_kernel_raise_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, fn: str
    ) -> None:
        fake = _FakeKernel(raise_exc=RuntimeError("boom"))
        monkeypatch.setattr(decay_math, "_import_kernel", lambda: fake)
        with structlog.testing.capture_logs() as logs:
            if fn == "fold":
                assert trailing_fold([0.25]).net == 0.25
            elif fn == "verdict":
                assert _verdict(0.7) == VERDICT_HEALTHY
            else:
                assert (
                    champion_dominates(
                        challenger_sharpe=1.1,
                        challenger_max_dd=0.1,
                        champion_sharpe=1.0,
                        champion_max_dd=0.1,
                    )
                    is True
                )
        assert any(entry["event"] == "decay_math.kernel_fallback" for entry in logs)
        # NOT latched — a transient raise degrades per call, never disables.
        assert decay_math._kernel_disabled is False


class TestBurnInAutorevert:
    def test_fold_mismatch_ships_oracle_and_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeKernel(fold=(9.9, 9.9, 9.9))
        monkeypatch.setattr(decay_math, "_import_kernel", lambda: fake)
        with structlog.testing.capture_logs() as logs:
            fold = trailing_fold([0.25, 0.5])
        # The ORACLE value shipped, never the poisoned kernel's.
        oracle = decay_math._oracle_trailing_fold([0.25, 0.5])
        assert (fold.sharpe, fold.max_drawdown, fold.net) == oracle
        assert decay_math._kernel_disabled is True
        assert any(entry["event"] == "decay_math.kernel_autoreverted" for entry in logs)
        # Latched: the kernel is never touched again this process.
        calls_before = fake.calls
        trailing_fold([0.25, 0.5])
        assert fake.calls == calls_before

    def test_verdict_mismatch_ships_oracle_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeKernel(verdict=1)  # claims DECAYED for a healthy input
        monkeypatch.setattr(decay_math, "_import_kernel", lambda: fake)
        assert _verdict(0.7) == VERDICT_HEALTHY  # the oracle verdict shipped
        assert decay_math._kernel_disabled is True

    def test_dominates_mismatch_ships_oracle_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeKernel(dominates=True)
        monkeypatch.setattr(decay_math, "_import_kernel", lambda: fake)
        assert (
            champion_dominates(
                challenger_sharpe=0.5,
                challenger_max_dd=0.1,
                champion_sharpe=1.0,
                champion_max_dd=0.1,
            )
            is False
        )
        assert decay_math._kernel_disabled is True

    def test_out_of_vocabulary_code_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refuse-poison: a code outside 0..3 latches even past burn-in."""
        fake = _FakeKernel(verdict=7)
        monkeypatch.setattr(decay_math, "_import_kernel", lambda: fake)
        # Exhaust the verdict burn-in so the code-range gate is the only net.
        decay_math._self_check_remaining["decay_verdict"] = 0
        assert _verdict(0.7) == VERDICT_HEALTHY
        assert decay_math._kernel_disabled is True

    def test_burn_in_exhausts_then_kernel_serves_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past SELF_CHECK_SAMPLES the oracle is no longer consulted."""
        oracle_calls = {"n": 0}
        real_oracle = decay_math._oracle_trailing_fold

        def _counting(returns: list[float]) -> tuple[float | None, float, float]:
            oracle_calls["n"] += 1
            return real_oracle(returns)

        monkeypatch.setattr(decay_math, "_oracle_trailing_fold", _counting)
        for _ in range(SELF_CHECK_SAMPLES + 3):
            trailing_fold([0.25, 0.5])
        assert oracle_calls["n"] == SELF_CHECK_SAMPLES
        assert decay_math._kernel_disabled is False

    def test_real_kernel_burn_in_stays_clean(self) -> None:
        """The REAL wheel passes its own burn-in (the anti-vacuous arm)."""
        for _ in range(SELF_CHECK_SAMPLES + 1):
            fold = trailing_fold([0.1, -0.2, 0.3])
        assert decay_math._kernel_disabled is False
        assert fold.n_trades == 3

    def test_real_kernel_verdict_and_domination_burn_in(self) -> None:
        for _ in range(SELF_CHECK_SAMPLES + 1):
            assert _verdict(0.7) == VERDICT_HEALTHY
            assert (
                champion_dominates(
                    challenger_sharpe=1.1,
                    challenger_max_dd=0.1,
                    champion_sharpe=1.0,
                    champion_max_dd=0.1,
                )
                is True
            )
        assert decay_math._kernel_disabled is False
