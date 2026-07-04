"""Unit tests for the flag-gated Rust sweep-metrics seam (rust_metrics.py).

DEC-SWEEP-RUST-METRICS-001. Covers the env-flag resolution, the f64-exactness
envelope guard, the kernel binding (wheel-missing / wheel-stale / kernel-error
/ refused-result fallbacks + the engaged-once signal), the domain validation,
and the observability memo — every branch, for the strict 100% line+branch
gate. The kernel-vs-Python-oracle bit-equality is pinned here at the seam
level (plus hypothesis in ``tests/property`` and the sweep golden end-to-end).
"""

from __future__ import annotations

import math
import struct
import sys

import fno_math
import pytest
from brutex.engine.rust_metrics import (
    FLAT_DRAWDOWN_STATS,
    MAX_EXACT_EQUITY_PAISA,
    SWEEP_RUST_METRICS_ENV,
    _valid_drawdown,  # pyright: ignore[reportPrivateUsage] — the seam's own suite
    drawdown_stats_via_kernel,
    exceeds_exact_equity_envelope,
    reset_observability_memo_for_tests,
    sweep_rust_metrics_enabled,
    warn_fallback_once,
)
from brutex.metrics.compute import _drawdown_stats  # pyright: ignore[reportPrivateUsage]
from brutex.metrics.types import EquityCurve
from structlog.testing import capture_logs

from tests.unit.test_metrics_compute import _trade  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _fresh_memo() -> None:
    """Each test observes first-occurrence log events (memo isolation)."""
    reset_observability_memo_for_tests()


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


# ── flag resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  On  "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "garbage", "2"])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is False


def test_flag_unset_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is OFF — the flip to ON-by-default is a separate later ADR."""
    monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
    assert sweep_rust_metrics_enabled() is False


# ── the flat-curve shortcut constant ────────────────────────────────────


def test_flat_drawdown_stats_bit_matches_the_real_scan() -> None:
    """(0, +0.0, 0, +0.0) equals ``_drawdown_stats`` on a flat curve — bit-level.

    The zero-trade shortcut's analytic identity, including the SIGN of the
    two zero floats (a ``-0.0`` would survive a value-level ``==``).
    """
    curve = EquityCurve(
        equity_paisa=(7_000, 7_000, 7_000), start_equity_paisa=7_000, span_years=1.0
    )
    scanned = _drawdown_stats(curve)
    assert scanned == FLAT_DRAWDOWN_STATS
    assert _bits(FLAT_DRAWDOWN_STATS[1]) == _bits(scanned[1])
    assert _bits(FLAT_DRAWDOWN_STATS[3]) == _bits(scanned[3])


# ── exactness-envelope guard ────────────────────────────────────────────


def test_envelope_empty_trades_within() -> None:
    assert exceeds_exact_equity_envelope(10_000_000, []) is False


def test_envelope_boundary_is_inclusive() -> None:
    """``start + Σ|pnl| == 2**52`` is the LAST admissible magnitude."""
    at = [_trade(MAX_EXACT_EQUITY_PAISA - 1)]
    assert exceeds_exact_equity_envelope(1, at) is False
    over = [_trade(MAX_EXACT_EQUITY_PAISA - 1), _trade(1)]
    assert exceeds_exact_equity_envelope(1, over) is True


def test_envelope_uses_absolute_pnl() -> None:
    """Losses count by magnitude — a large NEGATIVE Σpnl also exceeds."""
    trades = [_trade(-(MAX_EXACT_EQUITY_PAISA // 2)), _trade(-(MAX_EXACT_EQUITY_PAISA // 2))]
    assert exceeds_exact_equity_envelope(1_000, trades) is True


# ── warn-once memo ──────────────────────────────────────────────────────


def test_warn_fallback_once_logs_first_then_silent() -> None:
    with capture_logs() as logs:
        warn_fallback_once("some_reason", n_trades=3)
        warn_fallback_once("some_reason", n_trades=4)
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "some_reason"
    assert events[0]["n_trades"] == 3


def test_warn_fallback_once_new_reason_warns_again() -> None:
    with capture_logs() as logs:
        warn_fallback_once("reason_a")
        warn_fallback_once("reason_b")
    assert [e["reason"] for e in logs if e["event"] == "sweep_rust_metrics.fallback"] == [
        "reason_a",
        "reason_b",
    ]


def test_reset_restores_first_occurrence_logging() -> None:
    with capture_logs() as logs:
        warn_fallback_once("reason_a")
        reset_observability_memo_for_tests()
        warn_fallback_once("reason_a")
    assert len([e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]) == 2


# ── kernel result domain validation (the refuse-poison gate) ────────────


def test_valid_drawdown_accepts_a_real_kernel_shape() -> None:
    assert _valid_drawdown((100, 10.0, 2, 7.07), n_bars=4) is True
    assert _valid_drawdown((0, 0.0, 0, 0.0), n_bars=1) is True


@pytest.mark.parametrize(
    "poison",
    [
        [100, 10.0, 2, 7.07],  # not a tuple
        (100, 10.0, 2),  # wrong arity
        (100, 10.0, 2, 7.07, 0),  # wrong arity (long)
        ("100", 10.0, 2, 7.07),  # dd_paisa not int
        (True, 10.0, 2, 7.07),  # bool masquerading as int
        (100, 10.0, "2", 7.07),  # dd_bars not int
        (100, 10.0, True, 7.07),  # bool dd_bars
        (100, 10, 2, 7.07),  # pct not float
        (100, 10.0, 2, "x"),  # ulcer not float
        (-1, 10.0, 2, 7.07),  # negative dd_paisa
        (100, 10.0, -1, 7.07),  # negative duration
        (100, 10.0, 5, 7.07),  # duration exceeds n_bars
        (100, math.nan, 2, 7.07),  # non-finite pct
        (100, -0.5, 2, 7.07),  # negative pct
        (100, 10.0, 2, math.inf),  # non-finite ulcer
        (100, 10.0, 2, -0.5),  # negative ulcer
    ],
)
def test_valid_drawdown_rejects_poison(poison: object) -> None:
    assert _valid_drawdown(poison, n_bars=4) is False


# ── drawdown_stats_via_kernel ───────────────────────────────────────────


def test_kernel_call_matches_python_oracle_bitwise() -> None:
    """The seam's output == ``_drawdown_stats`` on the materialised curve."""
    start = 10_000_000
    exits = [(1, -5_000), (1, -2_000), (3, 9_000), (0, -1)]
    n_bars = 5
    got = drawdown_stats_via_kernel(n_bars, start, exits)
    assert got is not None
    realized = [0] * n_bars
    for idx, pnl in exits:
        realized[idx] += pnl
    equity = []
    running = start
    for delta in realized:
        running += delta
        equity.append(running)
    expected = _drawdown_stats(
        EquityCurve(equity_paisa=tuple(equity), start_equity_paisa=start, span_years=1.0)
    )
    assert got == expected
    assert _bits(got[1]) == _bits(expected[1])
    assert _bits(got[3]) == _bits(expected[3])


