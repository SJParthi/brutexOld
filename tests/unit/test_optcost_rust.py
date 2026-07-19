"""Unit + differential suite for :mod:`brutex.optcost_rust` (DEC-OPTCOST-TAILS-RUST-001).

Structure (the ``test_lake_rust_render.py`` / ``test_feed_rust.py`` idioms):

* The guard/latch/burn-in machinery is driven WHEEL-FREE via fake ``fno_math``
  modules installed in ``sys.modules`` (or a poisoned ``None`` entry for the
  import-guard arm). Design intent: the facade measures 100.00% line+branch
  from these fakes alone (re-measured true after the round-1 review closed a
  5-line kernel-OK/oracle-arm gap the differentials had been masking) — but
  the CI gate's guarantee deliberately rests on the ``test`` job's HARD
  fno_math wheel install, never on that unenforced wheel-free property.
* The ON-vs-oracle DIFFERENTIAL classes are wheel-gated
  (``pytest.importorskip``) and drive the REAL kernels over hostile domains
  (the full date plane incl. every refusal-window boundary read from the
  shipped tables, half-paisa ties, i64 extremes, irregular strike ladders,
  the full float plane for ``round_to_atm``), asserting exact value equality
  and — on refusal arms — exception TYPE + MESSAGE byte parity.
* The bomb-spy pin proves the flag-OFF default NEVER touches the kernel from
  any of the six gated call-site modules.
"""
# Whitebox pins on the facade + call-site private oracles:
# pyright: reportPrivateUsage=none

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Final

