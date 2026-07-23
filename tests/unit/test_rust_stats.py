"""Unit tests for the flag-gated Rust Stage-3/4 statistics seam (rust_stats.py).

DEC-METRICS-RUST-STATS-001 (slice 6, PR-1). Covers the ``BRUTEX_STATS_RUST``
tristate resolution, the per-function autorevert latch, the kernel binding
(wheel-missing / wheel-stale / kernel-error / refused-result fallbacks + the
engaged-once signal), the burn-in self-check (match consumes; mismatch ships
the ORACLE value and latches), and the observability memo — every branch, for
the strict 100% line+branch gate driven by THIS file alone (the ci.yml
``--include="*/brutex/metrics/rust_stats.py"`` entry). The kernel-vs-oracle
equivalence is additionally pinned by hypothesis in
``tests/property/test_rust_stats_equivalence.py`` and by the flag-ON golden
arm in ``tests/golden/test_cpcv_wfo_golden.py``.

Requires the ``fno_math`` wheel (CI builds it via ``pip install
./rust/fno-math``); a wheel-free environment fails these tests loudly rather
than passing vacuously (the engaged-assertion law).
"""

from __future__ import annotations

import ast
import math
import struct
import sys
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

import fno_math
import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import (
    _combine_moments,  # pyright: ignore[reportPrivateUsage]
    _fold_pass_count,  # pyright: ignore[reportPrivateUsage]
    _group_bounds,  # pyright: ignore[reportPrivateUsage]
    _group_moments,  # pyright: ignore[reportPrivateUsage]
    cpcv_pass_rate,
)
from brutex.metrics.rust_stats import (
    SELF_CHECK_SAMPLES,
    STATS_RUST_DEFAULT,
    STATS_RUST_ENV,
    _bounded_text,  # pyright: ignore[reportPrivateUsage]
    _consume_self_check_sample,  # pyright: ignore[reportPrivateUsage]
    _safe_context,  # pyright: ignore[reportPrivateUsage]
    _self_check_pending,  # pyright: ignore[reportPrivateUsage]
    _valid_pass_count,  # pyright: ignore[reportPrivateUsage]
    cpcv_pass_count_via_kernel,
    disable_kernel,
    kernel_disabled,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
    stats_rust_enabled,
    warn_fallback_once,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    import numpy.typing as npt


@pytest.fixture(autouse=True)
def _fresh_seam() -> None:
    """Each test observes first-occurrence log events and a fresh burn-in
    budget / unlatched kernels (the documented test-isolation seams)."""
    reset_observability_memo_for_tests()
    reset_self_check_for_tests()


def _golden_moments() -> tuple[
    npt.NDArray[np.intp], npt.NDArray[np.float64], npt.NDArray[np.float64]
]:
    """The cpcv golden fixture's per-group moments: returns [1, 2, -3, -4],
    S=4 singleton groups → exactly ONE of the C(4, 2)=6 folds passes."""
    counts = np.array([1, 1, 1, 1], dtype=np.intp)
    means = np.array([1.0, 2.0, -3.0, -4.0], dtype=np.float64)
    m2s = np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    return counts, means, m2s


def _call_kernel(
    *,
    n_total: int = 6,
    oracle_value: int = 1,
    oracle_calls: list[int] | None = None,
) -> int | None:
    """Drive the ONE FFI call site with the golden moments + a spying oracle."""
    counts, means, m2s = _golden_moments()

    def _oracle() -> int:
        if oracle_calls is not None:
            oracle_calls.append(1)
        return oracle_value

    return cpcv_pass_count_via_kernel(counts, means, m2s, 2, 0.0, n_total=n_total, oracle=_oracle)


# ── flag resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  On  "])
def test_flag_truthy_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STATS_RUST_ENV, value)
    assert stats_rust_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "  Off  "])
def test_flag_falsy_values_disable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(STATS_RUST_ENV, value)
    assert stats_rust_enabled() is False


def test_flag_unset_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFAULT-ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
    2026-07-23): an unset flag arms the Rust statistics kernels;
    ``BRUTEX_STATS_RUST=0`` stays the explicit kill switch."""
    monkeypatch.delenv(STATS_RUST_ENV, raising=False)
    with capture_logs() as logs:
        assert stats_rust_enabled() is True
    # SILENTLY on: the unset default emits NO fallback event (kills the
    # env-default mutant that fabricates a non-empty raw value).
    assert logs == []
    assert STATS_RUST_DEFAULT is True


@pytest.mark.parametrize("value", ["", "   "])
def test_flag_empty_follows_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Whitespace-empty short-circuits to the default BEFORE the lazy engine
    import (the wheel-free-default-arm law) — same verdict either way."""
    monkeypatch.setenv(STATS_RUST_ENV, value)
    assert stats_rust_enabled() is STATS_RUST_DEFAULT


@pytest.mark.parametrize("value", ["flase", "0ff", "enable", "garbage", "2"])
def test_flag_unrecognised_fails_safe_off_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An unrecognised non-empty value resolves OFF with exactly one
    ``flag_unrecognised`` warning naming the value (warn-once)."""
    monkeypatch.setenv(STATS_RUST_ENV, value)
    with capture_logs() as logs:
        assert stats_rust_enabled() is False
        assert stats_rust_enabled() is False  # warn-once, still OFF
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert len(events) == 1
    assert events[0]["reason"] == "flag_unrecognised"
    assert events[0]["fn"] == "flag"
    # Round 4: the env value is not seam-controlled — it rides the total,
    # bounded ``_safe_context`` (repr-quoted, control chars escaped).
    assert events[0]["value"] == repr(value)


def test_flag_engine_unavailable_degrades_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SET flag in an environment where ``brutex.engine`` cannot import
    (e.g. the trading_core wheel absent/stale — ``engine/__init__`` eagerly
    imports stage1a → trading_core) degrades to :data:`STATS_RUST_DEFAULT`
    with ONE warn-once ``flag_engine_unavailable`` event: the ImportError
    must never propagate out of ``cpcv_pass_rate`` (the "wheel state can
    never change results or error behaviour" contract covers BOTH wheels).
    """
    returns = np.linspace(-0.01, 0.01, 32)
    # The pure-Python oracle answer, computed BEFORE the import is poisoned.
    monkeypatch.setenv(STATS_RUST_ENV, "0")
    expected = cpcv_pass_rate(returns)
    monkeypatch.setenv(STATS_RUST_ENV, "1")
    # ``None`` in sys.modules makes ``from X import Y`` raise ImportError
    # even if the module was ALREADY imported earlier in the process (the
    # ``fno_math`` wheel-missing trick); ``monkeypatch.setitem`` restores
    # the prior entry — or its absence — when the test ends.
    monkeypatch.setitem(sys.modules, "brutex.engine.sweep_lookahead", None)
    with capture_logs() as logs:
        # The default constant, NOT a hardcoded False (flip-lever contract).
        assert stats_rust_enabled() is STATS_RUST_DEFAULT
        rate = cpcv_pass_rate(returns)  # no crash — the Python oracle serves
        assert stats_rust_enabled() is STATS_RUST_DEFAULT  # warn-once probe
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert len(events) == 1  # three resolutions above → exactly ONE warn
    assert events[0]["reason"] == "flag_engine_unavailable"
    assert events[0]["fn"] == "flag"
    assert struct.pack("<d", rate) == struct.pack("<d", expected)


def test_flag_env_name_and_default_pinned() -> None:
    """Literal pins (mutation teeth): the env var name and the default-ON
    constant (DEC-RUST-DEFAULT-ON-WAVE-001) are operator-facing contracts."""
    assert STATS_RUST_ENV == "BRUTEX_STATS_RUST"
    assert STATS_RUST_DEFAULT is True


def test_self_check_budgets_pinned() -> None:
    """The R8 per-function burn-in budgets (call cadence × oracle cost).

    PR-2 (DEC-METRICS-RUST-STATS-002) adds the DSR pair — cheap ~µs scalar
    oracles at per-candidate cadence, the cpcv-class K=16 budget.
    """
    assert dict(SELF_CHECK_SAMPLES) == {
        "cpcv": 16,
        "mc": 8,
        "pbo": 4,
        "spa": 2,
        "wfo": 16,
        "psr": 16,
        "sr_max": 16,
    }


# ── latch + observability primitives ───────────────────────────────────


def test_disable_kernel_latches_once_per_function() -> None:
    assert kernel_disabled("cpcv") is False
    with capture_logs() as logs:
        disable_kernel("cpcv", "self_check_mismatch", got=2, want=1)
        disable_kernel("cpcv", "different_reason")  # no-op: already latched
    events = [e for e in logs if e["event"] == "rust_stats.kernel_autoreverted"]
    assert len(events) == 1
    assert events[0]["fn"] == "cpcv"
    assert events[0]["reason"] == "self_check_mismatch"
    assert events[0]["got"] == 2
    assert events[0]["want"] == 1
    assert kernel_disabled("cpcv") is True


def test_latch_is_per_function_not_global() -> None:
    """A poisoned ``cpcv`` kernel must never disable a sibling function."""
    disable_kernel("cpcv", "self_check_mismatch")
    assert kernel_disabled("cpcv") is True
    assert kernel_disabled("pbo") is False


def test_warn_fallback_once_per_function_reason_pair() -> None:
    with capture_logs() as logs:
        warn_fallback_once("cpcv", "wheel_missing")
        warn_fallback_once("cpcv", "wheel_missing")  # memoized
        warn_fallback_once("pbo", "wheel_missing")  # different fn → warns
        warn_fallback_once("cpcv", "kernel_error")  # different reason → warns
    events = [(e["fn"], e["reason"]) for e in logs if e["event"] == "rust_stats.fallback"]
    assert events == [("cpcv", "wheel_missing"), ("pbo", "wheel_missing"), ("cpcv", "kernel_error")]


def test_reset_observability_memo_restores_first_occurrence() -> None:
    with capture_logs() as logs:
        warn_fallback_once("cpcv", "wheel_missing")
        reset_observability_memo_for_tests()
        warn_fallback_once("cpcv", "wheel_missing")
    assert len([e for e in logs if e["event"] == "rust_stats.fallback"]) == 2


def test_reset_self_check_named_function_and_remaining() -> None:
    reset_self_check_for_tests("cpcv", remaining=0)
    assert _self_check_pending("cpcv") is False
    reset_self_check_for_tests("cpcv")
    assert _self_check_pending("cpcv") is True
    # A named reset also unlatches exactly that function.
    disable_kernel("cpcv", "self_check_mismatch")
    disable_kernel("pbo", "self_check_mismatch")
    reset_self_check_for_tests("cpcv")
    assert kernel_disabled("cpcv") is False
    assert kernel_disabled("pbo") is True  # sibling latch untouched
    # An unknown function name defaults to a zero budget (no KeyError).
    reset_self_check_for_tests("nonesuch")
    assert _self_check_pending("nonesuch") is False


def test_reset_self_check_all_functions() -> None:
    reset_self_check_for_tests(remaining=0)
    assert all(not _self_check_pending(fn) for fn in SELF_CHECK_SAMPLES)
    disable_kernel("cpcv", "self_check_mismatch")
    reset_self_check_for_tests()
    assert kernel_disabled("cpcv") is False
    assert all(_self_check_pending(fn) for fn in SELF_CHECK_SAMPLES)


def test_reset_all_clears_extraneous_keys() -> None:
    """The ``fn=None`` arm restores the EXACT fresh-process view: a budget
    key inserted by a prior targeted reset of a never-registered name, and
    a latch on a never-registered name, are both CLEARED — never left
    behind as a superset of the registered set."""
    reset_self_check_for_tests("extraneous_fn_pin")  # inserts a stray key
    disable_kernel("extraneous_fn_pin", "self_check_mismatch")
    reset_self_check_for_tests()
    remaining = rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert set(remaining) == set(SELF_CHECK_SAMPLES)
    assert remaining == dict(SELF_CHECK_SAMPLES)  # values at the defaults
    assert rust_stats._KERNEL_DISABLED_REASON == {}  # pyright: ignore[reportPrivateUsage]
    assert kernel_disabled("extraneous_fn_pin") is False


def test_consume_self_check_clamps_at_zero() -> None:
    reset_self_check_for_tests("cpcv", remaining=1)
    assert _self_check_pending("cpcv") is True  # kills `> 0` → `> 1`
    _consume_self_check_sample("cpcv")
    assert _self_check_pending("cpcv") is False
    _consume_self_check_sample("cpcv")  # clamped — never negative
    assert _self_check_pending("cpcv") is False
    # Whitebox: the counter sits at EXACTLY 0 (kills `> 0` → `>= 0`, whose
    # over-consume would drive it negative).
    assert rust_stats._SELF_CHECK_REMAINING["cpcv"] == 0  # pyright: ignore[reportPrivateUsage]


