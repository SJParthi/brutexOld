"""Unit suite for the flag-gated Rust metrics-FOLD seam.

DEC-METRICS-RUST-COMPUTE-FOLD-001 — 100% line+branch on
``brutex/metrics/compute_rust.py`` from this file alone (the discovery-gate
stem law), plus the ``compute.py`` dispatch-chokepoint arms
(``_resolve_folds``). Layers:

* the flag matrix (Pattern A: only explicit truthy arms; falsy/unset OFF;
  junk OFF + warn-once) + the ``_TRUTHY``/``_FALSY`` mirror pin against
  ``sweep_lookahead``'s vocabulary (the ``feed_rust`` leaf precedent);
* every degrade arm — wheel missing / stale, kernel raise (poisoned wheel),
  PyO3 ``OverflowError`` on a beyond-i64 pnl (REAL kernel), the
  ``MemoryError`` raw re-raise, the refuse-poison structural gate over every
  wire-shape violation, the autorevert latch short-circuit;
* the burn-in self-check: clean burn-in consumes samples + serves the
  kernel wire; a bit-level MISMATCH ships the ORACLE wire (the INJECTED
  ``oracle_wire`` callable — the plain-data boundary's dependency
  inversion), latches the kernel off with ONE ``kernel_autoreverted``
  ERROR, and every later call falls back; the steady state (budget 0)
  serves WITHOUT ever evaluating the oracle (bomb-callable-pinned);
* the ``wire_compare_key`` teeth (signed zero, 1-ulp, the int-0 ≡ +0.0
  ``downside_sum_sq`` belt — and nothing else);
* the default-path NEVER-ROUTES bomb spy (flag unset/OFF ⇒
  ``_compute_metrics_impl`` provably never consults the kernel seam);
* the NON-VACUOUS flag-ON proof: the whole #1685 golden corpus through
  ``_compute_metrics_impl`` with an engagement spy + a widened burn-in
  (every case cross-checked) — bit-equal to the frozen pre-Slice-0 oracle;
* the hypothesis DUAL-ARM property: flag-ON ≡ flag-OFF at packed-byte
  equality per float field over the differential gate's generated corpora
  (strategies imported, never re-minted — CLAUDE.md §11 COMMON).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import math
import os
import struct
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from brutex.metrics import compute_rust
from brutex.metrics.compute import (
    TRAILING_WINDOW,
    Moments,
    RecencyFolds,
    TradeFolds,
    _compute_metrics_impl,
    _fold_trades,
    _fold_wire,
    _folds_from_wire,
    _recency_folds_from_wire,
    _recency_wire,
    _resolve_folds,
    _resolve_recency_folds,
    fold_recency,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import HealthCheck, given, settings
from structlog.testing import capture_logs

from tests.golden.test_compute_fold_combine_parity import (
    _CORPUS,
    _oracle_metrics_pre_slice0,
    _pack_metrics,
)
from tests.property.test_compute_fold_combine_differential import (
    _BENCH,
    _DRAWDOWN,
    _SPAN,
    _START_EQUITY,
    _TOTAL_BARS,
    _TRADES,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The kernel-path tests need the compiled wheel; the CI job that runs this
# gate installs it (the facade-golden importorskip precedent — a wheel-less
# run skips loudly and the standalone coverage stanza fails, never a silent
# false-pass).
fno_math = pytest.importorskip("fno_math")

_TS = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)

_FLAG = compute_rust.METRICS_RUST_FOLD_ENV


def _trade(pnl: int, mfe: int = 0, mae: int = 0, hold: int = 1) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=mfe,
        mae_paisa=mae,
        holding_bars=hold,
        entry_ts=_TS,
        exit_ts=_TS,
        direction=Side.BUY,
    )


#: The golden suite's pinned fold fixture (w, w, l, l, l, scratch, w).
_PIN_TRADES: list[ClosedTrade] = [
    _trade(500, 600, 100, 3),
    _trade(700, 800, 50, 2),
    _trade(-300, 50, 400, 2),
    _trade(-400, 10, 500, 4),
    _trade(-300, 20, 350, 1),
    _trade(0, 0, 0, 1),
    _trade(900, 950, 60, 5),
]

_PIN_START = 1_000_000


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fresh flag + burn-in + memo state per test (test isolation)."""
    monkeypatch.delenv(_FLAG, raising=False)
    compute_rust.reset_self_check_for_tests()
    compute_rust.reset_observability_memo_for_tests()
    yield
    compute_rust.reset_self_check_for_tests()
    compute_rust.reset_observability_memo_for_tests()