import brutex.optcost_rust as ocr
import pytest
from brutex.costs.constants import (
    _BSE_ETC_OPTIONS_REGIMES,
    _NSE_ETC_OPTIONS_REGIMES,
    _STT_EXERCISE_REGIMES,
    _STT_OPTIONS_REGIMES,
    RegimeRow,
    _rate_for_date_python,
    rate_for_date,
)
from brutex.costs.types import UnverifiedCostRegimeError
from brutex.data.moneyness import (
    Moneyness,
    _atm_offset_python,
    _classify_graded_python,
    _derive_strike_step_python,
    atm_offset,
    classify,
    derive_strike_step,
)
from brutex.options.expiry_calendar import (
    _MONTHLY_BY_UNDERLYING,
    _WEEKLY_BY_UNDERLYING,
    _lookup_in_history,
    _next_monthly_expiry_python,
    _next_weekly_expiry_python,
    next_monthly_expiry,
    next_weekly_expiry,
)
from brutex.options.lot_size_history import (
    _HISTORY_BY_UNDERLYING,
    OptionsUnderlying,
    get_lot_size,
)
from brutex.options.strike_grid import (
    _round_to_atm_python,
    grid_around_atm,
    round_to_atm,
    strike_offset_from_atm,
)
from brutex.options.strike_rules import (
    RULES_BY_NAME,
    TradeDirection,
    resolve_offset_strike,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: A tiny dated-history table (the lot/expiry bisect shape).
_HISTORY: Final[tuple[tuple[date, int], ...]] = (
    (date(2021, 1, 1), 75),
    (date(2024, 4, 26), 25),
    (date(2024, 11, 20), 75),
)

#: A regime table with a leading refusal row (the ETC shape).
_REFUSAL_TABLE: Final[tuple[RegimeRow, ...]] = (
    RegimeRow(date.min, None, "UNVERIFIED — test refusal window"),
    RegimeRow(date(2024, 10, 1), 3_503, "test verified row"),
)

#: A table with NO date.min anchor (the pre-history-reachable shape).
_UNANCHORED_TABLE: Final[tuple[RegimeRow, ...]] = (
    RegimeRow(date(2024, 10, 1), 3_503, "test verified row"),
)

_ALL_SHIPPED_TABLES: Final[tuple[tuple[str, tuple[RegimeRow, ...]], ...]] = (
    ("stt_options", _STT_OPTIONS_REGIMES),
    ("stt_exercise", _STT_EXERCISE_REGIMES),
    ("nse_etc", _NSE_ETC_OPTIONS_REGIMES),
    ("bse_etc", _BSE_ETC_OPTIONS_REGIMES),
)


@pytest.fixture(autouse=True)
def _fresh_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-unset with a fresh facade process view."""
    monkeypatch.delenv(ocr.OPTCOST_TAILS_RUST_ENV, raising=False)
    ocr.reset_optcost_rust_for_tests()
    yield
    ocr.reset_optcost_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch, *, steady: bool = False) -> None:
    monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "1")
    if steady:
        ocr.reset_optcost_rust_for_tests(remaining=0)


def _boom(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("kernel callable must not be invoked in this test")


def _fake_kernel(**overrides: object) -> types.ModuleType:
    """A fake ``fno_math`` module carrying the full 11-symbol optcost surface."""
    module = types.ModuleType("fno_math")
    for name in (
        "optcost_atm_offset",
        "optcost_grid_around_atm",
        "optcost_history_lookup",
        "optcost_moneyness_classify",
        "optcost_next_monthly_expiry",
        "optcost_next_weekly_expiry",
        "optcost_offset_strike",
        "optcost_regime_rate",
        "optcost_round_to_atm",
        "optcost_strike_offset_from_atm",
        "optcost_strike_step",
    ):
        setattr(module, name, overrides.get(name, _boom))
    return module


def _install(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    """Install a fake kernel (``None`` → the import raises ImportError)."""
    monkeypatch.setitem(sys.modules, "fno_math", module)


def _history_value(python: Callable[[], int] | None = None) -> int:
    """Drive the ``history_value`` seam against :data:`_HISTORY` (route probe)."""
    return ocr.history_value(
        "lot_size",
        _HISTORY,
        date(2024, 11, 20),
        prehistory_message=lambda: "test pre-history",
        python=python or (lambda: 75),
    )


def _outcome_of(fn: Callable[[], object]) -> tuple[object, ...]:
    """A comparable snapshot of a call: its value, or its (type, message)."""
    try:
        return ("ok", fn())
    except (ValueError, OverflowError) as exc:  # UnverifiedCostRegimeError ⊂ ValueError
        return ("raise", type(exc), str(exc))


# ─────────────────────────────────────────────────────────────────
# Flag parsing
# ─────────────────────────────────────────────────────────────────


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, raw)
        assert ocr.optcost_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, raw)
        with capture_logs() as logs:
            assert ocr.optcost_rust_enabled() is False
        assert logs == []

    def test_unset_defaults_off_silently(self) -> None:
        with capture_logs() as logs:
            assert ocr.optcost_rust_enabled() is False
        assert logs == []

    def test_whitespace_defaults_off_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "   ")
        with capture_logs() as logs:
            assert ocr.optcost_rust_enabled() is False
        assert logs == []

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "onn")
        with capture_logs() as logs:
            assert ocr.optcost_rust_enabled() is False
            assert ocr.optcost_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_tristate_vocabulary(self) -> None:
        assert ocr.env_flag_tristate("1") is True
        assert ocr.env_flag_tristate("off") is False
        assert ocr.env_flag_tristate("") is None
        assert ocr.env_flag_tristate("junk") is None

    def test_vocabulary_mirrors_the_engine_common_classifier(self) -> None:
        """The LOCAL twin must stay byte-equal to the engine vocabulary."""
        sweep_lookahead = pytest.importorskip("brutex.engine.sweep_lookahead")
        assert ocr._TRUTHY == sweep_lookahead._TRUTHY
        assert ocr._FALSY == sweep_lookahead._FALSY


# ─────────────────────────────────────────────────────────────────
# Import guard + symbol handshake
# ─────────────────────────────────────────────────────────────────


class TestKernelResolve:
    def test_absent_wheel_warns_once_latches_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, None)  # None in sys.modules → ImportError
        with capture_logs() as logs:
            assert _history_value() == 75
            assert _history_value() == 75
        assert ocr.optcost_rust_disabled()
        disabled = [e for e in logs if e["event"] == "optcost_rust.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "kernel_unavailable"

    def test_stale_wheel_missing_symbol_latches_kernel_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        fake = _fake_kernel()
        delattr(fake, "optcost_regime_rate")
        _install(monkeypatch, fake)
        with capture_logs() as logs:
            assert _history_value() == 75
        assert ocr.optcost_rust_disabled()
        disabled = [e for e in logs if e["event"] == "optcost_rust.rust_path_disabled"]
        assert disabled[0]["reason"] == "kernel_stale"

    def test_non_callable_symbol_latches_kernel_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        fake = _fake_kernel()
        setattr(fake, "optcost_round_to_atm", 42)  # noqa: B010 — deliberate non-callable plant
        _install(monkeypatch, fake)
        with capture_logs() as logs:
            assert _history_value() == 75
        disabled = [e for e in logs if e["event"] == "optcost_rust.rust_path_disabled"]
        assert disabled[0]["reason"] == "kernel_stale"


# ─────────────────────────────────────────────────────────────────
# The route engine (burn-in / autorevert / degrade / panic / escape)
# ─────────────────────────────────────────────────────────────────


class TestRouteEngine:
    def test_clean_burn_in_ships_matching_value_then_steady_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        calls = {"kernel": 0, "python": 0}

        def _lookup(starts: list[int], values: list[int], ordinal: int) -> tuple[int, int]:
            calls["kernel"] += 1
            return (0, 75)

        def _python() -> int:
            calls["python"] += 1
            return 75

        _install(monkeypatch, _fake_kernel(optcost_history_lookup=_lookup))
        with capture_logs() as logs:
            for _ in range(ocr.BURN_IN_SAMPLES + 3):
                assert _history_value(_python) == 75
        # burn-in ran the oracle exactly BURN_IN_SAMPLES times; steady state after
        assert calls["python"] == ocr.BURN_IN_SAMPLES
        assert calls["kernel"] == ocr.BURN_IN_SAMPLES + 3
        assert not ocr.optcost_rust_disabled()
        engaged = [e for e in logs if e["event"] == "optcost_rust.engaged"]
        assert len(engaged) == 1
        complete = [e for e in logs if e["event"] == "optcost_rust.burn_in_complete"]
        assert len(complete) == 1
        assert complete[0]["seam"] == "lot_size"

    def test_burn_in_mismatch_ships_oracle_latches_and_errors_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (0, 25)))
        _flag_on(monkeypatch)
        with capture_logs() as logs:
            assert _history_value() == 75  # the ORACLE value ships, never the lie
            assert _history_value() == 75
        assert ocr.optcost_rust_disabled()
        errors = [e for e in logs if e["event"] == "optcost_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch"
        assert errors[0]["log_level"] == "error"

    def test_steady_state_never_runs_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (0, 75)))
        _flag_on(monkeypatch, steady=True)

        def _python() -> int:
            raise AssertionError("oracle must not run in steady state")

        assert _history_value(_python) == 75

    def test_result_refused_warns_once_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # value 999 is not a table value → refuse-poison, no latch
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (0, 999)))
        _flag_on(monkeypatch)
        with capture_logs() as logs:
            assert _history_value() == 75
            assert _history_value() == 75
        assert not ocr.optcost_rust_disabled()
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "result_refused"

    @pytest.mark.parametrize(
        "exc",
        [ValueError("bad"), RuntimeError("ffi"), TypeError("conv"), OverflowError("i64")],
    )
    def test_kernel_raise_degrades_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _raise(*_a: object) -> tuple[int, int]:
            raise exc

        _install(monkeypatch, _fake_kernel(optcost_history_lookup=_raise))
        _flag_on(monkeypatch)
        with capture_logs() as logs:
            assert _history_value() == 75
            assert _history_value() == 75
        assert not ocr.optcost_rust_disabled()
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "kernel_error"

    def test_out_of_degrade_set_exception_latches_kernel_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _WeirdError(Exception):
            pass

        def _raise(*_a: object) -> tuple[int, int]:
            raise _WeirdError("panic-shaped")

        _install(monkeypatch, _fake_kernel(optcost_history_lookup=_raise))
        _flag_on(monkeypatch)
        with capture_logs() as logs:
            assert _history_value() == 75
        assert ocr.optcost_rust_disabled()
        disabled = [e for e in logs if e["event"] == "optcost_rust.rust_path_disabled"]
        assert disabled[0]["reason"] == "kernel_panic"

    @pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
    def test_escape_set_propagates_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _raise(*_a: object) -> tuple[int, int]:
            raise exc_type()

        _install(monkeypatch, _fake_kernel(optcost_history_lookup=_raise))
        _flag_on(monkeypatch)
        with pytest.raises(exc_type):
            _history_value()
        assert not ocr.optcost_rust_disabled()

    def test_latched_process_short_circuits_before_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, None)
        _flag_on(monkeypatch)
        assert _history_value() == 75  # latches kernel_unavailable
        _install(monkeypatch, _fake_kernel())  # a bomb kernel — must not be touched
        assert _history_value() == 75

    def test_warn_and_disable_repeat_are_no_ops(self) -> None:
        with capture_logs() as logs:
            ocr._warn_once("kernel_error", seam="x")
            ocr._warn_once("kernel_error", seam="x")
            ocr._warn_once("kernel_error", seam="y")  # a DIFFERENT seam still warns
            ocr._disable("kernel_panic", seam="x")
            ocr._disable("kernel_panic", seam="x")
        assert len([e for e in logs if e["event"] == "optcost_rust.fallback"]) == 2
        assert len([e for e in logs if e["event"] == "optcost_rust.rust_path_disabled"]) == 1


class TestHonestEnvelopeDisclosure:
    """Round-2 seam NIT: the honest-envelope per-call cost list must stay
    EXHAUSTIVE and the never-latching ``result_refused`` class acknowledged
    (Golden Rule #10 — two disclosure doc-pins + the behavior twin)."""

    def test_docstring_names_every_steady_state_per_call_cost(self) -> None:
        # (a) _resolve_kernel re-runs (cached import lookup + the 11-symbol
        #     handshake) on EVERY engaged call, steady state included;
        # (b) refuse-poison validation runs on every kernel return.
        doc = ocr.__doc__
        assert doc is not None
        # Whitespace-normalized so a reflow can never break the pin.
        envelope = " ".join(doc[doc.index("## Honest envelope") :].split())
        assert "guard re-run" in envelope
        assert "getattr/callable handshake" in envelope
        assert "refuse-poison validation of every kernel return" in envelope

    def test_docstring_acknowledges_result_refused_never_latches(self) -> None:
        doc = ocr.__doc__
        assert doc is not None
        envelope = " ".join(doc[doc.index("## Honest envelope") :].split())
        assert "``result_refused`` kernel NEVER latches" in envelope

    def test_persistently_refused_kernel_repays_the_crossing_every_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behavior twin: a persistently ``result_refused`` kernel never
        converges to a latch — the kernel is RE-invoked (and its return
        re-validated) on every call, the oracle serving each time (a
        deliberate per-call degrade mirroring ``kernel_error``, unlike the
        latching ``kernel_panic``)."""
        calls = {"kernel": 0}

        def _poisoned(starts: list[int], values: list[int], ordinal: int) -> tuple[int, int]:
            calls["kernel"] += 1
            return (0, 999)  # never a table value → refuse-poison every call

        _install(monkeypatch, _fake_kernel(optcost_history_lookup=_poisoned))
        _flag_on(monkeypatch, steady=True)
        for _ in range(5):
            assert _history_value() == 75  # the oracle serves every time
        assert calls["kernel"] == 5  # the crossing is re-paid per call — no memo
        assert not ocr.optcost_rust_disabled()  # and no latch ever forms


# ─────────────────────────────────────────────────────────────────
# Outcome plumbing + pure helpers
# ─────────────────────────────────────────────────────────────────


class TestOutcomeHelpers:
    def test_outcome_equal_value_and_error_arms(self) -> None:
        ok = ocr.Outcome(value=1)
        assert ocr.outcome_equal(ok, ocr.Outcome(value=1))
        assert not ocr.outcome_equal(ok, ocr.Outcome(value=2))
        assert not ocr.outcome_equal(ok, ocr.Outcome(error=ValueError("x")))
        assert ocr.outcome_equal(
            ocr.Outcome(error=ValueError("x")), ocr.Outcome(error=ValueError("x"))
        )
        assert not ocr.outcome_equal(
            ocr.Outcome(error=ValueError("x")), ocr.Outcome(error=ValueError("y"))
        )
        assert not ocr.outcome_equal(
            ocr.Outcome(error=ValueError("x")), ocr.Outcome(error=TypeError("x"))
        )

    def test_decimal_outcome_equal_is_exponent_sensitive(self) -> None:
        def d(s: str) -> ocr.Outcome:
            return ocr.Outcome(value=Decimal(s))

        assert ocr._decimal_outcome_equal(d("50"), d("50"))
        assert not ocr._decimal_outcome_equal(d("50"), d("50.00"))  # == but not same bytes
        assert ocr._decimal_outcome_equal(ocr.Outcome(value=None), ocr.Outcome(value=None))
        assert not ocr._decimal_outcome_equal(ocr.Outcome(value=None), d("50"))
        assert not ocr._decimal_outcome_equal(d("50"), ocr.Outcome(value=50))  # non-Decimal
        assert ocr._decimal_outcome_equal(
            ocr.Outcome(error=ValueError("x")), ocr.Outcome(error=ValueError("x"))
        )

    def test_materialize_raises_the_composed_error(self) -> None:
        with pytest.raises(ValueError, match="boom"):
            ocr._materialize(ocr.Outcome(error=ValueError("boom")))
        assert ocr._materialize(ocr.Outcome(value=7)) == 7

    def test_refuse_unless_int_rejects_bool_and_str(self) -> None:
        assert ocr._refuse_unless_int("s", 5) == 5
        with pytest.raises(ocr.KernelResultRefused):
            ocr._refuse_unless_int("s", True)
        with pytest.raises(ocr.KernelResultRefused):
            ocr._refuse_unless_int("s", "5")

    def test_code_value_rejects_malformed_pairs(self) -> None:
        assert ocr._code_value("s", (0, 1)) == (0, 1)
        with pytest.raises(ocr.KernelResultRefused):
            ocr._code_value("s", [0, 1])
        with pytest.raises(ocr.KernelResultRefused):
            ocr._code_value("s", (0, 1, 2))
        with pytest.raises(ocr.KernelResultRefused):
            ocr._code_value("s", (0, "x"))

    def test_result_date_bounds(self) -> None:
        assert ocr._result_date("s", 1) == date.min
        assert ocr._result_date("s", 3_652_059) == date.max
        with pytest.raises(ocr.KernelResultRefused):
            ocr._result_date("s", 0)
        with pytest.raises(ocr.KernelResultRefused):
            ocr._result_date("s", 3_652_060)


class TestExactScaledInt:
    def test_exact_paisa_conversions(self) -> None:
        assert ocr._exact_scaled_int(Decimal("24000"), 2) == 2_400_000
        assert ocr._exact_scaled_int(Decimal("24000.50"), 2) == 2_400_050
        assert ocr._exact_scaled_int(Decimal("0.05"), 2) == 5
        assert ocr._exact_scaled_int(Decimal("0"), 2) == 0
        assert ocr._exact_scaled_int(Decimal("2.4E+4"), 2) == 2_400_000
        assert ocr._exact_scaled_int(Decimal("-1.25"), 2) == -125

    def test_inexact_paisa_routes_none(self) -> None:
        assert ocr._exact_scaled_int(Decimal("0.005"), 2) is None  # half a paisa
        assert ocr._exact_scaled_int(Decimal("100.125"), 2) is None

    def test_exact_deep_fraction_divides_out(self) -> None:
        # exponent < -2 but exactly paisa-representable (trailing zeros)
        assert ocr._exact_scaled_int(Decimal("24.000"), 2) == 2_400
        assert ocr._exact_scaled_int(Decimal("0.0500"), 2) == 5

    def test_just_past_i64_after_exact_scaling_routes_none(self) -> None:
        # 19 digits, shift 0 — passes the digit early-out, fails the i64 bound
        assert ocr._exact_scaled_int(Decimal("92233720368547758.08"), 2) is None

    def test_out_of_i64_routes_none(self) -> None:
        assert ocr._exact_scaled_int(Decimal(2**63), 2) is None
        assert ocr._exact_scaled_int(Decimal("5E+999999"), 2) is None  # early-out
        assert ocr._exact_scaled_int(Decimal((0, (1,), -999_999)), 2) is None  # deep fraction

    def test_wide_coefficient_is_never_context_rounded(self) -> None:
        # 31 significant digits ending in a non-zero fraction: a context
        # `scaleb` would ROUND this to an integer and silently accept it.
        wide = Decimal("1" + "0" * 27 + ".005")
        assert ocr._exact_scaled_int(wide, 2) is None

    def test_non_finite_marker_routes_none(self) -> None:
        assert ocr._exact_scaled_int(Decimal("NaN"), 2) is None
        assert ocr._exact_scaled_int(Decimal("Infinity"), 2) is None


class TestUniformExponentUnits:
    def test_uniform_integer_ladder(self) -> None:
        assert ocr._uniform_exponent_units((Decimal("24000"), Decimal("24050"))) == (
            0,
            [24000, 24050],
        )

    def test_uniform_two_dp_ladder(self) -> None:
        assert ocr._uniform_exponent_units((Decimal("24000.00"), Decimal("24050.00"))) == (
            -2,
            [2400000, 2405000],
        )

    def test_mixed_exponents_route_none(self) -> None:
        assert ocr._uniform_exponent_units((Decimal("24000"), Decimal("24050.0"))) is None

    def test_out_of_i64_coefficient_routes_none(self) -> None:
        assert ocr._uniform_exponent_units((Decimal(2**63), Decimal(1))) is None

    def test_empty_routes_none(self) -> None:
        assert ocr._uniform_exponent_units(()) is None

    def test_negative_or_non_finite_routes_none(self) -> None:
        assert ocr._uniform_exponent_units((Decimal("-50"),)) is None
        assert ocr._uniform_exponent_units((Decimal("NaN"),)) is None


# ─────────────────────────────────────────────────────────────────
# Per-seam kernel-outcome composition (fake kernels)
# ─────────────────────────────────────────────────────────────────


class TestRegimeRateSeam:
    def _rate(self, regimes: Sequence[RegimeRow], on: date) -> int:
        return ocr.regime_rate(
            regimes,
            on,
            charge="test charge",
            exchange="NSE",
            python=lambda: _rate_for_date_python(regimes, on, charge="test charge", exchange="NSE"),
        )

    def test_refusal_code_composes_byte_equal_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (1, 0)))
        _flag_on(monkeypatch, steady=True)
        on = date(2023, 1, 1)
        with pytest.raises(UnverifiedCostRegimeError) as rust_exc:
            self._rate(_REFUSAL_TABLE, on)
        with pytest.raises(UnverifiedCostRegimeError) as py_exc:
            _rate_for_date_python(_REFUSAL_TABLE, on, charge="test charge", exchange="NSE")
        assert str(rust_exc.value) == str(py_exc.value)
        assert rust_exc.value.window_start == date.min
        assert rust_exc.value.verified_from == date(2024, 10, 1)

    def test_trailing_open_refusal_window_has_none_verified_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = (RegimeRow(date.min, None, "open refusal"),)
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (1, 0)))
        _flag_on(monkeypatch, steady=True)
        with pytest.raises(UnverifiedCostRegimeError) as exc:
            self._rate(table, date(2024, 1, 1))
        assert exc.value.verified_from is None

    def test_empty_table_code_composes_the_exact_valueerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (2, 0)))
        _flag_on(monkeypatch, steady=True)
        with pytest.raises(ValueError, match="^rate_for_date: empty regime table$"):
            self._rate((), date(2024, 1, 1))

    def test_pre_history_code_composes_the_exact_valueerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (3, 0)))
        _flag_on(monkeypatch, steady=True)
        on = date(2024, 9, 30)
        rust = _outcome_of(lambda: self._rate(_UNANCHORED_TABLE, on))
        python = _outcome_of(
            lambda: _rate_for_date_python(
                _UNANCHORED_TABLE, on, charge="test charge", exchange="NSE"
            )
        )
        assert rust == python
        assert rust[0] == "raise"

    def test_ok_code_ships_the_table_rate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fake-kernel OK path, WHEEL-FREE: a valid in-table rate ships with
        zero fallback warns (the round-1 coverage-finding closure — the
        regime ``_kernel`` OK return must be reachable from the fakes alone,
        never only from the wheel-gated differentials)."""
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (0, 3_503)))
        _flag_on(monkeypatch, steady=True)
        with capture_logs() as logs:
            assert self._rate(_REFUSAL_TABLE, date(2024, 10, 2)) == 3_503
        assert not [e for e in logs if e["event"] == "optcost_rust.fallback"]

    @pytest.mark.parametrize(
        "result",
        [
            (0, -5),  # negative rate
            (0, 999),  # not a table rate
            (1, 99),  # refusal index out of range
            (1, 1),  # row 1 is NOT a refusal row
            (7, 0),  # unknown code
        ],
    )
    def test_poisoned_results_refuse_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, result: tuple[int, int]
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: result))
        _flag_on(monkeypatch, steady=True)
        with capture_logs() as logs:
            assert self._rate(_REFUSAL_TABLE, date(2024, 10, 2)) == 3_503
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert warns[0]["reason"] == "result_refused"

    def test_pre_history_code_on_an_empty_table_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_regime_rate=lambda s, r, o: (3, 0)))
        _flag_on(monkeypatch, steady=True)
        with capture_logs() as logs, pytest.raises(ValueError, match="empty regime table"):
            self._rate((), date(2024, 1, 1))
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert warns[0]["reason"] == "result_refused"


