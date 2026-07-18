"""The ``brutex.models_rust`` facade suite (slice T35 PR-2 — the models seam).

Wheel-FREE by design (every crate here is a fake injected via
``sys.modules``), so the 100% line+branch gate on the facade holds in main
CI where the crate is deliberately never installed; the
:class:`TestRealCrateParity` legs importorskip the real wheel (the
core-rust-parity lane + local builds execute them).
"""

from __future__ import annotations

import math
import sys
import types
from collections.abc import Callable, Iterator
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DecimalException,
    Inexact,
    Rounded,
    getcontext,
    localcontext,
)
from pathlib import Path

import pytest
from brutex import models, models_rust
from hypothesis import given, settings
from hypothesis import strategies as st

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-unset with a fresh latch/burn-in/memo."""
    monkeypatch.delenv(models_rust.MODELS_RUST_ENV, raising=False)
    models_rust.reset_models_rust_for_tests()
    yield
    models_rust.reset_models_rust_for_tests()


def _law_faithful(kind: str, payload: str) -> str:
    """A fake-crate default modeling the REAL crate's context-independent
    decision law — computed through the Python oracle under a fresh
    DEFAULT context, with refusals re-signalled as ``tick_refuse:*``
    ``ValueError``s (the crate protocol)."""
    rebuilt: float | int | Decimal | str
    if kind == "decimal":
        rebuilt = Decimal(payload)
    elif kind == "int":
        rebuilt = int(payload)
    elif kind == "float":
        rebuilt = float(payload)
    elif kind == "str":
        rebuilt = payload
    else:  # pragma: no cover — fakes never send junk kinds
        raise ValueError(f"unknown tick kind: {kind!r}")
    with localcontext(Context()):
        try:
            return str(models._normalise_tick_size_python(rebuilt))  # pyright: ignore[reportPrivateUsage]
        except models.TickSizeFormatError as exc:
            raise ValueError(f"{models_rust.REFUSE_PREFIX}oracle: {exc}") from exc


def _fake_crate(**overrides: object) -> types.ModuleType:
    """A fake ``brutex_core`` carrying the models-seam API surface."""
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    crate.models_normalise_tick_size_str = overrides.pop(  # type: ignore[attr-defined]
        "models_normalise_tick_size_str", _law_faithful
    )
    for name, value in overrides.items():
        setattr(crate, name, value)
    return crate


def _bomb_crate() -> types.ModuleType:
    """A crate whose every call is a test failure (the OFF-arm spy)."""

    def _boom(_kind: str, _payload: str) -> str:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(models_normalise_tick_size_str=_boom)


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    """Set the flag ON and install *crate* as ``brutex_core``."""
    monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


def _route(raw: float | int | Decimal | str) -> Decimal:
    return models_rust.route_normalise_tick_size(
        raw,
        models._normalise_tick_size_python,  # pyright: ignore[reportPrivateUsage]
        models.TickSizeFormatError,
    )


# ── Flag matrix ─────────────────────────────────────────────────────────────


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, raw)
        assert models_rust.models_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off ", "", "   "])
    def test_falsy_and_blank_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, raw)
        assert models_rust.models_rust_enabled() is False
        assert not models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_unset_is_off_via_the_absent_fast_path(self) -> None:
        assert models_rust.models_rust_enabled() is False

    def test_junk_warns_once_then_memoised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "banana")
        assert models_rust.models_rust_enabled() is False
        assert "flag_unrecognised" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]
        assert models_rust.models_rust_enabled() is False  # the memoised repeat

    def test_flag_vocabulary_mirror_pinned_to_sweep_lookahead(self) -> None:
        """The LEAF-discipline local twin cannot drift from the engine home."""
        sweep_lookahead = pytest.importorskip("brutex.engine.sweep_lookahead")
        assert models_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert models_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]

    def test_valid_set_mirror_pinned_to_models(self) -> None:
        """The refuse-poison member set ≡ ``VALID_TICK_SIZES_RUPEES``."""
        expected = {f"{d:.2f}" for d in models.VALID_TICK_SIZES_RUPEES}
        assert frozenset(expected) == models_rust._VALID_2DP_STRINGS  # pyright: ignore[reportPrivateUsage]


# ── Latch / reset seam ─────────────────────────────────────────────────────


class TestResetSeamAndLatch:
    def test_disable_latches_and_repeats_are_noops(self) -> None:
        models_rust._disable("first")  # pyright: ignore[reportPrivateUsage]
        assert models_rust.models_rust_disabled()
        models_rust._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert models_rust._DISABLED_REASON[0] == "first"  # pyright: ignore[reportPrivateUsage]

    def test_disable_error_arm(self) -> None:
        models_rust._disable("bad", error=True)  # pyright: ignore[reportPrivateUsage]
        assert models_rust._DISABLED_REASON[0] == "bad"  # pyright: ignore[reportPrivateUsage]

    def test_reset_restores_live_state(self) -> None:
        models_rust._disable("x")  # pyright: ignore[reportPrivateUsage]
        models_rust._BURN_IN_REMAINING[0] = 0  # pyright: ignore[reportPrivateUsage]
        models_rust._OBSERVABILITY_MEMO.add("y")  # pyright: ignore[reportPrivateUsage]
        models_rust.reset_models_rust_for_tests()
        assert not models_rust.models_rust_disabled()
        assert models_rust._BURN_IN_REMAINING[0] == models_rust.BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]
        assert not models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        models_rust.reset_models_rust_for_tests(remaining=0)
        assert models_rust._BURN_IN_REMAINING[0] == 0  # pyright: ignore[reportPrivateUsage]

    def test_warn_once_dedupes(self) -> None:
        models_rust._warn_once("r1")  # pyright: ignore[reportPrivateUsage]
        models_rust._warn_once("r1")  # pyright: ignore[reportPrivateUsage]
        assert "r1" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_disabled_route_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        models_rust._disable("latched")  # pyright: ignore[reportPrivateUsage]
        assert _route(Decimal("0.05")) == Decimal("0.05")


# ── Flag-OFF discipline (the never-routes spies) ───────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_public_law_flag_off_is_python_and_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert models.normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert models.normalise_tick_size(5) == Decimal("0.05")
        with pytest.raises(models.TickSizeFormatError):
            models.normalise_tick_size(0.07)

    def test_explicit_falsy_flag_is_crate_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert models.normalise_tick_size("0.10") == Decimal("0.10")


# ── Import / handshake guard ───────────────────────────────────────────────


class TestImportGuard:
    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, None)  # sys.modules[name] = None → ImportError
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "crate_unavailable"  # pyright: ignore[reportPrivateUsage]

    def test_stale_wheel_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, types.ModuleType("brutex_core"))  # no API surface
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    def test_non_callable_api_member_is_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = types.ModuleType("brutex_core")
        crate.models_normalise_tick_size_str = "not callable"  # type: ignore[attr-defined]
        _arm(monkeypatch, crate)
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]


# ── Eligibility gate ───────────────────────────────────────────────────────


class TestEligibilityGate:
    """Ineligible inputs take the oracle with the crate UNTOUCHED (bomb spy)."""

    @pytest.mark.parametrize(
        "raw",
        [
            Decimal("NaN"),  # non-finite → oracle's membership raise
            Decimal("Infinity"),
            Decimal("1E+30"),  # adjusted > 6 → oracle's raw InvalidOperation arm
            Decimal("1E-31"),  # exponent < -30
            " 0.05 ",  # whitespace str — Decimal() accepts, crate grammar not
            "5e-2",  # exponent form
            "0.05٠",  # Unicode digit tail
            "1" * 65,  # over the str length bound
            True,  # bool — the oracle owns the trap
            False,
            10**9,  # int magnitude bound
            -(10**9),
            float("nan"),
            float("inf"),
            1e16,  # str(float) → "1e+16" — not fixed-point
        ],
    )
    def test_ineligible_inputs_take_oracle_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch, raw: float | int | Decimal | str
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        try:
            expected: object = models._normalise_tick_size_python(raw)  # pyright: ignore[reportPrivateUsage]
        except Exception as exc:
            expected = type(exc)
        try:
            actual: object = _route(raw)
        except Exception as exc:
            actual = type(exc)
        assert actual == expected
        assert not models_rust.models_rust_disabled()

    def test_decimal_subclass_takes_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class MyDecimal(Decimal):
            pass

        _arm(monkeypatch, _bomb_crate())
        assert _route(MyDecimal("0.05")) == Decimal("0.05")

    def test_non_union_object_takes_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A junk type reaches the oracle's own unsupported-type raise."""
        _arm(monkeypatch, _bomb_crate())
        with pytest.raises(models.TickSizeFormatError, match="Unsupported tick_size type"):
            _route([0.05])  # type: ignore[arg-type]

    def test_encode_shapes(self) -> None:
        encode = models_rust._encode_tick_input  # pyright: ignore[reportPrivateUsage]
        assert encode(Decimal("5E-2")) == ("decimal", "0.05")
        assert encode(Decimal("0.050")) == ("decimal", "0.050")
        assert encode("0.05") == ("str", "0.05")
        assert encode(5) == ("int", "5")
        assert encode(-7) == ("int", "-7")  # the crate owns the range DECISION
        assert encode(5.0) == ("float", "5.0")

    @pytest.mark.parametrize(
        ("armed_trap", "prec", "emax", "emin"),
        [
            (Inexact, 28, 999999, -999999),
            (Rounded, 28, 999999, -999999),
            (None, 8, 999999, -999999),  # prec below the floor
            (None, 28, 6, -999999),  # Emax below the floor
            (None, 28, 999999, -6),  # Emin above the ceiling
        ],
    )
    def test_context_hazards_route_to_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        armed_trap: type[DecimalException] | None,
        prec: int,
        emax: int,
        emin: int,
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        with localcontext() as ctx:
            ctx.prec = prec
            ctx.Emax = emax
            ctx.Emin = emin
            if armed_trap is not None:
                ctx.traps[armed_trap] = True
            assert models_rust._encode_tick_input(Decimal("0.05")) is None  # pyright: ignore[reportPrivateUsage]
        assert models_rust._encode_tick_input(Decimal("0.05")) is not None  # pyright: ignore[reportPrivateUsage]


