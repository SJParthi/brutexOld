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
    routed_freshness_stale,
    routed_gate_fragment,
    routed_liveness_tail,
    routed_preflight_missing,
    routed_progress_derived,
    routed_rank_plan,
    routed_run_path,
    routed_sla_decision,
    routed_stall_timeout,
    sweep_ops_rust_disabled,
    sweep_ops_rust_enabled,
)
from structlog.testing import capture_logs

from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

# ─────────────────────────── helpers / fixtures ───────────────────────────


@pytest.fixture(autouse=True)
def _isolate_facade_state(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Reset the facade latch/burn-in/memo around every test; flag unset."""
    monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
    reset_sweep_ops_rust_for_tests()
    yield
    reset_sweep_ops_rust_for_tests()


def _good_crate() -> SimpleNamespace:
    """A behavior-correct fake crate (the shared §11-COMMON builder)."""
    return build_fake_sweep_ops_crate()


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
        # Slice-1b laws: equally crate-free when the flag is OFF.
        assert routed_progress_derived(
            0, 0, 0, 0.0, 0.0, python_law=lambda: (0.0, 0.0, 0.0, None, 0.0)
        ) == (0.0, 0.0, 0.0, None, 0.0)
        assert routed_sla_decision(
            1, 30.0, 345_600.0, False, python_law=lambda: (30.0, "allowed")
        ) == (30.0, "allowed")
        assert routed_rank_plan(
            1, 2**33, "auto", True, True, python_law=lambda: (1024, "batch")
        ) == (1024, "batch")
        assert routed_preflight_missing({}, [], [], python_law=lambda: ()) == ()
        assert routed_freshness_stale({}, python_law=lambda: ()) == ()
        assert (
            routed_liveness_tail(True, 0.0, 0.0, 600.0, python_law=lambda: "running") == "running"
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


# ───────────── slice 1b — routed_progress_derived law ─────────────

_DERIVED_EXPECTED = (12.5, 10.0, 2.5, 70.0, 0.16)


def _call_progress_derived() -> tuple[float, float, float, float | None, float]:
    return routed_progress_derived(200, 25, 4, 100.0, 110.0, python_law=lambda: _DERIVED_EXPECTED)


class TestRoutedProgressDerived:
    def test_parity_with_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert _call_progress_derived() == _DERIVED_EXPECTED

    def test_parity_idle_snapshot_eta_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        idle = (0.0, 0.0, 0.0, None, 0.0)
        assert routed_progress_derived(0, 0, 0, 0.0, 0.0, python_law=lambda: idle) == idle

    @pytest.mark.parametrize(
        "args",
        [
            (True, 25, 4, 100.0, 110.0),  # bool count (int subclass — lossy)
            (2**53 + 1, 25, 4, 100.0, 110.0),  # count outside the exact band
            (200, 25, 4, 100, 110.0),  # int clock stamp
            (200, 25.0, 4, 100.0, 110.0),  # float count
        ],
    )
    def test_input_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch, args: Any) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            result = routed_progress_derived(*args, python_law=lambda: _DERIVED_EXPECTED)
        assert result == _DERIVED_EXPECTED
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "progress_derived_input_refused"]
        assert len(warns) == 1

    def test_crate_unavailable_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, ImportError("no module"))
        assert _call_progress_derived() == _DERIVED_EXPECTED

    def test_degrade_error_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: object) -> tuple[float, float, float, float | None, float]:
            raise OverflowError("out of i64")

        crate.progress_derived = boom
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_progress_derived() == _DERIVED_EXPECTED
            assert _call_progress_derived() == _DERIVED_EXPECTED
        warns = [e for e in logs if e.get("reason") == "progress_derived_error"]
        assert len(warns) == 1
        assert sweep_ops_rust_disabled() is False  # degrade, not latch

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
    ) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        def boom(*args: object) -> tuple[float, float, float, float | None, float]:
            raise exc("escape")

        crate.progress_derived = boom
        _install_crate(monkeypatch, crate)
        with pytest.raises(exc):
            _call_progress_derived()

    def test_panic_belt_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()

        class WeirdCrateFault(Exception):
            pass

        def boom(*args: object) -> tuple[float, float, float, float | None, float]:
            raise WeirdCrateFault("panic-shaped")

        crate.progress_derived = boom
        _install_crate(monkeypatch, crate)
        assert _call_progress_derived() == _DERIVED_EXPECTED
        assert sweep_ops_rust_disabled() is True

    @pytest.mark.parametrize(
        "result",
        [
            42,  # not a tuple
            (12.5, 10.0, 2.5, 70.0),  # wrong arity
            ("12.5", 10.0, 2.5, 70.0, 0.16),  # non-float percent
            (12.5, 10.0, 2.5, "70", 0.16),  # non-float, non-None eta
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.progress_derived = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_progress_derived() == _DERIVED_EXPECTED
        warns = [e for e in logs if e.get("reason") == "progress_derived_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.progress_derived = lambda *args: (99.9, 10.0, 2.5, 70.0, 0.16)  # wrong percent
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_progress_derived() == _DERIVED_EXPECTED
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_progress_derived"
        assert sweep_ops_rust_disabled() is True

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[float, float, float, float | None, float]:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_progress_derived(200, 25, 4, 100.0, 110.0, python_law=forbidden_oracle)
            == _DERIVED_EXPECTED
        )


# ─────────────── slice 1b — routed_sla_decision law ───────────────


class TestRoutedSlaDecision:
    @pytest.mark.parametrize(
        ("tuple_count", "acknowledge", "expected"),
        [
            (100, False, (3000.0, "allowed")),
            (11_521, False, (345_630.0, "refused")),
            (11_521, True, (345_630.0, "acknowledged")),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tuple_count: int,
        acknowledge: bool,
        expected: tuple[float, str],
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_sla_decision(
                tuple_count, 30.0, 345_600.0, acknowledge, python_law=lambda: expected
            )
            == expected
        )

    @pytest.mark.parametrize(
        "args",
        [
            (True, 30.0, 345_600.0, False),  # bool tuple_count
            (100, 30, 345_600.0, False),  # int per-tuple seconds
            (100, 30.0, 345_600, False),  # int envelope seconds
        ],
    )
    def test_input_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch, args: Any) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = (3000.0, "allowed")
        with capture_logs() as logs:
            result = routed_sla_decision(*args, python_law=lambda: sentinel)
        assert result == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "sla_decision_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            3000.0,  # not a tuple
            (3000.0,),  # wrong arity
            (3000, "allowed"),  # non-float projected
            (3000.0, 7),  # non-str token
            (3000.0, "banana"),  # unknown token
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.sla_decision = lambda *args: result
        _install_crate(monkeypatch, crate)
        sentinel = (3000.0, "allowed")
        with capture_logs() as logs:
            assert (
                routed_sla_decision(100, 30.0, 345_600.0, False, python_law=lambda: sentinel)
                == sentinel
            )
        warns = [e for e in logs if e.get("reason") == "sla_decision_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.sla_decision = lambda *args: (3000.0, "refused")  # wrong verdict
        _install_crate(monkeypatch, crate)
        oracle = (3000.0, "allowed")
        with capture_logs() as logs:
            assert (
                routed_sla_decision(100, 30.0, 345_600.0, False, python_law=lambda: oracle)
                == oracle
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_sla_decision"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[float, str]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_sla_decision(100, 30.0, 345_600.0, False, python_law=forbidden_oracle) == (
            3000.0,
            "allowed",
        )


# ──────────────── slice 1b — routed_rank_plan law ────────────────


class TestRoutedRankPlan:
    @pytest.mark.parametrize(
        ("n_masks", "budget", "mode", "has_top_k", "digest", "expected"),
        [
            (1000, 8 * 1024**2, "auto", True, True, (1_024_000, "batch")),
            (1000, 1, "auto", True, True, (1_024_000, "bounded")),
            (1000, 1, "on", True, True, (1_024_000, "bounded")),
            (1000, 1, "off", True, True, (1_024_000, "refuse_off")),
            (1000, 1, "auto", False, True, (1_024_000, "refuse_no_top_k")),
            (1000, 1, "auto", True, False, (1_024_000, "refuse_no_digest")),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        n_masks: int,
        budget: int,
        mode: str,
        has_top_k: bool,
        digest: bool,
        expected: tuple[int, str],
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_rank_plan(n_masks, budget, mode, has_top_k, digest, python_law=lambda: expected)
            == expected
        )

    @pytest.mark.parametrize(
        "args",
        [
            (True, 1, "auto", True, True),  # bool n_masks
            (1000, 1.0, "auto", True, True),  # float budget
            (1000, 1, "banana", True, True),  # unknown mode token
            (1000, 1, 7, True, True),  # non-str mode
        ],
    )
    def test_input_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch, args: Any) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = (1_024_000, "batch")
        with capture_logs() as logs:
            result = routed_rank_plan(*args, python_law=lambda: sentinel)
        assert result == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "rank_plan_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            1_024_000,  # not a tuple
            (1_024_000,),  # wrong arity
            (True, "batch"),  # bool projected (not exact int)
            (1_024_000.0, "batch"),  # float projected
            (1_024_000, 7),  # non-str verdict
            (1_024_000, "banana"),  # unknown verdict
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.rank_plan_decision = lambda *args: result
        _install_crate(monkeypatch, crate)
        sentinel = (1_024_000, "batch")
        with capture_logs() as logs:
            assert (
                routed_rank_plan(1000, 8 * 1024**2, "auto", True, True, python_law=lambda: sentinel)
                == sentinel
            )
        warns = [e for e in logs if e.get("reason") == "rank_plan_result_refused"]
        assert len(warns) == 1

    def test_bounded_without_top_k_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A `bounded` verdict with no finite top_k is structurally impossible
        from the Python law — a wheel emitting one is refuse-poisoned."""
        _enable(monkeypatch)
        crate = _good_crate()
        crate.rank_plan_decision = lambda *args: (1_024_000, "bounded")
        _install_crate(monkeypatch, crate)
        sentinel = (1_024_000, "refuse_no_top_k")
        with capture_logs() as logs:
            assert (
                routed_rank_plan(1000, 1, "auto", False, True, python_law=lambda: sentinel)
                == sentinel
            )
        warns = [e for e in logs if e.get("reason") == "rank_plan_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.rank_plan_decision = lambda *args: (999, "batch")  # wrong projection
        _install_crate(monkeypatch, crate)
        oracle = (1_024_000, "batch")
        with capture_logs() as logs:
            assert (
                routed_rank_plan(1000, 8 * 1024**2, "auto", True, True, python_law=lambda: oracle)
                == oracle
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_rank_plan"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[int, str]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_rank_plan(
            1000, 8 * 1024**2, "auto", True, True, python_law=forbidden_oracle
        ) == (1_024_000, "batch")


# ──────────── slice 1b — the two preflight fold laws ────────────

_SIZES = {("NSE-NIFTY", "1minute"): 100, ("NSE-NIFTY", "5minute"): 0}
_MISSING_EXPECTED = (("NSE-NIFTY", "5minute"), ("BSE-SENSEX", "1minute"), ("BSE-SENSEX", "5minute"))


def _call_preflight_missing() -> tuple[tuple[str, str], ...]:
    return routed_preflight_missing(
        _SIZES,
        ["NSE-NIFTY", "BSE-SENSEX"],
        ["1minute", "5minute"],
        python_law=lambda: _MISSING_EXPECTED,
    )


class TestRoutedPreflightMissing:
    def test_parity_with_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert _call_preflight_missing() == _MISSING_EXPECTED

    @pytest.mark.parametrize(
        ("sizes", "symbols", "intervals"),
        [
            ({"NSE-NIFTY": 1}, ["A"], ["1minute"]),  # non-tuple mapping key
            ({("NSE-NIFTY", 5): 1}, ["A"], ["1minute"]),  # non-str key member
            ({("A", "1minute"): True}, ["A"], ["1minute"]),  # bool count
            ({("A", "1minute"): 1.0}, ["A"], ["1minute"]),  # float count
            ({("A", "1minute"): 1}, [7], ["1minute"]),  # non-str symbol
            ({("A", "1minute"): 1}, ["A"], [7]),  # non-str interval
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, sizes: Any, symbols: Any, intervals: Any
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = (("A", "1minute"),)
        with capture_logs() as logs:
            result = routed_preflight_missing(
                sizes, symbols, intervals, python_law=lambda: sentinel
            )
        assert result == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "preflight_missing_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            "not-a-list",
            [("a",)],  # wrong pair arity
            [("a", 7)],  # non-str member
            [["a", "b"]],  # list item, not tuple
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.preflight_missing = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_preflight_missing() == _MISSING_EXPECTED
        warns = [e for e in logs if e.get("reason") == "preflight_missing_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.preflight_missing = lambda *args: []  # wrongly claims nothing missing
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_preflight_missing() == _MISSING_EXPECTED
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_preflight_missing"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[tuple[str, str], ...]:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_preflight_missing(
                _SIZES,
                ["NSE-NIFTY", "BSE-SENSEX"],
                ["1minute", "5minute"],
                python_law=forbidden_oracle,
            )
            == _MISSING_EXPECTED
        )


_VERDICTS = {("Z", "5minute"): "stale", ("A", "1day"): "fresh", ("A", "5minute"): "weird"}
_STALE_EXPECTED = (("A", "5minute"), ("Z", "5minute"))


class TestRoutedFreshnessStale:
    def test_parity_with_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_freshness_stale(_VERDICTS, python_law=lambda: _STALE_EXPECTED) == _STALE_EXPECTED
        )

    @pytest.mark.parametrize(
        "verdicts",
        [
            {"A": "fresh"},  # non-tuple mapping key
            {("A", 5): "fresh"},  # non-str key member
            {("A", "1day"): 7},  # non-str verdict
        ],
    )
    def test_input_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch, verdicts: Any) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = (("A", "1day"),)
        with capture_logs() as logs:
            result = routed_freshness_stale(verdicts, python_law=lambda: sentinel)
        assert result == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "freshness_stale_input_refused"]
        assert len(warns) == 1

    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.freshness_stale = lambda *args: [("a", 7)]
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_freshness_stale(_VERDICTS, python_law=lambda: _STALE_EXPECTED)
                == _STALE_EXPECTED
            )
        warns = [e for e in logs if e.get("reason") == "freshness_stale_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.freshness_stale = lambda *args: []  # wrongly claims all fresh
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_freshness_stale(_VERDICTS, python_law=lambda: _STALE_EXPECTED)
                == _STALE_EXPECTED
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_freshness_stale"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[tuple[str, str], ...]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_freshness_stale(_VERDICTS, python_law=forbidden_oracle) == _STALE_EXPECTED


# ─────────────── slice 1b — routed_liveness_tail law ───────────────


class TestRoutedLivenessTail:
    @pytest.mark.parametrize(
        ("pid_ok", "now_wall", "expected"),
        [
            (False, 1000.0, "not_running"),
            (True, 1601.0, "stale"),
            (True, 1000.5, "running"),
        ],
    )
    def test_parity_with_oracle(
        self, monkeypatch: pytest.MonkeyPatch, pid_ok: bool, now_wall: float, expected: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_liveness_tail(pid_ok, now_wall, 1000.0, 600.0, python_law=lambda: expected)
            == expected
        )

    @pytest.mark.parametrize(
        "args",
        [
            (1, 1000.0, 999.0, 600.0),  # non-bool pid_ok
            (True, 1000, 999.0, 600.0),  # int now_wall
            (True, 1000.0, 999.0, 600),  # int fresh_seconds
        ],
    )
    def test_input_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch, args: Any) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            result = routed_liveness_tail(*args, python_law=lambda: "running")
        assert result == "running"
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "liveness_tail_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize("result", [42, "banana"])
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.liveness_tail = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_liveness_tail(True, 1000.5, 1000.0, 600.0, python_law=lambda: "running")
                == "running"
            )
        warns = [e for e in logs if e.get("reason") == "liveness_tail_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.liveness_tail = lambda *args: "running"  # wrong for a stale payload
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_liveness_tail(True, 1601.0, 1000.0, 600.0, python_law=lambda: "stale")
                == "stale"
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_liveness_tail"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_liveness_tail(True, 1000.5, 1000.0, 600.0, python_law=forbidden_oracle) == (
            "running"
        )