@pytest.fixture
def _degrade_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the strict-Rust gate OFF for one test (the fall-back arm).

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` **ON**, so
    every fall-back this facade reports RAISES
    :class:`~brutex.config.rust_strict.RustFallbackRefused` instead of
    returning its historic sentinel. Degrade-to-Python remains a SUPPORTED
    mode behind that kill switch, and the tests below are exactly the tests
    that exercise it — so they opt into the kill switch rather than weaken
    what they assert.

    Deliberately NOT autouse: strict mode must keep its teeth for every
    other test in this module (the flag matrix's armed/disarmed arms, the
    parity corpus, the validator suites, the never-routes bomb spies), so
    the opt-out is visible in each degrade test's own signature.
    """
    monkeypatch.setenv("BRUTEX_RUST_STRICT", "0")


def _valid_wire() -> tuple[Any, ...]:
    """A structurally valid kernel wire tuple for the pin fixture."""
    returns = [t.pnl_paisa / _PIN_START for t in _PIN_TRADES]
    wire: tuple[Any, ...] = fno_math.metrics_fold_trades(
        [t.pnl_paisa for t in _PIN_TRADES],
        [t.mfe_paisa for t in _PIN_TRADES],
        [t.mae_paisa for t in _PIN_TRADES],
        [t.holding_bars for t in _PIN_TRADES],
        returns,
    )
    return wire


def _plant_kernel(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> None:
    """Install a fake ``fno_math`` module exposing *kernel* as the symbol."""
    fake = types.SimpleNamespace(metrics_fold_trades=kernel)
    monkeypatch.setitem(sys.modules, "fno_math", fake)


def _oracle_for(trades: Sequence[ClosedTrade], start: int) -> Any:
    """The consumer-side burn-in oracle callable for *trades*/*start*."""
    return lambda: _fold_wire(_fold_trades(trades, start))


def _oracle_bomb() -> Any:
    """An oracle_wire that must NEVER be evaluated on the tested path."""
    raise AssertionError("oracle_wire must not be evaluated on this path")


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


class TestFlagMatrix:
    """Pattern A: only an explicit truthy value arms the kernel."""

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on ", "True"])
    def test_truthy_arms(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_FLAG, value)
        assert compute_rust.metrics_rust_fold_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
    def test_falsy_disarms(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_FLAG, value)
        assert compute_rust.metrics_rust_fold_enabled() is False

    def test_unset_falls_to_default_on(self) -> None:
        # DEC-RUST-DEFAULT-ON-WAVE-001: unset ⇒ the kernel is ON.
        assert _FLAG not in os.environ
        assert compute_rust.metrics_rust_fold_enabled() is True
        assert compute_rust.METRICS_RUST_FOLD_DEFAULT is True

    def test_whitespace_empty_falls_to_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "   ")
        assert compute_rust.metrics_rust_fold_enabled() is compute_rust.METRICS_RUST_FOLD_DEFAULT

    @pytest.mark.usefixtures("_degrade_mode")
    def test_junk_is_off_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "ture")
        with capture_logs() as logs:
            assert compute_rust.metrics_rust_fold_enabled() is False
            assert compute_rust.metrics_rust_fold_enabled() is False
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        # repr()'d for log-repr uniformity with the module's own
        # kernel_error/result_refused sites (the #1605 hostile-review
        # MINOR-1, folded by the W2 carrier).
        assert warns[0]["value"] == repr("ture")


class TestVocabularyMirrorPin:
    """The inline leaf vocabulary must stay byte-equal to the engine's."""

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        from brutex.engine import sweep_lookahead

        assert compute_rust._TRUTHY == sweep_lookahead._TRUTHY
        assert compute_rust._FALSY == sweep_lookahead._FALSY


class TestResolveKernel:
    """Wheel resolution: real, missing, stale — each a named warn-once."""

    def test_real_wheel_resolves(self) -> None:
        kernel = compute_rust._resolve_kernel()
        assert kernel is fno_math.metrics_fold_trades

    @pytest.mark.usefixtures("_degrade_mode")
    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        with capture_logs() as logs:
            assert compute_rust._resolve_kernel() is None
            assert compute_rust._resolve_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "wheel_missing"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_wheel_stale_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", types.SimpleNamespace())
        with capture_logs() as logs:
            assert compute_rust._resolve_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "wheel_stale"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_wheel_stale_on_both_faces_names_each_symbol_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dedupe keys per (reason, symbol) — review-round fix.

        Pre-fix the memo keyed on the reason alone, so a wheel stale on
        BOTH kernel faces logged ``wheel_stale`` once and MUTED the second
        symbol; each missing face now warns exactly once, repeats stay
        silent.
        """
        monkeypatch.setitem(sys.modules, "fno_math", types.SimpleNamespace())
        with capture_logs() as logs:
            assert compute_rust._resolve_kernel("metrics_fold_trades") is None
            assert compute_rust._resolve_kernel("metrics_fold_recency") is None
            # Repeats on either face stay deduped.
            assert compute_rust._resolve_kernel("metrics_fold_trades") is None
            assert compute_rust._resolve_kernel("metrics_fold_recency") is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert [(w["reason"], w["symbol"]) for w in warns] == [
            ("wheel_stale", "metrics_fold_trades"),
            ("wheel_stale", "metrics_fold_recency"),
        ]


class TestFoldViaKernelDegrades:
    """Every kernel-call fault serves the row from the Python oracle.

    Every degrade arm passes ``oracle_wire=_oracle_bomb`` — the injected
    oracle is only ever evaluated inside a PASSED-structural-gate burn-in
    compare, so a degrade path must never touch it (pinned here).
    """

    @pytest.mark.usefixtures("_degrade_mode")
    def test_kernel_disabled_short_circuits(self) -> None:
        compute_rust.disable_kernel("test_latch")
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
                is None
            )
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "kernel_disabled"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_wheel_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert (
            compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
            is None
        )

    @pytest.mark.parametrize(
        "exc", [ValueError("boundary"), TypeError("conversion"), RuntimeError("poisoned")]
    )
    @pytest.mark.usefixtures("_degrade_mode")
    def test_kernel_raise_degrades(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def _bomb(*_args: object) -> object:
            raise exc

        _plant_kernel(monkeypatch, _bomb)
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
                is None
            )
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "kernel_error"
        assert repr(exc) in str(warns[0]["error"])

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(*_args: object) -> object:
            raise MemoryError

        _plant_kernel(monkeypatch, _oom)
        with pytest.raises(MemoryError):
            compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)

    @pytest.mark.usefixtures("_degrade_mode")
    def test_beyond_i64_pnl_degrades_via_real_kernel(self) -> None:
        """A pnl outside i64 fails PyO3 conversion → OverflowError → oracle."""
        trades = [_trade(2**70), _trade(-1)]
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(trades, 1_000, oracle_wire=_oracle_bomb) is None
            )
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "kernel_error"
        # The oracle arm handles the same input fine (unbounded Python ints).
        folds = _fold_trades(trades, 1_000)
        assert folds.net_pnl_paisa == 2**70 - 1

    @pytest.mark.usefixtures("_degrade_mode")
    def test_invalid_wire_refused_loudly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _plant_kernel(monkeypatch, lambda *_a: ("junk",))
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
                is None
            )
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
                is None
            )
        errors = _events(logs, "metrics_rust_fold.kernel_result_refused")
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["n_trades"] == len(_PIN_TRADES)


def _poison_counts(wire: tuple[Any, ...], **changes: object) -> tuple[Any, ...]:
    """Rebuild *wire* with named counts-tuple slots replaced."""
    slots = ["n", "winners", "losers", "max_holding", "largest_loss", "max_l", "max_w"]
    counts = list(wire[0])
    for name, value in changes.items():
        counts[slots.index(name)] = value
    return (tuple(counts), wire[1], wire[2], wire[3])


class TestValidFoldWire:
    """The refuse-poison structural gate — every violation refused."""

    def test_valid_wire_accepted(self) -> None:
        assert compute_rust._valid_fold_wire(_valid_wire(), len(_PIN_TRADES))

    @pytest.mark.parametrize(
        "poison",
        [
            "not-a-tuple",
            ("a", "b"),  # wrong outer arity
        ],
    )
    def test_outer_shape_refused(self, poison: object) -> None:
        assert not compute_rust._valid_fold_wire(poison, len(_PIN_TRADES))

    def test_inner_shapes_refused(self) -> None:
        wire = _valid_wire()
        n = len(_PIN_TRADES)
        assert not compute_rust._valid_fold_wire((wire[0][:6], wire[1], wire[2], wire[3]), n)
        assert not compute_rust._valid_fold_wire((wire[0], wire[1][:5], wire[2], wire[3]), n)
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], wire[2][:4], wire[3]), n)
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], wire[2], wire[3][:2]), n)
        assert not compute_rust._valid_fold_wire(([1], wire[1], wire[2], wire[3]), n)

    def test_non_int_slots_refused(self) -> None:
        wire = _valid_wire()
        n = len(_PIN_TRADES)
        # A bool is NOT an acceptable int (the _is_int law).
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, winners=True), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, n="7"), n)
        sums = list(wire[1])
        sums[0] = 1100.0
        assert not compute_rust._valid_fold_wire((wire[0], tuple(sums), wire[2], wire[3]), n)

    def test_count_laws_refused(self) -> None:
        wire = _valid_wire()
        n = len(_PIN_TRADES)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, n=n + 1), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, winners=n + 1), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, losers=-1), n)
        # winners + losers > n with each individually in range.
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, winners=n, losers=n), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, max_l=n + 1), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, max_w=-1), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, max_holding=0), n)
        assert not compute_rust._valid_fold_wire(_poison_counts(wire, largest_loss=1), n)

    def test_moments_laws_refused(self) -> None:
        wire = _valid_wire()
        n = len(_PIN_TRADES)
        moments = list(wire[2])
        moments[0] = float(n)  # non-int moments.n
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], tuple(moments), wire[3]), n)
        moments = list(wire[2])
        moments[0] = n + 1  # moments.n != n_trades
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], tuple(moments), wire[3]), n)
        moments = list(wire[2])
        moments[2] = "0.5"  # non-float moment
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], tuple(moments), wire[3]), n)

    def test_floats_laws_refused(self) -> None:
        wire = _valid_wire()
        n = len(_PIN_TRADES)
        floats = list(wire[3])
        floats[1] = 1  # int where a float is required
        assert not compute_rust._valid_fold_wire((wire[0], wire[1], wire[2], tuple(floats)), n)


class TestWireCompareKey:
    """The bit-compare key's teeth — and its ONE deliberate equivalence."""

    def test_key_distinguishes_one_ulp(self) -> None:
        wire = _fold_wire(_fold_trades(_PIN_TRADES, _PIN_START))
        moments = list(wire[2])
        moments[1] = math.nextafter(moments[1], 1.0)
        bumped: Any = (wire[0], wire[1], tuple(moments), wire[3])
        assert compute_rust.wire_compare_key(wire) != compute_rust.wire_compare_key(bumped)

    def test_key_distinguishes_signed_zero(self) -> None:
        wire = _fold_wire(_fold_trades([_trade(7), _trade(7)], 100))
        assert wire[3][2] >= 0.0  # p5
        floats = list(wire[3])
        floats[2] = -0.0
        flipped: Any = (wire[0], wire[1], wire[2], tuple(floats))
        assert compute_rust.wire_compare_key(wire) != compute_rust.wire_compare_key(flipped)

    def test_key_equates_int_zero_and_float_zero_downside(self) -> None:
        """The ONE sanctioned equivalence: Python's `sum([])` int 0 ≡ +0.0.

        The consumer's `_fold_wire` projection float-normalises the fold's
        legitimate int-``0`` downside; the key's own ``float(...)`` is the
        total BELT — a hand-built wire carrying the raw int ``0`` still
        keys equal to the ``+0.0`` wire, and nothing else does.
        """
        folds = _fold_trades([_trade(7), _trade(7)], 100)
        assert isinstance(folds.downside_sum_sq, int)  # the raw fold shape
        wire = _fold_wire(folds)
        assert isinstance(wire[3][0], float)  # the projection normalised it
        raw_int = (wire[0], wire[1], wire[2], (0, wire[3][1], wire[3][2]))
        assert compute_rust.wire_compare_key(raw_int) == compute_rust.wire_compare_key(wire)

    def test_key_distinguishes_int_fields(self) -> None:
        wire = _fold_wire(_fold_trades(_PIN_TRADES, _PIN_START))
        counts = list(wire[0])
        counts[1] += 1  # winners
        bumped: Any = (tuple(counts), wire[1], wire[2], wire[3])
        assert compute_rust.wire_compare_key(wire) != compute_rust.wire_compare_key(bumped)


class TestBurnIn:
    """The #1531/#1532 burn-in self-check + autorevert latch."""

    def test_clean_burn_in_serves_kernel_and_consumes(self) -> None:
        assert compute_rust.self_check_pending()
        before = compute_rust._SELF_CHECK_REMAINING[0]
        with capture_logs() as logs:
            wire = compute_rust.fold_wire_via_kernel(
                _PIN_TRADES, _PIN_START, oracle_wire=_oracle_for(_PIN_TRADES, _PIN_START)
            )
        assert wire is not None
        oracle = _fold_wire(_fold_trades(_PIN_TRADES, _PIN_START))
        assert compute_rust.wire_compare_key(wire) == compute_rust.wire_compare_key(oracle)
        assert compute_rust._SELF_CHECK_REMAINING[0] == before - 1
        assert not compute_rust.kernel_disabled()
        engaged = _events(logs, "metrics_rust_fold.engaged")
        assert len(engaged) == 1

    def test_engaged_logged_once_per_process(self) -> None:
        oracle = _oracle_for(_PIN_TRADES, _PIN_START)
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=oracle)
                is not None
            )
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=oracle)
                is not None
            )
        assert len(_events(logs, "metrics_rust_fold.engaged")) == 1

    @pytest.mark.usefixtures("_degrade_mode")
    def test_mismatch_ships_oracle_and_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = _valid_wire()
        floats = list(wire[3])
        floats[1] = math.nextafter(floats[1], math.inf)  # 1-ulp p95 poison
        poisoned = (wire[0], wire[1], wire[2], tuple(floats))
        _plant_kernel(monkeypatch, lambda *_a: poisoned)
        oracle = _fold_wire(_fold_trades(_PIN_TRADES, _PIN_START))
        with capture_logs() as logs:
            served = compute_rust.fold_wire_via_kernel(
                _PIN_TRADES, _PIN_START, oracle_wire=_oracle_for(_PIN_TRADES, _PIN_START)
            )
        # The ORACLE wire ships — never the mismatching kernel value.
        assert served is not None
        assert compute_rust.wire_compare_key(served) == compute_rust.wire_compare_key(oracle)
        assert compute_rust.kernel_disabled()
        errors = _events(logs, "metrics_rust_fold.kernel_autoreverted")
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["reason"] == "burn_in_mismatch"
        # Every later call short-circuits to the oracle path.
        with capture_logs() as logs:
            assert (
                compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
                is None
            )
        assert _events(logs, "metrics_rust_fold.fallback")[0]["reason"] == "kernel_disabled"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_disable_kernel_repeat_is_noop(self) -> None:
        with capture_logs() as logs:
            compute_rust.disable_kernel("first")
            compute_rust.disable_kernel("second")
        errors = _events(logs, "metrics_rust_fold.kernel_autoreverted")
        assert len(errors) == 1
        assert errors[0]["reason"] == "first"

    def test_steady_state_serves_without_oracle(self) -> None:
        """Post-burn-in the injected oracle callable is NEVER evaluated."""
        compute_rust.reset_self_check_for_tests(remaining=0)
        assert not compute_rust.self_check_pending()
        wire = compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
        assert wire is not None
        assert wire[1][0] == 1100  # net_pnl_paisa

    def test_consume_clamps_at_zero(self) -> None:
        compute_rust.reset_self_check_for_tests(remaining=0)
        compute_rust.consume_self_check_sample()
        assert compute_rust._SELF_CHECK_REMAINING[0] == 0
        compute_rust.reset_self_check_for_tests()
        assert compute_rust._SELF_CHECK_REMAINING[0] == compute_rust.SELF_CHECK_SAMPLES


class TestDispatchChokepoint:
    """`_resolve_folds` — the ONE compute.py branch the seam adds."""

    def test_kill_switch_path_never_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag "0" (the explicit kill switch): the kernel seam is provably untouched.

        RETARGETED (DEC-RUST-DEFAULT-ON-WAVE-001): unset is now default-ON,
        so the never-routes corpus pin rides the explicit falsy arm.
        """
        monkeypatch.setenv(_FLAG, "0")

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the kill-switch path must never consult the kernel seam")

        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", _seam_bomb)
        for name, trades, tb, bench, start, span, dd in _CORPUS:
            expected = _oracle_metrics_pre_slice0(
                trades, tb, bench, start_equity_paisa=start, span_years=span, drawdown=dd
            )
            actual = _compute_metrics_impl(
                trades, tb, bench, start_equity_paisa=start, span_years=span, drawdown=dd
            )
            assert _pack_metrics(actual) == _pack_metrics(expected), name

    def test_flag_off_explicit_never_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "0")

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("flag OFF must never consult the kernel seam")

        monkeypatch.setattr(compute_rust, "fold_wire_via_kernel", _seam_bomb)
        folds = _resolve_folds(_PIN_TRADES, _PIN_START)
        assert folds == _fold_trades(_PIN_TRADES, _PIN_START)

    def test_flag_on_kernel_serves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        folds = _resolve_folds(_PIN_TRADES, _PIN_START)
        oracle = _fold_trades(_PIN_TRADES, _PIN_START)
        assert compute_rust.wire_compare_key(_fold_wire(folds)) == compute_rust.wire_compare_key(
            _fold_wire(oracle)
        )
        # The dispatch rebuilt genuine dataclasses from the wire.
        assert isinstance(folds, TradeFolds)
        assert isinstance(folds.moments, Moments)

    @pytest.mark.usefixtures("_degrade_mode")
    def test_flag_on_kernel_unavailable_falls_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)
        folds = _resolve_folds(_PIN_TRADES, _PIN_START)
        assert folds == _fold_trades(_PIN_TRADES, _PIN_START)


class TestFlagOnCorpusParity:
    """The NON-VACUOUS flag-ON proof over the whole #1685 golden corpus."""

    def test_corpus_bit_equal_with_kernel_engaged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        # Widen the burn-in so EVERY corpus case is kernel-vs-oracle
        # cross-checked inside the seam, not just the first 16.
        compute_rust.reset_self_check_for_tests(remaining=10_000)
        calls = [0]
        real = fno_math.metrics_fold_trades

        def _spy(*args: object) -> object:
            calls[0] += 1
            return real(*args)

        monkeypatch.setattr(fno_math, "metrics_fold_trades", _spy)
        for name, trades, tb, bench, start, span, dd in _CORPUS:
            expected = _oracle_metrics_pre_slice0(
                trades, tb, bench, start_equity_paisa=start, span_years=span, drawdown=dd
            )
            actual = _compute_metrics_impl(
                trades, tb, bench, start_equity_paisa=start, span_years=span, drawdown=dd
            )
            assert _pack_metrics(actual) == _pack_metrics(expected), name
        # Engaged on every non-empty case; no autorevert fired.
        nonempty = sum(1 for case in _CORPUS if case[1])
        assert calls[0] == nonempty
        assert not compute_rust.kernel_disabled()

    def test_neumaier_downside_case_served_by_kernel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The burn-in-caught 1-ulp class: ten -0.1 returns whose squares'
        compensated sum differs from the plain fold — the kernel must serve
        CPython 3.12's `sum()` answer bit-for-bit."""
        monkeypatch.setenv(_FLAG, "1")
        trades = [_trade(-1000) for _ in range(10)]
        folds = _resolve_folds(trades, 10_000)
        oracle = _fold_trades(trades, 10_000)
        assert not compute_rust.kernel_disabled()
        assert struct.pack("<d", folds.downside_sum_sq) == struct.pack("<d", oracle.downside_sum_sq)
        assert struct.pack("<d", folds.downside_sum_sq) == struct.pack(
            "<d", float.fromhex("0x1.999999999999bp-4")
        )


class TestDualArmProperty:
    """Hypothesis: flag-ON ≡ flag-OFF at packed-byte equality per field."""

    @given(
        trades=_TRADES,
        total_bars=_TOTAL_BARS,
        bench=_BENCH,
        start_equity=_START_EQUITY,
        span=_SPAN,
        drawdown=_DRAWDOWN,
    )
    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_flag_on_bit_equals_flag_off(
        self,
        trades: Sequence[ClosedTrade],
        total_bars: int,
        bench: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        prior = os.environ.get(_FLAG)
        # The OFF baseline = the explicit "0" kill switch → the Python oracle.
        # The default is flipped ON (DEC-RUST-DEFAULT-ON-WAVE-001), so popping
        # the env (unset) would resolve ON and make `on == off` a vacuous
        # Rust==Rust; "0" pins the genuine oracle arm (the sibling-test idiom).
        os.environ[_FLAG] = "0"
        try:
            off = _pack_metrics(
                _compute_metrics_impl(
                    trades,
                    total_bars,
                    bench,
                    start_equity_paisa=start_equity,
                    span_years=span,
                    drawdown=drawdown,
                )
            )
            # Fresh burn-in per example: the seam itself cross-checks the
            # kernel folds against the oracle folds bit-for-bit, so a
            # divergence fails HERE (autorevert) — never a silent serve.
            compute_rust.reset_self_check_for_tests()
            compute_rust.reset_observability_memo_for_tests()
            os.environ[_FLAG] = "1"
            on = _pack_metrics(
                _compute_metrics_impl(
                    trades,
                    total_bars,
                    bench,
                    start_equity_paisa=start_equity,
                    span_years=span,
                    drawdown=drawdown,
                )
            )
        finally:
            if prior is None:
                os.environ.pop(_FLAG, None)
            else:
                os.environ[_FLAG] = prior
        assert on == off
        assert not compute_rust.kernel_disabled()


class TestFoldsBitEquality:
    """Direct kernel-vs-oracle wire bit equality on hostile shapes."""

    @pytest.mark.parametrize(
        ("name", "trades", "start"),
        [
            ("one_win", [_trade(500, 600, 100, 3)], 1_000_000),
            ("one_loss", [_trade(-500, 10, 600, 2)], 1_000_000),
            ("one_scratch", [_trade(0, 50, 50, 1)], 1_000_000),
            ("pin", _PIN_TRADES, _PIN_START),
            (
                "denormal_adjacent",
                [_trade(1), _trade(-1), _trade(1)],
                2**62,
            ),
            (
                "huge_magnitudes",
                [_trade(-(10**13), 0, 10**13, 375)] * 4 + [_trade(10**13, 10**13, 0, 375)],
                10**6,
            ),
            ("neumaier_band", [_trade(-1000) for _ in range(10)], 10_000),
        ],
    )
    def test_kernel_wire_bit_equal_oracle(
        self, name: str, trades: list[ClosedTrade], start: int
    ) -> None:
        compute_rust.reset_self_check_for_tests(remaining=0)  # no cross-check aid
        wire = compute_rust.fold_wire_via_kernel(trades, start, oracle_wire=_oracle_bomb)
        assert wire is not None, name
        oracle = _fold_wire(_fold_trades(trades, start))
        assert compute_rust.wire_compare_key(wire) == compute_rust.wire_compare_key(oracle), name
        # The consumer-side rebuild yields genuine dataclasses with genuine
        # Python ints (the i128 crossing) — not duck-typed stand-ins.
        folds = _folds_from_wire(wire)
        assert isinstance(folds, TradeFolds)
        assert isinstance(folds.moments, Moments)
        assert isinstance(folds.net_pnl_paisa, int)
        assert isinstance(folds.sum_holding_bars, int)


# ─────────────────────────────────────────────────────────────────────────
# The RECENCY wire face (DEC-METRICS-TRAILING-WINDOW-001 Stage-C2) — the
# ``fold_recency`` twin: its own burn-in budget over the SHARED autorevert
# latch, the epoch-µs conversion law, the refuse-poison gate (structure +
# windowed count + sorted non-negative keys + the exact conservation law),
# every degrade arm, the compute.py dispatch (``_resolve_recency_folds``)
# and the NON-VACUOUS flag-ON parity over hostile window/bucket edges.

_ANCHOR = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
_ANCHOR_US = compute_rust.datetime_to_epoch_us(_ANCHOR)
_CUTOFF_US = compute_rust.datetime_to_epoch_us(_ANCHOR - TRAILING_WINDOW)


def _rtrade(pnl: int, *, age_us: int) -> ClosedTrade:
    """A trade exiting ``age_us`` microseconds before the pinned anchor."""
    exit_ts = _ANCHOR - timedelta(microseconds=age_us)
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=max(pnl, 0),
        mae_paisa=max(-pnl, 0),
        holding_bars=1,
        entry_ts=exit_ts - timedelta(minutes=5),
        exit_ts=exit_ts,
        direction=Side.BUY,
    )


_DAY_US = 86_400_000_000

#: A mixed stream exercising both window arms + four distinct buckets:
#: ages 0d + 29d (bucket 0, in-window), 30d (bucket 1, in-window),
#: 731d (bucket 24, OUTSIDE the 730d window), 3000d (bucket 100, outside).
_RECENCY_TRADES: list[ClosedTrade] = [
    _rtrade(500, age_us=0),
    _rtrade(-300, age_us=29 * _DAY_US),
    _rtrade(700, age_us=30 * _DAY_US),
    _rtrade(-400, age_us=731 * _DAY_US),
    _rtrade(900, age_us=3000 * _DAY_US),
]


def _recency_oracle_for(trades: Sequence[ClosedTrade], start: int) -> Any:
    """The consumer-side recency burn-in oracle for *trades*/*start*."""
    return lambda: _recency_wire(fold_recency(trades, start, trailing_anchor=_ANCHOR))


def _recency_via_kernel(
    trades: Sequence[ClosedTrade] = _RECENCY_TRADES,
    start: int = _PIN_START,
    oracle: Any = None,
) -> Any:
    return compute_rust.recency_wire_via_kernel(
        trades,
        start,
        anchor_us=_ANCHOR_US,
        cutoff_us=_CUTOFF_US,
        oracle_wire=oracle if oracle is not None else _oracle_bomb,
    )


def _plant_recency_kernel(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> None:
    """Fake ``fno_math`` exposing *kernel* as ``metrics_fold_recency``."""
    fake = types.SimpleNamespace(
        metrics_fold_trades=fno_math.metrics_fold_trades,
        metrics_fold_recency=kernel,
    )
    monkeypatch.setitem(sys.modules, "fno_math", fake)


def _kernel_recency_wire(trades: Sequence[ClosedTrade] = _RECENCY_TRADES) -> tuple[Any, ...]:
    """A structurally valid RAW recency kernel result for *trades*."""
    returns = [t.pnl_paisa / _PIN_START for t in trades]
    exit_us = [compute_rust.datetime_to_epoch_us(t.exit_ts) for t in trades]
    result: tuple[Any, ...] = fno_math.metrics_fold_recency(
        [t.pnl_paisa for t in trades],
        [t.mfe_paisa for t in trades],
        [t.mae_paisa for t in trades],
        [t.holding_bars for t in trades],
        returns,
        exit_us,
        _ANCHOR_US,
        _CUTOFF_US,
    )
    return result


class TestEpochUs:
    """`datetime_to_epoch_us` — the one exact epoch basis."""

    def test_exact_microsecond_resolution(self) -> None:
        ts = datetime(2026, 1, 1, 10, 0, 0, 1, tzinfo=UTC)
        assert compute_rust.datetime_to_epoch_us(ts) == _ANCHOR_US + 1

    def test_pre_1970_is_negative_and_exact(self) -> None:
        ts = datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
        assert compute_rust.datetime_to_epoch_us(ts) == -1

    def test_tz_offset_invariance(self) -> None:
        # The SAME instant expressed in IST compares equal in epoch µs.
        from datetime import timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        assert compute_rust.datetime_to_epoch_us(
            _ANCHOR.astimezone(ist)
        ) == compute_rust.datetime_to_epoch_us(_ANCHOR)

    def test_matches_the_python_window_compare(self) -> None:
        # exit >= cutoff in datetime space ⟺ exit_us >= cutoff_us — the
        # boundary instant itself (equality) included.
        cutoff = _ANCHOR - TRAILING_WINDOW
        at = compute_rust.datetime_to_epoch_us(cutoff)
        assert at == _CUTOFF_US
        assert compute_rust.datetime_to_epoch_us(cutoff - timedelta(microseconds=1)) == at - 1


class TestValidRecencyWire:
    """The recency refuse-poison gate — every violation refused."""

    def _net(self, trades: Sequence[ClosedTrade] = _RECENCY_TRADES) -> int:
        return sum(t.pnl_paisa for t in trades)

    def test_real_kernel_result_accepted(self) -> None:
        result = _kernel_recency_wire()
        assert compute_rust._valid_recency_wire(result, 3, self._net())

    def test_tuple_buckets_accepted_too(self) -> None:
        trailing, buckets = _kernel_recency_wire()
        assert compute_rust._valid_recency_wire((trailing, tuple(buckets)), 3, self._net())

    @pytest.mark.parametrize("poison", [None, [], ("only",), ("a", "b", "c"), "junk"])
    def test_outer_shape_refused(self, poison: object) -> None:
        assert not compute_rust._valid_recency_wire(poison, 3, 0)

    def test_trailing_presence_must_match_windowed_count(self) -> None:
        trailing, buckets = _kernel_recency_wire()
        # None trailing with a non-empty window — refused.
        assert not compute_rust._valid_recency_wire((None, buckets), 3, self._net())
        # A present trailing with a ZERO windowed count — refused.
        assert not compute_rust._valid_recency_wire((trailing, buckets), 0, self._net())
        # A present trailing whose n differs from the windowed count — the
        # inner _valid_fold_wire law.
        assert not compute_rust._valid_recency_wire((trailing, buckets), 2, self._net())

    def test_none_trailing_accepted_for_empty_window(self) -> None:
        outside = [_rtrade(-400, age_us=731 * _DAY_US), _rtrade(900, age_us=3000 * _DAY_US)]
        result = _kernel_recency_wire(outside)
        assert result[0] is None
        assert compute_rust._valid_recency_wire(result, 0, 500)

    @pytest.mark.parametrize(
        "buckets",
        [
            "junk",  # not a sequence of pairs
            [],  # empty (n_trades >= 1 ⇒ at least one bucket)
            [[0, 200]],  # pair not a tuple
            [(0, 200, 1)],  # wrong pair arity
            [(True, 200)],  # bool key refused (genuine-int law)
            [(0, True)],  # bool sum refused
            [(0, 200.0)],  # float sum refused
            [(-1, 200)],  # negative key
            [(1, 100), (1, 100)],  # equal keys (strictly ascending law)
            [(1, 100), (0, 100)],  # descending keys
            [(0, 100), (1, 99)],  # conservation broken (net is 200 here)
        ],
    )
    def test_bucket_poisons_refused(self, buckets: object) -> None:
        trades = [_rtrade(100, age_us=0), _rtrade(100, age_us=40 * _DAY_US)]
        result = _kernel_recency_wire(trades)
        assert not compute_rust._valid_recency_wire((result[0], buckets), 2, 200)


class TestRecencyViaKernelDegrades:
    """Every recency kernel fault serves the row from the Python oracle."""

    @pytest.mark.usefixtures("_degrade_mode")
    def test_kernel_disabled_short_circuits(self) -> None:
        compute_rust.disable_kernel("test_latch")
        with capture_logs() as logs:
            assert _recency_via_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "kernel_disabled"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_wheel_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        with capture_logs() as logs:
            assert _recency_via_kernel() is None
        assert _events(logs, "metrics_rust_fold.fallback")[0]["reason"] == "wheel_missing"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_pre_c2_wheel_degrades_as_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wheel carrying the FOLD face but not the recency face (the
        # pre-C2 shape) degrades wheel_stale, naming the missing symbol.
        _plant_kernel(monkeypatch, fno_math.metrics_fold_trades)
        with capture_logs() as logs:
            assert _recency_via_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert warns[0]["reason"] == "wheel_stale"
        assert warns[0]["symbol"] == "metrics_fold_recency"

    @pytest.mark.parametrize(
        "exc", [ValueError("boundary"), TypeError("conversion"), RuntimeError("poisoned")]
    )
    @pytest.mark.usefixtures("_degrade_mode")
    def test_kernel_raise_degrades(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def _bomb(*_args: object) -> object:
            raise exc

        _plant_recency_kernel(monkeypatch, _bomb)
        with capture_logs() as logs:
            assert _recency_via_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert warns[0]["reason"] == "kernel_error"
        assert repr(exc) in str(warns[0]["error"])

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(*_args: object) -> object:
            raise MemoryError

        _plant_recency_kernel(monkeypatch, _oom)
        with pytest.raises(MemoryError):
            _recency_via_kernel()

    @pytest.mark.usefixtures("_degrade_mode")
    def test_beyond_i64_anchor_degrades_via_real_kernel(self) -> None:
        """An anchor outside i64 fails PyO3 conversion → OverflowError → oracle."""
        with capture_logs() as logs:
            assert (
                compute_rust.recency_wire_via_kernel(
                    _RECENCY_TRADES,
                    _PIN_START,
                    anchor_us=2**70,
                    cutoff_us=2**70 - 730 * _DAY_US,
                    oracle_wire=_oracle_bomb,
                )
                is None
            )
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert warns[0]["reason"] == "kernel_error"

    @pytest.mark.usefixtures("_degrade_mode")
    def test_invalid_wire_refused_loudly_once_own_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _plant_recency_kernel(monkeypatch, lambda *_a: ("junk",))
        with capture_logs() as logs:
            assert _recency_via_kernel() is None
            assert _recency_via_kernel() is None
        errors = _events(logs, "metrics_rust_fold.recency_kernel_result_refused")
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["n_trades"] == len(_RECENCY_TRADES)


class TestRecencyBurnIn:
    """The recency face's own budget over the SHARED autorevert latch."""

    def test_clean_burn_in_serves_kernel_and_consumes_own_budget(self) -> None:
        fold_before = compute_rust._SELF_CHECK_REMAINING[0]
        before = compute_rust._RECENCY_SELF_CHECK_REMAINING[0]
        wire = _recency_via_kernel(oracle=_recency_oracle_for(_RECENCY_TRADES, _PIN_START))
        assert wire is not None
        assert compute_rust._RECENCY_SELF_CHECK_REMAINING[0] == before - 1
        # The FOLD face's budget is untouched — per-face budgets.
        assert compute_rust._SELF_CHECK_REMAINING[0] == fold_before
        assert not compute_rust.kernel_disabled()

    @pytest.mark.usefixtures("_degrade_mode")
    def test_mismatch_ships_oracle_and_latches_the_shared_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trailing, buckets = _kernel_recency_wire()

        def _wrong(*_args: object) -> object:
            # Conservation-consistent poison: shift ONE bucket's sum and
            # compensate in another so the structural gate passes and only
            # the burn-in bit-compare can catch it.
            pairs = list(buckets)
            pairs[0] = (pairs[0][0], pairs[0][1] + 1)
            pairs[-1] = (pairs[-1][0], pairs[-1][1] - 1)
            return (trailing, pairs)

        _plant_recency_kernel(monkeypatch, _wrong)
        oracle = _recency_oracle_for(_RECENCY_TRADES, _PIN_START)
        with capture_logs() as logs:
            wire = _recency_via_kernel(oracle=oracle)
        # The ORACLE wire shipped, not the poisoned kernel wire.
        assert wire == oracle()
        assert compute_rust.kernel_disabled()
        errors = _events(logs, "metrics_rust_fold.kernel_autoreverted")
        assert len(errors) == 1
        assert errors[0]["reason"] == "recency_burn_in_mismatch"
        # The SHARED latch: the FOLD face now short-circuits too.
        assert (
            compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
            is None
        )

    def test_steady_state_serves_without_oracle(self) -> None:
        compute_rust.reset_self_check_for_tests(remaining=0)
        wire = _recency_via_kernel()  # oracle bomb — must not be evaluated
        assert wire is not None
        assert wire == _recency_wire(
            fold_recency(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        )

    def test_recency_engaged_logged_once_per_process(self) -> None:
        compute_rust.reset_self_check_for_tests(remaining=0)
        with capture_logs() as logs:
            assert _recency_via_kernel() is not None
            assert _recency_via_kernel() is not None
        infos = _events(logs, "metrics_rust_fold.recency_engaged")
        assert len(infos) == 1

    def test_consume_clamps_at_zero(self) -> None:
        compute_rust.reset_self_check_for_tests(remaining=0)
        assert not compute_rust.recency_self_check_pending()
        compute_rust.consume_recency_self_check_sample()
        assert compute_rust._RECENCY_SELF_CHECK_REMAINING[0] == 0


class TestRecencyDispatchChokepoint:
    """`_resolve_recency_folds` — the compute.py recency dispatch arms."""

    def test_kill_switch_path_never_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RETARGETED (DEC-RUST-DEFAULT-ON-WAVE-001): unset is now
        # default-ON, so the never-routes pin rides the explicit "0" arm.
        monkeypatch.setenv(_FLAG, "0")

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the kill-switch path must never consult the kernel seam")

        monkeypatch.setattr(compute_rust, "recency_wire_via_kernel", _seam_bomb)
        folds = _resolve_recency_folds(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        assert folds == fold_recency(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)

    def test_flag_off_explicit_never_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "0")

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("flag OFF must never consult the kernel seam")

        monkeypatch.setattr(compute_rust, "recency_wire_via_kernel", _seam_bomb)
        folds = _resolve_recency_folds(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        assert folds == fold_recency(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)

    def test_naive_anchor_takes_the_python_value_error_flag_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a naive anchor must never reach the kernel seam")

        monkeypatch.setattr(compute_rust, "recency_wire_via_kernel", _seam_bomb)
        with pytest.raises(ValueError, match="must be tz-aware"):
            _resolve_recency_folds(
                _RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR.replace(tzinfo=None)
            )

    def test_flag_on_kernel_serves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        folds = _resolve_recency_folds(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        oracle = fold_recency(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        assert compute_rust.recency_wire_compare_key(
            _recency_wire(folds)
        ) == compute_rust.recency_wire_compare_key(_recency_wire(oracle))
        # The dispatch rebuilt genuine dataclasses from the wire.
        assert isinstance(folds, RecencyFolds)
        assert folds.trailing is not None
        assert isinstance(folds.trailing, TradeFolds)
        assert isinstance(folds.trailing.moments, Moments)

    def test_flag_on_none_trailing_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        outside = [_rtrade(-400, age_us=731 * _DAY_US), _rtrade(900, age_us=3000 * _DAY_US)]
        folds = _resolve_recency_folds(outside, _PIN_START, trailing_anchor=_ANCHOR)
        assert folds.trailing is None
        assert folds == fold_recency(outside, _PIN_START, trailing_anchor=_ANCHOR)

    @pytest.mark.usefixtures("_degrade_mode")
    def test_flag_on_kernel_unavailable_falls_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "fno_math", None)
        folds = _resolve_recency_folds(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)
        assert folds == fold_recency(_RECENCY_TRADES, _PIN_START, trailing_anchor=_ANCHOR)

    def test_recency_wire_pair_round_trips(self) -> None:
        for trades in (
            _RECENCY_TRADES,
            [_rtrade(-400, age_us=731 * _DAY_US)],  # trailing-None shape
        ):
            folds = fold_recency(trades, _PIN_START, trailing_anchor=_ANCHOR)
            assert _recency_folds_from_wire(_recency_wire(folds)) == folds


class TestRecencyFlagOnParity:
    """NON-VACUOUS flag-ON proof over hostile window/bucket edge streams."""

    _EDGE_STREAMS: ClassVar[list[list[ClosedTrade]]] = [
        _RECENCY_TRADES,
        # The cutoff straddle: exactly AT the cutoff instant (IN), 1 µs
        # earlier (OUT).
        [_rtrade(100, age_us=730 * _DAY_US), _rtrade(200, age_us=730 * _DAY_US + 1)],
        # Bucket edges 29/30/31 days + the fractional-day floor.
        [
            _rtrade(10, age_us=29 * _DAY_US),
            _rtrade(20, age_us=30 * _DAY_US - 1),
            _rtrade(30, age_us=30 * _DAY_US),
            _rtrade(40, age_us=31 * _DAY_US),
        ],
        # A defensively-future exit (negative age — clamps to bucket 0).
        [_rtrade(-50, age_us=-1), _rtrade(75, age_us=0)],
        # All outside the window (trailing None).
        [_rtrade(-400, age_us=731 * _DAY_US), _rtrade(900, age_us=3000 * _DAY_US)],
        # A single trade (the smallest legal stream).
        [_rtrade(1, age_us=45 * _DAY_US)],
    ]

    def test_edge_streams_bit_equal_with_kernel_engaged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Widen the burn-in so EVERY stream is kernel-vs-oracle
        # cross-checked inside the seam.
        compute_rust.reset_self_check_for_tests(remaining=10_000)
        calls = [0]
        real = fno_math.metrics_fold_recency

        def _spy(*args: object) -> object:
            calls[0] += 1
            return real(*args)

        monkeypatch.setattr(fno_math, "metrics_fold_recency", _spy)
        for trades in self._EDGE_STREAMS:
            # The oracle leg rides the explicit "0" kill switch (unset is
            # default-ON since DEC-RUST-DEFAULT-ON-WAVE-001).
            monkeypatch.setenv(_FLAG, "0")
            expected = _compute_metrics_impl(
                trades,
                100,
                0.0,
                start_equity_paisa=_PIN_START,
                span_years=2.0,
                drawdown=(0, 0.0, 0, 0.0),
                trailing_anchor=_ANCHOR,
            )
            monkeypatch.setenv(_FLAG, "1")
            actual = _compute_metrics_impl(
                trades,
                100,
                0.0,
                start_equity_paisa=_PIN_START,
                span_years=2.0,
                drawdown=(0, 0.0, 0, 0.0),
                trailing_anchor=_ANCHOR,
            )
            assert _pack_metrics(actual) == _pack_metrics(expected)
        assert calls[0] == len(self._EDGE_STREAMS)
        assert not compute_rust.kernel_disabled()
