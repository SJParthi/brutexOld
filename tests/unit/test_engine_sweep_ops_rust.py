"""Wheel-free unit matrix for `brutex/engine/sweep_ops_rust.py` (DEC-RUST-SWEEP-OPS-001).

Drives the facade to 100% line+branch WITHOUT the ``brutex_sweep_ops`` wheel:
every engaged path runs against a FAKE crate injected by patching the
facade's ``importlib`` name binding (deterministic whether or not the real
wheel is installed — the injected resolver shadows it). Real-wheel parity
lives in ``tests/integration/test_sweep_ops_rust_parity.py``.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.engine import sweep_ops_rust
from brutex.engine.sweep_ops_rust import (
    BURN_IN_SAMPLES,
    SWEEP_OPS_RUST_ENV,
    reset_sweep_ops_rust_for_tests,
    routed_gate_fragment,
    routed_run_path,
    routed_stall_timeout,
    sweep_ops_rust_disabled,
    sweep_ops_rust_enabled,
)
from structlog.testing import capture_logs

# ─────────────────────────── helpers / fixtures ───────────────────────────


@pytest.fixture(autouse=True)
def _isolate_facade_state(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Reset the facade latch/burn-in/memo around every test; flag unset."""
    monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
    reset_sweep_ops_rust_for_tests()
    yield
    reset_sweep_ops_rust_for_tests()


def _good_crate() -> SimpleNamespace:
    """A behavior-correct fake crate (the Python laws re-implemented)."""

    def join_path_segments(base: str, segments: list[str]) -> str:
        acc = base
        for segment in segments:
            if acc and not acc.endswith("/"):
                acc += "/"
            acc += segment
        return acc

    def gate_config_fragment(
        overfit: tuple[float, float, float, float] | None,
        stage3_enable: bool,
        top_k: int,
        max_fevals: int,
        max_restarts: int,
    ) -> tuple[Any, Any]:
        stage3 = (top_k, max_fevals, max_restarts) if stage3_enable else None
        return (overfit, stage3)

    def stall_scaled_timeout(factor: float, estimate: float, base: float) -> tuple[float, bool]:
        scaled = factor * estimate
        if scaled > base:
            return (scaled, True)
        return (base, False)

    return SimpleNamespace(
        __version__="0.1.0-fake",
        join_path_segments=join_path_segments,
        gate_config_fragment=gate_config_fragment,
        stall_scaled_timeout=stall_scaled_timeout,
    )


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Patch the facade's importlib binding to resolve *crate* (or raise)."""

    def import_module(name: str) -> object:
        assert name == "brutex_sweep_ops"
        if isinstance(crate, BaseException) or (
            isinstance(crate, type) and issubclass(crate, BaseException)
        ):
            raise crate  # type: ignore[misc]
        return crate

    monkeypatch.setattr(sweep_ops_rust, "importlib", SimpleNamespace(import_module=import_module))