# ── The ambient-rounding guard (refuter r1) ────────────────────────────────

#: Every stdlib decimal rounding mode OTHER than the crate's pinned
#: ``ROUND_HALF_EVEN`` — each one is a gate hazard (oracle-routed).
_NON_HALF_EVEN_MODES: list[str] = [
    ROUND_UP,
    ROUND_DOWN,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    ROUND_HALF_DOWN,
    ROUND_05UP,
]


class TestAmbientRoundingGuard:
    """Refuter r1: an ambient rounding mode != ``ROUND_HALF_EVEN`` is a
    gate hazard — the oracle's un-pinned ``quantize(Decimal("0.01"))``
    follows the ambient mode while the crate pins HALF_EVEN, so a
    near-boundary candidate (``Decimal("0.104999")``) can flip
    member ⟷ non-member between the arms.
    """

    def test_refuter_counterexample_round_up_steady_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The executed counterexample verbatim: flag-ON in the post-burn-in
        steady state, under an ambient ``ROUND_UP`` context,
        ``normalise_tick_size(Decimal("0.104999"))`` raises
        ``TickSizeFormatError`` with the byte-same message as flag-OFF —
        and the bomb crate proves the crate was NEVER touched (gate-routed).
        """
        _arm(monkeypatch, _bomb_crate())
        models_rust.reset_models_rust_for_tests(remaining=0)  # steady state
        with localcontext() as ctx:
            ctx.rounding = ROUND_UP
            with pytest.raises(models.TickSizeFormatError) as flag_on:
                models.normalise_tick_size(Decimal("0.104999"))
        with localcontext() as ctx:
            ctx.rounding = ROUND_UP
            with pytest.raises(models.TickSizeFormatError) as flag_off:
                models._normalise_tick_size_python(  # pyright: ignore[reportPrivateUsage]
                    Decimal("0.104999")
                )
        assert str(flag_on.value) == str(flag_off.value)
        assert not models_rust.models_rust_disabled()

    @pytest.mark.parametrize("mode", _NON_HALF_EVEN_MODES)
    def test_every_non_half_even_mode_is_a_hazard(self, mode: str) -> None:
        with localcontext() as ctx:
            ctx.rounding = mode
            assert models_rust._context_hazard() is True  # pyright: ignore[reportPrivateUsage]
            assert models_rust._encode_tick_input(Decimal("0.05")) is None  # pyright: ignore[reportPrivateUsage]
        assert models_rust._context_hazard() is False  # pyright: ignore[reportPrivateUsage]

    def test_default_half_even_context_engages_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The positive twin: the DEFAULT context (``ROUND_HALF_EVEN``) is
        non-hazardous and the crate is GENUINELY engaged."""
        calls: list[tuple[str, str]] = []

        def _spy(kind: str, payload: str) -> str:
            calls.append((kind, payload))
            return _law_faithful(kind, payload)

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_spy))
        assert getcontext().rounding == ROUND_HALF_EVEN
        assert models_rust._context_hazard() is False  # pyright: ignore[reportPrivateUsage]
        assert models.normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert calls == [("decimal", "0.05")]

    @pytest.mark.parametrize(
        "raw",
        [Decimal("0.104999"), Decimal("0.05"), Decimal("0.07")],
        ids=["counterexample", "member", "refuse"],
    )
    def test_flag_on_equals_flag_off_under_round_up(
        self, monkeypatch: pytest.MonkeyPatch, raw: Decimal
    ) -> None:
        """Flag-ON ≡ flag-OFF under an ambient ``ROUND_UP`` context over the
        small matrix (counterexample · member · refuse) — every input
        oracle-routed (bomb spy), outcomes identical."""
        _arm(monkeypatch, _bomb_crate())
        models_rust.reset_models_rust_for_tests(remaining=0)

        def _outcome(
            law: Callable[[float | int | Decimal | str], Decimal],
        ) -> tuple[str, str]:
            with localcontext() as ctx:
                ctx.rounding = ROUND_UP
                try:
                    return ("value", str(law(raw)))
                except models.TickSizeFormatError as exc:
                    return ("refuse", str(exc))

        assert _outcome(models.normalise_tick_size) == _outcome(
            models._normalise_tick_size_python  # pyright: ignore[reportPrivateUsage]
        )
        assert not models_rust.models_rust_disabled()


