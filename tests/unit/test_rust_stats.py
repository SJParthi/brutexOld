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

import math
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import fno_math
import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import (
    _fold_pass_count,  # pyright: ignore[reportPrivateUsage]
    _group_bounds,  # pyright: ignore[reportPrivateUsage]
    _group_moments,  # pyright: ignore[reportPrivateUsage]
    cpcv_pass_rate,
)
from brutex.metrics.rust_stats import (
    SELF_CHECK_SAMPLES,
    STATS_RUST_DEFAULT,
    STATS_RUST_ENV,
    _consume_self_check_sample,  # pyright: ignore[reportPrivateUsage]
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


def test_flag_unset_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFAULT-OFF: the flip to ON-by-default is a SEPARATE later ADR (the
    ADR-0017 precedent); until then an unset flag keeps every caller on the
    verbatim pure-Python path."""
    monkeypatch.delenv(STATS_RUST_ENV, raising=False)
    with capture_logs() as logs:
        assert stats_rust_enabled() is False
    # SILENTLY off: the unset default emits NO fallback event (kills the
    # env-default mutant that fabricates a non-empty raw value).
    assert logs == []
    assert STATS_RUST_DEFAULT is False


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
    assert events[0]["value"] == value


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
    """Literal pins (mutation teeth): the env var name and the default-OFF
    constant are operator-facing contracts."""
    assert STATS_RUST_ENV == "BRUTEX_STATS_RUST"
    assert STATS_RUST_DEFAULT is False


def test_self_check_budgets_pinned() -> None:
    """The R8 per-function burn-in budgets (call cadence × oracle cost)."""
    assert dict(SELF_CHECK_SAMPLES) == {"cpcv": 16, "mc": 8, "pbo": 4, "spa": 2, "wfo": 16}


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
    assert repr(exc) in str(events[0]["error"])


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
    assert events[0]["result"] == repr(poison)
    assert events[0]["n_total"] == 6
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
    """Rate-level refuter repro VERBATIM (scratchpad repro_int_minsharpe.py,
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
    assert events[0]["got"] == 1
    assert events[0]["want"] == 2
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
