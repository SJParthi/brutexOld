"""Unit tests for the ALPHA-vs-BUY&HOLD metric (DEC-ALPHA-BUYHOLD-METRIC-001).

100% line+branch on :mod:`brutex.engine.alpha_metric`: the shared domain law,
the pure-Python oracle values, the kernel routing (wheel-missing /
symbol-missing / kernel-raised / poisoned-result degrades — each warn-once),
the :func:`compute_alpha_values` refuse-poison cross-check, the paisa
conversion + baseline projection, and the DEFAULT-OFF fail-loud
``BRUTEX_ALPHA_GATE`` resolution.

The kernel arms are driven by FAKE ``fno_math`` modules (deterministic on a
wheel-free box); a real-wheel belt test additionally proves the live kernel
engages when installed.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from fractions import Fraction
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from brutex.engine import alpha_metric
from brutex.engine.alpha_metric import (
    ALPHA_GATE_ENV,
    AlphaBaseline,
    AlphaMetricError,
    AlphaValues,
    alpha_baseline_from_series,
    alpha_vs_buyhold_pct,
    alpha_vs_buyhold_pct_python,
    compute_alpha_values,
    price_to_paisa,
    reset_alpha_kernel_for_tests,
    resolve_alpha_gate_enabled,
)
from brutex.engine.baseline import BaselineSeries
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice() -> Iterator[None]:
    """Run this module's DEGRADE lattice with the strict gate switched OFF.

    Under DEC-RUST-STRICT-NO-FALLBACK-001 the shipped default is strict-ON, so
    every CLASS-A/CLASS-B degrade this suite exercises (``wheel_missing``,
    ``symbol_missing``, ``kernel_raised``, ``result_not_float``,
    ``alpha_cross_check_failed``) would RAISE instead of serving the oracle
    triple. The mode is DECLARED here rather than by weakening a single
    assertion; the strict-ON refusals of the SAME reasons are pinned in
    :class:`TestStrictRefusal` below.

    Owns its OWN :class:`pytest.MonkeyPatch` context (never the shared
    ``monkeypatch``) so its teardown cannot interleave with the per-test
    ``delenv`` in :meth:`TestStrictRefusal._strict_on` — the
    ``test_ranking_cvar_gate`` precedent.

    FUNCTION-scoped deliberately (the same precedent): ``conftest.py``'s
    ``pytest_runtest_teardown`` hook runs ``trylast=True`` and restores the
    process-wide strict baseline after EVERY test, so a ``scope="module"``
    ``setenv`` would be wiped after the first test in the file and every later
    degrade case would run strict-ON.
    """
    from brutex.config import rust_strict

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(rust_strict.RUST_STRICT_ENV, "0")
        yield


@pytest.fixture(autouse=True)
def _fresh_kernel_memo() -> Iterator[None]:
    """Isolate the process-level kernel memo + warn-once registry per test."""
    reset_alpha_kernel_for_tests()
    yield
    reset_alpha_kernel_for_tests()


def _fake_fno_math(monkeypatch: pytest.MonkeyPatch, kernel: object | None) -> ModuleType:
    """Install a fake ``fno_math`` module (``None`` kernel = no symbol)."""
    module = ModuleType("fno_math")
    if kernel is not None:
        module.alpha_vs_buyhold_pct = kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fno_math", module)
    return module


class TestDomainValidation:
    """The ONE domain law both arms share (kernel guards are unreachable)."""

    @pytest.mark.parametrize(
        ("pnl", "equity", "anchor", "close", "needle"),
        [
            (1.5, 1, 1, 1, "net_pnl_paisa must be an int"),
            (True, 1, 1, 1, "net_pnl_paisa must be an int"),
            (0, "1", 1, 1, "start_equity_paisa must be an int"),
            (0, 1, None, 1, "anchor_open_paisa must be an int"),
            (0, 1, 1, 2.0, "last_close_paisa must be an int"),
            (2**63, 1, 1, 1, "net_pnl_paisa outside the i64 envelope"),
            (-(2**63) - 1, 1, 1, 1, "net_pnl_paisa outside the i64 envelope"),
            (0, 2**63, 1, 1, "start_equity_paisa outside the i64 envelope"),
            (0, 0, 1, 1, "start_equity_paisa must be >= 1"),
            (0, -7, 1, 1, "start_equity_paisa must be >= 1"),
            (0, 1, 0, 1, "anchor_open_paisa must be >= 1"),
            (0, 1, 1, -1, "last_close_paisa must be >= 0"),
        ],
    )
    def test_refusals_name_the_field(
        self, pnl: object, equity: object, anchor: object, close: object, needle: str
    ) -> None:
        with pytest.raises(AlphaMetricError, match="alpha_vs_buyhold_pct"):
            alpha_vs_buyhold_pct_python(pnl, equity, anchor, close)  # type: ignore[arg-type]
        with pytest.raises(AlphaMetricError) as excinfo:
            alpha_vs_buyhold_pct(pnl, equity, anchor, close)  # type: ignore[arg-type]
        assert needle in str(excinfo.value)

    def test_boundary_values_accepted(self) -> None:
        # i64 corners + the zero-close price are IN-domain (belt vs the
        # kernel's own guards; results stay finite).
        assert alpha_vs_buyhold_pct_python(2**63 - 1, 2**63 - 1, 2**63 - 1, 0) == pytest.approx(
            100.0 - (-100.0)
        )
        assert alpha_vs_buyhold_pct_python(-(2**63), 1, 1, 0) < 0


class TestOracleValues:
    """Hand-derived oracle values (the golden module carries the frozen set)."""

    def test_plus_ten_minus_four(self) -> None:
        # strategy +₹1,00,000 on ₹10,00,000 = +10%; B&H ₹25,000→₹26,000 = +4%.
        assert alpha_vs_buyhold_pct_python(10_000_000, 100_000_000, 2_500_000, 2_600_000) == 6.0

    def test_negative_alpha(self) -> None:
        # strategy −5% vs B&H +2.5% → −7.5.
        assert alpha_vs_buyhold_pct_python(-5_000_000, 100_000_000, 2_000_000, 2_050_000) == -7.5

    def test_convert_first_law_beyond_2_53(self) -> None:
        # |values| > 2**53: the oracle MUST convert-first (float(a)/float(b)),
        # not exact-rational-divide — the bit-identity law with the kernel.
        pnl, equity = 2**60 + 1, 3
        expected = float(pnl) / float(equity) * 100.0
        got = alpha_vs_buyhold_pct_python(pnl, equity, 1, 1)
        assert got == expected
        # The exact-rational path genuinely DIFFERS here (verified: the
        # correctly-rounded Fraction result is 1 ULP above the convert-first
        # result, since float(2**60 + 1) rounds down to 2**60) — proving the
        # convert-first law has teeth at |values| > 2**53 (review r1 NIT 3).
        assert float(Fraction(pnl, equity) * 100) != expected


class TestKernelRouting:
    def test_wheel_missing_degrades_to_oracle_warn_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)  # import → ImportError
        with capture_logs() as logs:
            first = alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
            second = alpha_vs_buyhold_pct(-5_000_000, 100_000_000, 2_000_000, 2_050_000)
        assert first == 6.0
        assert second == -7.5
        events = [log for log in logs if log["event"] == "alpha_metric.kernel_unavailable"]
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"

    def test_symbol_missing_degrades_warn_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_fno_math(monkeypatch, kernel=None)
        with capture_logs() as logs:
            assert alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000) == 6.0
        events = [log for log in logs if log["event"] == "alpha_metric.kernel_unavailable"]
        assert len(events) == 1
        assert events[0]["reason"] == "symbol_missing"

    def test_fake_kernel_result_is_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[int, int, int, int]] = []

        def kernel(pnl: int, equity: int, anchor: int, close: int) -> float:
            calls.append((pnl, equity, anchor, close))
            return 6.0

        _fake_fno_math(monkeypatch, kernel)
        assert alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000) == 6.0
        assert calls == [(10_000_000, 100_000_000, 2_500_000, 2_600_000)]

    def test_kernel_memoized_one_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel_calls: list[int] = []

        def kernel(*args: int) -> float:
            kernel_calls.append(1)
            return 6.0

        _fake_fno_math(monkeypatch, kernel)
        alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        # Swap in a POISON module after the first resolve: the memo must keep
        # serving the resolved callable, never re-import.
        monkeypatch.setitem(sys.modules, "fno_math", None)
        alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        assert len(kernel_calls) == 2

    def test_kernel_raise_latches_off_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kernel(*args: int) -> float:
            raise ValueError("poisoned kernel")

        _fake_fno_math(monkeypatch, kernel)
        with capture_logs() as logs:
            got = alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
            again = alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        assert got == 6.0
        assert again == 6.0
        events = [log for log in logs if log["event"] == "alpha_metric.kernel_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "kernel_raised"

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kernel(*args: int) -> float:
            raise MemoryError

        _fake_fno_math(monkeypatch, kernel)
        with pytest.raises(MemoryError):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)

    @pytest.mark.parametrize("poison", [6, "6.0", None, True])
    def test_non_float_result_latches_off(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _fake_fno_math(monkeypatch, lambda *args: poison)
        with capture_logs() as logs:
            assert alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000) == 6.0
        events = [log for log in logs if log["event"] == "alpha_metric.kernel_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "result_not_float"

    def test_real_wheel_engages_bit_identically(self) -> None:
        """Belt: the LIVE kernel (when installed) matches the oracle exactly."""
        pytest.importorskip("fno_math")
        got = alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        assert got == alpha_vs_buyhold_pct_python(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        assert alpha_metric._KERNEL_MEMO["fn"] is not None  # kernel resolved live


class TestWarnOnce:
    def test_second_emission_of_the_same_event_is_suppressed(self) -> None:
        with capture_logs() as logs:
            alpha_metric._warn_once("alpha_metric.test_event", detail=1)
            alpha_metric._warn_once("alpha_metric.test_event", detail=2)
        events = [log for log in logs if log["event"] == "alpha_metric.test_event"]
        assert len(events) == 1
        assert events[0]["detail"] == 1


class TestComputeAlphaValues:
    def test_triple_and_exact_subtraction_law(self) -> None:
        values = compute_alpha_values(10_000_000, 100_000_000, AlphaBaseline(2_500_000, 2_600_000))
        assert values == AlphaValues(strategy_return_pct=10.0, bh_return_pct=4.0, alpha_pct=6.0)
        assert values.alpha_pct == values.strategy_return_pct - values.bh_return_pct

    def test_cross_check_latches_a_value_poisoned_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A well-SHAPED wrong float sails past the type gate; the leg
        # cross-check catches it — the oracle value ships, the kernel latches.
        _fake_fno_math(monkeypatch, lambda *args: 123.456)
        with capture_logs() as logs:
            values = compute_alpha_values(
                10_000_000, 100_000_000, AlphaBaseline(2_500_000, 2_600_000)
            )
        assert values.alpha_pct == 6.0
        events = [log for log in logs if log["event"] == "alpha_metric.kernel_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "alpha_cross_check_failed"
        # Latched: the next compute never consults the poisoned kernel again.
        values2 = compute_alpha_values(-5_000_000, 100_000_000, AlphaBaseline(2_000_000, 2_050_000))
        assert values2.alpha_pct == -7.5


class TestBaselineProjection:
    def test_alpha_baseline_validation(self) -> None:
        with pytest.raises(AlphaMetricError, match="anchor_open_paisa must be >= 1"):
            AlphaBaseline(anchor_open_paisa=0, last_close_paisa=1)
        with pytest.raises(AlphaMetricError, match="last_close_paisa must be >= 0"):
            AlphaBaseline(anchor_open_paisa=1, last_close_paisa=-1)

    @pytest.mark.parametrize(
        ("price", "paisa"),
        [
            (Decimal("25000.00"), 2_500_000),
            (Decimal("0.01"), 1),
            # ROUND_HALF_UP at the half-boundary (the round_money law).
            (Decimal("1.005"), 101),
            (Decimal("14020.7999"), 1_402_080),
        ],
    )
    def test_price_to_paisa_exact(self, price: Decimal, paisa: int) -> None:
        assert price_to_paisa(price) == paisa

    def test_from_series_uses_anchor_and_last_close(self) -> None:
        series = BaselineSeries(
            instrument="NIFTY",
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            anchor_open=Decimal("25000.00"),
            sessions=(
                (date(2024, 1, 1), Decimal("25100.00")),
                (date(2024, 1, 3), Decimal("26000.00")),
            ),
            total_return_pct=4.0,
            source_note="fixture",
        )
        baseline = alpha_baseline_from_series(series)
        assert baseline == AlphaBaseline(anchor_open_paisa=2_500_000, last_close_paisa=2_600_000)


class TestGateEnvResolution:
    def test_unset_and_blank_default_off(self) -> None:
        assert resolve_alpha_gate_enabled({}) is False
        assert resolve_alpha_gate_enabled({ALPHA_GATE_ENV: "  "}) is False

    @pytest.mark.parametrize("token", ["1", "on", "true", "yes", "ON", "True"])
    def test_truthy_enables(self, token: str) -> None:
        assert resolve_alpha_gate_enabled({ALPHA_GATE_ENV: token}) is True

    @pytest.mark.parametrize("token", ["0", "off", "false", "no", "OFF"])
    def test_falsy_disables(self, token: str) -> None:
        assert resolve_alpha_gate_enabled({ALPHA_GATE_ENV: token}) is False

    def test_junk_raises_naming_the_env(self) -> None:
        with pytest.raises(ValueError, match=ALPHA_GATE_ENV):
            resolve_alpha_gate_enabled({ALPHA_GATE_ENV: "maybe"})

    def test_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALPHA_GATE_ENV, "1")
        assert resolve_alpha_gate_enabled() is True
        monkeypatch.delenv(ALPHA_GATE_ENV)
        assert resolve_alpha_gate_enabled() is False


class TestConftestBelt:
    """The suite-wide hermeticity belt (tests/conftest.py) drift pins."""

    def test_env_name_matches_the_conftest_literal(self) -> None:
        # conftest.py pins the literal (wheel-free discipline); this pin
        # keeps the two homes equal so they can never silently drift.
        assert ALPHA_GATE_ENV == "BRUTEX_ALPHA_GATE"

    def test_suite_runs_with_the_gate_defaulted_off(self) -> None:
        import os

        assert os.environ.get(ALPHA_GATE_ENV, "0") in {"0", "off", "false", "no"}


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Every OTHER test in this module runs with ``BRUTEX_RUST_STRICT=0``. These
    DELETE the kill switch so strict mode takes its shipped default (ON) and
    pin that this facade's ONE choke point (:func:`_refuse_if_strict`, reached
    from both ``_kernel`` arms and from ``_disable_kernel``) REFUSES instead of
    quietly serving the Python oracle.

    Both classes are covered:

    * **CLASS A — Rust absent or broke** — ``wheel_missing``,
      ``symbol_missing``, ``kernel_raised``.
    * **CLASS B — Rust answered wrongly** — ``result_not_float``,
      ``alpha_cross_check_failed``.

    There is deliberately no CLASS-C case: this facade's exempt set is EMPTY
    and provably so (:data:`alpha_metric._STRICT_EXEMPT_REASONS`), because
    ``_validate_inputs`` runs BEFORE the kernel is consulted, so an input that
    reaches the crate is one the crate is defined on.
    """

    @staticmethod
    @contextmanager
    def _strict_on() -> Generator[None]:
        """Delete the kill switch for the DURATION OF THE TEST BODY ONLY.

        Owns its OWN :class:`pytest.MonkeyPatch` context rather than the
        shared ``monkeypatch`` fixture: ``_strict_off_degrade_lattice`` sets
        ``BRUTEX_RUST_STRICT=0`` in a *separate* context and pytest tears
        fixtures down LIFO across fixture objects — a shared-``monkeypatch``
        ``delenv`` here would record ``"0"`` as the value to restore and leak
        the kill switch into every later test file in the process.
        """
        from brutex.config import rust_strict

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
            assert rust_strict.strict_mode() is True
            yield

    # ── CLASS A — Rust absent or broke ─────────────────────────────────────

    def test_class_a_wheel_missing_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setitem(sys.modules, "fno_math", None)  # import raises
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="wheel_missing"):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)

    def test_class_a_symbol_missing_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        _fake_fno_math(monkeypatch, kernel=None)  # module present, symbol absent
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="symbol_missing"):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)

    def test_class_a_kernel_raise_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        def _boom(*_args: int) -> float:
            raise RuntimeError("crate fault")

        _fake_fno_math(monkeypatch, _boom)
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="kernel_raised"):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)

    # ── CLASS B — Rust answered wrongly ────────────────────────────────────

    @pytest.mark.parametrize("poison", [6, True, None, "6.0"])
    def test_class_b_non_float_result_refuses(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        _fake_fno_math(monkeypatch, lambda *_args: poison)
        with self._strict_on(), pytest.raises(RustFallbackRefused, match="result_not_float"):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)

    def test_class_b_cross_check_failure_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        # A well-shaped float that is simply WRONG — the refuse-poison belt.
        _fake_fno_math(monkeypatch, lambda *_args: 123.456)
        baseline = AlphaBaseline(anchor_open_paisa=2_500_000, last_close_paisa=2_600_000)
        with (
            self._strict_on(),
            pytest.raises(RustFallbackRefused, match="alpha_cross_check_failed"),
        ):
            compute_alpha_values(10_000_000, 100_000_000, baseline)

    # ── MemoryError keeps its RAW escape ───────────────────────────────────

    def test_memory_error_still_reraises_raw_under_strict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict mode must not convert an OOM into a refusal.

        ``MemoryError`` is re-raised BEFORE the choke point, so the operator
        still sees the real fault rather than a fall-back verdict.
        """

        def _oom(*_args: int) -> float:
            raise MemoryError

        _fake_fno_math(monkeypatch, _oom)
        with self._strict_on(), pytest.raises(MemoryError):
            alpha_vs_buyhold_pct(10_000_000, 100_000_000, 2_500_000, 2_600_000)


class TestCensusFlagDeclaration:
    """The facade DISOWNS its ranking gate so the census reads it correctly.

    ``BRUTEX_ALPHA_GATE`` is the operator RANKING gate, not a Rust kill
    switch — the Rust arm here is unconditionally attempted. Without the
    declaration the census's shape heuristics read the gate as the flag and
    reported this facade ``OFF``.
    """

    def test_declaration_names_both_halves_of_the_shape(self) -> None:
        declared = alpha_metric.RUST_CENSUS_NON_RUST_FLAGS
        # The VALUE (what the env-value scan matches) …
        assert ALPHA_GATE_ENV in declared
        # … the CONSTANT name, and the PREDICATE name (what the *_enabled
        # scan matches). Missing either half leaves the census mis-reading.
        assert {"ALPHA_GATE_ENV", "resolve_alpha_gate_enabled"} <= declared

    def test_declaration_is_exported_in_dunder_all(self) -> None:
        """The name is READ REFLECTIVELY, so it must be exported deliberately.

        ``rust_census.census`` reads it with
        ``getattr(module, "RUST_CENSUS_NON_RUST_FLAGS", None)`` — a consumer
        no intra-module analysis can see, which is exactly why CodeQL's
        ``py/unused-global-variable`` flagged it. The export is the honest
        fix (it IS part of this module's public contract with the census),
        and this pin stops a future ``__all__`` tidy-up from silently
        reopening the alert.
        """
        assert "RUST_CENSUS_NON_RUST_FLAGS" in alpha_metric.__all__
        # Every exported name must resolve — a stale entry is its own defect.
        for name in alpha_metric.__all__:
            assert hasattr(alpha_metric, name), name

    def test_census_reports_the_facade_engaged_not_off(self) -> None:
        """The census-visible effect, pinned end to end.

        Fails LOUDLY if the declaration is deleted, if the census stops
        honouring it, or if the gate's default flips — any of which would
        silently restore the ``OFF`` misreading.
        """
        pytest.importorskip("fno_math")
        from brutex.observability.rust_census import census

        rows = {row.module: row for row in census(["brutex.engine.alpha_metric"])}
        row = rows["brutex.engine.alpha_metric"]
        assert row.env_var is None, "the ranking gate must not be read as the Rust flag"
        assert row.flag_on is None, "the ranking gate must not be read as the Rust flag"
        assert row.crate == "fno_math"
        assert row.verdict == "ENGAGED"