def test_self_check_helpers_on_a_never_registered_function() -> None:
    """A function name NEVER inserted into the budget dict: pending is False
    (kills the `.get(fn, 0)` → `.get(fn, 1)` default mutant) and consume is
    a clean no-op that never inserts nor raises (under the consume-side
    default mutant a missing key would ``KeyError`` on the decrement)."""
    fresh = "never_registered_fn_pin"
    assert fresh not in rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert _self_check_pending(fresh) is False
    _consume_self_check_sample(fresh)
    assert fresh not in rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert _self_check_pending(fresh) is False


def test_internal_memo_and_latch_shapes_pinned() -> None:
    """Whitebox pins on the process-state SHAPES (mutation teeth): the memo
    keys are the documented ``fn:reason`` / ``fn:__engaged__`` forms and the
    latch stores the caller's reason VERBATIM — the observability memo and
    latch dict are the module's only mutable state, so their key/value
    shapes are contracts, not incidentals."""
    assert rust_stats._ENGAGED_KEY == "__engaged__"  # pyright: ignore[reportPrivateUsage]
    assert rust_stats._FLAG_FN == "flag"  # pyright: ignore[reportPrivateUsage]
    warn_fallback_once("cpcv", "wheel_missing")
    assert "cpcv:wheel_missing" in rust_stats._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]
    disable_kernel("cpcv", "self_check_mismatch")
    latch = rust_stats._KERNEL_DISABLED_REASON  # pyright: ignore[reportPrivateUsage]
    assert latch["cpcv"] == "self_check_mismatch"


# ── result domain validation ────────────────────────────────────────────


@pytest.mark.parametrize(
    "result",
    [(1,), "1", 1.0, None, True, False, -1, 7],
)
def test_valid_pass_count_rejects_non_domain(result: object) -> None:
    """Wrong type, ``bool`` (an ``int`` subtype), negative, or beyond
    ``n_total`` — all refused."""
    assert _valid_pass_count(result, 6) is False


@pytest.mark.parametrize("result", [0, 1, 6])
def test_valid_pass_count_accepts_domain(result: int) -> None:
    assert _valid_pass_count(result, 6) is True


# ── the FFI call site: fallbacks ────────────────────────────────────────


def test_wheel_missing_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.modules['fno_math'] = None`` makes the lazy in-function import
    raise ImportError — the wheel-ABSENT environment."""
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert _call_kernel() is None
        assert _call_kernel() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["wheel_missing"]
    assert events[0]["fn"] == "cpcv"


def test_wheel_stale_falls_back_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel that imports but predates the slice-6 symbol degrades loudly."""
    monkeypatch.delattr(fno_math, "stats_cpcv_pass_count")
    with capture_logs() as logs:
        assert _call_kernel() is None
        assert _call_kernel() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["wheel_stale"]
    assert events[0]["attr"] == "stats_cpcv_pass_count"


@pytest.mark.parametrize("exc", [ValueError("boom"), TypeError("boom"), OverflowError("boom")])
def test_kernel_error_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def _raise(*args: object) -> int:
        raise exc

    monkeypatch.setattr(fno_math, "stats_cpcv_pass_count", _raise)
    with capture_logs() as logs:
        assert _call_kernel() is None
        assert _call_kernel() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_error"]
    # Round 4: the context rides ``_safe_context`` — a benign exception's
    # repr passes through EXACTLY (the totality guard changes nothing on
    # the healthy path).
    assert events[0]["error"] == repr(exc)


def test_memory_error_propagates_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM is a GLOBAL stop — never swallowed into a fallback."""

    def _oom(*args: object) -> int:
        raise MemoryError

    monkeypatch.setattr(fno_math, "stats_cpcv_pass_count", _oom)
    with pytest.raises(MemoryError):
        _call_kernel()


@pytest.mark.parametrize("poison", [True, -1, 7, (1,), "1", 1.0])
def test_refused_result_is_loud_once_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, poison: object
) -> None:
    """A domain-invalid kernel result is refused with ONE ERROR (never
    silently adopted) and the call is served by the Python oracle."""

    def _poison(*args: object) -> object:
        return poison

    monkeypatch.setattr(fno_math, "stats_cpcv_pass_count", _poison)
    with capture_logs() as logs:
        assert _call_kernel() is None
        assert _call_kernel() is None  # second refusal is memoized
    events = [e for e in logs if e["event"] == "rust_stats.kernel_result_refused"]
    assert len(events) == 1
    assert events[0]["fn"] == "cpcv"
    # Round 4: both contexts ride ``_safe_context`` — benign reprs pass
    # through exactly; ``n_total`` is stringified (bounded-total law).
    assert events[0]["result"] == repr(poison)
    assert events[0]["n_total"] == "6"
    # Whitebox: the refusal memo key is the documented ``fn:reason`` form.
    assert "cpcv:kernel_result_refused" in rust_stats._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]


def test_latched_kernel_short_circuits_with_one_warning() -> None:
    disable_kernel("cpcv", "self_check_mismatch")
    with capture_logs() as logs:
        assert _call_kernel() is None
        assert _call_kernel() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_disabled"]


# ── the FFI call site: min_sharpe exactness domain guard ────────────────
# Refuter-round-1 CONFIRMED defect (fix round 2): the seam fed the kernel
# ``float(min_sharpe)`` while the Python oracle compares the ORIGINAL object
# EXACTLY (CPython's mixed-type int↔float comparison is exact), so a
# type-legal int threshold past 2^53 gave ON≠OFF and falsely LATCHED a
# faithful kernel during burn-in. The guard engages the kernel only when the
# conversion is value-exact; anything else is a pre-burn-in domain refusal.


def test_min_sharpe_inexact_int_is_domain_refused_before_burn_in() -> None:
    """Moment-level refuter repro VERBATIM through the seam: the group-0
    fold Sharpe is EXACTLY ``2^53 + 4``; ``min_sharpe = 2^53 + 3`` (int) is
    not float-representable — ``float()`` rounds it UP onto the fold Sharpe
    (ties-to-even), so the raw kernel (996.0 > 996.0 → 0) and the exact
    oracle (…996 > …995 → 1) genuinely DIVERGE. The seam must refuse the
    kernel BEFORE burn-in: warn-once ``min_sharpe_inexact``, no sample
    consumed, no autorevert risk, the burn-in oracle never invoked here."""
    sharpe_val = float(2**53 + 4)
    m_int = 2**53 + 3
    assert float(m_int) == sharpe_val  # the information loss the guard sees
    assert m_int < sharpe_val  # ...while the EXACT compare still orders them
    counts = np.array([2, 2], dtype=np.intp)
    means = np.array([sharpe_val, 0.0], dtype=np.float64)
    m2s = np.array([2.0, 0.0], dtype=np.float64)
    # The divergence the guard exists for (documented at the raw layers —
    # the oracle compares exactly, the rounded kernel cannot):
    assert _fold_pass_count(counts, means, m2s, 2, 1, m_int) == 1
    raw_kernel = fno_math.stats_cpcv_pass_count(
        [2, 2], [sharpe_val, 0.0], [2.0, 0.0], 1, float(m_int)
    )
    assert raw_kernel == 0
    budgets_before = dict(rust_stats._SELF_CHECK_REMAINING)  # pyright: ignore[reportPrivateUsage]

    def _forbidden() -> int:
        raise AssertionError("a domain refusal must not run the burn-in oracle")

    with capture_logs() as logs:
        assert (
            cpcv_pass_count_via_kernel(counts, means, m2s, 1, m_int, n_total=2, oracle=_forbidden)
            is None
        )
        assert (  # second refusal is memoized (warn-once)
            cpcv_pass_count_via_kernel(counts, means, m2s, 1, m_int, n_total=2, oracle=_forbidden)
            is None
        )
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_inexact"]
    assert events[0]["fn"] == "cpcv"
    assert events[0]["min_sharpe"] == repr(m_int)
    # No burn-in sample consumed; no autorevert fired; latch clean.
    assert budgets_before == rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert kernel_disabled("cpcv") is False
    assert not any(e["event"] == "rust_stats.kernel_autoreverted" for e in logs)


@pytest.mark.parametrize("exact_int", [0, 1, -1, 2**53])
def test_min_sharpe_value_exact_int_stays_on_the_kernel(exact_int: int) -> None:
    """An int threshold whose float image is VALUE-exact engages the kernel
    normally and matches the exact oracle (kills the guard's ``!=`` → ``==``
    mutant, under which every exact threshold would refuse and engagement
    could never fire)."""
    counts, means, m2s = _golden_moments()
    expected = _fold_pass_count(counts, means, m2s, 4, 2, exact_int)
    reset_self_check_for_tests("cpcv", remaining=0)
    with capture_logs() as logs:
        got = cpcv_pass_count_via_kernel(
            counts, means, m2s, 2, exact_int, n_total=6, oracle=lambda: -1
        )
    assert got == expected
    assert any(e["event"] == "rust_stats.engaged" for e in logs)
    assert not any(e["event"] == "rust_stats.fallback" for e in logs)


def test_min_sharpe_nan_takes_the_oracle_by_design() -> None:
    """``float('nan') != float('nan')`` is True, so a NaN threshold
    conservatively takes the domain refusal → the Python oracle serves —
    the DOCUMENTED choice. SAFE and result-identical: ``sharpe > nan`` is
    False on BOTH engines (every fold a non-pass), pinned here against the
    raw kernel AND the oracle before asserting the refusal; a degenerate
    all-non-pass threshold has no kernel win worth a NaN special case."""
    counts, means, m2s = _golden_moments()
    nan = float("nan")
    # Both engines agree the answer is 0 — the refusal changes WHICH engine
    # serves, never the result.
    assert _fold_pass_count(counts, means, m2s, 4, 2, nan) == 0
    raw_kernel = fno_math.stats_cpcv_pass_count(
        [1, 1, 1, 1], [1.0, 2.0, -3.0, -4.0], [0.0, 0.0, 0.0, 0.0], 2, nan
    )
    assert raw_kernel == 0
    with capture_logs() as logs:
        assert (
            cpcv_pass_count_via_kernel(counts, means, m2s, 2, nan, n_total=6, oracle=lambda: 0)
            is None
        )
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_inexact"]
    assert kernel_disabled("cpcv") is False
    # Rate level: both arms serve the identical all-non-pass answer.
    returns = [0.01, -0.02, 0.03, -0.01]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=nan)
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=nan)
    assert struct.pack("<d", on) == struct.pack("<d", off) == struct.pack("<d", 0.0)


def test_refuter_round1_rate_level_on_off_bit_equal_and_no_false_autorevert() -> None:
    """Rate-level refuter repro VERBATIM (the refuter round-1 repro script,
    made deterministic): ULP-wobble returns around 1.0 whose fold Sharpe is
    EXACTLY ``2^53 + 4``, with the int threshold ``2^53 + 3`` one below it.
    Pre-guard: OFF read 2/6 while ON read 0.0 (bits differ) and the burn-in
    falsely LATCHED the faithful kernel. Post-guard: ON≡OFF bit-equal in
    burn-in AND steady state, no autorevert fires, the latch is clean
    afterwards."""
    vals = [1.0 + w * 2.0**-52 for w in (2, 1, 2, 2, 1, 2, 2, 0)]
    m_int = 2**53 + 3  # exact compare: BELOW the 2^53+4 fold Sharpe; float() rounds ONTO it
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(vals, n_groups=4, n_test_groups=2, min_sharpe=m_int)
        assert off == 2 / 6  # the exact-compare answer (2 of the 6 folds pass)
        mp.setenv(STATS_RUST_ENV, "1")
        # Steady state (post-burn-in — the refuter's 17th-call shape).
        reset_self_check_for_tests("cpcv", remaining=0)
        on_steady = cpcv_pass_rate(vals, n_groups=4, n_test_groups=2, min_sharpe=m_int)
        # Fresh-process burn-in (the false-autorevert shape).
        reset_self_check_for_tests("cpcv")
        reset_observability_memo_for_tests()
        with capture_logs() as logs:
            on_burn_in = cpcv_pass_rate(vals, n_groups=4, n_test_groups=2, min_sharpe=m_int)
    assert struct.pack("<d", on_steady) == struct.pack("<d", off)
    assert struct.pack("<d", on_burn_in) == struct.pack("<d", off)
    assert not any(e["event"] == "rust_stats.kernel_autoreverted" for e in logs)
    assert any(
        e["event"] == "rust_stats.fallback" and e["reason"] == "min_sharpe_inexact" for e in logs
    )
    assert kernel_disabled("cpcv") is False  # latch state clean afterwards


# ── the FFI call site: exact-TYPE gate (refuter round 2) ────────────────
# Refuter-round-2 CONFIRMED defects (fix round 3), both in the round-2
# equality-only guard: (D1) the hoisted ``float(min_sharpe)`` ran OUTSIDE
# the narrow catch, so an int past float range CRASHED the ON arm
# (OverflowError) where OFF returns a rate — and it changed the exception
# TYPE/site for garbage thresholds ("abc": ON ValueError vs OFF TypeError);
# (D2) the guard tested conversion EQUALITY while the oracle's predicate is
# ORDERING — a type-legal float SUBCLASS with reversed ``__lt__`` (honest
# ``__eq__``) passed the guard yet flipped ``sharpe > min_sharpe``:
# steady-state ON≠OFF bit divergence AND a burn-in FALSE AUTOREVERT. The
# exact-TYPE gate closes both: only exact-type float (non-NaN) / int / bool
# with a value-exact (guarded) conversion engages; everything else refuses
# to the oracle, which applies the ORIGINAL object's semantics on BOTH arms.


class _DescendingFloat(float):
    """Refuter-A round-2 D2 attack verbatim: a type-legal ``float`` subclass
    (``isinstance(…, float)`` is True; mypy-clean for ``min_sharpe: float``)
    whose ORDERING is reversed while equality stays honest — it passes any
    value-exactness test (``float(ms) == ms``) yet flips the oracle's
    ``sharpe > min_sharpe`` dispatch (the reflected ``__lt__`` outranks
    ``float.__gt__``)."""

    def __gt__(self, other: float) -> bool:
        return float.__lt__(self, other)

    def __lt__(self, other: float) -> bool:
        return float.__gt__(self, other)

    def __ge__(self, other: float) -> bool:
        return float.__le__(self, other)

    def __le__(self, other: float) -> bool:
        return float.__ge__(self, other)


class _FloatReturnsStr:
    """A ``__float__`` protocol liar (returns a non-float). The seam must
    never call ``float()`` on it — both arms raise the oracle's comparison
    TypeError identically."""

    def __float__(self) -> str:  # the deliberate protocol lie under test
        return "not-a-float"


def test_min_sharpe_float_subclass_refuses_to_oracle_on_off_bit_equal() -> None:
    """Refuter-round-2 D2 repro VERBATIM (the round-2 repro script): pre-fix
    the ordering-reversed subclass gave steady-state ON 0.833… ≠ OFF 0.166…
    (bits differ) and the burn-in falsely LATCHED the faithful kernel
    (got=5 want=1). Post-fix the exact-type gate refuses the subclass
    (``min_sharpe_foreign_type``, type name only — never repr) so the
    oracle — with the ORIGINAL object's own ordering — serves BOTH arms
    bit-identically, no autorevert, no engage, latch clean."""
    ms = _DescendingFloat(0.5)
    # The properties that defeat an equality-only guard:
    assert isinstance(ms, float)
    assert float(ms) == ms
    returns = [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=ms)
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv", remaining=0)  # steady state
        on_steady = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=ms)
        reset_self_check_for_tests("cpcv")  # fresh-process burn-in
        reset_observability_memo_for_tests()
        with capture_logs() as logs:
            on_burn_in = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=ms)
    assert struct.pack("<d", on_steady) == struct.pack("<d", off)
    assert struct.pack("<d", on_burn_in) == struct.pack("<d", off)
    assert kernel_disabled("cpcv") is False  # NO false autorevert
    assert not any(e["event"] == "rust_stats.kernel_autoreverted" for e in logs)
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_foreign_type"]
    assert events[0]["fn"] == "cpcv"
    assert events[0]["min_sharpe_type"] == "_DescendingFloat"


@pytest.mark.parametrize("huge", [10**400, -(10**400), 2**1024])
def test_min_sharpe_int_past_float_range_refuses_not_crashes(huge: int) -> None:
    """Refuter-round-2 D1 repro (the round-2 repro script): a type-legal
    ``int`` past float range makes ``float()`` raise OverflowError — pre-fix
    the hoisted conversion sat OUTSIDE the narrow catch and CRASHED the ON
    arm where OFF returns a rate. Post-fix the overflow is caught into the
    SAME ``min_sharpe_inexact`` not-float-representable refusal: ON≡OFF
    bit-equal via the oracle, one warn, no burn-in sample consumed, no
    latch, never a raise."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025] * 4
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=8, n_test_groups=4, min_sharpe=huge)
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")  # fresh burn-in budget
        budgets_before = dict(rust_stats._SELF_CHECK_REMAINING)  # pyright: ignore[reportPrivateUsage]
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=8, n_test_groups=4, min_sharpe=huge)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_inexact"]
    # Round 4 (sanctioned change over the round-2 exact-repr pin): every
    # context is length-clamped — all three trio members repr past the
    # 200-char bound, so the context carries the truncated prefix + the
    # honest marker naming the omitted count (never a log-bloating value).
    full = repr(huge)
    assert len(full) > 200  # every trio member exercises the clamp
    assert events[0]["min_sharpe"] == full[:200] + f"<+{len(full) - 200} chars truncated>"
    assert budgets_before == rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert kernel_disabled("cpcv") is False
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)


