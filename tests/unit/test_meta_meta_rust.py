"""Unit gate for :mod:`brutex.meta.meta_rust` (the ``BRUTEX_META_GATE`` facade).

DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001: the Pattern-A
flag matrix, the OFF-arm bomb-crate spy (the crate is NEVER consulted while
OFF), wheel_missing/wheel_stale degrades, the narrow kernel-error degrade
set (MemoryError re-raises RAW), the refuse-poison output gates, the
16-call burn-in bit-compare with ship-the-ORACLE autorevert + process
latch, and the reset seam.
"""

from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import pytest
import structlog.testing
from brutex.meta.abstain import abstain_masks_python
from brutex.meta.logistic import score_logistic as oracle_score
from brutex.meta.meta_rust import (
    BURN_IN_CALLS,
    META_GATE_ENV,
    META_RUST_AUTOREVERT_EVENT,
    META_RUST_FALLBACK_EVENT,
    META_RUST_FLAG_EVENT,
    abstain_reason_masks,
    meta_gate_enabled,
    reset_meta_rust_for_tests,
    score_probabilities,
)

NAN = float("nan")
FEATURES = [0.5, -1.5, 2.0, 0.25]
WEIGHTS = [0.3, -0.7]
ABSTAIN_ARGS = (
    [0, 1, 2, 3],
    0b0111,
    [15.0, NAN, 50.0, 10.0],
    12.0,
    20.0,
    [0, 1, 0, 1],
    True,
    [0.9, 0.5, NAN, 0.7],
    0.7,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_meta_rust_for_tests()


def oracle_probs() -> list[float]:
    return oracle_score(FEATURES, 2, WEIGHTS, 0.1)


def fake_kernel(score: object = None, abstain: object = None) -> SimpleNamespace:
    """A sys.modules-installable fake wheel (the bomb-crate spy shape)."""

    def _bomb(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the crate was consulted when it must not be")

    return SimpleNamespace(
        score_logistic=score if score is not None else _bomb,
        abstain_masks=abstain if abstain is not None else _bomb,
    )


class TestFlag:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(META_GATE_ENV, raising=False)
        assert meta_gate_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_explicit_truthy_enables(self, value: str) -> None:
        assert meta_gate_enabled(value) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
    def test_falsy_and_empty_are_off(self, value: str) -> None:
        assert meta_gate_enabled(value) is False

    def test_junk_is_off_with_one_warn(self) -> None:
        with structlog.testing.capture_logs() as logs:
            assert meta_gate_enabled("bogus") is False
            assert meta_gate_enabled("bogus") is False
        flag_events = [e for e in logs if e["event"] == META_RUST_FLAG_EVENT]
        assert len(flag_events) == 1

    def test_env_read_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(META_GATE_ENV, "1")
        assert meta_gate_enabled() is True
        monkeypatch.setenv(META_GATE_ENV, "0")
        assert meta_gate_enabled() is False


class TestOffArm:
    def test_off_serves_the_oracle_and_never_touches_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(META_GATE_ENV, raising=False)
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel())  # bomb
        assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        assert abstain_reason_masks(*ABSTAIN_ARGS) == abstain_masks_python(*ABSTAIN_ARGS)

    def test_off_arm_propagates_oracle_validation_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(META_GATE_ENV, raising=False)
        with pytest.raises(ValueError, match="n_features"):
            score_probabilities([], 0, [], 0.0)
        with pytest.raises(ValueError, match="column lengths"):
            abstain_reason_masks([0], 1, [], NAN, NAN, [0], True, [NAN], NAN)


class TestWheelDegrades:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(META_GATE_ENV, "1")

    def test_wheel_missing_degrades_warn_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_meta", None)  # import raises
        with structlog.testing.capture_logs() as logs:
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
            assert abstain_reason_masks(*ABSTAIN_ARGS) == abstain_masks_python(*ABSTAIN_ARGS)
        fallbacks = [e for e in logs if e["event"] == META_RUST_FALLBACK_EVENT]
        assert [e["reason"] for e in fallbacks] == ["wheel_missing"]

    def test_wheel_stale_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = SimpleNamespace(score_logistic=lambda *a: [], abstain_masks=None)
        monkeypatch.setitem(sys.modules, "brutex_meta", stale)
        with structlog.testing.capture_logs() as logs:
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        assert [e["reason"] for e in logs if e["event"] == META_RUST_FALLBACK_EVENT] == [
            "wheel_stale"
        ]

    @pytest.mark.parametrize(
        "exc", [ValueError("x"), RuntimeError("panic"), TypeError("t"), OverflowError("o")]
    )
    def test_kernel_errors_degrade_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(*_args: object) -> object:
            raise exc

        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=_raise, abstain=_raise))
        with structlog.testing.capture_logs() as logs:
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
            assert abstain_reason_masks(*ABSTAIN_ARGS) == abstain_masks_python(*ABSTAIN_ARGS)
        reasons = [e["reason"] for e in logs if e["event"] == META_RUST_FALLBACK_EVENT]
        assert reasons == ["kernel_error"]  # warn-once across BOTH kernels

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_global_stops_reraise_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _raise(*_args: object) -> object:
            raise exc_type()

        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=_raise, abstain=_raise))
        with pytest.raises(exc_type):
            score_probabilities(FEATURES, 2, WEIGHTS, 0.1)
        with pytest.raises(exc_type):
            abstain_reason_masks(*ABSTAIN_ARGS)