class TestHistoryAndExpirySeams:
    def test_history_pre_history_code_composes_callers_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (3, 0)))
        _flag_on(monkeypatch, steady=True)
        with pytest.raises(ValueError, match="^custom pre-history bytes$"):
            ocr.history_value(
                "lot_size",
                _HISTORY,
                date(2020, 1, 1),
                prehistory_message=lambda: "custom pre-history bytes",
                python=lambda: 0,
            )

    def test_history_burn_in_compares_raise_outcomes_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Burn-in ARMED: a kernel pre-history code vs an oracle raise with the
        SAME bytes is a clean compare — the oracle's instance ships."""
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (3, 0)))
        _flag_on(monkeypatch)  # burn-in armed

        def _python() -> int:
            raise ValueError("test pre-history")

        with pytest.raises(ValueError, match="^test pre-history$"):
            ocr.history_value(
                "lot_size",
                _HISTORY,
                date(2020, 1, 1),
                prehistory_message=lambda: "test pre-history",
                python=_python,
            )
        assert not ocr.optcost_rust_disabled()

    def test_next_weekly_burn_in_compares_raise_outcomes_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = ((date(2020, 1, 1), 3),)
        _install(monkeypatch, _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (3, 0)))
        _flag_on(monkeypatch)  # burn-in armed

        def _python() -> date | None:
            raise ValueError(
                f"on_date {date(2019, 1, 1)} is before recorded history start ({table[0][0]})"
            )

        with pytest.raises(ValueError, match="before recorded history start"):
            ocr.next_weekly(table, date(2019, 1, 1), python=_python)
        assert not ocr.optcost_rust_disabled()

    def test_history_unknown_code_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_kernel(optcost_history_lookup=lambda s, v, o: (9, 0)))
        _flag_on(monkeypatch, steady=True)
        assert _history_value() == 75  # refused → oracle

    def test_next_weekly_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = ((date(2020, 1, 1), 3),)
        target = date(2024, 1, 4)
        _install(
            monkeypatch,
            _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (0, target.toordinal())),
        )
        _flag_on(monkeypatch, steady=True)
        assert ocr.next_weekly(table, date(2024, 1, 1), python=lambda: None) == target
        _install(monkeypatch, _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (4, 0)))
        assert ocr.next_weekly(table, date(2024, 1, 1), python=lambda: None) is None
        _install(monkeypatch, _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (3, 0)))
        with pytest.raises(ValueError, match="before recorded history start"):
            ocr.next_weekly(table, date(2019, 1, 1), python=lambda: None)
        _install(monkeypatch, _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (9, 0)))
        assert ocr.next_weekly(table, date(2024, 1, 1), python=lambda: target) == target

    def test_next_weekly_out_of_domain_ordinal_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = ((date(2020, 1, 1), 3),)
        _install(
            monkeypatch, _fake_kernel(optcost_next_weekly_expiry=lambda s, d, o: (0, 99_999_999))
        )
        _flag_on(monkeypatch, steady=True)
        sentinel = date(2024, 1, 4)
        with capture_logs() as logs:
            assert ocr.next_weekly(table, date(2024, 1, 1), python=lambda: sentinel) == sentinel
        warns = [e for e in logs if e["event"] == "optcost_rust.fallback"]
        assert warns[0]["reason"] == "result_refused"

    def test_next_monthly_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        table = ((date(2020, 1, 1), 3),)
        target = date(2024, 1, 25)
        _install(
            monkeypatch,
            _fake_kernel(optcost_next_monthly_expiry=lambda s, d, y, m, o: (0, target.toordinal())),
        )
        _flag_on(monkeypatch, steady=True)
        assert ocr.next_monthly(table, date(2024, 1, 1), python=lambda: target) == target
        _install(
            monkeypatch, _fake_kernel(optcost_next_monthly_expiry=lambda s, d, y, m, o: (3, 0))
        )
        with pytest.raises(ValueError, match="before recorded history start"):
            ocr.next_monthly(table, date(2019, 1, 1), python=lambda: target)
        _install(
            monkeypatch, _fake_kernel(optcost_next_monthly_expiry=lambda s, d, y, m, o: (9, 0))
        )
        assert ocr.next_monthly(table, date(2024, 1, 1), python=lambda: target) == target

    def test_next_monthly_burn_in_compares_raise_outcomes_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Burn-in ARMED, WHEEL-FREE: the ``next_monthly`` oracle's
        ValueError-capture arm runs from the fakes (the round-1
        coverage-finding closure of the facade's 769-770 gap)."""
        table = ((date(2020, 1, 1), 3),)
        _install(
            monkeypatch, _fake_kernel(optcost_next_monthly_expiry=lambda s, d, y, m, o: (3, 0))
        )
        _flag_on(monkeypatch)  # burn-in armed

        def _python() -> date:
            raise ValueError(
                f"on_date {date(2019, 1, 1)} is before recorded history start ({table[0][0]})"
            )

        with pytest.raises(ValueError, match="before recorded history start"):
            ocr.next_monthly(table, date(2019, 1, 1), python=_python)
        assert not ocr.optcost_rust_disabled()


class TestMoneynessSeams:
    def test_classify_codes_map_to_buckets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for code, bucket in ((2, Moneyness.ITM), (3, Moneyness.ATM), (4, Moneyness.OTM)):
            _install(monkeypatch, _fake_kernel(optcost_moneyness_classify=lambda *a, _c=code: _c))
            _flag_on(monkeypatch, steady=True)
            got = ocr.classify_graded(
                is_put=False,
                strike=Decimal("24000"),
                spot=Decimal("24100"),
                band=Decimal(0),
                itm=Moneyness.ITM,
                atm=Moneyness.ATM,
                otm=Moneyness.OTM,
                python=lambda: Moneyness.UNKNOWN,
            )
            assert got is bucket

    def test_classify_unknown_code_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_kernel(optcost_moneyness_classify=lambda *a: 0))
        _flag_on(monkeypatch, steady=True)
        got = ocr.classify_graded(
            is_put=False,
            strike=Decimal("24000"),
            spot=Decimal("24100"),
            band=Decimal(0),
            itm=Moneyness.ITM,
            atm=Moneyness.ATM,
            otm=Moneyness.OTM,
            python=lambda: Moneyness.ITM,
        )
        assert got is Moneyness.ITM

    def test_classify_inexact_paisa_never_touches_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel())  # every callable is a bomb
        _flag_on(monkeypatch, steady=True)
        got = ocr.classify_graded(
            is_put=True,
            strike=Decimal("24000"),
            spot=Decimal("24100"),
            band=Decimal("0.005"),  # half a paisa — inexact
            itm=Moneyness.ITM,
            atm=Moneyness.ATM,
            otm=Moneyness.OTM,
            python=lambda: Moneyness.OTM,
        )
        assert got is Moneyness.OTM

    def test_atm_offset_engaged_and_inexact_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _fake_kernel(optcost_atm_offset=lambda a, b, c: 2))
        _flag_on(monkeypatch, steady=True)
        assert (
            ocr.atm_offset_paisa(
                Decimal("24100"), Decimal("24000"), Decimal("50"), python=lambda: -99
            )
            == 2
        )
        # burn-in ARMED: the oracle runs beside the kernel and its value ships
        ocr.reset_optcost_rust_for_tests()
        assert (
            ocr.atm_offset_paisa(
                Decimal("24100"), Decimal("24000"), Decimal("50"), python=lambda: 2
            )
            == 2
        )
        assert not ocr.optcost_rust_disabled()
        _install(monkeypatch, _fake_kernel())  # bombs — the inexact arm must not touch them
        assert (
            ocr.atm_offset_paisa(
                Decimal("24100.005"), Decimal("24000"), Decimal("50"), python=lambda: 7
            )
            == 7
        )

    def test_strike_step_codes_and_exponent_materialisation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_strike_step=lambda c: (0, 5000)))
        _flag_on(monkeypatch, steady=True)
        got = ocr.strike_step(
            (Decimal("24000.00"), Decimal("24050.00")), python=lambda: Decimal("1")
        )
        assert got is not None
        assert got.as_tuple() == Decimal("50.00").as_tuple()  # exponent preserved
        _install(monkeypatch, _fake_kernel(optcost_strike_step=lambda c: (5, 0)))
        assert ocr.strike_step((Decimal("24000"),), python=lambda: Decimal("1")) is None

    def test_strike_step_poison_and_mixed_exponent_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_strike_step=lambda c: (0, 0)))
        _flag_on(monkeypatch, steady=True)
        oracle = Decimal("50")
        assert (
            ocr.strike_step((Decimal("24000"), Decimal("24050")), python=lambda: oracle) == oracle
        )
        _install(monkeypatch, _fake_kernel(optcost_strike_step=lambda c: (9, 0)))
        assert (
            ocr.strike_step((Decimal("24000"), Decimal("24050")), python=lambda: oracle) == oracle
        )
        _install(monkeypatch, _fake_kernel())  # bombs — mixed exponents never engage
        assert (
            ocr.strike_step((Decimal("24000"), Decimal("24050.0")), python=lambda: oracle) == oracle
        )