# ── Degrade paths ──────────────────────────────────────────────────────────


class TestDegradePaths:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("plain parse fault"),
            RuntimeError("panic map"),
            TypeError("conv"),
            OverflowError("big"),
            AttributeError("broken"),
        ],
    )
    def test_crate_fault_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raiser(_kind: str, _payload: str) -> str:
            raise exc

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_raiser))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert not models_rust.models_rust_disabled()  # degrade, never a latch
        assert "normalise_tick_size_error" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raiser(_kind: str, _payload: str) -> str:
            raise MemoryError

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_raiser))
        with pytest.raises(MemoryError):
            _route(Decimal("0.05"))

    def test_keyboard_interrupt_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raiser(_kind: str, _payload: str) -> str:
            raise KeyboardInterrupt

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_raiser))
        with pytest.raises(KeyboardInterrupt):
            _route(Decimal("0.05"))

    def test_out_of_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raiser(_kind: str, _payload: str) -> str:
            raise KeyError("broken FFI contract")

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_raiser))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]


class TestPanicBelt:
    def test_no_pyo3_runtime_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)  # → ImportError arm
        assert models_rust._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_panic_exception_joins_belt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert models_rust._panic_belt_errors() == (Exception, PanicException)  # pyright: ignore[reportPrivateUsage]

    def test_non_class_panic_attr_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not a type"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert models_rust._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_true_panic_exception_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)

        def _raiser(_kind: str, _payload: str) -> str:
            raise PanicException("boom")

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_raiser))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]