def _install_import_bomb(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch importlib so ANY crate import records + fails the test."""
    calls: list[str] = []

    def import_module(name: str) -> object:
        calls.append(name)
        raise AssertionError("the crate must never be imported on this path")

    monkeypatch.setattr(sweep_ops_rust, "importlib", SimpleNamespace(import_module=import_module))
    return calls


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")


def _run_path_oracle() -> Path:
    return Path("/lake/sweeps") / "runs" / "abc"


def _call_run_path() -> Path:
    return routed_run_path("/lake/sweeps", ("runs", "abc"), python_law=_run_path_oracle)


# ───────────────────────────── the flag law ─────────────────────────────


class TestFlagLaw:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_enables(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, raw)
        assert sweep_ops_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
    def test_falsy_disables(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, raw)
        assert sweep_ops_rust_enabled() is False

    def test_unset_is_off(self) -> None:
        assert sweep_ops_rust_enabled() is False

    def test_blank_is_off_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "   ")
        with capture_logs() as logs:
            assert sweep_ops_rust_enabled() is False
        assert logs == []

    def test_junk_is_off_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert sweep_ops_rust_enabled() is False
            assert sweep_ops_rust_enabled() is False  # repeat is silent
        warns = [e for e in logs if e["event"] == "sweep_ops_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_off_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _install_import_bomb(monkeypatch)
        assert _call_run_path() == _run_path_oracle()
        assert routed_gate_fragment(None, False, (1, 2, 3), python_law=lambda: None) is None
        assert routed_stall_timeout(10.0, 1.0, 300.0, python_law=lambda: (300.0, False)) == (
            300.0,
            False,
        )
        assert calls == []

    def test_disabled_latch_predicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert sweep_ops_rust_disabled() is False
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no wheel"))
        _call_run_path()
        assert sweep_ops_rust_disabled() is True
        # Latched: the very next call short-circuits to the oracle without
        # a resolve attempt (the import bomb proves no second import).
        _install_import_bomb(monkeypatch)
        assert _call_run_path() == _run_path_oracle()


# ─────────────────────── import guard / handshake ───────────────────────


class TestCrateResolution:
    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no module"))
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
        disabled = [e for e in logs if e["event"] == "sweep_ops_rust.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_unavailable"

    def test_stale_wheel_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        del crate.stall_scaled_timeout  # a pre-slice-1 install
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
        disabled = [e for e in logs if e["event"] == "sweep_ops_rust.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_stale"

    def test_disable_repeat_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no module"))
        with capture_logs() as logs:
            _call_run_path()
            # The latch holds; a second disable attempt logs nothing new.
            sweep_ops_rust._disable("some_other_reason")
        disabled = [e for e in logs if e["event"] == "sweep_ops_rust.rust_path_disabled"]
        assert len(disabled) == 1

    def test_engaged_info_logged_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        with capture_logs() as logs:
            _call_run_path()
            _call_run_path()
        engaged = [e for e in logs if e["event"] == "sweep_ops_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"


# ───────────────────────── routed_run_path law ─────────────────────────


class TestRoutedRunPath:
    def test_parity_with_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert _call_run_path() == _run_path_oracle()

    @pytest.mark.parametrize(
        ("base", "segments"),
        [
            (Path("/lake"), ("runs", "abc")),  # non-str base (Path)
            ("/lake", ("runs", 7)),  # non-str segment
            ("/lake", ("runs", "")),  # empty segment
            ("/lake", ("ru/ns", "abc")),  # slash in segment
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, base: Any, segments: Any
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            result = routed_run_path(base, segments, python_law=_run_path_oracle)
        assert result == _run_path_oracle()
        assert calls == []  # refused BEFORE any crate import
        warns = [e for e in logs if e.get("reason") == "run_path_input_refused"]
        assert len(warns) == 1

    def test_degrade_error_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(base: str, segments: list[str]) -> str:
            raise ValueError("bad args")

        crate.join_path_segments = boom
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
            assert _call_run_path() == _run_path_oracle()
        warns = [e for e in logs if e.get("reason") == "run_path_error"]
        assert len(warns) == 1
        assert sweep_ops_rust_disabled() is False  # degrade, not latch

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(base: str, segments: list[str]) -> str:
            raise exc("escape")

        crate.join_path_segments = boom
        _install_crate(monkeypatch, crate)
        with pytest.raises(exc):
            _call_run_path()

    def test_panic_belt_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        class WeirdCrateFault(Exception):
            """Not in the narrow degrade set — the panic-belt Exception arm."""

        def boom(base: str, segments: list[str]) -> str:
            raise WeirdCrateFault("panic-shaped")

        crate.join_path_segments = boom
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
        disabled = [e for e in logs if e["event"] == "sweep_ops_rust.rust_path_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "crate_panic"
        assert sweep_ops_rust_disabled() is True

    def test_nonstr_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.join_path_segments = lambda base, segments: 42
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
        warns = [e for e in logs if e.get("reason") == "run_path_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.join_path_segments = lambda base, segments: "/wrong/place"
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_run_path() == _run_path_oracle()
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert len(reverts) == 1
        assert reverts[0]["reason"] == "burn_in_mismatch_run_path"
        assert sweep_ops_rust_disabled() is True
        # Latched forever after: the oracle serves.
        assert _call_run_path() == _run_path_oracle()

    def test_burn_in_completes_then_steady_state_skips_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        oracle_calls: list[int] = []

        def counting_oracle() -> Path:
            oracle_calls.append(1)
            return _run_path_oracle()

        with capture_logs() as logs:
            for _ in range(BURN_IN_SAMPLES):
                assert (
                    routed_run_path("/lake/sweeps", ("runs", "abc"), python_law=counting_oracle)
                    == _run_path_oracle()
                )
        complete = [e for e in logs if e["event"] == "sweep_ops_rust.burn_in_complete"]
        assert len(complete) == 1
        assert complete[0]["law"] == "run_path"
        assert len(oracle_calls) == BURN_IN_SAMPLES
        # Steady state: the oracle is no longer consulted.
        assert (
            routed_run_path("/lake/sweeps", ("runs", "abc"), python_law=counting_oracle)
            == _run_path_oracle()
        )
        assert len(oracle_calls) == BURN_IN_SAMPLES

    def test_steady_state_via_reset_remaining_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> Path:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_run_path("/lake/sweeps", ("runs", "abc"), python_law=forbidden_oracle)
            == _run_path_oracle()
        )


# ─────────────────────── routed_gate_fragment law ───────────────────────


_OVERFIT = (0.95, 0.05, 0.0, 0.0)


def _fragment_oracle_both() -> dict[str, Any]:
    return {"overfit": list(_OVERFIT), "stage3": [10, 200, 3]}


class TestRoutedGateFragment:
    @pytest.mark.parametrize(
        ("overfit", "stage3_enable", "knobs", "expected"),
        [
            (None, False, (1, 2, 3), None),
            (_OVERFIT, False, (1, 2, 3), {"overfit": list(_OVERFIT)}),
            (None, True, (10, 200, 3), {"stage3": [10, 200, 3]}),
            (_OVERFIT, True, (10, 200, 3), _fragment_oracle_both()),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        overfit: tuple[float, float, float, float] | None,
        stage3_enable: bool,
        knobs: tuple[int, int, int],
        expected: dict[str, Any] | None,
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        result = routed_gate_fragment(overfit, stage3_enable, knobs, python_law=lambda: expected)
        assert result == expected

    def test_nonfloat_overfit_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = {"overfit": [1, 0.05, 0.0, 0.0]}
        with capture_logs() as logs:
            result = routed_gate_fragment(
                (1, 0.05, 0.0, 0.0),  # int dsr_min — lossy across f64
                False,
                (1, 2, 3),
                python_law=lambda: sentinel,
            )
        assert result is sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "gate_fragment_input_refused"]
        assert warns[0]["leg"] == "overfit"

    def test_bool_knob_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            result = routed_gate_fragment(
                None, True, (True, 2, 3), python_law=lambda: {"stage3": [True, 2, 3]}
            )
        assert result == {"stage3": [True, 2, 3]}
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "gate_fragment_input_refused"]
        assert warns[0]["leg"] == "stage3"

    def test_crate_unavailable_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no module"))
        assert routed_gate_fragment(None, False, (1, 2, 3), python_law=lambda: None) is None

    def test_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: object) -> tuple[Any, Any]:
            raise OverflowError("out of i64")

        crate.gate_config_fragment = boom
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            result = routed_gate_fragment(
                None, True, (2**70, 2, 3), python_law=lambda: {"stage3": [2**70, 2, 3]}
            )
        assert result == {"stage3": [2**70, 2, 3]}
        warns = [e for e in logs if e.get("reason") == "gate_fragment_error"]
        assert len(warns) == 1

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: object) -> tuple[Any, Any]:
            raise exc("escape")

        crate.gate_config_fragment = boom
        _install_crate(monkeypatch, crate)
        with pytest.raises(exc):
            routed_gate_fragment(None, False, (1, 2, 3), python_law=lambda: None)

    def test_panic_belt_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        class WeirdCrateFault(Exception):
            pass

        def boom(*args: object) -> tuple[Any, Any]:
            raise WeirdCrateFault("panic-shaped")

        crate.gate_config_fragment = boom
        _install_crate(monkeypatch, crate)
        assert routed_gate_fragment(None, False, (1, 2, 3), python_law=lambda: None) is None
        assert sweep_ops_rust_disabled() is True

    @pytest.mark.parametrize(
        "legs",
        [
            "not-a-tuple",  # whole result malformed → TypeError unpack → degrade
            ((0.95, 0.05, 0.0), None),  # overfit leg wrong arity
            (("a", "b", "c", "d"), None),  # overfit leg non-float members
            ([0.95, 0.05, 0.0, 0.0], None),  # overfit leg non-tuple
            (None, (1, 2)),  # stage3 leg wrong arity
            (None, (1, 2, True)),  # stage3 leg bool member
            (None, [1, 2, 3]),  # stage3 leg non-tuple
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, legs: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.gate_config_fragment = lambda *args: legs
        _install_crate(monkeypatch, crate)
        sentinel = {"overfit": list(_OVERFIT)}
        result = routed_gate_fragment(_OVERFIT, False, (1, 2, 3), python_law=lambda: sentinel)
        assert result is sentinel

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        # Well-shaped but WRONG: drops the stage3 leg.
        crate.gate_config_fragment = lambda *args: (None, None)
        _install_crate(monkeypatch, crate)
        oracle = {"stage3": [10, 200, 3]}
        with capture_logs() as logs:
            result = routed_gate_fragment(None, True, (10, 200, 3), python_law=lambda: oracle)
        assert result is oracle
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_gate_fragment"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> dict[str, Any] | None:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_gate_fragment(_OVERFIT, True, (10, 200, 3), python_law=forbidden_oracle)
            == _fragment_oracle_both()
        )


# ─────────────────────── routed_stall_timeout law ───────────────────────


class TestRoutedStallTimeout:
    @pytest.mark.parametrize(
        ("factor", "estimate", "expected"),
        [
            (10.0, 60.0, (600.0, True)),
            (10.0, 30.0, (300.0, False)),  # exact-boundary → default arm
            (10.0, 1.0, (300.0, False)),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        factor: float,
        estimate: float,
        expected: tuple[float, bool],
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_stall_timeout(factor, estimate, 300.0, python_law=lambda: expected) == expected
        )

    def test_nonfloat_input_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            result = routed_stall_timeout(10.0, 60, 300.0, python_law=lambda: (600.0, True))
        assert result == (600.0, True)
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "stall_timeout_input_refused"]
        assert len(warns) == 1

    def test_crate_unavailable_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no module"))
        assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True)) == (
            600.0,
            True,
        )

    def test_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: float) -> tuple[float, bool]:
            raise RuntimeError("crate fault")

        crate.stall_scaled_timeout = boom
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True)) == (
                600.0,
                True,
            )
        warns = [e for e in logs if e.get("reason") == "stall_timeout_error"]
        assert len(warns) == 1

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: float) -> tuple[float, bool]:
            raise exc("escape")

        crate.stall_scaled_timeout = boom
        _install_crate(monkeypatch, crate)
        with pytest.raises(exc):
            routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True))

    def test_panic_belt_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        class WeirdCrateFault(Exception):
            pass

        def boom(*args: float) -> tuple[float, bool]:
            raise WeirdCrateFault("panic-shaped")

        crate.stall_scaled_timeout = boom
        _install_crate(monkeypatch, crate)
        assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True)) == (
            600.0,
            True,
        )
        assert sweep_ops_rust_disabled() is True

    @pytest.mark.parametrize(
        "result",
        [
            600.0,  # not a tuple
            (600.0,),  # wrong arity
            ("600", True),  # non-float timeout
            (600.0, 1),  # non-bool flag
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.stall_scaled_timeout = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True)) == (
                600.0,
                True,
            )
        warns = [e for e in logs if e.get("reason") == "stall_timeout_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.stall_scaled_timeout = lambda *args: (999.0, True)  # wrong value
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=lambda: (600.0, True)) == (
                600.0,
                True,
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_stall_timeout"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[float, bool]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_stall_timeout(10.0, 60.0, 300.0, python_law=forbidden_oracle) == (600.0, True)