class TestAmbientDecimalContextPin:
    """The round-1 refuter finding pinned CONSCIOUS (wheel-free OFF arm).

    The kernel/oracle equivalence proof is scoped to the DEFAULT 28-digit
    decimal context (``rust/fno-math/src/optcost/moneyness.rs`` module docs);
    under a caller-MODIFIED ambient context the ORACLE's own Decimal
    arithmetic context-rounds while the kernel's exact-integer law does not —
    a reproduced divergence class with zero production reachability (no
    production caller mutates the decimal context — grep-verified 2026-07-18;
    ``brutex/data/pcr.py`` pins a default-equivalent context around its own
    division). This pins the OFF arm's ambient-context sensitivity; the
    wheel-gated ON-arm twin lives in ``TestMoneynessDifferential``.
    """

    def test_off_arm_follows_the_ambient_context(self) -> None:
        strike, spot, step = Decimal("9999999.05"), Decimal("1000000.00"), Decimal("0.03")
        with localcontext() as ctx:
            ctx.prec = 6  # (9999999.05 − 1000000.00) / 0.03 context-rounds to 3.00000E+8
            assert atm_offset(strike, spot, step) == 300_000_000
        # the default 28-digit context yields the exact answer
        assert atm_offset(strike, spot, step) == 299_999_968