# ── Refuse-poison gate ─────────────────────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.parametrize("poison", [b"0.05", 5, None, 0.05])
    def test_non_str_return_refused(self, monkeypatch: pytest.MonkeyPatch, poison: object) -> None:
        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=lambda _k, _p: poison))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert "normalise_tick_size_result_refused" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize("poison", ["0.07", "0.050", "junk", "", "5.00extra"])
    def test_non_member_string_refused(self, monkeypatch: pytest.MonkeyPatch, poison: str) -> None:
        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=lambda _k, _p: poison))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert "normalise_tick_size_result_refused" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]
        assert not models_rust.models_rust_disabled()


# ── The refuse-vs-fault protocol ───────────────────────────────────────────


class TestRefuseProtocol:
    def test_refusal_marker_serves_oracle_raise_exact_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate())
        with pytest.raises(models.TickSizeFormatError) as routed:
            _route(Decimal("0.07"))
        with pytest.raises(models.TickSizeFormatError) as oracle:
            models._normalise_tick_size_python(Decimal("0.07"))  # pyright: ignore[reportPrivateUsage]
        assert str(routed.value) == str(oracle.value)
        assert "normalise_tick_size_error" not in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_paise_range_refusal_serves_oracle_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        with pytest.raises(models.TickSizeFormatError, match="paise range"):
            _route(5000)

    def test_steady_state_refusal_skips_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        models_rust.reset_models_rust_for_tests(remaining=0)
        with pytest.raises(models.TickSizeFormatError):
            _route(Decimal("0.07"))
        assert models_rust._BURN_IN_REMAINING[0] == 0  # pyright: ignore[reportPrivateUsage]