class TestRefusePoison:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(META_GATE_ENV, "1")

    @pytest.mark.parametrize(
        ("poison", "detail"),
        [
            ([0.5], "result_shape"),  # wrong row count
            ((0.5, 0.5), "result_shape"),  # not a list
            ([0.5, "x"], "result_types"),
            ([0.5, 1.5], "probability_range"),  # outside [0, 1]
            ([0.5, NAN], "probability_range"),  # non-finite on finite inputs
        ],
    )
    def test_poisoned_scorer_output_degrades(
        self, monkeypatch: pytest.MonkeyPatch, poison: object, detail: str
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=lambda *a: poison))
        with structlog.testing.capture_logs() as logs:
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        invalid = [e for e in logs if e["event"] == META_RUST_FALLBACK_EVENT]
        assert invalid and invalid[0]["detail"] == detail

    def test_non_finite_inputs_permit_non_finite_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A NaN input legitimately yields NaN — the range gate only fires on
        # all-finite inputs (never refuses honest IEEE propagation).
        def _kernel(f: list[float], n: int, w: list[float], b: float) -> list[float]:
            return oracle_score(f, n, w, b)

        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=_kernel))
        result = score_probabilities([NAN], 1, [1.0], 0.0)
        assert math.isnan(result[0])

    @pytest.mark.parametrize(
        ("poison", "detail"),
        [
            ([0, 0, 0], "result_shape"),  # wrong length
            ("abc", "result_shape"),  # not bytes/list
            ([0, 1, 2, 99], "mask_range"),  # beyond the 5-bit vocabulary
            ([0, 1, 2, -1], "mask_range"),
            ([0, 1, 2, 3.5], "mask_range"),
        ],
    )
    def test_poisoned_abstain_output_degrades(
        self, monkeypatch: pytest.MonkeyPatch, poison: object, detail: str
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(abstain=lambda *a: poison))
        with structlog.testing.capture_logs() as logs:
            assert abstain_reason_masks(*ABSTAIN_ARGS) == abstain_masks_python(*ABSTAIN_ARGS)
        invalid = [e for e in logs if e["event"] == META_RUST_FALLBACK_EVENT]
        assert invalid and invalid[0]["detail"] == detail

    def test_bytes_result_is_first_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The real PyO3 edge returns Vec<u8> as Python bytes.
        expected = abstain_masks_python(*ABSTAIN_ARGS)
        monkeypatch.setitem(
            sys.modules, "brutex_meta", fake_kernel(abstain=lambda *a: bytes(expected))
        )
        assert abstain_reason_masks(*ABSTAIN_ARGS) == expected