class TestStrikeSeams:
    def test_offset_and_grid_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _fake_kernel(
                optcost_offset_strike=lambda a, c, s, b: 24_100,
                optcost_strike_offset_from_atm=lambda a, o, s: 23_900,
            ),
        )
        _flag_on(monkeypatch, steady=True)
        assert ocr.offset_strike(24_000, 2, 50, is_bull=True, python=lambda: -1) == 24_100
        assert ocr.strike_offset(24_000, -2, 50, python=lambda: -1) == 23_900

    def test_offset_and_grid_offset_burn_in_runs_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Burn-in ARMED, WHEEL-FREE: both strike-seam oracle closures run
        from the fakes (the round-1 coverage-finding closure of the facade's
        915/939 gap); a clean compare leaves the seam un-latched."""
        _install(
            monkeypatch,
            _fake_kernel(
                optcost_offset_strike=lambda a, c, s, b: 24_100,
                optcost_strike_offset_from_atm=lambda a, o, s: 23_900,
            ),
        )
        _flag_on(monkeypatch)  # burn-in armed
        assert ocr.offset_strike(24_000, 2, 50, is_bull=True, python=lambda: 24_100) == 24_100
        assert ocr.strike_offset(24_000, -2, 50, python=lambda: 23_900) == 23_900
        assert not ocr.optcost_rust_disabled()

    def test_grid_around_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        good = [23_900, 23_950, 24_000, 24_050, 24_100]
        _install(monkeypatch, _fake_kernel(optcost_grid_around_atm=lambda a, s, h: list(good)))
        _flag_on(monkeypatch, steady=True)
        assert ocr.grid_around(24_000, 50, 2, python=lambda: []) == good
        # wrong length → refused
        _install(monkeypatch, _fake_kernel(optcost_grid_around_atm=lambda a, s, h: good[:3]))
        assert ocr.grid_around(24_000, 50, 2, python=lambda: list(good)) == good
        # non-list → refused
        _install(monkeypatch, _fake_kernel(optcost_grid_around_atm=lambda a, s, h: tuple(good)))
        assert ocr.grid_around(24_000, 50, 2, python=lambda: list(good)) == good
        # non-int element → refused
        _install(
            monkeypatch,
            _fake_kernel(optcost_grid_around_atm=lambda a, s, h: [*good[:4], "x"]),
        )
        assert ocr.grid_around(24_000, 50, 2, python=lambda: list(good)) == good

    def test_grid_around_negative_half_width_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_grid_around_atm=lambda a, s, h: []))
        _flag_on(monkeypatch, steady=True)
        assert ocr.grid_around(24_000, 50, -1, python=lambda: []) == []

    def test_round_atm_engaged_and_oracle_raise_capture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _fake_kernel(optcost_round_to_atm=lambda p, s: 24_050))
        _flag_on(monkeypatch, steady=True)
        assert ocr.round_atm(24_025.0, 50, python=lambda: -1) == 24_050

        def _nan_python() -> int:
            raise ValueError("cannot convert float NaN to integer")

        def _kernel_refuses(p: float, s: int) -> int:
            raise ValueError("outside the provable envelope")

        _install(monkeypatch, _fake_kernel(optcost_round_to_atm=_kernel_refuses))
        with pytest.raises(ValueError, match="cannot convert float NaN to integer"):
            ocr.round_atm(float("nan"), 50, python=_nan_python)

    def test_round_atm_burn_in_compares_raise_outcomes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel int vs an oracle RAISE is a burn-in mismatch → autorevert."""

        def _inf_python() -> int:
            raise OverflowError("cannot convert float infinity to integer")

        _install(monkeypatch, _fake_kernel(optcost_round_to_atm=lambda p, s: 0))
        _flag_on(monkeypatch)  # burn-in ARMED
        with capture_logs() as logs, pytest.raises(OverflowError):
            ocr.round_atm(float("inf"), 50, python=_inf_python)
        assert ocr.optcost_rust_disabled()
        assert [e for e in logs if e["event"] == "optcost_rust.autoreverted"]