@pytest.mark.parametrize(
    "garbage",
    ["abc", Decimal("sNaN"), complex(1, 2), _FloatReturnsStr()],
    ids=["str", "decimal_snan", "complex", "float_returns_str"],
)
def test_min_sharpe_garbage_types_raise_identically_on_and_off(garbage: object) -> None:
    """Exception-parity quartet (refuter-round-2 D1 generalization): pre-fix
    the ON arm raised from the guard's own ``float()`` — "abc" gave
    ValueError where OFF gives TypeError; ``Decimal('sNaN')`` gave
    ValueError where OFF gives ``decimal.InvalidOperation``. Post-fix the
    foreign-type refusal routes BOTH arms through the oracle's comparison,
    so the exception TYPE and MESSAGE are identical (raised at the same
    site) and nothing latches."""
    returns = [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]

    def _arm(flag: str) -> tuple[str, str]:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(STATS_RUST_ENV, flag)
            reset_self_check_for_tests("cpcv")
            reset_observability_memo_for_tests()
            try:
                rate = cpcv_pass_rate(
                    returns,
                    n_groups=4,
                    n_test_groups=2,
                    min_sharpe=garbage,  # type: ignore[arg-type]  # the deliberate bad type under test
                )
            except Exception as exc:  # the parity harness compares arbitrary raises
                return (type(exc).__name__, str(exc))
            return ("value", struct.pack("<d", rate).hex())

    off_outcome = _arm("0")
    on_outcome = _arm("1")
    assert off_outcome[0] != "value"  # every quartet member raises on OFF
    assert on_outcome == off_outcome  # identical exception type + message
    assert kernel_disabled("cpcv") is False


def test_min_sharpe_garbage_with_undefined_folds_returns_on_both_arms() -> None:
    """The value-vs-exception shape (refuter A's state-torture Part A): a
    zero-variance series makes every fold Sharpe undefined, so the oracle's
    comparison is NEVER reached and OFF returns 0.0 even for a garbage
    threshold — pre-fix the ON arm raised ValueError from the guard's
    ``float("abc")`` instead. Post-fix ON refuses to the oracle and returns
    the identical 0.0."""
    returns = [0.01] * 8  # constant → zero variance → every fold undefined
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe="abc")  # type: ignore[arg-type]  # deliberate
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe="abc")  # type: ignore[arg-type]  # deliberate
    assert struct.pack("<d", on) == struct.pack("<d", off) == struct.pack("<d", 0.0)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_foreign_type"]
    assert events[0]["min_sharpe_type"] == "str"


@pytest.mark.parametrize(
    "np_ms",
    [np.float64(0.5), np.int64(3)],
    ids=["np_float64", "np_int64"],
)
def test_min_sharpe_numpy_scalar_refuses_to_oracle_value_equal(np_ms: float) -> None:
    """SANCTIONED NARROWING pin (refuter round 2, documented in the seam
    header): ``np.float64``/``np.int64`` thresholds previously ENGAGED the
    kernel (their conversions are value-exact); the exact-type gate now
    refuses them (``min_sharpe_foreign_type``) — SAFE because the oracle
    serves both arms with numpy's own promoting comparison (refuter-A
    verified it agrees with the kernel everywhere), and performance-neutral
    because the in-repo production callers pass plain Python floats."""
    returns = [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=np_ms)
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=np_ms)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_foreign_type"]
    assert events[0]["min_sharpe_type"] == type(np_ms).__name__
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)
    assert kernel_disabled("cpcv") is False


@pytest.mark.parametrize("exact_bool", [True, False])
def test_min_sharpe_bool_engages_the_kernel(exact_bool: bool) -> None:
    """The documented bool choice: ``bool`` is an exact, UNSUBCLASSABLE int
    subtype carrying int's own ordering — it engages the kernel like the
    exact 0/1 it is (kills the ``is bool`` arm-drop mutant, under which a
    bool threshold would silently refuse)."""
    counts, means, m2s = _golden_moments()
    expected = _fold_pass_count(counts, means, m2s, 4, 2, exact_bool)
    reset_self_check_for_tests("cpcv", remaining=0)
    with capture_logs() as logs:
        got = cpcv_pass_count_via_kernel(
            counts, means, m2s, 2, exact_bool, n_total=6, oracle=lambda: -1
        )
    assert got == expected
    assert any(e["event"] == "rust_stats.engaged" for e in logs)
    assert not any(e["event"] == "rust_stats.fallback" for e in logs)


# ── the FFI call site: refusal-context totality (refuter round 3) ───────
# Refuter-round-3 CONFIRMED defects (fix round 4), BOTH in the round-3
# refusal paths' own warn-CONTEXT expressions — evaluated as call arguments
# BEFORE the warn-once memo check, so each raised on EVERY call: (R3-1,
# refuter 3 — a residual of round-2 D1) the OverflowError refusal arm's
# ``repr(min_sharpe)`` is itself PARTIAL — CPython ≥3.11 caps int→str at
# ``sys.get_int_max_str_digits()`` (default 4300), so a type-legal exact
# int past ~4300 digits CRASHED the ON arm (ValueError) where OFF returns
# a rate; (R3-2, refuter 1) the foreign refusal's ``ms_type.__name__``
# lookup dispatches through the foreign TYPE's METACLASS MRO — a hostile
# metaclass ``__name__`` property raised ON-only where OFF serves the
# oracle. Both fixed by making the contexts TOTAL: the digit-limit-guarded
# ``_inexact_threshold_context`` + the C-level ``type.__dict__['__name__']``
# getset fetch in ``_type_name``.


class _MetaRaisingName(type):
    """Refuter-1 round-3 attack verbatim (the round-3 repro script): a
    metaclass whose ``__name__`` is a raising property — it shadows
    ``type``'s C-level getset for ordinary attribute lookup, so the
    pre-fix ``ms_type.__name__`` raised RuntimeError ON-only."""

    @property
    def __name__(cls) -> str:  # type: ignore[override]  # the deliberate hostile shape under test
        raise RuntimeError("hostile metaclass __name__")


class _HostileNamed(metaclass=_MetaRaisingName):
    """Foreign threshold whose reflected ``__lt__`` answers the oracle's
    ``sharpe > min_sharpe`` compare with True (every defined fold passes)."""

    def __lt__(self, other: object) -> bool:
        return True


def test_min_sharpe_hostile_metaclass_name_refuses_on_off_identical() -> None:
    """Refuter-round-3 R3-2 repro VERBATIM (the round-3 repro script):
    pre-fix OFF returned 1.0 (the oracle dispatches the object's reflected
    ``__lt__``) while ON raised RuntimeError from the foreign refusal's
    ``ms_type.__name__`` lookup through the hostile METACLASS MRO — on
    every call, refuting the "never dispatches ANY operation to a foreign
    object" claim at the type level. Post-fix the name is fetched through
    ``type``'s C-level getset descriptor (``_type_name``), so ON ALSO
    refuses to the oracle and returns the identical 1.0, with the foreign
    warn carrying the type's REAL slot name."""
    returns = [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]
    ms = _HostileNamed()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=ms)  # type: ignore[arg-type]  # the deliberate foreign type under test
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        reset_observability_memo_for_tests()
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=ms)  # type: ignore[arg-type]  # the deliberate foreign type under test
    # Every defined fold passes on both arms (the reflected __lt__ is True).
    assert struct.pack("<d", on) == struct.pack("<d", off) == struct.pack("<d", 1.0)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_foreign_type"]
    # The SAFE lookup reads the real C-level name slot the hostile
    # metaclass property shadows (kills the _type_name lookup mutants).
    assert events[0]["min_sharpe_type"] == "_HostileNamed"
    assert kernel_disabled("cpcv") is False
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)


