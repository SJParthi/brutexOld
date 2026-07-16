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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from brutex.metrics import compute_rust
from brutex.metrics.compute import (
    Moments,
    TradeFolds,
    _compute_metrics_impl,
    _fold_trades,
    _fold_wire,
    _folds_from_wire,
    _resolve_folds,
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

    def test_unset_falls_to_default_off(self) -> None:
        assert _FLAG not in os.environ
        assert compute_rust.metrics_rust_fold_enabled() is False
        assert compute_rust.METRICS_RUST_FOLD_DEFAULT is False

    def test_whitespace_empty_falls_to_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "   ")
        assert compute_rust.metrics_rust_fold_enabled() is False

    def test_junk_is_off_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "ture")
        with capture_logs() as logs:
            assert compute_rust.metrics_rust_fold_enabled() is False
            assert compute_rust.metrics_rust_fold_enabled() is False
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "ture"


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

    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        with capture_logs() as logs:
            assert compute_rust._resolve_kernel() is None
            assert compute_rust._resolve_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "wheel_missing"

    def test_wheel_stale_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", types.SimpleNamespace())
        with capture_logs() as logs:
            assert compute_rust._resolve_kernel() is None
        warns = _events(logs, "metrics_rust_fold.fallback")
        assert len(warns) == 1
        assert warns[0]["reason"] == "wheel_stale"


class TestFoldViaKernelDegrades:
    """Every kernel-call fault serves the row from the Python oracle.

    Every degrade arm passes ``oracle_wire=_oracle_bomb`` — the injected
    oracle is only ever evaluated inside a PASSED-structural-gate burn-in
    compare, so a degrade path must never touch it (pinned here).
    """

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

    def test_wheel_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert (
            compute_rust.fold_wire_via_kernel(_PIN_TRADES, _PIN_START, oracle_wire=_oracle_bomb)
            is None
        )

    @pytest.mark.parametrize(
        "exc", [ValueError("boundary"), TypeError("conversion"), RuntimeError("poisoned")]
    )
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

    def test_default_path_never_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag unset (the default): the kernel seam is provably untouched."""

        def _seam_bomb(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the default path must never consult the kernel seam")

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
        prior = os.environ.pop(_FLAG, None)
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