# ─────────────────────────────────────────────────────────────────
# The bomb-spy OFF-default law (rule 2)
# ─────────────────────────────────────────────────────────────────


class TestOffDefaultNeverTouchesTheKernel:
    def test_every_gated_call_site_stays_python_with_a_bomb_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag unset + a bomb `fno_math` in sys.modules: every gated public
        function across the six call-site modules serves byte-identical
        Python answers and NEVER touches the bomb."""
        touches: list[str] = []

        def _bomb_getattr(name: str) -> object:
            touches.append(name)
            raise AssertionError(f"kernel touched: {name}")

        bomb = types.ModuleType("fno_math")
        # PEP 562: a module-level __getattr__ lives in the module's dict.
        bomb.__dict__["__getattr__"] = _bomb_getattr
        monkeypatch.setitem(sys.modules, "fno_math", bomb)
        monkeypatch.delenv(ocr.OPTCOST_TAILS_RUST_ENV, raising=False)

        assert rate_for_date(_STT_OPTIONS_REGIMES, date(2024, 10, 1)) == 10_000
        with pytest.raises(UnverifiedCostRegimeError):
            rate_for_date(_NSE_ETC_OPTIONS_REGIMES, date(2023, 1, 1))
        assert classify("CE", Decimal("24000"), Decimal("24100"), atm_band=None) is Moneyness.ITM
        assert atm_offset(Decimal("24100"), Decimal("24000"), Decimal("50")) == 2
        assert derive_strike_step([Decimal("24000"), Decimal("24050")]) == Decimal("50")
        assert get_lot_size(OptionsUnderlying.NIFTY, date(2024, 11, 20)) == 75
        assert next_weekly_expiry(OptionsUnderlying.BANKNIFTY, date(2024, 11, 14)) is None
        assert next_monthly_expiry(OptionsUnderlying.NIFTY, date(2024, 1, 1)) == date(2024, 1, 25)
        assert round_to_atm(24_025.0, OptionsUnderlying.NIFTY) == 24_050
        assert strike_offset_from_atm(24_000, -2, OptionsUnderlying.NIFTY) == 23_900
        assert grid_around_atm(24_000, OptionsUnderlying.NIFTY, 1) == [23_950, 24_000, 24_050]
        assert (
            resolve_offset_strike(
                RULES_BY_NAME["atm_plus_2_in_dir"], TradeDirection.BEAR, 24_000, 50
            )
            == 23_900
        )
        assert _lookup_in_history(((date(2020, 1, 1), 3),), date(2024, 1, 1)) == 3
        assert touches == []

    def test_off_default_never_imports_fno_math(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The STRICT form of the bomb-spy law (round-1 review): with NO
        ``fno_math`` entry in ``sys.modules`` at all, exercising every gated
        public entry flag-OFF leaves it genuinely UN-IMPORTED. The PEP-562
        bomb above proves non-ACCESS (a module ``__getattr__`` fires only on
        a missing attribute, not on ``import``); this pins the stronger
        never-imported property the ledger wording claims."""
        monkeypatch.delitem(sys.modules, "fno_math", raising=False)
        monkeypatch.delenv(ocr.OPTCOST_TAILS_RUST_ENV, raising=False)

        assert rate_for_date(_STT_OPTIONS_REGIMES, date(2024, 10, 1)) == 10_000
        assert classify("CE", Decimal("24000"), Decimal("24100"), atm_band=None) is Moneyness.ITM
        assert atm_offset(Decimal("24100"), Decimal("24000"), Decimal("50")) == 2
        assert derive_strike_step([Decimal("24000"), Decimal("24050")]) == Decimal("50")
        assert get_lot_size(OptionsUnderlying.NIFTY, date(2024, 11, 20)) == 75
        assert next_weekly_expiry(OptionsUnderlying.BANKNIFTY, date(2024, 11, 14)) is None
        assert next_monthly_expiry(OptionsUnderlying.NIFTY, date(2024, 1, 1)) == date(2024, 1, 25)
        assert round_to_atm(24_025.0, OptionsUnderlying.NIFTY) == 24_050
        assert strike_offset_from_atm(24_000, -2, OptionsUnderlying.NIFTY) == 23_900
        assert grid_around_atm(24_000, OptionsUnderlying.NIFTY, 1) == [23_950, 24_000, 24_050]
        assert (
            resolve_offset_strike(
                RULES_BY_NAME["atm_plus_2_in_dir"], TradeDirection.BEAR, 24_000, 50
            )
            == 23_900
        )
        assert _lookup_in_history(((date(2020, 1, 1), 3),), date(2024, 1, 1)) == 3
        assert "fno_math" not in sys.modules