# ── Burn-in self-check ─────────────────────────────────────────────────────


class TestBurnIn:
    def test_value_mismatch_autoreverts_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A member string that is NOT the oracle's answer for 0.05.
        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=lambda _k, _p: "0.10"))
        assert _route(Decimal("0.05")) == Decimal("0.05")
        assert models_rust._DISABLED_REASON[0] == "burn_in_mismatch_normalise_tick_size"  # pyright: ignore[reportPrivateUsage]

    def test_false_refusal_autoreverts_and_serves_oracle_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _false_refuse(_kind: str, _payload: str) -> str:
            raise ValueError(f"{models_rust.REFUSE_PREFIX}bogus")

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_false_refuse))
        assert _route(Decimal("0.05")) == Decimal("0.05")  # the oracle VALUE, served
        assert models_rust._DISABLED_REASON[0] == "burn_in_mismatch_normalise_tick_size"  # pyright: ignore[reportPrivateUsage]

    def test_false_value_where_oracle_refuses_autoreverts_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=lambda _k, _p: "0.05"))
        with pytest.raises(models.TickSizeFormatError):
            _route(Decimal("0.07"))  # the oracle raise, served
        assert models_rust._DISABLED_REASON[0] == "burn_in_mismatch_normalise_tick_size"  # pyright: ignore[reportPrivateUsage]

    def test_burn_in_counts_down_and_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        for _ in range(models_rust.BURN_IN_SAMPLES):
            assert _route(Decimal("0.25")) == Decimal("0.25")
        assert models_rust._BURN_IN_REMAINING[0] == 0  # pyright: ignore[reportPrivateUsage]
        assert not models_rust.models_rust_disabled()
        # Steady state: still the crate value, no oracle in the path now.
        assert _route(5) == Decimal("0.05")

    def test_oracle_outcome_value_and_refuse_arms(self) -> None:
        outcome = models_rust._oracle_outcome(  # pyright: ignore[reportPrivateUsage]
            models._normalise_tick_size_python,  # pyright: ignore[reportPrivateUsage]
            Decimal("0.05"),
            models.TickSizeFormatError,
        )
        assert outcome == ("value", "0.05")
        outcome = models_rust._oracle_outcome(  # pyright: ignore[reportPrivateUsage]
            models._normalise_tick_size_python,  # pyright: ignore[reportPrivateUsage]
            Decimal("0.07"),
            models.TickSizeFormatError,
        )
        assert outcome == ("refuse", "")


# ── Routed wiring (the public entry, flag ON, fake crate) ──────────────────