class TestBurnIn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(META_GATE_ENV, "1")

    def test_matching_kernel_serves_through_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _kernel(f: list[float], n: int, w: list[float], b: float) -> list[float]:
            calls["n"] += 1
            return oracle_score(f, n, w, b)

        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=_kernel))
        for _ in range(BURN_IN_CALLS + 4):
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        assert calls["n"] == BURN_IN_CALLS + 4  # the kernel keeps serving

    def test_mismatch_ships_the_oracle_and_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        drifted = [min(1.0, p + 1e-9) for p in oracle_probs()]
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=lambda *a: list(drifted)))
        with structlog.testing.capture_logs() as logs:
            served = score_probabilities(FEATURES, 2, WEIGHTS, 0.1)
        assert served == oracle_probs()  # the ORACLE value ships
        assert [e["event"] for e in logs if e["event"] == META_RUST_AUTOREVERT_EVENT] == [
            META_RUST_AUTOREVERT_EVENT
        ]
        # Latched: the crate is NEVER consulted again this process (bomb).
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel())
        assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        assert abstain_reason_masks(*ABSTAIN_ARGS) == abstain_masks_python(*ABSTAIN_ARGS)

    def test_abstain_mismatch_latches_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        good = abstain_masks_python(*ABSTAIN_ARGS)
        drifted = list(good)
        drifted[0] ^= 1  # flip one reason bit
        monkeypatch.setitem(
            sys.modules, "brutex_meta", fake_kernel(abstain=lambda *a: list(drifted))
        )
        with structlog.testing.capture_logs() as logs:
            assert abstain_reason_masks(*ABSTAIN_ARGS) == good
        assert any(e["event"] == META_RUST_AUTOREVERT_EVENT for e in logs)

    def test_post_burn_in_the_kernel_word_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # HONEST envelope pin: after BURN_IN_CALLS bit-identical calls the
        # kernel serves alone — a later drift is NOT re-compared (the
        # documented burn-in window law, the #1531/#1532 shape).
        state = {"n": 0}
        drifted = [min(1.0, p + 1e-9) for p in oracle_probs()]

        def _kernel(f: list[float], n: int, w: list[float], b: float) -> list[float]:
            state["n"] += 1
            if state["n"] <= BURN_IN_CALLS:
                return oracle_score(f, n, w, b)
            return list(drifted)

        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=_kernel))
        for _ in range(BURN_IN_CALLS):
            score_probabilities(FEATURES, 2, WEIGHTS, 0.1)
        assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == drifted

    def test_reset_seam_clears_the_latch_and_counters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drifted = [min(1.0, p + 1e-9) for p in oracle_probs()]
        monkeypatch.setitem(sys.modules, "brutex_meta", fake_kernel(score=lambda *a: list(drifted)))
        score_probabilities(FEATURES, 2, WEIGHTS, 0.1)  # latches
        reset_meta_rust_for_tests()
        # Un-latched again: the kernel is consulted (and re-latches).
        with structlog.testing.capture_logs() as logs:
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()
        assert any(e["event"] == META_RUST_AUTOREVERT_EVENT for e in logs)


class TestRealWheel:
    """Parity against the COMPILED crate (skipped when the wheel is absent)."""

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_meta", reason="brutex-meta wheel not built")
        monkeypatch.setenv(META_GATE_ENV, "1")

    def test_score_parity_through_burn_in(self) -> None:
        for _ in range(BURN_IN_CALLS + 2):
            assert score_probabilities(FEATURES, 2, WEIGHTS, 0.1) == oracle_probs()

    def test_abstain_parity_through_burn_in(self) -> None:
        expected = abstain_masks_python(*ABSTAIN_ARGS)
        for _ in range(BURN_IN_CALLS + 2):
            assert abstain_reason_masks(*ABSTAIN_ARGS) == expected

    def test_structural_junk_still_raises_valueerror(self) -> None:
        # The kernel's ValueError degrades to the oracle, which raises the
        # SAME class — the caller contract is arm-independent.
        with pytest.raises(ValueError, match="n_features"):
            score_probabilities([1.0], 0, [], 0.0)

    def test_concrete_ffi_return_types_on_the_real_wheel(self) -> None:
        # N-1 truth pin (probed on the BUILT wheel, pyo3 0.29): `Vec<u8>`
        # crosses as Python `bytes` — the .pyi's documented contract — and
        # `Vec<f64>` as `list[float]`. A pyo3 bump that changes either
        # concrete type fails HERE, not silently in the facade normalizer.
        import brutex_meta

        masks = brutex_meta.abstain_masks([0], 1, [NAN], NAN, NAN, [0], False, [NAN], NAN)
        assert type(masks) is bytes
        probs = brutex_meta.score_logistic([0.0], 1, [1.0], 0.0)
        assert type(probs) is list
        assert all(type(p) is float for p in probs)