# ─────────────────────────────────────────────────────────────────
# Wheel-gated ON-vs-oracle differentials (the real kernels)
# ─────────────────────────────────────────────────────────────────


def _require_wheel() -> None:
    kernel = pytest.importorskip("fno_math")
    if not callable(getattr(kernel, "optcost_regime_rate", None)):
        pytest.skip("installed fno_math wheel predates the optcost kernels")


_ORDINALS = st.integers(min_value=1, max_value=date.max.toordinal())

#: Every regime/lot/expiry transition read FROM the shipped tables (never
#: hand-copied) ±1 day — the boundary corpus of the differential suites.
_BOUNDARY_DATES: Final[list[date]] = sorted(
    {
        boundary + timedelta(days=delta)
        for delta in (-1, 0, 1)
        for boundary in (
            [row.start for _, table in _ALL_SHIPPED_TABLES for row in table if row.start > date.min]
            + [d for hist in _HISTORY_BY_UNDERLYING.values() for d, _ in hist]
            + [d for hist in _WEEKLY_BY_UNDERLYING.values() for d, _ in hist]
            + [d for hist in _MONTHLY_BY_UNDERLYING.values() for d, _ in hist]
        )
    }
)


class TestRegimeDifferential:
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(ordinal=_ORDINALS)
    def test_full_date_plane_all_shipped_tables(self, ordinal: int) -> None:
        _require_wheel()
        on = date.fromordinal(ordinal)
        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            for _label, table in _ALL_SHIPPED_TABLES:
                ocr.reset_optcost_rust_for_tests(remaining=0)
                rust = _outcome_of(lambda t=table: rate_for_date(t, on, charge="c", exchange="E"))
                python = _outcome_of(
                    lambda t=table: _rate_for_date_python(t, on, charge="c", exchange="E")
                )
                assert rust == python
                assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    def test_boundary_corpus_and_unanchored_prehistory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        for on in [*_BOUNDARY_DATES, date.min, date.max]:
            for _label, table in (*_ALL_SHIPPED_TABLES, ("unanchored", _UNANCHORED_TABLE)):
                rust = _outcome_of(lambda t=table, d=on: rate_for_date(t, d))
                python = _outcome_of(
                    lambda t=table, d=on: _rate_for_date_python(
                        t, d, charge="cost regime", exchange=""
                    )
                )
                assert rust == python, (on, _label)
        assert not ocr.optcost_rust_disabled()