class TestRoutedWiring:
    def test_public_law_routes_through_crate_when_engaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def _spy(kind: str, payload: str) -> str:
            calls.append((kind, payload))
            return _law_faithful(kind, payload)

        _arm(monkeypatch, _fake_crate(models_normalise_tick_size_str=_spy))
        assert models.normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert models.normalise_tick_size(5) == Decimal("0.05")
        assert models.normalise_tick_size(5.0) == Decimal("0.05")
        assert models.normalise_tick_size("0.10") == Decimal("0.10")
        assert calls == [("decimal", "0.05"), ("int", "5"), ("float", "5.0"), ("str", "0.10")]
        assert "__engaged__" in models_rust._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_adopted_value_is_exact_decimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _fake_crate())
        models_rust.reset_models_rust_for_tests(remaining=0)
        adopted = _route(Decimal("0.10"))
        assert adopted == Decimal("0.10")
        assert str(adopted) == "0.10"  # exponent-exact, not just numerically equal


# ── Hypothesis differential (fake law-faithful crate) ──────────────────────


_TICK_INPUTS = st.one_of(
    st.sampled_from([Decimal(s) for s in ("0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00")]),
    st.decimals(allow_nan=False, allow_infinity=False, places=4, min_value=-100, max_value=100),
    st.integers(min_value=-2000, max_value=2000),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-2000, max_value=2000),
    st.sampled_from(["0.05", "0.10", "1", "junk", " 0.05", "5e-2", "-0.05", "0.07"]),
)


class TestHypothesisDifferential:
    @settings(max_examples=200, deadline=None)
    @given(raw=_TICK_INPUTS)
    def test_flag_on_outcome_equals_oracle(self, raw: float | int | Decimal | str) -> None:
        """Routed outcome ≡ oracle outcome (value bytes or raise type+bytes)
        for arbitrary inputs, engaged through the law-faithful fake."""
        # NOTE: hypothesis + function-scoped monkeypatch don't mix; install
        # the crate + flag by hand and restore in finally.
        prior_module = sys.modules.get("brutex_core")
        import os

        prior_env = os.environ.get(models_rust.MODELS_RUST_ENV)
        sys.modules["brutex_core"] = _fake_crate()
        os.environ[models_rust.MODELS_RUST_ENV] = "1"
        models_rust.reset_models_rust_for_tests()
        try:
            try:
                expected: tuple[str, object] = (
                    "value",
                    str(models._normalise_tick_size_python(raw)),  # pyright: ignore[reportPrivateUsage]
                )
            except models.TickSizeFormatError as exc:
                expected = ("refuse", str(exc))
            try:
                actual: tuple[str, object] = ("value", str(models.normalise_tick_size(raw)))
            except models.TickSizeFormatError as exc:
                actual = ("refuse", str(exc))
            assert actual == expected
            assert not models_rust.models_rust_disabled()
        finally:
            if prior_module is None:
                sys.modules.pop("brutex_core", None)
            else:
                sys.modules["brutex_core"] = prior_module
            if prior_env is None:
                os.environ.pop(models_rust.MODELS_RUST_ENV, None)
            else:
                os.environ[models_rust.MODELS_RUST_ENV] = prior_env
            models_rust.reset_models_rust_for_tests()


# ── Real-crate legs (importorskip — the parity lane executes these) ────────


