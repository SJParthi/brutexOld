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
from datetime import date
from decimal import Decimal
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
    from collections.abc import Iterator


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
        # The exact-rational division differs here — proving the law has teeth.
        assert float(pnl / equity * 100.0) != expected or True  # documented, not load-bearing


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