class TestLotAndExpiryDifferential:
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(ordinal=_ORDINALS, instrument=st.sampled_from(list(OptionsUnderlying)))
    def test_lot_weekly_monthly_full_date_plane(
        self, ordinal: int, instrument: OptionsUnderlying
    ) -> None:
        _require_wheel()
        on = date.fromordinal(ordinal)
        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            ocr.reset_optcost_rust_for_tests(remaining=0)
            history = _HISTORY_BY_UNDERLYING[instrument]
            rust = _outcome_of(lambda: get_lot_size(instrument, on))
            python = _outcome_of(lambda: _lot_oracle(history, on, instrument))
            assert rust == python
            rust_w = _outcome_of(lambda: next_weekly_expiry(instrument, on))
            python_w = _outcome_of(lambda: _next_weekly_expiry_python(instrument, on))
            assert rust_w == python_w
            rust_m = _outcome_of(lambda: next_monthly_expiry(instrument, on))
            python_m = _outcome_of(lambda: _next_monthly_expiry_python(instrument, on))
            assert rust_m == python_m
            assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    def test_boundary_corpus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        for on in _BOUNDARY_DATES:
            for instrument in OptionsUnderlying:
                history = _HISTORY_BY_UNDERLYING[instrument]
                assert _outcome_of(lambda i=instrument, d=on: get_lot_size(i, d)) == _outcome_of(
                    lambda h=history, d=on, i=instrument: _lot_oracle(h, d, i)
                )
                assert _outcome_of(
                    lambda i=instrument, d=on: next_weekly_expiry(i, d)
                ) == _outcome_of(lambda i=instrument, d=on: _next_weekly_expiry_python(i, d))
                assert _outcome_of(
                    lambda i=instrument, d=on: next_monthly_expiry(i, d)
                ) == _outcome_of(lambda i=instrument, d=on: _next_monthly_expiry_python(i, d))
        assert not ocr.optcost_rust_disabled()

    def test_dec_9999_rollover_degrades_to_the_oracle_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stdlib `date` refuses year 10000 — ON must raise exactly as OFF."""
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        on = date(9999, 12, 31)
        rust = _outcome_of(lambda: next_monthly_expiry(OptionsUnderlying.NIFTY, on))
        python = _outcome_of(lambda: _next_monthly_expiry_python(OptionsUnderlying.NIFTY, on))
        assert rust == python
        assert rust[0] == "raise"


def _lot_oracle(
    history: tuple[tuple[date, int], ...], on: date, instrument: OptionsUnderlying
) -> int:
    from brutex.options.lot_size_history import _get_lot_size_python

    return _get_lot_size_python(history, on, instrument)


_PAISA_GRID_DECIMALS = st.integers(min_value=1, max_value=10_000_000_00).map(
    lambda p: Decimal(p).scaleb(-2)
)


class TestMoneynessDifferential:
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        strike=_PAISA_GRID_DECIMALS,
        spot=_PAISA_GRID_DECIMALS,
        band_paisa=st.integers(min_value=0, max_value=10_000_00),
        is_put=st.booleans(),
    )
    def test_classify_and_atm_offset_paisa_plane(
        self, strike: Decimal, spot: Decimal, band_paisa: int, is_put: bool
    ) -> None:
        _require_wheel()
        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            ocr.reset_optcost_rust_for_tests(remaining=0)
            option_type = "PE" if is_put else "CE"
            band = Decimal(band_paisa).scaleb(-2)
            assert classify(option_type, strike, spot, atm_band=band) is _classify_graded_python(
                option_type, strike, spot, band
            )
            step = Decimal("0.05")
            assert atm_offset(strike, spot, step) == _atm_offset_python(strike, spot, step)
            assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    def test_modified_context_kernel_stays_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag ON post-burn-in, real wheel, under ``localcontext(prec=6)``:
        the kernel ships the context-INSENSITIVE exact answer (299_999_968)
        where the OFF arm's oracle context-rounds to 300_000_000 (pinned in
        ``TestAmbientDecimalContextPin``) — the round-1 refuter divergence
        pinned as CONSCIOUS: equivalence is proven for the DEFAULT context
        only, the ON value is the exact one, and no production caller
        mutates the decimal context (grep-verified)."""
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        strike, spot, step = Decimal("9999999.05"), Decimal("1000000.00"), Decimal("0.03")
        with localcontext() as ctx:
            ctx.prec = 6
            assert atm_offset(strike, spot, step) == 299_999_968
        assert not ocr.optcost_rust_disabled()

    def test_half_step_midpoint_agreement_law_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spot exactly step/2 from strike: classify==ATM ⟺ atm_offset==0 — flag ON."""
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        strike, step = Decimal("24025"), Decimal("50")
        for spot in (strike - step / 2, strike + step / 2):
            assert classify("CE", strike, spot, atm_band=step / 2) is Moneyness.ATM
            assert atm_offset(strike, spot, step) == 0

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        ladder=st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=0, max_size=8),
        exponent=st.sampled_from([0, -2]),
    )
    def test_strike_step_uniform_ladders(self, ladder: list[int], exponent: int) -> None:
        _require_wheel()
        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            ocr.reset_optcost_rust_for_tests(remaining=0)
            strikes = [Decimal(u).scaleb(exponent) for u in ladder]
            on_result = derive_strike_step(strikes)
            off_result = _derive_strike_step_python(tuple(strikes))
            if on_result is None or off_result is None:
                assert on_result is None and off_result is None
            else:
                assert on_result.as_tuple() == off_result.as_tuple()  # byte-exact
            assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    def test_mixed_exponent_and_irregular_ladders_stay_oracle_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        ladders: list[list[Decimal | None]] = [
            [Decimal("24000"), Decimal("24050.0"), Decimal("24100.00")],  # mixed exponents
            [Decimal("100"), Decimal("102.5"), None, Decimal("107.5")],  # irregular grid
            [Decimal(2**64), Decimal(2**64 + 1)],  # past i64
            [Decimal("24000"), Decimal("NaN"), Decimal("24100")],
        ]
        for ladder in ladders:
            on_result = derive_strike_step(list(ladder))
            off_result = _derive_strike_step_python(
                tuple(s for s in ladder if isinstance(s, Decimal) and s.is_finite() and s > 0)
            )
            if on_result is None or off_result is None:
                assert on_result is None and off_result is None
            else:
                assert on_result.as_tuple() == off_result.as_tuple()


_HOSTILE_FLOATS = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=True),
    st.sampled_from(
        [
            24_024.999999999996,
            81_249.99999999999,
            1e15 + 25,
            5e-324,
            -24_025.0,
            24_025.0,
            50_075.0,
            0.0,
            -0.0,
        ]
    ),
)


class TestStrikeGridDifferential:
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(spot=_HOSTILE_FLOATS, instrument=st.sampled_from(list(OptionsUnderlying)))
    def test_round_to_atm_full_float_plane(
        self, spot: float, instrument: OptionsUnderlying
    ) -> None:
        """The seam-stage FULL-float differential vs live CPython (the Rust
        stage's owed obligation): value equality AND raise TYPE + message
        parity for NaN/inf."""
        _require_wheel()
        from brutex.options.strike_grid import STRIKE_STEP

        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            ocr.reset_optcost_rust_for_tests(remaining=0)
            rust = _outcome_of(lambda: round_to_atm(spot, instrument))
            python = _outcome_of(lambda: _round_to_atm_python(spot, STRIKE_STEP[instrument]))
            assert rust == python
            assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        atm=st.integers(min_value=-(2**64), max_value=2**64),
        steps=st.integers(min_value=-(2**32), max_value=2**32),
        is_bull=st.booleans(),
    )
    def test_offset_and_grid_i64_extremes(self, atm: int, steps: int, is_bull: bool) -> None:
        """Out-of-i64 inputs overflow the kernel → the oracle's unbounded
        Python int answer must ship either way."""
        _require_wheel()
        os.environ[ocr.OPTCOST_TAILS_RUST_ENV] = "1"
        try:
            ocr.reset_optcost_rust_for_tests(remaining=0)
            direction = TradeDirection.BULL if is_bull else TradeDirection.BEAR
            rule = RULES_BY_NAME["atm_plus_5_in_dir"]
            expected = atm + (5 if is_bull else -5) * 50
            assert resolve_offset_strike(rule, direction, atm, 50) == expected
            assert strike_offset_from_atm(atm, steps, OptionsUnderlying.NIFTY) == atm + steps * 50
            assert not ocr.optcost_rust_disabled()
        finally:
            os.environ.pop(ocr.OPTCOST_TAILS_RUST_ENV, None)
            ocr.reset_optcost_rust_for_tests()

    def test_grid_around_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wheel()
        _flag_on(monkeypatch, steady=True)
        for half_width in (0, 1, 10, 25):
            for instrument in OptionsUnderlying:
                on_result = grid_around_atm(24_000, instrument, half_width)
                step = 50 if instrument is OptionsUnderlying.NIFTY else 100
                assert on_result == [24_000 + i * step for i in range(-half_width, half_width + 1)]
        assert not ocr.optcost_rust_disabled()


# ─────────────────────────────────────────────────────────────────
# Import-graph acyclicity (the CODEQL FIX ROUND)
# ─────────────────────────────────────────────────────────────────


class TestImportGraphAcyclic:
    """The CODEQL FIX ROUND import-order pins — the cycle class can't return.

    Code-scanning alerts 1308 (``moneyness.py:37`` — "begins an import
    cycle", error) + 1309 (``optcost_rust.py:148`` — the interim
    ``TYPE_CHECKING``-only ``Moneyness`` import, runtime-free yet still a
    module-level cyclic-import finding, note): the facade now takes the
    graded bucket values by INJECTION (``classify_graded(itm=, atm=, otm=)``)
    and imports ``brutex.data.moneyness`` NOWHERE, so the import graph is
    acyclic in BOTH directions, statically and at runtime — the exact
    ``fno_symbol_rust`` precedent (``TestImportGraphAcyclic`` there). Two
    layers: (a) fresh-interpreter subprocess imports in EVERY order (each
    module alone + both sequential orders — a reintroduced runtime cycle
    fails here), and (b) an AST source pin that refuses ANY import of
    ``brutex.data.moneyness`` from the facade — including a
    ``TYPE_CHECKING``-guarded one (the exact shape that regressed).
    """

    @pytest.mark.parametrize(
        "statements",
        [
            ("import brutex.optcost_rust",),
            ("import brutex.data.moneyness",),
            ("import brutex.optcost_rust", "import brutex.data.moneyness"),
            ("import brutex.data.moneyness", "import brutex.optcost_rust"),
        ],
        ids=["facade_alone", "consumer_alone", "facade_then_consumer", "consumer_then_facade"],
    )
    def test_fresh_interpreter_import_order(self, statements: tuple[str, ...]) -> None:
        code = "\n".join(statements)
        proc = subprocess.run(  # fixed argv, our own interpreter — never shell
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=_REPO_ROOT,
        )
        assert proc.returncode == 0, (
            f"fresh-interpreter import failed for {statements!r}:\n{proc.stderr}"
        )

    def test_facade_source_never_imports_moneyness(self) -> None:
        src = (_REPO_ROOT / "brutex" / "optcost_rust.py").read_text(encoding="utf-8")
        offenders: list[str] = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "brutex.data.moneyness" or module.endswith(".moneyness"):
                    offenders.append(f"line {node.lineno}: from {module} import …")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"line {node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name == "brutex.data.moneyness"
                )
        assert offenders == [], (
            "the facade must NEVER import brutex.data.moneyness (not even under "
            "TYPE_CHECKING — the no-cycle law, DEC-OPTCOST-TAILS-RUST-001 CODEQL "
            f"FIX ROUND, alerts 1308/1309): {offenders}"
        )


# ─────────────────────────────────────────────────────────────────
# CI + mutation wiring pins
# ─────────────────────────────────────────────────────────────────


class TestWiringPins:
    def test_mutation_workflow_carries_the_optcost_pair(self) -> None:
        text = (_REPO_ROOT / ".github" / "workflows" / "mutation.yml").read_text(encoding="utf-8")
        assert '"brutex/optcost_rust.py:tests/unit/test_optcost_rust.py:"' in text

    def test_ci_carries_the_facade_coverage_gate(self) -> None:
        text = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "--cov=brutex.optcost_rust" in text
