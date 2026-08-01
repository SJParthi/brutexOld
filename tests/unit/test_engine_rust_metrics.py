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
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused
from brutex.engine.rust_metrics import (
    FLAT_DRAWDOWN_STATS,
    MAX_EXACT_EQUITY_PAISA,
    SELF_CHECK_SAMPLES,
    SWEEP_RUST_METRICS_DEFAULT,
    SWEEP_RUST_METRICS_ENV,
    _valid_drawdown,  # pyright: ignore[reportPrivateUsage] — the seam's own suite
    consume_self_check_sample,
    disable_kernel,
    drawdown_compare_key,
    drawdown_stats_via_kernel,
    exceeds_exact_equity_envelope,
    kernel_disabled,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    self_check_pending,
    sweep_rust_metrics_enabled,
    warn_fallback_once,
)
from brutex.metrics.compute import _drawdown_stats  # pyright: ignore[reportPrivateUsage]
from brutex.metrics.types import EquityCurve
from structlog.testing import capture_logs

from tests.unit.test_metrics_compute import _trade  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _fresh_memo() -> None:
    """Each test observes first-occurrence log events (memo isolation) and a
    fresh burn-in budget / unlatched kernel (self-check isolation)."""
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()


@pytest.fixture
def _strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable strict mode for tests that exercise the DEGRADE contract.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` ON by
    default, so every fall-back this facade announces (junk flag, absent /
    stale wheel, a degrade-set raise, a poisoned kernel return, a burn-in
    mismatch) now RAISES instead of degrading. Warn-and-degrade remains a
    fully supported, kill-switched mode — these tests pin THAT mode, so they
    opt into it explicitly by name rather than having their assertions
    weakened. Deliberately NOT autouse: a test that does not name this
    fixture runs under production strict mode.

    Tests of ``exit_indices_demoted`` need no opt-in — it is excluded from
    the gate because Rust still serves it (``rust_metrics._NOT_A_FALLBACK``),
    and :class:`TestStrictMode` pins exactly that.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


# ── flag resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  On  "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is False


def test_flag_unset_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is ON — ADR-0017 (DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001),
    the 'separate later ADR' DEC-SWEEP-RUST-METRICS-001 deferred the flip to.
    ``BRUTEX_SWEEP_RUST_METRICS=0`` is the operator kill switch."""
    monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
    assert sweep_rust_metrics_enabled() is True
    assert SWEEP_RUST_METRICS_DEFAULT is True


@pytest.mark.parametrize("value", ["", "   "])
def test_flag_empty_follows_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Unset/whitespace-empty falls to the default constant — the flip lever
    (the ONLY vocabulary that follows :data:`SWEEP_RUST_METRICS_DEFAULT`)."""
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is SWEEP_RUST_METRICS_DEFAULT


@pytest.mark.parametrize("value", ["flase", "0ff", "disable", "garbage", "2"])
@pytest.mark.usefixtures("_strict_off")
def test_flag_unrecognised_fails_safe_off_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A typo'd kill switch still KILLS under default-ON — fail-safe OFF.

    An unrecognised non-empty value resolves OFF with exactly one
    ``flag_unrecognised`` fallback warning naming the value; a silent
    follow-the-default would leave a misbehaving kernel serving every mask
    while the operator believes it disabled (the seeded fail-safe law).
    """
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    with capture_logs() as logs:
        assert sweep_rust_metrics_enabled() is False
        assert sweep_rust_metrics_enabled() is False  # warn-once, still OFF
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "flag_unrecognised"
    assert events[0]["value"] == value


def test_flag_unset_follows_the_default_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset resolves to the ONE default constant — the flip lever."""
    monkeypatch.delenv(SWEEP_RUST_METRICS_ENV, raising=False)
    assert sweep_rust_metrics_enabled() is SWEEP_RUST_METRICS_DEFAULT


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "  Off  "])
def test_flag_explicit_falsy_is_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A ``_FALSY`` value is OFF regardless of the default constant."""
    monkeypatch.setenv(SWEEP_RUST_METRICS_ENV, value)
    assert sweep_rust_metrics_enabled() is False


# ── burn-in self-check + autorevert latch primitives ────────────────────


def test_self_check_budget_counts_down_and_clamps() -> None:
    reset_self_check_for_tests(remaining=2)
    assert self_check_pending() is True
    consume_self_check_sample()
    assert self_check_pending() is True
    consume_self_check_sample()
    assert self_check_pending() is False
    consume_self_check_sample()  # clamped — never goes negative
    assert self_check_pending() is False


def test_reset_restores_the_full_burn_in_budget() -> None:
    reset_self_check_for_tests(remaining=0)
    assert self_check_pending() is False
    reset_self_check_for_tests()
    assert self_check_pending() is True
    assert SELF_CHECK_SAMPLES > 0


@pytest.mark.usefixtures("_strict_off")
def test_disable_kernel_latches_and_logs_error_once() -> None:
    assert kernel_disabled() is False
    with capture_logs() as logs:
        disable_kernel("self_check_mismatch", n_bars=5)
        disable_kernel("some_other_reason")  # no-op: already latched
    assert kernel_disabled() is True
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.kernel_autoreverted"]
    assert len(events) == 1
    assert events[0]["reason"] == "self_check_mismatch"
    assert events[0]["n_bars"] == 5


@pytest.mark.usefixtures("_strict_off")
def test_latched_kernel_short_circuits_with_warn_once() -> None:
    """Once autoreverted, the seam never touches the kernel again this process."""
    disable_kernel("self_check_mismatch")
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, -10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(0, -10)]) is None
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert [e["reason"] for e in events] == ["kernel_disabled"]
    assert not [e for e in logs if e["event"] == "sweep_rust_metrics.engaged"]


def test_drawdown_compare_key_is_bit_exact_on_signed_zero() -> None:
    """-0.0 == +0.0 under ==; the compare key must distinguish them."""
    plus = (0, 0.0, 0, 0.0)
    minus = (0, -0.0, 0, 0.0)
    assert plus == minus  # float equality is blind to the sign
    assert drawdown_compare_key(plus) != drawdown_compare_key(minus)
    assert drawdown_compare_key(plus) == drawdown_compare_key((0, 0.0, 0, 0.0))


def test_drawdown_compare_key_is_total_on_huge_ints() -> None:
    """A poisoned huge int must not raise (ints are compared, never packed)."""
    huge = (1 << 100, 1.0, 3, 2.0)
    assert drawdown_compare_key(huge) == drawdown_compare_key((1 << 100, 1.0, 3, 2.0))
    assert drawdown_compare_key(huge) != drawdown_compare_key((0, 1.0, 3, 2.0))


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


@pytest.mark.usefixtures("_strict_off")
def test_warn_fallback_once_logs_first_then_silent() -> None:
    with capture_logs() as logs:
        warn_fallback_once("some_reason", n_trades=3)
        warn_fallback_once("some_reason", n_trades=4)
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "some_reason"
    assert events[0]["n_trades"] == 3


@pytest.mark.usefixtures("_strict_off")
def test_warn_fallback_once_new_reason_warns_again() -> None:
    with capture_logs() as logs:
        warn_fallback_once("reason_a")
        warn_fallback_once("reason_b")
    assert [e["reason"] for e in logs if e["event"] == "sweep_rust_metrics.fallback"] == [
        "reason_a",
        "reason_b",
    ]


@pytest.mark.usefixtures("_strict_off")
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


@pytest.mark.usefixtures("_strict_off")
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


@pytest.mark.usefixtures("_strict_off")
def test_wheel_stale_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel that imports but lacks the Phase-B symbol degrades loudly."""
    monkeypatch.delattr(fno_math, "sweep_drawdown_stats")
    with capture_logs() as logs:
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
        assert drawdown_stats_via_kernel(2, 1_000, [(0, 10)]) is None
    events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
    assert [e["reason"] for e in events] == ["wheel_stale"]


@pytest.mark.usefixtures("_strict_off")
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


@pytest.mark.usefixtures("_strict_off")
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


# ── strict mode: no silent Python fall-back ─────────────────────────────


class TestStrictMode:
    """DEC-RUST-STRICT-NO-FALLBACK-001 at this facade's three choke points.

    Every path that hands a mask to the Python oracle must REFUSE under
    ``BRUTEX_RUST_STRICT`` — and the ONE reason that is *not* a fall-back
    (``exit_indices_demoted``, where Rust still serves) must NOT.
    """

    @pytest.mark.parametrize(
        "reason",
        [
            "flag_unrecognised",
            "wheel_missing",
            "wheel_stale",
            "kernel_disabled",
            "kernel_error",
            "equity_envelope_exceeded",
        ],
    )
    def test_every_genuine_fallback_reason_refuses(self, reason: str) -> None:
        """Each reason that routes a mask to Python raises, naming itself."""
        with pytest.raises(RustFallbackRefused) as excinfo:
            warn_fallback_once(reason, n_trades=3)
        message = str(excinfo.value)
        assert "brutex.engine.rust_metrics" in message
        assert reason in message
        assert RUST_STRICT_ENV in message

    def test_exit_indices_demoted_does_not_refuse(self) -> None:
        """The ONE non-fall-back reason stays a plain warn-once.

        ``_exit_pairs`` demotes to the bisect fold and hands the IDENTICAL
        pairs to the SAME Rust kernel — Rust serves, so refusing would crash
        a correct mask (``rust_metrics._NOT_A_FALLBACK``). It must still be
        ANNOUNCED, so the degrade sentinel keeps seeing the site.
        """
        with capture_logs() as logs:
            warn_fallback_once("exit_indices_demoted", n_trades=2)
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert len(events) == 1
        assert events[0]["reason"] == "exit_indices_demoted"

    def test_disable_kernel_refuses_before_latching(self) -> None:
        """A burn-in mismatch raises, and does NOT leave a latched process.

        Refusing BEFORE the latch matters: a refused process must not be left
        in a silently-degraded steady state where every later mask is served
        by Python.
        """
        with pytest.raises(RustFallbackRefused) as excinfo:
            disable_kernel("self_check_mismatch", n_bars=5)
        assert "self_check_mismatch" in str(excinfo.value)
        assert kernel_disabled() is False

    def test_poisoned_kernel_result_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``kernel_result_refused`` arm is gated too.

        It announces through its OWN ``_LOG.error`` rather than
        ``warn_fallback_once``, so without its own gate call it would be the
        one unrefused Python path left.
        """

        def _poison(n_bars: int, start: int, exits: list[tuple[int, int]]) -> object:
            return ("not", "a", "drawdown", "tuple")

        monkeypatch.setattr(fno_math, "sweep_drawdown_stats", _poison)
        with pytest.raises(RustFallbackRefused) as excinfo:
            drawdown_stats_via_kernel(2, 1_000, [(0, 10)])
        assert "kernel_result_refused" in str(excinfo.value)

    def test_healthy_kernel_call_is_untouched_by_strict_mode(self) -> None:
        """Strict mode changes NOTHING on the path Rust serves.

        The whole point: with the wheel present and the input inside the
        envelope there is no fall-back to refuse, so the kernel answers
        exactly as it does with strict mode off.
        """
        assert drawdown_stats_via_kernel(4, 1_000, [(1, -100), (3, 200)]) == (
            100,
            10.0,
            2,
            _ulcer(),
        )

    @pytest.mark.usefixtures("_strict_off")
    def test_strict_off_restores_the_degrade_contract(self) -> None:
        """The kill switch is the ONE lever back to warn-and-degrade."""
        with capture_logs() as logs:
            warn_fallback_once("wheel_missing")
        events = [e for e in logs if e["event"] == "sweep_rust_metrics.fallback"]
        assert len(events) == 1
        assert events[0]["reason"] == "wheel_missing"


def _ulcer() -> float:
    """Ulcer index of equity [1000, 900, 900, 1100] — the known-values curve."""
    dd_pct = 100.0 / 1000.0
    return math.sqrt((dd_pct * dd_pct + dd_pct * dd_pct) / 4.0) * 100.0