def test_kernel_success_logs_engaged_once() -> None:
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, -10)]) is not None
        assert drawdown_stats_via_kernel(2, 1_000, [(1, -10)]) is not None
    engaged = [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]
    assert len(engaged) == 1
    assert engaged[0]["n_bars"] == 2


def test_kernel_error_falls_back_with_one_warning() -> None:
    """A real kernel ValueError (index out of range) degrades to the oracle."""
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(5, 10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(5, 10)]) is None
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "kernel_error"
    assert "out of range" in events[0]["error"]


def test_kernel_memory_error_reraises_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001) — never a fallback."""

    def _oom(n_bars: int, start: int, exits: list[tuple[int, int]]) -> object:
        raise MemoryError

    monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _oom)
    with pytest.raises(MemoryError):
        drawdown_stats_via_kernel(2, 1_000, [(0, 10)])


def test_wheel_stale_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel that imports but lacks the Phase-B symbol degrades loudly."""
    monkeypatch.delattr(fno_math, "sweep_drawdown_stats")
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert [e["reason"] for e in events] == ["wheel_stale"]


def test_wheel_missing_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truly absent wheel (ImportError arm) degrades loudly.

    ``sys.modules['fno_math'] = None`` makes the lazy in-function import
    raise ImportError — the stdlib's blocked-import convention.
    """
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert [e["reason"] for e in events] == ["wheel_missing"]


def test_poisoned_result_is_refused_loudly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A domain-invalid kernel return is REFUSED (ERROR log), never adopted."""

    def _poison(n_bars: int, start: int, exits: list[tuple[int, int]]) -> object:
        return (0, math.nan, 0, 0.0)

    monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _poison)
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
    refused = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_result_refused"]
    assert len(refused) == 1
    assert refused[0]["n_bars"] == 2
    # No engaged signal ever fired for a refused result.
    assert not [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]


def test_domain_valid_poison_flows_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A domain-VALID poison is returned — the seam cannot detect it.

    The honest envelope of the runtime refuse gate: only the golden /
    property suites can catch an in-domain wrong answer. This test pins that
    the seam genuinely CONSUMES the kernel result (the vacuous-gate trap:
    were the seam secretly recomputing in Python, the poison would vanish).
    """

    def _poison(n_bars: int, start: int, exits: list[tuple[int, int]]) -> object:
        return (12_345, 6.25, 1, 3.5)

    monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _poison)
    assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) == (12_345, 6.25, 1, 3.5)


# ── serial-fold guarantee: no rayon in the metrics kernel ───────────────


def test_metrics_kernel_source_has_no_rayon() -> None:
    """The drawdown fold is ORDER-SENSITIVE — a rayon reduction would change
    the sum_sq_pct fold order and the result. Pin the structural guarantee
    (the kernel is strictly serial) at the source level so it cannot
    silently regress; mirrors the repo's redteam source pins.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "rust" / "fno-math" / "src" / "metrics.rs"
    ).read_text(encoding="utf-8")
    # Match USE sites, not the docstring prose that (correctly) forbids them.
    assert "use rayon" not in src
    assert "rayon::" not in src
    assert ".mul_add(" not in src  # an explicit FMA would fuse where CPython rounds twice