def test_min_sharpe_int_past_repr_digit_limit_refuses_not_crashes() -> None:
    """Refuter-round-3 R3-1 repro VERBATIM (the round-3 repro script): a
    type-legal exact int past BOTH float range and CPython's int→str digit
    cap took the OverflowError refusal arm and then CRASHED in the
    refusal's own ``repr(min_sharpe)`` (ValueError: Exceeds the limit …) —
    ON raised on every call where OFF returns a rate, the residual of the
    round-2 D1 fix (whose regression trio caps at 401 digits). Post-fix
    the warn context degrades to the bounded ``bit_length`` placeholder
    and the refusal never raises: ON≡OFF bit-equal via the oracle, no
    burn-in sample consumed, no latch."""
    huge = 10**5000  # 5001 digits — past the default 4300-digit repr cap
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025] * 4
    limit_before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)  # pin the CPython DEFAULT — deterministic
    try:
        # The trap itself, pinned: int.__repr__ is partial under the limit.
        with pytest.raises(ValueError, match="Exceeds the limit"):
            repr(huge)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(STATS_RUST_ENV, "0")
            off = cpcv_pass_rate(returns, n_groups=8, n_test_groups=4, min_sharpe=huge)
            mp.setenv(STATS_RUST_ENV, "1")
            reset_self_check_for_tests("cpcv")  # fresh burn-in budget
            budgets_before = dict(rust_stats._SELF_CHECK_REMAINING)  # pyright: ignore[reportPrivateUsage]
            with capture_logs() as logs:
                on = cpcv_pass_rate(returns, n_groups=8, n_test_groups=4, min_sharpe=huge)
    finally:
        sys.set_int_max_str_digits(limit_before)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_inexact"]
    # The exact placeholder — the context is size-bounded, conversion-free
    # (kills the _inexact_threshold_context f-string/except-arm mutants).
    assert (
        events[0]["min_sharpe"]
        == f"<int past the int->str digit limit; bit_length={huge.bit_length()}>"
    )
    assert budgets_before == rust_stats._SELF_CHECK_REMAINING  # pyright: ignore[reportPrivateUsage]
    assert kernel_disabled("cpcv") is False
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)


# ── kernel-OUTPUT context totality (refuter round 4) ─────────────────────
# Refuter-round-4 CONFIRMED defect (fix round 5): the kernel-OUTPUT-side
# warn contexts bare-repr()'d a KERNEL-controlled object —
# ``kernel_result_refused``'s ``result=repr(result)`` and ``kernel_error``'s
# ``error=repr(exc)`` — so a byzantine wheel returning a >4300-digit int
# (case A), raising ValueError(10**5000) (case B), or raising an exception
# subclass with a hostile ``__repr__`` (case C) made the seam's OWN refusal
# logging raise ON-only where OFF returns a rate (the round-3 INPUT-context
# fix, un-generalized on the output side). Fix round 5 ends the CLASS: ONE
# total, bounded helper (``_safe_context`` over ``_bounded_text``) carries
# EVERY not-seam-controlled context value, kernel results are exact-TYPE
# validated (``_valid_pass_count``: ``type(result) is int`` — no foreign
# comparison dispatch on the output side either), and the AST structural
# pin (TestLogContextStructuralPin) forbids re-opening the class.