# ────────────────── slice 1b — drift + surface pins ──────────────────


class TestSliceOneBDriftPins:
    def test_fake_crate_carries_every_handshake_symbol(self) -> None:
        """A fake missing a `_CRATE_API` symbol turns routing tests VACUOUS
        (crate_stale latch → oracle serves) — pin the shared builder's
        surface so extending the handshake without extending the fake fails
        loudly here instead."""
        crate = _good_crate()
        missing = [
            name for name in sweep_ops_rust._CRATE_API if not callable(getattr(crate, name, None))
        ]
        assert missing == []

    def test_sla_tokens_mirror_enum(self) -> None:
        from brutex.engine.sweep_sla_guard import SlaDecision

        assert {d.value for d in SlaDecision} == sweep_ops_rust._SLA_DECISION_TOKENS

    def test_rank_mode_tokens_mirror_enum(self) -> None:
        from brutex.engine.sweep_mem_budget import BoundedRankMode

        assert {m.value for m in BoundedRankMode} == sweep_ops_rust._RANK_MODE_TOKENS

    def test_liveness_tokens_mirror_enum_minus_unknown(self) -> None:
        from brutex.engine.sweep_liveness import RunVerdict

        assert {v.value for v in RunVerdict} - {"unknown"} == sweep_ops_rust._LIVENESS_TAIL_TOKENS

    def test_all_laws_key_every_burn_in_budget(self) -> None:
        assert set(sweep_ops_rust._ALL_LAWS) == set(sweep_ops_rust._BURN_IN_REMAINING)

    def test_exact_count_band_is_2_pow_53(self) -> None:
        assert sweep_ops_rust._MAX_EXACT_COUNT == 2**53