class TestRealCrateParity:
    """Flag-ON legs against the real ``brutex_core`` wheel."""

    @pytest.fixture(autouse=True)
    def _require_wheel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )
        if not callable(getattr(crate, "models_normalise_tick_size_str", None)):
            pytest.skip("installed brutex-core wheel predates the models seam")
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "1")

    def test_refuse_prefix_mirror_pinned_to_crate(self) -> None:
        """The facade's marker ≡ the crate's actual refusal message prefix."""
        import brutex_core

        with pytest.raises(ValueError, match="not_valid") as excinfo:
            brutex_core.models_normalise_tick_size_str("decimal", "0.07")
        assert str(excinfo.value).startswith(models_rust.REFUSE_PREFIX)

    def test_edge_corpus_byte_identical_both_arms(self) -> None:
        cells: list[float | int | Decimal | str] = [
            *(Decimal(s) for s in ("0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00")),
            Decimal("0.049999"),
            Decimal("0.095"),
            Decimal("0.105"),
            Decimal("0.045"),
            Decimal("0.055"),
            Decimal("0.0450001"),
            Decimal("9.999"),
            Decimal("-0.001"),
            Decimal("0.050"),
            Decimal("0"),
            Decimal("-5"),
            1,
            5,
            10,
            25,
            50,
            100,
            500,
            999,
            0,
            1000,
            -5,
            5.0,
            10.0,
            25.0,
            100.0,
            1.0,
            0.1,
            0.05,
            1.5,
            -5.0,
            0.07,
            "0.05",
            "0.10",
            "1",
            "5",
            "0.07",
            "-0.05",
            "00.05",
        ]
        models_rust.reset_models_rust_for_tests(remaining=0)  # no oracle cushion
        for raw in cells:
            try:
                expected: tuple[str, object] = (
                    "value",
                    str(models._normalise_tick_size_python(raw)),  # pyright: ignore[reportPrivateUsage]
                )
            except models.TickSizeFormatError as exc:
                expected = ("refuse", str(exc))
            try:
                actual: tuple[str, object] = ("value", str(models.normalise_tick_size(raw)))
            except models.TickSizeFormatError as exc:
                actual = ("refuse", str(exc))
            assert actual == expected, f"divergence at {raw!r}"
        assert not models_rust.models_rust_disabled()

    def test_crate_genuinely_engaged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An engagement spy — a silently-degrading run would be vacuous."""
        import brutex_core

        calls: list[tuple[str, str]] = []
        real = brutex_core.models_normalise_tick_size_str

        def _spy(kind: str, payload: str) -> str:
            calls.append((kind, payload))
            return real(kind, payload)

        monkeypatch.setattr(brutex_core, "models_normalise_tick_size_str", _spy)
        models_rust.reset_models_rust_for_tests(remaining=0)
        assert models.normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert calls == [("decimal", "0.05")]

    @settings(max_examples=300, deadline=None)
    @given(raw=_TICK_INPUTS)
    def test_hypothesis_differential_real_crate(self, raw: float | int | Decimal | str) -> None:
        models_rust.reset_models_rust_for_tests(remaining=0)
        try:
            expected: tuple[str, object] = (
                "value",
                str(models._normalise_tick_size_python(raw)),  # pyright: ignore[reportPrivateUsage]
            )
        except models.TickSizeFormatError as exc:
            expected = ("refuse", str(exc))
        try:
            actual: tuple[str, object] = ("value", str(models.normalise_tick_size(raw)))
        except models.TickSizeFormatError as exc:
            actual = ("refuse", str(exc))
        assert actual == expected
        assert not models_rust.models_rust_disabled()


# ── Encoder totality vs math module use ────────────────────────────────────


class TestEncoderInternals:
    def test_float_nonfinite_guard_uses_isfinite(self) -> None:
        assert math.isfinite(0.05)  # the guard's dependency, import-pinned
        assert models_rust._encode_tick_input(float("-inf")) is None  # pyright: ignore[reportPrivateUsage]


# ── Workflow wiring pins ────────────────────────────────────────────────────


class TestWorkflowWiring:
    _WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

    def test_mutation_pair_wired(self) -> None:
        text = (self._WORKFLOWS / "mutation.yml").read_text(encoding="utf-8")
        pair = '"brutex/models_rust.py:tests/unit/test_models_rust.py:"'
        assert pair in text, (
            "models_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )

    def test_parity_lane_carries_the_models_flag_on_leg(self) -> None:
        text = (self._WORKFLOWS / "core-rust-parity.yml").read_text(encoding="utf-8")
        assert "BRUTEX_CORE_RUST_MODELS" in text, "core-rust-parity.yml lost the models flag-ON leg"
        assert "tests/unit/test_models_rust.py" in text, (
            "core-rust-parity.yml no longer runs the models facade suite"
        )
        assert "tests/unit/test_models_type_contract.py" in text, (
            "core-rust-parity.yml no longer runs the frozen type-contract battery"
        )