class _HostileReprError(ValueError):
    """Round-4 case C verbatim (the round-4 repro script): a ValueError
    SUBCLASS — squarely inside the narrow catch — whose ``__repr__`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("boom from exception __repr__")


class _RaisingRepr:
    """A plain object whose ``__repr__`` raises (the helper-level shape)."""

    def __repr__(self) -> str:
        raise RuntimeError("hostile __repr__")


class _OomRepr:
    """A ``__repr__`` raising MemoryError — the global-stop carve-out."""

    def __repr__(self) -> str:
        raise MemoryError


class _EvilStr(str):
    """A str SUBCLASS a hostile ``__repr__`` can smuggle past ``repr()``
    (CPython's PyUnicode_Check accepts subclasses) — lying ``__len__``/
    slicing dispatch could then detonate inside the clamp or renderer."""


class _SmuggleRepr:
    """``__repr__`` returning the str SUBCLASS above — must be refused."""

    def __repr__(self) -> str:
        return _EvilStr("evil")


class _HostileInt(int):
    """Byzantine kernel RESULT: an int SUBCLASS whose comparisons and repr
    all raise — pre-fix ``_valid_pass_count``'s ``0 <= result`` dispatched
    the subclass's own ``__ge__`` FIRST (subclass reflected-operand
    priority) and raised ON-only where OFF returns a rate."""

    __hash__ = int.__hash__

    def __ge__(self, other: int) -> bool:
        raise RuntimeError("hostile __ge__")

    def __le__(self, other: int) -> bool:
        raise RuntimeError("hostile __le__")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("hostile __eq__")

    def __ne__(self, other: object) -> bool:
        raise RuntimeError("hostile __ne__")

    def __repr__(self) -> str:
        raise RuntimeError("hostile __repr__")


class _PlainIntSub(int):
    """A benign int subclass — the sanctioned-narrowing pin (previously
    ADOPTED as a valid result; now refused by the exact-type output gate)."""


def test_bounded_text_boundary_and_marker_pinned() -> None:
    """The length-clamp law: AT the cap untouched; one char past truncates
    with the exact honest marker naming the omitted count (kills the
    cap-value, ``<=``→``<``, slice-index and marker-literal mutants)."""
    assert rust_stats._CONTEXT_MAX_CHARS == 200  # pyright: ignore[reportPrivateUsage]
    assert _bounded_text("") == ""
    assert _bounded_text("x" * 200) == "x" * 200
    assert _bounded_text("x" * 201) == "x" * 200 + "<+1 chars truncated>"
    assert _bounded_text("y" * 500) == "y" * 200 + "<+300 chars truncated>"


def test_safe_context_benign_values_are_exact_bounded_reprs() -> None:
    """The healthy path is byte-identical to a bare ``repr`` (the totality
    guard changes nothing for well-behaved values) — and a reprable-but-huge
    value is clamped, never served whole."""
    assert _safe_context(7) == "7"
    assert _safe_context("x") == "'x'"
    assert _safe_context(None) == "None"
    assert _safe_context(ValueError("boom")) == "ValueError('boom')"
    out = _safe_context("x" * 250)  # repr = 252 chars
    assert out == "'" + "x" * 199 + "<+52 chars truncated>"


def test_safe_context_raising_repr_degrades_to_typed_placeholder() -> None:
    assert _safe_context(_RaisingRepr()) == "<_RaisingRepr repr-failed>"


def test_safe_context_memory_error_propagates_bare() -> None:
    """OOM is a GLOBAL stop — never swallowed into a log placeholder (the
    documented carve-out, the same law as the kernel call's narrow catch)."""
    with pytest.raises(MemoryError):
        _safe_context(_OomRepr())


class _BaseExcRepr:
    """A ``__repr__`` raising a NON-``Exception`` ``BaseException`` — the
    rest of the global-stop family the ``except Exception`` guard
    deliberately lets through (refuter-round-5 INFO: the pre-round-5
    docstring named only MemoryError)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __repr__(self) -> str:
        raise self._exc


@pytest.mark.parametrize(
    "exc_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
    ids=lambda t: str(t.__name__),
)
def test_safe_context_non_exception_base_exceptions_propagate_bare(
    exc_type: type[BaseException],
) -> None:
    """The FULL global-stop carve-out, docstring-truth pin (refuter round
    5): ``_safe_context`` catches ``Exception`` ONLY, so a byzantine
    ``__repr__`` raising KeyboardInterrupt/SystemExit/GeneratorExit
    propagates BARE exactly like the explicitly re-raised MemoryError — a
    global stop is never swallowed into a log placeholder (correct and
    idiomatic; production-unreachable — the real kernel returns a plain
    int / raises a plain ValueError)."""
    with pytest.raises(exc_type):
        _safe_context(_BaseExcRepr(exc_type()))


def test_safe_context_str_subclass_repr_is_refused() -> None:
    """A ``repr`` returning a str SUBCLASS is refused into the placeholder
    (kills the ``type(text) is str`` exactness-check mutants)."""
    out = _safe_context(_SmuggleRepr())
    assert out == "<_SmuggleRepr repr-failed>"
    assert type(out) is str


def test_safe_context_placeholder_names_type_via_c_level_slot() -> None:
    """Compound hostile shape: a raising ``__repr__`` on an instance whose
    TYPE carries the round-3 hostile metaclass — the placeholder still
    names the REAL type (``_type_name``'s C-level getset, no metaclass
    dispatch; the placeholder arm has no second failure mode)."""

    class _HostileBoth(metaclass=_MetaRaisingName):
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    assert _safe_context(_HostileBoth()) == "<_HostileBoth repr-failed>"


def test_safe_context_huge_int_digit_cap_placeholder() -> None:
    """``int.__repr__`` raising past the digit cap degrades to the typed
    placeholder (the case-A helper-level shape)."""
    limit_before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)  # pin the CPython DEFAULT — deterministic
    try:
        assert _safe_context(10**5000) == "<int repr-failed>"
    finally:
        sys.set_int_max_str_digits(limit_before)


def test_byzantine_huge_int_result_refused_total_on_off_bit_equal() -> None:
    """Round-4 case A verbatim (the round-4 repro script): a byzantine
    kernel returns 10**5000 — domain-invalid, so the refusal fires, and
    pre-fix the refusal's OWN ``result=repr(result)`` raised ValueError
    (digit cap) ON-only where OFF returns a rate. Post-fix: ONE
    kernel_result_refused ERROR carrying the typed placeholder, the oracle
    serves, ON≡OFF bit-equal, the repeat call is memo-silent."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]
    limit_before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(STATS_RUST_ENV, "0")
            off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
            mp.setattr(fno_math, "stats_cpcv_pass_count", lambda *_args: 10**5000)
            mp.setenv(STATS_RUST_ENV, "1")
            reset_self_check_for_tests("cpcv")
            with capture_logs() as logs:
                on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
                on_repeat = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
    finally:
        sys.set_int_max_str_digits(limit_before)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    assert struct.pack("<d", on_repeat) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.kernel_result_refused"]
    assert len(events) == 1  # loud once; the repeat takes the silent memo path
    assert events[0]["result"] == "<int repr-failed>"
    assert events[0]["n_total"] == "6"
    assert kernel_disabled("cpcv") is False
    assert not any(e["event"] == "rust_stats.engaged" for e in logs)


def test_byzantine_kernel_error_huge_int_arg_total_on_off_bit_equal() -> None:
    """Round-4 case B verbatim: the kernel raises ValueError(10**5000) —
    inside the narrow catch, but pre-fix the fallback's OWN
    ``error=repr(exc)`` re-raised the digit-cap ValueError ON-only.
    Post-fix: one kernel_error warn carrying the typed placeholder, the
    oracle serves, ON≡OFF bit-equal, no latch."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]

    def _raise_huge(*_args: object) -> int:
        raise ValueError(10**5000)

    limit_before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(STATS_RUST_ENV, "0")
            off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
            mp.setattr(fno_math, "stats_cpcv_pass_count", _raise_huge)
            mp.setenv(STATS_RUST_ENV, "1")
            reset_self_check_for_tests("cpcv")
            with capture_logs() as logs:
                on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
    finally:
        sys.set_int_max_str_digits(limit_before)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_error"]
    assert events[0]["error"] == "<ValueError repr-failed>"
    assert kernel_disabled("cpcv") is False


def test_byzantine_kernel_error_hostile_repr_total_on_off_bit_equal() -> None:
    """Round-4 case C verbatim: the kernel raises a ValueError SUBCLASS
    whose ``__repr__`` raises RuntimeError — pre-fix the fallback's OWN
    ``repr(exc)`` re-raised it ON-only. Post-fix: the placeholder names the
    real exception type, the oracle serves, ON≡OFF bit-equal, no latch."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]

    def _raise_hostile(*_args: object) -> int:
        raise _HostileReprError("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
        mp.setattr(fno_math, "stats_cpcv_pass_count", _raise_hostile)
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_error"]
    assert events[0]["error"] == "<_HostileReprError repr-failed>"
    assert kernel_disabled("cpcv") is False


def test_valid_pass_count_refuses_int_subclass_without_dispatch() -> None:
    """The exact-TYPE output gate (round-4 completeness sweep): an int
    SUBCLASS result is refused BEFORE any comparison can dispatch its
    foreign ``__ge__``/``__le__`` (pre-fix ``0 <= result`` raised the
    hostile RuntimeError via subclass reflected-operand priority), and a
    benign subclass is refused too (SANCTIONED NARROWING — previously
    adopted; the oracle serves, ON≡OFF either way)."""
    assert _valid_pass_count(_HostileInt(1), 6) is False  # no raise = no dispatch
    assert _valid_pass_count(_PlainIntSub(1), 6) is False


def test_byzantine_int_subclass_result_refused_on_off_bit_equal() -> None:
    """End-to-end: a byzantine kernel returning ``_HostileInt(1)`` — a
    domain-plausible VALUE wearing a hostile TYPE. Pre-fix the validation
    itself raised ON-only from the subclass ``__ge__``; post-fix the
    exact-type gate refuses it with no dispatch (the context degrades to
    the typed placeholder — its ``__repr__`` raises too) and the oracle
    serves ON≡OFF bit-equal."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.025]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
        mp.setattr(fno_math, "stats_cpcv_pass_count", lambda *_args: _HostileInt(1))
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2)
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.kernel_result_refused"]
    assert len(events) == 1
    assert events[0]["result"] == "<_HostileInt repr-failed>"
    assert kernel_disabled("cpcv") is False


def test_min_sharpe_foreign_long_type_name_context_bounded() -> None:
    """A foreign threshold whose TYPE NAME is attacker-sized: the total
    ``_type_name`` lookup still cannot bloat the log — the foreign-type
    context rides ``_bounded_text`` (kills the bound-drop mutant at the
    foreign warn site)."""
    long_named = type("T" * 300, (float,), {})
    returns = [0.01, 0.02, -0.005, 0.03, 0.015, -0.01, 0.02, 0.01]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(STATS_RUST_ENV, "0")
        off = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=long_named(0.5))
        mp.setenv(STATS_RUST_ENV, "1")
        reset_self_check_for_tests("cpcv")
        with capture_logs() as logs:
            on = cpcv_pass_rate(returns, n_groups=4, n_test_groups=2, min_sharpe=long_named(0.5))
    assert struct.pack("<d", on) == struct.pack("<d", off)
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["min_sharpe_foreign_type"]
    assert events[0]["min_sharpe_type"] == "T" * 200 + "<+100 chars truncated>"
    assert kernel_disabled("cpcv") is False


# ── the FFI call site: burn-in + engagement ─────────────────────────────


def test_kernel_happy_path_burn_in_match_and_engaged_once() -> None:
    """The REAL kernel over the golden moments: burn-in compare passes (the
    oracle agrees at 1), one sample is consumed, ``engaged`` logs ONCE."""
    calls: list[int] = []
    before = SELF_CHECK_SAMPLES["cpcv"]
    with capture_logs() as logs:
        assert _call_kernel(oracle_value=1, oracle_calls=calls) == 1
        assert _call_kernel(oracle_value=1, oracle_calls=calls) == 1
    assert len(calls) == 2  # burn-in still pending → oracle ran both times
    assert rust_stats._SELF_CHECK_REMAINING["cpcv"] == before - 2  # pyright: ignore[reportPrivateUsage]
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    assert len(engaged) == 1
    assert engaged[0]["fn"] == "cpcv"
    # Whitebox: the engaged memo key is the documented ``fn:__engaged__``
    # form — namespaced apart from every warn reason.
    assert "cpcv:__engaged__" in rust_stats._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]


def test_burn_in_mismatch_ships_oracle_value_and_latches() -> None:
    """A kernel-vs-oracle mismatch SHIPS THE ORACLE VALUE (correctness
    first), latches the function, and every later call short-circuits."""
    with capture_logs() as logs:
        # The real kernel returns 1; a poisoned oracle stub of 2 simulates a
        # kernel numerical bug from the compare's viewpoint.
        assert _call_kernel(oracle_value=2) == 2
    events = [e for e in logs if e["event"] == "rust_stats.kernel_autoreverted"]
    assert len(events) == 1
    assert events[0]["reason"] == "self_check_mismatch"
    # Round 4: got/want ride ``_safe_context`` at the binding's call site
    # (bounded-total strings — a huge-but-in-domain count can never
    # detonate the render of a raw int context past the digit cap).
    assert events[0]["got"] == "1"
    assert events[0]["want"] == "2"
    assert kernel_disabled("cpcv") is True
    assert _call_kernel(oracle_value=2) is None  # latched — Python serves


def test_steady_state_skips_the_oracle() -> None:
    """Post-burn-in the oracle must NOT run (the amortised-nil law)."""
    reset_self_check_for_tests("cpcv", remaining=0)

    def _forbidden() -> int:
        raise AssertionError("oracle must not run post-burn-in")

    counts, means, m2s = _golden_moments()
    result = cpcv_pass_count_via_kernel(counts, means, m2s, 2, 0.0, n_total=6, oracle=_forbidden)
    assert result == 1


def test_kernel_matches_python_oracle_on_real_inputs() -> None:
    """Seam-level integration: the REAL kernel equals the extracted Python
    fold loop on a spread of deterministic inputs (the property suite widens
    this with hypothesis)."""
    rng = np.random.default_rng(42)
    for n, s, k in [(96, 16, 8), (40, 8, 3), (12, 4, 2), (7, 3, 1)]:
        arr = rng.normal(0.0, 0.01, n)
        count, mean, m2 = _group_moments(arr, _group_bounds(n, s))
        expected = _fold_pass_count(count, mean, m2, s, k, 0.0)
        reset_self_check_for_tests("cpcv", remaining=0)
        got = cpcv_pass_count_via_kernel(
            count, mean, m2, k, 0.0, n_total=math.comb(s, k), oracle=lambda: -1
        )
        assert got == expected


# ── ULP-boundary kernel-drift discriminators (refuter round 5) ───────────
# Refuter-round-5 CONFIRMED finding (refuter 2, MINOR — fix round 6): the
# Python-side ON-arm parity battery was BLIND to ULP-level fold-order kernel
# drift — an algebraically-equal op-order swap in the kernel's mean update
# (`delta * n_b / combined` → `delta / combined * n_b`, cpcv.rs fold_sharpe)
# rebuilt into the wheel passed ALL 52 pre-round-5 Python-side parity/golden
# tests, because every engineered boundary case used power-of-2 group counts
# (multiply/divide by 2^k is exact, so both orders are bit-identical) and a
# random threshold lands within 1 ULP of a fold Sharpe with probability
# ~1e-16. These pins close the gap AT the Python battery: over fixtures with
# NON-power-of-2 group counts whose fold arithmetic is proven rounding-ACTIVE
# in-test (against the op-order witness below), the threshold is
# nextafter-BRACKETED around an achieved fold Sharpe S*, so the integer
# pass-count pair (min_sharpe=S* → that fold is a strict-`>` NON-pass;
# min_sharpe=nextafter(S*, -inf) → it MUST pass) flips on ANY ≥1-ULP drift
# of that fold's kernel Sharpe, in either direction. Fail-against-mutant-
# wheel PROVEN for the op-order swap (M11) and the MIN_SAMPLES 2→1 mutant
# (M10) — the MIGRATION.md #1928 round-5 record carries the transcript.


def _mutant_order_fold_sharpe(
    counts: npt.NDArray[np.intp],
    means: npt.NDArray[np.float64],
    m2s: npt.NDArray[np.float64],
    group_ids: tuple[int, ...],
) -> float | None:
    """The refuter-round-5 op-order WITNESS — the oracle fold with the ONE
    algebraically-equal mean-update reorder that survived the whole
    pre-round-5 Python battery. NOT an oracle: it exists solely to prove a
    fixture's fold arithmetic is rounding-ACTIVE (the two orders round
    differently there), so the nextafter-bracketed threshold pins genuinely
    carry 1-ULP teeth on that fixture — a fixture the witness cannot move
    fails :func:`_first_rounding_active_fold_sharpe` loudly, never pins
    vacuously."""
    n_total = 0
    mean_total = 0.0
    m2_total = 0.0
    for gid in group_ids:
        n_b = int(counts[gid])
        mean_b = float(means[gid])
        m2_b = float(m2s[gid])
        combined = n_total + n_b
        delta = mean_b - mean_total
        mean_total += delta / combined * n_b  # the ONE reorder under test
        m2_total += m2_b + delta * delta * n_total * n_b / combined
        n_total = combined
    if n_total < 2:
        return None
    variance = m2_total / n_total
    if not math.isfinite(variance) or variance <= 0.0:
        return None
    return mean_total / math.sqrt(variance)


def _first_rounding_active_fold_sharpe(
    counts: npt.NDArray[np.intp],
    means: npt.NDArray[np.float64],
    m2s: npt.NDArray[np.float64],
    k: int,
) -> float:
    """The first lexicographic fold whose Sharpe the op-order witness MOVES.

    The fixture-sensitivity gate: raises when the fixture has no
    rounding-active fold (power-of-2 counts / order-insensitive moments), so
    a future fixture edit can never silently defang the ULP pins."""
    for combo in combinations(range(len(counts)), k):
        s_star = _combine_moments(counts, means, m2s, combo)
        if s_star is None:
            continue
        if _mutant_order_fold_sharpe(counts, means, m2s, combo) != s_star:
            return s_star
    raise AssertionError("fixture is not rounding-active — the ULP pin would be vacuous")


#: (counts, means, m2s, k) across (S, k) shapes AND magnitude regimes; every
#: counts vector is non-power-of-2 (the class the pre-round-5 battery never
#: exercised at a bracketed threshold).
_ULP_FIXTURES = [
    pytest.param([7, 4, 9], [0.1, 0.3, -0.11], [0.5, 0.25, 1.3], 2, id="s3_k2_mag_e-1"),
    pytest.param(
        [3, 5, 7, 11, 13],
        [1.1e-9, -2.3e-9, 3.1e-9, -0.7e-9, 1.9e-9],
        [1e-19, 3e-19, 7e-19, 2e-19, 5e-19],
        3,
        id="s5_k3_mag_e-9",
    ),
    pytest.param(
        [9, 7, 5, 3, 11, 13],
        [1.1e6, -0.3e6, 0.7e6, 1.9e6, -1.3e6, 0.9e6],
        [1e13, 3e13, 7e13, 2e13, 5e13, 4e13],
        4,
        id="s6_k4_mag_e6",
    ),
]


@pytest.mark.parametrize(("counts", "means", "m2s", "k"), _ULP_FIXTURES)
def test_ulp_bracketed_thresholds_discriminate_kernel_fold_drift(
    counts: list[int], means: list[float], m2s: list[float], k: int
) -> None:
    """The ULP-boundary discriminator (refuter-round-5 F1 closed): with the
    threshold AT an achieved fold Sharpe S* the fold is a strict-``>``
    NON-pass, and at ``nextafter(S*, -inf)`` it MUST pass — the kernel's
    integer counts must equal the oracle's EXACTLY at both thresholds
    (steady state, so the kernel's RAW answer is compared, never the
    burn-in-shipped oracle value). Any kernel whose fold arithmetic drifts
    by even 1 ULP on the bracketed fold flips one of the two counts."""
    counts_a = np.array(counts, dtype=np.intp)
    means_a = np.array(means, dtype=np.float64)
    m2s_a = np.array(m2s, dtype=np.float64)
    s = len(counts)
    s_star = _first_rounding_active_fold_sharpe(counts_a, means_a, m2s_a, k)
    below = math.nextafter(s_star, -math.inf)
    n_total = math.comb(s, k)
    oracle_at = _fold_pass_count(counts_a, means_a, m2s_a, s, k, s_star)
    oracle_below = _fold_pass_count(counts_a, means_a, m2s_a, s, k, below)
    assert oracle_below - oracle_at >= 1  # the bracket sits AT an achieved fold Sharpe

    def _forbidden() -> int:
        raise AssertionError("steady state must not run the burn-in oracle")

    reset_self_check_for_tests("cpcv", remaining=0)  # steady state: the kernel's RAW answer
    with capture_logs() as logs:
        got_at = cpcv_pass_count_via_kernel(
            counts_a, means_a, m2s_a, k, s_star, n_total=n_total, oracle=_forbidden
        )
        got_below = cpcv_pass_count_via_kernel(
            counts_a, means_a, m2s_a, k, below, n_total=n_total, oracle=_forbidden
        )
    assert got_at == oracle_at
    assert got_below == oracle_below
    assert any(e["event"] == "rust_stats.engaged" for e in logs)  # never vacuous
    assert not any(e["event"] == "rust_stats.fallback" for e in logs)
    assert kernel_disabled("cpcv") is False


def test_kernel_min_samples_law_on_count1_groups_with_positive_m2() -> None:
    """The MIN_SAMPLES law through the PYTHON battery (kills the kernel's
    ``MIN_SAMPLES 2→1`` mutant — refuter-round-5 M10): k=1 folds over
    count-1 groups have ``n_total = 1 < _MIN_SAMPLES`` — undefined, a
    NON-pass on BOTH engines even with positive M2. The moment shape is
    deliberately UNREACHABLE from ``_group_moments`` (a count-1 group
    always carries M2 == 0.0 exactly there, where variance ≤ 0 makes the
    fold a non-pass under either constant) — this drives the seam's public
    FFI surface directly, the same driver-level fixture the in-crate cargo
    kill uses; under the mutant every fold computes variance = 1.0 > 0 and
    Sharpe 5/6/7 > 0 → count 3, not 0."""
    counts = np.array([1, 1, 1], dtype=np.intp)
    means = np.array([5.0, 6.0, 7.0], dtype=np.float64)
    m2s = np.array([1.0, 1.0, 1.0], dtype=np.float64)
    assert _fold_pass_count(counts, means, m2s, 3, 1, 0.0) == 0  # the oracle law

    def _forbidden() -> int:
        raise AssertionError("steady state must not run the burn-in oracle")

    reset_self_check_for_tests("cpcv", remaining=0)
    with capture_logs() as logs:
        got = cpcv_pass_count_via_kernel(counts, means, m2s, 1, 0.0, n_total=3, oracle=_forbidden)
    assert got == 0
    assert any(e["event"] == "rust_stats.engaged" for e in logs)  # never vacuous
    assert not any(e["event"] == "rust_stats.fallback" for e in logs)


# ── the round-4 STRUCTURAL pin: log-context totality by construction ─────


class TestLogContextStructuralPin:
    """AST law ending the rounds-2→4 whack-a-mole (input conversion → input
    contexts → output contexts): NO value the seam does not control can
    reach a log context except through the sanctioned total helpers.

    Parsed from the module SOURCE, so a future edit cannot silently re-open
    the class: every argument of a ``_LOG.*`` / ``warn_fallback_once`` /
    ``disable_kernel`` call must be a constant, an allowed routing name, or
    a call to a sanctioned total helper — a bare ``repr(...)`` /
    ``str(...)`` / f-string context fails this pin at the exact line — and
    the ``repr``/``str`` builtins may appear ONLY inside the sanctioned
    helper bodies (where they are guarded + bounded). Round-5 extension
    (refuter-1 INFO closed): the pin RECURSES into a sanctioned helper's
    own ARGUMENTS — an f-string / bare stringification NESTED inside a
    helper call (``_safe_context(f"{x}")``) is evaluated BEFORE the helper
    runs and could raise ON-only, so helper args must themselves be total
    by construction (constants, plain names, or nested total-helper calls,
    recursively).
    """

    _SANCTIONED_HELPERS = frozenset(
        {"_safe_context", "_inexact_threshold_context", "_bounded_text"}
    )
    #: Calls allowed INSIDE a sanctioned helper's arguments: the helpers
    #: themselves plus the C-level-slot ``_type_name`` (total by design).
    _TOTAL_ARG_CALLS = _SANCTIONED_HELPERS | frozenset({"_type_name"})
    _REPR_OWNERS = frozenset({"_safe_context", "_inexact_threshold_context"})
    # "arg" (PR-2, DEC-METRICS-RUST-STATS-002): the seam-controlled
    # argument-name routing string the shared input gates thread — every
    # call site passes a str CONSTANT into the ``arg`` parameter, so the
    # name load runs no foreign code (same class as fn/reason/attr).
    _ALLOWED_CONTEXT_NAMES = frozenset({"fn", "reason", "attr", "arg"})
    _CHOKEPOINTS = frozenset({"warn_fallback_once", "disable_kernel"})

    @classmethod
    def _total_helper_arg_ok(cls, node: ast.expr) -> bool:
        """A sanctioned helper's ARGUMENT must itself be total by
        construction: a constant, a plain name (a local/param load runs no
        foreign code), or a nested call to a total helper whose own
        arguments satisfy the same rule RECURSIVELY — an f-string, a bare
        ``repr``/``str``, or a ``%``/``.format``/``.join`` expression
        nested inside a sanctioned call is evaluated before the helper
        runs and could raise ON-only (the round-5 future-edit gap)."""
        if isinstance(node, ast.Constant | ast.Name):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in cls._TOTAL_ARG_CALLS
            and not node.keywords
        ):
            return all(cls._total_helper_arg_ok(arg) for arg in node.args)
        return False

    @classmethod
    def _context_value_ok(cls, value: ast.expr) -> bool:
        """The TOP-level context-value law: a constant, an allowed routing
        name, or a sanctioned-total-helper call whose arguments recurse
        through :meth:`_total_helper_arg_ok`."""
        if isinstance(value, ast.Constant):
            return True
        if isinstance(value, ast.Name):
            return value.id in cls._ALLOWED_CONTEXT_NAMES
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in cls._SANCTIONED_HELPERS
            and not value.keywords
        ):
            return all(cls._total_helper_arg_ok(arg) for arg in value.args)
        return False

    @classmethod
    def _calls_with_owner(cls) -> list[tuple[str | None, ast.Call]]:
        """Every Call node in the module, tagged with its enclosing
        top-level function name (``None`` = module level)."""
        module_file = rust_stats.__file__
        assert module_file is not None
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        owned: list[tuple[str | None, ast.Call]] = []
        in_function: set[int] = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        owned.append((node.name, sub))
                        in_function.add(id(sub))
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Call) and id(sub) not in in_function:
                owned.append((None, sub))
        return owned

    @staticmethod
    def _is_context_call(call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id == "_LOG"
        return isinstance(func, ast.Name) and func.id in {"warn_fallback_once", "disable_kernel"}

    def test_every_log_context_value_is_total_by_construction(self) -> None:
        context_calls = [(o, c) for o, c in self._calls_with_owner() if self._is_context_call(c)]
        assert len(context_calls) >= 12  # anti-vacuity: the real call census (37 post-PR-2)
        helper_call_values = 0
        for owner, call in context_calls:
            where = f"{owner or '<module>'}:{call.lineno}"
            for arg in call.args:
                assert isinstance(arg, ast.Constant | ast.Name), (
                    f"{where}: positional log/warn args must be constants or plain names"
                )
            for kw in call.keywords:
                if kw.arg is None:  # **context forwarding
                    assert owner in self._CHOKEPOINTS, (
                        f"{where}: **context forwarding is allowed only inside the chokepoints"
                    )
                    continue
                value = kw.value
                if isinstance(value, ast.Call):
                    helper_call_values += 1
                assert self._context_value_ok(value), (
                    f"{where}: context key {kw.arg!r} must be a constant, an allowed routing "
                    "name, or a sanctioned total-helper call whose arguments are themselves "
                    "total (constants / plain names / nested total-helper calls) — a bare "
                    "repr()/str()/f-string context, TOP-LEVEL or NESTED inside a helper arg, "
                    "re-opens the refuter-round-2..5 detonation class"
                )
        # Anti-vacuity for the round-5 recursion: the module's real census
        # carries 10 helper-call context values whose ARGUMENTS the recursive
        # rule genuinely walked (incl. the nested _bounded_text(_type_name(…))).
        assert helper_call_values >= 8

    def test_repr_and_str_builtins_only_inside_sanctioned_helpers(self) -> None:
        guarded_reprs = 0
        for owner, call in self._calls_with_owner():
            func = call.func
            if isinstance(func, ast.Name) and func.id in {"repr", "str"}:
                assert owner in self._REPR_OWNERS, (
                    f"{owner or '<module>'}:{call.lineno}: bare {func.id}() outside the "
                    "sanctioned total helpers"
                )
                guarded_reprs += 1
        assert guarded_reprs >= 2  # anti-vacuity: both helpers really guard a repr

    @staticmethod
    def _parsed_context_value(snippet: str) -> ast.expr:
        """The first keyword-argument VALUE of a parsed chokepoint call."""
        call = ast.parse(snippet, mode="eval").body
        assert isinstance(call, ast.Call)
        return call.keywords[0].value

    @pytest.mark.parametrize(
        "snippet",
        [
            'warn_fallback_once("cpcv", "r", v=_safe_context(f"{x}"))',
            'warn_fallback_once("cpcv", "r", v=_safe_context(repr(x)))',
            'disable_kernel("cpcv", "r", got=_safe_context(str(x)))',
            'warn_fallback_once("cpcv", "r", v=_safe_context("%s" % x))',
            'warn_fallback_once("cpcv", "r", v=_safe_context("{}".format(x)))',
            'warn_fallback_once("cpcv", "r", v=_bounded_text("".join(parts)))',
            'warn_fallback_once("cpcv", "r", v=_bounded_text(_type_name(f"{x}")))',
            'warn_fallback_once("cpcv", "r", v=_safe_context(x, key=f"{x}"))',
        ],
        ids=[
            "fstring_arg",
            "repr_arg",
            "str_arg",
            "percent_format_arg",
            "dot_format_arg",
            "join_arg",
            "fstring_inside_nested_type_name",
            "keyword_smuggle",
        ],
    )
    def test_pin_rejects_stringification_nested_in_helper_args(self, snippet: str) -> None:
        """Round-5 teeth (refuter-1 INFO closed): an ON-only-raising
        expression NESTED inside a sanctioned helper's argument — evaluated
        BEFORE the helper's totality guard can run — is rejected by the SAME
        validator the module pin executes, so the documented future-edit gap
        (``_safe_context(f"{x}")`` previously passed both pins) is closed."""
        assert self._context_value_ok(self._parsed_context_value(snippet)) is False

    @pytest.mark.parametrize(
        "snippet",
        [
            'warn_fallback_once("cpcv", "r", error=_safe_context(exc))',
            'warn_fallback_once("cpcv", "r", value=_safe_context(raw))',
            'warn_fallback_once("cpcv", "r", ms=_inexact_threshold_context(min_sharpe))',
            'warn_fallback_once("cpcv", "r", t=_bounded_text(_type_name(ms_type)))',
            'warn_fallback_once("cpcv", "r", lit=_safe_context("literal"))',
        ],
        ids=["name_arg", "raw_name_arg", "inexact_helper", "nested_type_name", "constant_arg"],
    )
    def test_pin_accepts_the_sanctioned_module_shapes(self, snippet: str) -> None:
        """No false positive: the whitelist admits exactly the module's real
        shapes (plain names, constants, the nested C-level ``_type_name``) —
        and the module-census pin above proves the live call sites still
        pass under the recursive rule."""
        assert self._context_value_ok(self._parsed_context_value(snippet)) is True


# ── mutation-workflow wiring pin ────────────────────────────────────────


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The DEC-LAKE-STATS-MANIFEST-001 round-8 lesson: an unwired mutation gate
    presented as a deferred one is a Golden-Rule-#10 defect. The
    module:suite pair sits in the workflow's per-module ``mutmut`` loop
    (3-field ``module:testfile:timeout`` format — the EMPTY third field
    keeps the exact historical runner); this deterministic pin fails if the
    pair is ever dropped. HONEST: it pins the WIRING only — the weekly run
    executes on its own schedule/push triggers.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/metrics/rust_stats.py:tests/unit/test_rust_stats.py:"' in text, (
            "rust_stats.py is not wired into mutation.yml's per-module mutmut loop"
        )


# ═════════════════════════════════════════════════════════════════════════
# DEC-METRICS-RUST-STATS-002 (slice 6, PR-2) — the stats-tail bindings.
# Every arm of psr / sr_max / pbo / wfo + the shared PR-2 gate helpers,
# for the strict 100% line+branch gate driven by THIS file alone.
# ═════════════════════════════════════════════════════════════════════════

from decimal import Decimal as _Dec  # noqa: E402  (appended PR-2 section)

from brutex.metrics.deflated_sharpe import (  # noqa: E402
    _expected_max_tail,  # pyright: ignore[reportPrivateUsage]
    _psr_tail,  # pyright: ignore[reportPrivateUsage]
)
from brutex.metrics.rust_stats import (  # noqa: E402
    _count_as_i64,  # pyright: ignore[reportPrivateUsage]
    _f64_bits,  # pyright: ignore[reportPrivateUsage]
    _flat_exact_floats,  # pyright: ignore[reportPrivateUsage]
    _numeric_as_f64,  # pyright: ignore[reportPrivateUsage]
    _valid_float_result,  # pyright: ignore[reportPrivateUsage]
    _valid_pbo_result,  # pyright: ignore[reportPrivateUsage]
    expected_max_sharpe_via_kernel,
    pbo_cscv_via_kernel,
    psr_via_kernel,
    wfo_pass_count_via_kernel,
)

# The fixed PSR fixture's radicand (finite, > 0) — the fixture args
# (0.15, 0.0, 250, -0.6, 5.5) live inline as ``_call_psr``'s defaults.
_PSR_RADICAND = 1.0 - (-0.6) * 0.15 + (5.5 - 1.0) / 4.0 * 0.15 * 0.15


def _psr_true() -> float:
    return _psr_tail(0.15, 0.0, 250, _PSR_RADICAND)


def _call_psr(
    *,
    oracle_value: float | None = None,
    oracle_calls: list[int] | None = None,
    observed: float = 0.15,
    benchmark: float = 0.0,
    n: int = 250,
    skew: float = -0.6,
    kurt: float = 5.5,
) -> float | None:
    value = _psr_true() if oracle_value is None else oracle_value

    def _oracle() -> float:
        if oracle_calls is not None:
            oracle_calls.append(1)
        return value

    return psr_via_kernel(observed, benchmark, n, skew, kurt, oracle=_oracle)


def _call_sr_max(
    *,
    oracle_value: float | None = None,
    oracle_calls: list[int] | None = None,
    n_trials: int = 1000,
    variance: float = 0.04,
) -> float | None:
    value = _expected_max_tail(1000, 0.04) if oracle_value is None else oracle_value

    def _oracle() -> float:
        if oracle_calls is not None:
            oracle_calls.append(1)
        return value

    return expected_max_sharpe_via_kernel(n_trials, variance, oracle=_oracle)


# The fixed PBO fixture: S=2 blocks × N=2 strategies, is_count=1 →
# C(2, 1) = 2 splits, both logit 0.0 → overfit 2 (pinned by the Rust
# in-crate `known_two_split_case` twin).
_PBO_SUM = [[1.0, 0.5], [-1.0, 0.5]]
_PBO_SUMSQ = [[1.0, 0.25], [1.0, 0.25]]
_PBO_TRUE = (2, [0.0, 0.0])


def _call_pbo(
    *,
    oracle_value: tuple[int, list[float]] | None = None,
    oracle_calls: list[int] | None = None,
    n_total: int = 2,
    evaluator_is_default: bool = True,
    block_sum: list[list[float]] | None = None,
    n_splits: int = 2,
    n_strat: int = 2,
    is_count: int = 1,
) -> tuple[int, list[float]] | None:
    value = _PBO_TRUE if oracle_value is None else oracle_value

    def _oracle() -> tuple[int, list[float]]:
        if oracle_calls is not None:
            oracle_calls.append(1)
        return (value[0], list(value[1]))

    return pbo_cscv_via_kernel(
        _PBO_SUM if block_sum is None else block_sum,
        _PBO_SUMSQ,
        n_splits=n_splits,
        n_strat=n_strat,
        is_count=is_count,
        n_total=n_total,
        evaluator_is_default=evaluator_is_default,
        oracle=_oracle,
    )


# The fixed WFO fixture: one walk over [1, 2, 3, 4] → sharpe > 0 → 1 pass.
_WFO_RETURNS = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float64)


def _call_wfo(
    *,
    oracle_value: int = 1,
    oracle_calls: list[int] | None = None,
    min_sharpe: float = 0.0,
    n_walks: int = 1,
) -> int | None:
    def _oracle() -> int:
        if oracle_calls is not None:
            oracle_calls.append(1)
        return oracle_value

    return wfo_pass_count_via_kernel(
        _WFO_RETURNS, [0], [4], min_sharpe, n_walks=n_walks, oracle=_oracle
    )


# ── the shared PR-2 gate helpers ────────────────────────────────────────


def test_f64_bits_distinguishes_signed_zero_and_nan_payloads() -> None:
    assert _f64_bits(0.0) != _f64_bits(-0.0)
    assert _f64_bits(float("nan")) == _f64_bits(float("nan"))
    assert _f64_bits(1.0) == struct.pack("<d", 1.0)


def test_numeric_as_f64_exact_float_engages() -> None:
    assert _numeric_as_f64("psr", 1.5, arg="x") == 1.5


def test_numeric_as_f64_nan_refuses_with_one_warning() -> None:
    with capture_logs() as logs:
        assert _numeric_as_f64("psr", float("nan"), arg="x") is None
        assert _numeric_as_f64("psr", float("nan"), arg="x") is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_inexact"]
    assert events[0]["arg"] == "x"


@pytest.mark.parametrize("exact_int", [0, 1, -7, 2**53, -(2**53)])
def test_numeric_as_f64_value_exact_int_engages(exact_int: int) -> None:
    got = _numeric_as_f64("psr", exact_int, arg="x")
    assert got == float(exact_int)
    assert type(got) is float


def test_numeric_as_f64_bool_engages_as_exact_int() -> None:
    assert _numeric_as_f64("psr", True, arg="x") == 1.0
    assert _numeric_as_f64("psr", False, arg="x") == 0.0


@pytest.mark.parametrize("bad_int", [2**53 + 1, 10**400])
def test_numeric_as_f64_inexact_or_overflowing_int_refuses(bad_int: int) -> None:
    with capture_logs() as logs:
        assert _numeric_as_f64("psr", bad_int, arg="x") is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_inexact"]


@pytest.mark.parametrize("foreign", [_Dec("1.5"), "1.5", None, np.float64(1.5)])
def test_numeric_as_f64_foreign_type_refuses(foreign: object) -> None:
    with capture_logs() as logs:
        assert _numeric_as_f64("psr", foreign, arg="x") is None  # type: ignore[arg-type]
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_foreign_type"]
    assert "value_type" in events[0]


def test_count_as_i64_exact_int_and_bool() -> None:
    assert _count_as_i64("psr", 5, arg="n", minimum=2) == 5
    assert _count_as_i64("psr", 2**63 - 1, arg="n", minimum=2) == 2**63 - 1
    assert _count_as_i64("wfo", True, arg="n", minimum=1) == 1


@pytest.mark.parametrize("bad", [1, 0, -5, 2**63])
def test_count_as_i64_out_of_range_refuses(bad: int) -> None:
    with capture_logs() as logs:
        assert _count_as_i64("psr", bad, arg="n", minimum=2) is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_unsupported"]


@pytest.mark.parametrize("foreign", [2.0, _Dec(2), "2", None])
def test_count_as_i64_foreign_type_refuses(foreign: object) -> None:
    with capture_logs() as logs:
        assert _count_as_i64("psr", foreign, arg="n", minimum=2) is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_foreign_type"]


def test_flat_exact_floats_happy_row_major() -> None:
    assert _flat_exact_floats("pbo", [[1.0, 2.0], [3.0, 4.0]], arg="m") == [1.0, 2.0, 3.0, 4.0]


class _SubFloat(float):
    """A float subclass — the reflected-op poisoning class the gate refuses."""


@pytest.mark.parametrize("bad", [1, _SubFloat(1.0), "1.0", None])
def test_flat_exact_floats_foreign_entry_refuses(bad: object) -> None:
    with capture_logs() as logs:
        assert _flat_exact_floats("pbo", [[1.0, bad]], arg="m") is None  # type: ignore[list-item]
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["arg_foreign_type"]


def test_valid_float_result_exact_type_only() -> None:
    assert _valid_float_result(1.5) is True
    assert _valid_float_result(float("nan")) is True  # NaN flows (SR_0 law)
    assert _valid_float_result(_SubFloat(1.5)) is False
    assert _valid_float_result(1) is False
    assert _valid_float_result("1.5") is False


@pytest.mark.parametrize(
    "bad",
    [
        "x",  # not a tuple
        (1,),  # wrong arity
        (1, [0.0], 2),  # wrong arity
        [1, [0.0]],  # list, not tuple
        (True, [0.0]),  # bool count (exact-int law)
        (-1, [0.0]),  # count below 0
        (2, [0.0]),  # count beyond n_total (n_total=1)
        (1.0, [0.0]),  # float count
        (1, (0.0,)),  # tuple logits, not list
        (1, [0.0, 0.0]),  # wrong logits length
        (1, [1]),  # non-float logit
        (0, [_SubFloat(0.0)]),  # float-subclass logit
    ],
)
def test_valid_pbo_result_rejects_non_domain(bad: object) -> None:
    assert _valid_pbo_result(bad, 1) is False


def test_valid_pbo_result_accepts_domain() -> None:
    assert _valid_pbo_result((1, [0.5]), 1) is True
    assert _valid_pbo_result((0, [0.5, -0.5]), 2) is True


# ── psr binding ─────────────────────────────────────────────────────────


def test_psr_happy_path_burn_in_match_and_engaged_once() -> None:
    calls: list[int] = []
    with capture_logs() as logs:
        got = _call_psr(oracle_calls=calls)
        again = _call_psr(oracle_calls=calls)
    assert got is not None and again is not None
    assert _f64_bits(got) == _f64_bits(_psr_true())
    assert len(calls) == 2  # burn-in ran the oracle on both calls
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    assert len(engaged) == 1 and engaged[0]["fn"] == "psr"


def test_psr_burn_in_mismatch_ships_oracle_value_and_latches() -> None:
    with capture_logs() as logs:
        got = _call_psr(oracle_value=0.123456)
    assert got == 0.123456  # the ORACLE value shipped, never the kernel's
    assert kernel_disabled("psr") is True
    events = [e for e in logs if e["event"] == "rust_stats.kernel_autoreverted"]
    assert len(events) == 1 and events[0]["fn"] == "psr"
    # Later calls short-circuit with one warn.
    with capture_logs() as logs:
        assert _call_psr() is None
    fallbacks = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in fallbacks] == ["kernel_disabled"]


def test_psr_steady_state_skips_the_oracle() -> None:
    reset_self_check_for_tests("psr", remaining=0)
    calls: list[int] = []
    got = _call_psr(oracle_calls=calls)
    assert got is not None and _f64_bits(got) == _f64_bits(_psr_true())
    assert calls == []  # no oracle run post-burn-in


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"observed": float("nan")}, "arg_inexact"),  # NaN float
        ({"benchmark": 10**400}, "arg_inexact"),  # int past float range
        ({"skew": _Dec("0.5")}, "arg_foreign_type"),
        ({"kurt": 2**53 + 1}, "arg_inexact"),  # inexact int
        ({"n": 2.0}, "arg_foreign_type"),  # float-typed count
        ({"n": 1}, "arg_unsupported"),  # below minimum
    ],
)
def test_psr_gate_refusals_run_before_burn_in(kwargs: dict[str, object], reason: str) -> None:
    """Every gate refusal is pre-burn-in: no sample consumed, no latch."""
    with capture_logs() as logs:
        assert _call_psr(**kwargs) is None  # type: ignore[arg-type]
    assert kernel_disabled("psr") is False
    assert rust_stats._SELF_CHECK_REMAINING["psr"] == SELF_CHECK_SAMPLES["psr"]  # pyright: ignore[reportPrivateUsage]
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == [reason]


def test_psr_exact_int_args_engage_bit_equal() -> None:
    """Value-exact int arguments engage and match the oracle's mixed-type
    arithmetic bitwise (int→f64 conversion is correctly rounded on both)."""
    radicand = 1.0 - 0 * 0.15 + (3 - 1.0) / 4.0 * 0.15 * 0.15
    true_value = _psr_tail(0.15, 0.0, 250, radicand)
    got = psr_via_kernel(0.15, 0, 250, 0, 3, oracle=lambda: true_value)
    assert got is not None and _f64_bits(got) == _f64_bits(true_value)


def test_psr_wheel_missing_and_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fno_math", None)
    with capture_logs() as logs:
        assert _call_psr() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["wheel_missing"]
    monkeypatch.undo()
    reset_observability_memo_for_tests()
    monkeypatch.delattr(fno_math, "stats_psr")
    with capture_logs() as logs:
        assert _call_psr() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["wheel_stale"]
    assert events[0]["attr"] == "stats_psr"


def test_psr_kernel_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object) -> float:
        raise ValueError("boom")

    monkeypatch.setattr(fno_math, "stats_psr", _raise)
    with capture_logs() as logs:
        assert _call_psr() is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_error"]


def test_psr_memory_error_propagates_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    def _oom(*args: object) -> float:
        raise MemoryError

    monkeypatch.setattr(fno_math, "stats_psr", _oom)
    with pytest.raises(MemoryError):
        _call_psr()


@pytest.mark.parametrize("poison", ["0.5", 1, _SubFloat(0.5), None])
def test_psr_poisoned_result_refused_loudly_once(
    monkeypatch: pytest.MonkeyPatch, poison: object
) -> None:
    monkeypatch.setattr(fno_math, "stats_psr", lambda *a: poison)
    with capture_logs() as logs:
        assert _call_psr() is None
        assert _call_psr() is None
    events = [e for e in logs if e["event"] == "rust_stats.kernel_result_refused"]
    assert len(events) == 1 and events[0]["fn"] == "psr"


# ── sr_max binding ──────────────────────────────────────────────────────


def test_sr_max_happy_path_burn_in_match_and_engaged_once() -> None:
    calls: list[int] = []
    with capture_logs() as logs:
        got = _call_sr_max(oracle_calls=calls)
    assert got is not None
    assert _f64_bits(got) == _f64_bits(_expected_max_tail(1000, 0.04))
    assert len(calls) == 1
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    assert len(engaged) == 1 and engaged[0]["fn"] == "sr_max"


def test_sr_max_burn_in_mismatch_ships_oracle_value_and_latches() -> None:
    got = _call_sr_max(oracle_value=42.0)
    assert got == 42.0
    assert kernel_disabled("sr_max") is True
    assert kernel_disabled("psr") is False  # per-function latch
    with capture_logs() as logs:
        assert _call_sr_max() is None  # the latch short-circuits
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_disabled"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"n_trials": 1}, "arg_unsupported"),  # below the dispatch minimum
        ({"n_trials": 2**63}, "arg_unsupported"),  # past i64
        ({"n_trials": 2.0}, "arg_foreign_type"),
        ({"variance": float("nan")}, "arg_inexact"),  # conservative NaN refusal
        ({"variance": _Dec("0.5")}, "arg_foreign_type"),
    ],
)
def test_sr_max_gate_refusals(kwargs: dict[str, object], reason: str) -> None:
    with capture_logs() as logs:
        assert _call_sr_max(**kwargs) is None  # type: ignore[arg-type]
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == [reason]


def test_sr_max_astronomical_n_trials_kernel_error_refuses_to_oracle() -> None:
    """The AS241 raise domain (1 - 1/N rounds to 1.0): the kernel raises
    ValueError → warn-once → None, so the caller's oracle raises the
    identical StatisticsError — exception parity by construction."""
    with capture_logs() as logs:
        got = _call_sr_max(n_trials=2**62)
    assert got is None
    events = [e for e in logs if e["event"] == "rust_stats.fallback"]
    assert [e["reason"] for e in events] == ["kernel_error"]


def test_sr_max_wheel_stale_and_poisoned_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(fno_math, "stats_expected_max_sharpe")
    with capture_logs() as logs:
        assert _call_sr_max() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["wheel_stale"]
    monkeypatch.undo()
    reset_observability_memo_for_tests()
    monkeypatch.setattr(fno_math, "stats_expected_max_sharpe", lambda *a: "nope")
    with capture_logs() as logs:
        assert _call_sr_max() is None
    assert [e["event"] for e in logs if "refused" in e["event"]] == [
        "rust_stats.kernel_result_refused"
    ]


def test_sr_max_kernel_error_and_memory_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object) -> float:
        raise OverflowError("boom")

    monkeypatch.setattr(fno_math, "stats_expected_max_sharpe", _raise)
    with capture_logs() as logs:
        assert _call_sr_max() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_error"]

    def _oom(*args: object) -> float:
        raise MemoryError

    monkeypatch.setattr(fno_math, "stats_expected_max_sharpe", _oom)
    with pytest.raises(MemoryError):
        _call_sr_max()


def test_sr_max_steady_state_skips_the_oracle() -> None:
    reset_self_check_for_tests("sr_max", remaining=0)
    calls: list[int] = []
    got = _call_sr_max(oracle_calls=calls)
    assert got is not None and calls == []


# ── pbo binding ─────────────────────────────────────────────────────────


def test_pbo_happy_path_burn_in_match_and_engaged_once() -> None:
    calls: list[int] = []
    with capture_logs() as logs:
        got = _call_pbo(oracle_calls=calls)
    assert got is not None
    count, logits = got
    assert count == 2 and [_f64_bits(v) for v in logits] == [_f64_bits(0.0)] * 2
    assert len(calls) == 1
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    assert len(engaged) == 1 and engaged[0]["fn"] == "pbo"


def test_pbo_burn_in_count_mismatch_ships_oracle_and_latches() -> None:
    got = _call_pbo(oracle_value=(1, [0.0, 0.0]))
    assert got == (1, [0.0, 0.0])  # the oracle tuple shipped verbatim
    assert kernel_disabled("pbo") is True


def test_pbo_burn_in_logit_bit_mismatch_latches() -> None:
    got = _call_pbo(oracle_value=(2, [0.0, -0.0]))  # -0.0 ≠ 0.0 bitwise
    assert got == (2, [0.0, -0.0])
    assert kernel_disabled("pbo") is True


def test_pbo_burn_in_oracle_length_mismatch_latches() -> None:
    """A wrong-length oracle (a seam bug, never healthy) latches instead of
    raising ON-only out of the strict zip."""
    got = _call_pbo(oracle_value=(2, [0.0]))
    assert got == (2, [0.0])
    assert kernel_disabled("pbo") is True


def test_pbo_custom_evaluator_and_cap_refuse() -> None:
    with capture_logs() as logs:
        assert _call_pbo(evaluator_is_default=False) is None
        assert _call_pbo(n_total=2_000_001) is None
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == ["custom_evaluator", "combination_cap"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"n_splits": 1}, "arg_unsupported"),  # below minimum
        ({"n_strat": 2.0}, "arg_foreign_type"),
        ({"is_count": 0}, "arg_unsupported"),  # below minimum
        ({"block_sum": [[1.0, _Dec(2)]]}, "arg_foreign_type"),
    ],
)
def test_pbo_gate_refusals(kwargs: dict[str, object], reason: str) -> None:
    with capture_logs() as logs:
        assert _call_pbo(**kwargs) is None  # type: ignore[arg-type]
    reasons = [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"]
    assert reasons == [reason]


def test_pbo_sumsq_foreign_entry_refuses() -> None:
    """The SECOND matrix rides the same exact-float scan."""
    got = pbo_cscv_via_kernel(
        [[1.0, 2.0]],
        [[1.0, "x"]],  # type: ignore[list-item]
        n_splits=2,
        n_strat=2,
        is_count=1,
        n_total=2,
        evaluator_is_default=True,
        oracle=lambda: (0, [0.0, 0.0]),
    )
    assert got is None


def test_pbo_latched_short_circuits() -> None:
    disable_kernel("pbo", "self_check_mismatch")
    with capture_logs() as logs:
        assert _call_pbo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_disabled"]


def test_pbo_wheel_stale_kernel_error_poisoned_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(fno_math, "stats_pbo_cscv")
    with capture_logs() as logs:
        assert _call_pbo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["wheel_stale"]
    monkeypatch.undo()
    reset_observability_memo_for_tests()

    def _raise(*args: object) -> tuple[int, list[float]]:
        raise TypeError("boom")

    monkeypatch.setattr(fno_math, "stats_pbo_cscv", _raise)
    with capture_logs() as logs:
        assert _call_pbo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_error"]
    monkeypatch.setattr(fno_math, "stats_pbo_cscv", lambda *a: (3, [0.0, 0.0]))
    with capture_logs() as logs:
        assert _call_pbo() is None  # count beyond n_total refused
    assert [e["event"] for e in logs if "refused" in e["event"]] == [
        "rust_stats.kernel_result_refused"
    ]

    def _oom(*args: object) -> tuple[int, list[float]]:
        raise MemoryError

    monkeypatch.setattr(fno_math, "stats_pbo_cscv", _oom)
    with pytest.raises(MemoryError):
        _call_pbo()


def test_pbo_steady_state_skips_the_oracle() -> None:
    reset_self_check_for_tests("pbo", remaining=0)
    calls: list[int] = []
    got = _call_pbo(oracle_calls=calls)
    assert got == (2, [0.0, 0.0]) and calls == []


# ── wfo binding ─────────────────────────────────────────────────────────


def test_wfo_happy_path_burn_in_match_and_engaged_once() -> None:
    calls: list[int] = []
    with capture_logs() as logs:
        got = _call_wfo(oracle_calls=calls)
    assert got == 1
    assert len(calls) == 1
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    assert len(engaged) == 1 and engaged[0]["fn"] == "wfo"


def test_wfo_burn_in_mismatch_ships_oracle_and_latches() -> None:
    got = _call_wfo(oracle_value=0)
    assert got == 0
    assert kernel_disabled("wfo") is True


def test_wfo_latched_short_circuits() -> None:
    disable_kernel("wfo", "self_check_mismatch")
    with capture_logs() as logs:
        assert _call_wfo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_disabled"]


def test_wfo_min_sharpe_gate_mirrors_the_cpcv_law() -> None:
    """The cross-pin: the shared _numeric_as_f64 law agrees with the cpcv
    binding's inline min_sharpe gate on the hostile threshold matrix."""
    for hostile in (float("nan"), 2**53 + 1, 10**400, _Dec("0.5"), np.float64(0.5)):
        with capture_logs() as logs:
            assert _call_wfo(min_sharpe=hostile) is None  # type: ignore[arg-type]
        wfo_reasons = {e["reason"] for e in logs if e["event"] == "rust_stats.fallback"}
        reset_observability_memo_for_tests()
        counts, means, m2s = _golden_moments()
        with capture_logs() as logs:
            assert (
                cpcv_pass_count_via_kernel(
                    counts,
                    means,
                    m2s,
                    2,
                    hostile,  # type: ignore[arg-type]  # the hostile-threshold matrix
                    n_total=6,
                    oracle=lambda: 0,  # type: ignore[arg-type]
                )
                is None
            )
        cpcv_reasons = {
            e["reason"].replace("min_sharpe_", "arg_")
            for e in logs
            if e["event"] == "rust_stats.fallback"
        }
        assert wfo_reasons == cpcv_reasons, f"gate drift at {hostile!r}"
        reset_observability_memo_for_tests()


def test_wfo_exact_int_min_sharpe_engages() -> None:
    got = _call_wfo(min_sharpe=100, oracle_value=0)  # type: ignore[arg-type]
    assert got == 0  # sharpe ≈ 2.236 not > 100 — kernel serves the 0


def test_wfo_wheel_stale_kernel_error_poisoned_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(fno_math, "stats_wfo_pass_count")
    with capture_logs() as logs:
        assert _call_wfo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["wheel_stale"]
    monkeypatch.undo()
    reset_observability_memo_for_tests()

    def _raise(*args: object) -> int:
        raise ValueError("boom")

    monkeypatch.setattr(fno_math, "stats_wfo_pass_count", _raise)
    with capture_logs() as logs:
        assert _call_wfo() is None
    assert [e["reason"] for e in logs if e["event"] == "rust_stats.fallback"] == ["kernel_error"]
    monkeypatch.setattr(fno_math, "stats_wfo_pass_count", lambda *a: 2)
    with capture_logs() as logs:
        assert _call_wfo(n_walks=1) is None  # beyond n_walks — _valid_pass_count reused
    assert [e["event"] for e in logs if "refused" in e["event"]] == [
        "rust_stats.kernel_result_refused"
    ]

    def _oom(*args: object) -> int:
        raise MemoryError

    monkeypatch.setattr(fno_math, "stats_wfo_pass_count", _oom)
    with pytest.raises(MemoryError):
        _call_wfo()


def test_wfo_steady_state_skips_the_oracle() -> None:
    reset_self_check_for_tests("wfo", remaining=0)
    calls: list[int] = []
    got = _call_wfo(oracle_calls=calls)
    assert got == 1 and calls == []
