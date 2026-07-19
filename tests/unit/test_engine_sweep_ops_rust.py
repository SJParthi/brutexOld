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
    routed_audit_entry_hash,
    routed_chain_link_state,
    routed_chain_verify_step,
    routed_checkpoint_adoption_verdict,
    routed_checkpoint_record_decision,
    routed_count_violations,
    routed_exit_status_resolution,
    routed_freshness_stale,
    routed_gate_fragment,
    routed_liveness_tail,
    routed_preflight_missing,
    routed_progress_derived,
    routed_quarantine_next_ordinal,
    routed_rank_plan,
    routed_run_path,
    routed_sla_decision,
    routed_stall_timeout,
    routed_stamp_verdict,
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
        # Slice-2 laws: equally crate-free when the flag is OFF.
        assert (
            routed_audit_entry_hash("0", "p", "1.0", "a", "B", "c", "{}", python_law=lambda: "h")
            == "h"
        )
        assert routed_chain_verify_step(
            0, "p", 0, "p", "1.0", "a", "B", "c", "{}", "h", python_law=lambda: ("ok", "h", "")
        ) == ("ok", "h", "")
        assert (
            routed_stamp_verdict("h", "h", "s", "s", "r", None, None, None, python_law=lambda: "ok")
            == "ok"
        )
        assert routed_chain_link_state(
            "sweep", 1, None, False, None, None, None, None, python_law=lambda: ("pending", "d")
        ) == ("pending", "d")
        assert routed_count_violations(1, 1, 0, 0, 0, 0, None, False, python_law=lambda: "") == ""
        # Slice-3 laws: equally crate-free when the flag is OFF.
        assert routed_exit_status_resolution(
            "running", None, 1.0, None, None, python_law=lambda: (1.0, None, False)
        ) == (1.0, None, False)
        assert routed_quarantine_next_ordinal([], "run-", "", python_law=lambda: 1) == 1
        assert (
            routed_checkpoint_record_decision(0, -1, 10, python_law=lambda: "advance") == "advance"
        )
        assert (
            routed_checkpoint_adoption_verdict("a", 10, "a", 10, python_law=lambda: "adopt")
            == "adopt"
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


# ─────────────── slice 2 — routed_audit_entry_hash law ───────────────

#: The cross-language BLAKE3 golden (pip blake3 == the crate — the same
#: literal is pinned in cargo `audit_chain::tests` and the parity suite).
_G1_FIELDS = (
    "0",
    "0" * 64,
    "1700000000.5",
    "sweep",
    "SWEEP_START",
    "sweep:abc",
    '{"total_combos":22100}',
)
_G1_HASH = "ab1df54e0d8428288ece95f9b5e462f4af17beb3046ea429c6ee86d67e33ff2e"


class TestRoutedAuditEntryHash:
    def test_parity_with_oracle_and_golden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_audit_entry_hash(*_G1_FIELDS, python_law=lambda: _G1_HASH) == _G1_HASH

    @pytest.mark.parametrize("bad_index", range(7))
    def test_nonstr_field_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, bad_index: int
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        fields: list[Any] = list(_G1_FIELDS)
        fields[bad_index] = b"bytes"
        with capture_logs() as logs:
            assert routed_audit_entry_hash(*fields, python_law=lambda: "oracle") == "oracle"
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "audit_entry_hash_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            7,  # not a str
            "ab",  # wrong length
            "G" * 64,  # non-hex chars
            _G1_HASH.upper(),  # uppercase hex refused (the oracle is lowercase)
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.audit_entry_hash = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_audit_entry_hash(*_G1_FIELDS, python_law=lambda: _G1_HASH) == _G1_HASH
        warns = [e for e in logs if e.get("reason") == "audit_entry_hash_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.audit_entry_hash = lambda *args: "f" * 64  # well-shaped wrong digest
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_audit_entry_hash(*_G1_FIELDS, python_law=lambda: _G1_HASH) == _G1_HASH
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_audit_entry_hash"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_audit_entry_hash(*_G1_FIELDS, python_law=forbidden_oracle) == _G1_HASH


# ─────────────── slice 2 — routed_chain_verify_step law ───────────────


def _ok_step_args() -> tuple[Any, ...]:
    return (0, "0" * 64, 0, "0" * 64, *_G1_FIELDS[2:], _G1_HASH)


class TestRoutedChainVerifyStep:
    def test_parity_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        expected = ("ok", _G1_HASH, "")
        assert routed_chain_verify_step(*_ok_step_args(), python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        ("mutate", "token"),
        [
            ({"seq": 5}, "seq_out_of_order"),
            ({"prev_hash": "x" * 64}, "prev_hash_mismatch"),
            ({"entry_hash": "f" * 64}, "entry_hash_mismatch"),
        ],
    )
    def test_parity_failure_tokens(
        self, monkeypatch: pytest.MonkeyPatch, mutate: dict[str, Any], token: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        names = (
            "expected_seq",
            "prev_expected",
            "seq",
            "prev_hash",
            "ts_str",
            "actor",
            "action",
            "subject",
            "payload_json",
            "entry_hash",
        )
        args: dict[str, Any] = dict(zip(names, _ok_step_args(), strict=True))
        args.update(mutate)
        ordered = tuple(args[name] for name in names)
        from brutex.engine.sweep_audit import _verify_step_python

        oracle = _verify_step_python(*ordered)
        assert oracle[0] == token
        assert routed_chain_verify_step(*ordered, python_law=lambda: oracle) == oracle

    @pytest.mark.parametrize(
        "mutate",
        [
            {"seq": True},  # bool seq (str(True) != Rust "true")
            {"expected_seq": 1.0},  # float expected_seq
            {"actor": b"sweep"},  # non-str field
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, mutate: dict[str, Any]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        names = (
            "expected_seq",
            "prev_expected",
            "seq",
            "prev_hash",
            "ts_str",
            "actor",
            "action",
            "subject",
            "payload_json",
            "entry_hash",
        )
        args: dict[str, Any] = dict(zip(names, _ok_step_args(), strict=True))
        args.update(mutate)
        ordered = tuple(args[name] for name in names)
        sentinel = ("ok", "h", "")
        with capture_logs() as logs:
            assert routed_chain_verify_step(*ordered, python_law=lambda: sentinel) == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "chain_verify_step_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            "ok",  # not a tuple
            ("ok", "h"),  # wrong arity
            ("ok", "h", 7),  # non-str member
            ("banana", "h", ""),  # unknown token
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.chain_verify_step = lambda *args: result
        _install_crate(monkeypatch, crate)
        sentinel = ("ok", _G1_HASH, "")
        with capture_logs() as logs:
            assert routed_chain_verify_step(*_ok_step_args(), python_law=lambda: sentinel) == (
                sentinel
            )
        warns = [e for e in logs if e.get("reason") == "chain_verify_step_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.chain_verify_step = lambda *args: ("entry_hash_mismatch", "a", "b")
        _install_crate(monkeypatch, crate)
        oracle = ("ok", _G1_HASH, "")
        with capture_logs() as logs:
            assert routed_chain_verify_step(*_ok_step_args(), python_law=lambda: oracle) == oracle
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_chain_verify_step"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[str, str, str]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_chain_verify_step(*_ok_step_args(), python_law=forbidden_oracle) == (
            "ok",
            _G1_HASH,
            "",
        )


# ─────────────── slice 2 — routed_stamp_verdict law ───────────────


class TestRoutedStampVerdict:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("h", "h", "s", "s", "r", None, None, None), "ok"),
            (("h1", "h2", "s", "s", "r", None, None, None), "hash_mismatch"),
            (("h", "h", "s1", "s2", "r", None, None, None), "hash_mismatch"),
            (("h", "h", "s", "s", "r1", "r2", None, None), "hash_mismatch"),
            (("h", "h", "s", "s", "r", None, "d1", "d2"), "hash_mismatch"),
            (("h", "h", "s", "s", "r", None, "d1", None), "ok"),  # G7 one-sided
            (("h", "h", "s", "s", "r", None, None, "d2"), "ok"),  # G7 one-sided
        ],
    )
    def test_parity_with_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...], expected: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_stamp_verdict(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (b"h", "h", "s", "s", "r", None, None, None),  # non-str mandatory
            ("h", "h", "s", "s", "r", 7, None, None),  # non-str optional
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            assert routed_stamp_verdict(*args, python_law=lambda: "ok") == "ok"
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "stamp_verdict_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize("result", [7, "missing_stamp", "banana"])
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.stamp_verdict = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_stamp_verdict(
                    "h", "h", "s", "s", "r", None, None, None, python_law=lambda: "ok"
                )
                == "ok"
            )
        warns = [e for e in logs if e.get("reason") == "stamp_verdict_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.stamp_verdict = lambda *args: "hash_mismatch"  # wrong verdict
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_stamp_verdict(
                    "h", "h", "s", "s", "r", None, None, None, python_law=lambda: "ok"
                )
                == "ok"
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_stamp_verdict"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_stamp_verdict(
                "h", "h", "s", "s", "r", None, None, None, python_law=forbidden_oracle
            )
            == "ok"
        )


# ─────────────── slice 2 — routed_chain_link_state law ───────────────


class TestRoutedChainLinkState:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (
                ("sweep", 1, "hash_mismatch", False, None, None, None, None),
                ("red", "stamp hash_mismatch"),
            ),
            (("stage3", 2, None, True, "stage3", None, None, None), ("running", "in progress")),
            (("sweep", 1, "ok", False, None, None, None, None), ("green", "stamp valid")),
            (
                ("cascade", 3, None, True, None, None, None, None),
                ("amber", "running; stage unknown"),
            ),
            (
                ("preflight", 0, None, False, None, "refused", "too big", None),
                ("red", "refused: too big"),
            ),
            (
                ("stage3", 2, None, False, "stage3", "halted", None, "OSError"),
                ("red", "run halted at this stage (OSError)"),
            ),
            (("preflight", 0, None, False, None, None, None, None), ("pending", "no evidence")),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        args: tuple[Any, ...],
        expected: tuple[str, str],
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_chain_link_state(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (7, 1, None, False, None, None, None, None),  # non-str link
            ("sweep", True, None, False, None, None, None, None),  # bool idx
            ("sweep", 1, None, 1, None, None, None, None),  # non-bool running
            ("sweep", 1, "banana", False, None, None, None, None),  # unknown verdict
            ("sweep", 1, None, False, "nope", None, None, None),  # foreign last_stage
            ("sweep", 1, None, False, None, 7, None, None),  # non-str terminal
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = ("pending", "no evidence")
        with capture_logs() as logs:
            assert routed_chain_link_state(*args, python_law=lambda: sentinel) == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "chain_link_state_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            "pending",  # not a tuple
            ("pending",),  # wrong arity
            (7, "detail"),  # non-str state
            ("pending", 7),  # non-str detail
            ("banana", "detail"),  # unknown state token
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.chain_link_state = lambda *args: result
        _install_crate(monkeypatch, crate)
        sentinel = ("pending", "no evidence")
        with capture_logs() as logs:
            assert (
                routed_chain_link_state(
                    "sweep", 1, None, False, None, None, None, None, python_law=lambda: sentinel
                )
                == sentinel
            )
        warns = [e for e in logs if e.get("reason") == "chain_link_state_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.chain_link_state = lambda *args: ("red", "wrong")
        _install_crate(monkeypatch, crate)
        oracle = ("pending", "no evidence")
        with capture_logs() as logs:
            assert (
                routed_chain_link_state(
                    "sweep", 1, None, False, None, None, None, None, python_law=lambda: oracle
                )
                == oracle
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_chain_link_state"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[str, str]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_chain_link_state(
            "sweep", 1, None, False, None, None, None, None, python_law=forbidden_oracle
        ) == ("pending", "no evidence")


# ─────────────── slice 2 — routed_count_violations law ───────────────


class TestRoutedCountViolations:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ((10, 10, 4, 1, 2, 3, 3, False), ""),
            ((10, 11, 4, 0, 0, 0, None, False), "snapshot enumerated 11 != armed total 10"),
            ((10, 10, 8, 0, 0, 5, None, False), "evaluated 8 + in_flight 5 > enumerated 10"),
            (
                (10, 10, 4, 5, 0, 0, 9, True),
                "skipped 5 > evaluated 4 (skipped ⊆ evaluated); "
                "evaluated went backwards: 4 < 9; "
                "run done but evaluated 4 != enumerated 10",
            ),
        ],
    )
    def test_parity_with_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...], expected: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_count_violations(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (True, 10, 4, 0, 0, 0, None, False),  # bool counter
            (10, 10, 4, 0, 0, 0, 1.5, False),  # non-int prev_evaluated
            (10, 10, 4, 0, 0, 0, None, 1),  # non-bool is_done
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            assert routed_count_violations(*args, python_law=lambda: "") == ""
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "count_violations_input_refused"]
        assert len(warns) == 1

    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.count_reconciliation = lambda *args: None  # bare None is refused
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_count_violations(10, 10, 4, 0, 0, 0, None, False, python_law=lambda: "")
                == ""
            )
        warns = [e for e in logs if e.get("reason") == "count_violations_result_refused"]
        assert len(warns) == 1

    def test_overflow_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The crate's ONE checked arithmetic maps an i64 overflow to
        # ValueError — the facade degrades (the real crate does this; the
        # fake mirrors it here via a raising stand-in).
        _enable(monkeypatch)
        crate = _good_crate()

        def raising(*args: Any) -> str:
            raise ValueError("evaluated + in_flight overflows i64")

        crate.count_reconciliation = raising
        _install_crate(monkeypatch, crate)
        oracle = "evaluated 9223372036854775807 + in_flight 1 > enumerated 1"
        with capture_logs() as logs:
            assert (
                routed_count_violations(
                    1, 1, 2**63 - 1, 0, 0, 1, None, False, python_law=lambda: oracle
                )
                == oracle
            )
        warns = [e for e in logs if e.get("reason") == "count_violations_error"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.count_reconciliation = lambda *args: "phantom violation"
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_count_violations(10, 10, 4, 0, 0, 0, None, False, python_law=lambda: "")
                == ""
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_count_violations"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_count_violations(10, 10, 4, 0, 0, 0, None, False, python_law=forbidden_oracle)
            == ""
        )


# ─────────────────── slice 2 — drift pins ───────────────────


class TestSliceTwoDriftPins:
    def test_chain_links_mirror_chain_map(self) -> None:
        from brutex.observability.chain_map import LINKS

        assert sweep_ops_rust._CHAIN_LINKS == LINKS

    def test_stamp_verdict_tokens_mirror_enum(self) -> None:
        from brutex.observability.stage_stamp import StampVerdict

        assert {v.value for v in StampVerdict} == sweep_ops_rust._STAMP_VERDICT_TOKENS

    def test_stamp_verdict_results_are_a_token_subset(self) -> None:
        assert sweep_ops_rust._STAMP_VERDICT_RESULTS < sweep_ops_rust._STAMP_VERDICT_TOKENS

    def test_link_state_tokens_mirror_enum(self) -> None:
        from brutex.observability.chain_map import LinkState

        assert {s.value for s in LinkState} == sweep_ops_rust._LINK_STATE_TOKENS

    def test_verify_tokens_mirror_reason_map(self) -> None:
        from brutex.engine.sweep_audit import _VERIFY_STEP_REASONS

        assert sweep_ops_rust._CHAIN_VERIFY_TOKENS - {"ok"} == set(_VERIFY_STEP_REASONS)

    def test_hash_hex_len_mirrors_sweep_audit(self) -> None:
        from brutex.engine.sweep_audit import BLAKE3_HEX_LEN

        assert sweep_ops_rust._HASH_HEX_LEN == BLAKE3_HEX_LEN


# ─────────────────── slice 3 — the durable-artifacts family ───────────────────


class TestRoutedExitStatusResolution:
    _OK = ("running", None, 100.5, None, None)

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("running", None, 100.5, None, None), (100.5, None, False)),
            (("running", 50.25, 100.5, True, None), (50.25, None, True)),
            (("complete", 50.25, 100.5, None, None), (50.25, 100.5, False)),
            (("failed", None, 100.5, None, True), (100.5, 100.5, True)),
            (("suspect_complete", None, 100.5, False, False), (100.5, 100.5, True)),
            (("refused", None, 100.5, False, True), (100.5, 100.5, False)),
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        args: tuple[Any, ...],
        expected: tuple[Any, ...],
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_exit_status_resolution(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            ("exploded", None, 1.0, None, None),  # unknown state token
            (b"running", None, 1.0, None, None),  # non-str state
            ("running", 5, 1.0, None, None),  # int prior (exact-float gate)
            ("running", float("nan"), 1.0, None, None),  # non-finite prior
            ("running", None, float("inf"), None, None),  # non-finite now
            ("running", None, 1, None, None),  # int now
            ("running", None, 1.0, 1, None),  # int suspect
            ("running", None, 1.0, None, 0),  # int prior_suspect
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        sentinel = (1.0, None, False)
        with capture_logs() as logs:
            assert routed_exit_status_resolution(*args, python_law=lambda: sentinel) == sentinel
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "exit_status_resolution_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize(
        "result",
        [
            "ok",  # not a tuple
            (1.0, None),  # wrong arity
            (float("nan"), None, False),  # non-finite started
            (1.0, float("inf"), False),  # non-finite ended
            (1.0, None, 1),  # non-bool suspect
            (1, None, False),  # int started
        ],
    )
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.exit_status_resolution = lambda *args: result
        _install_crate(monkeypatch, crate)
        sentinel = (1.0, None, False)
        with capture_logs() as logs:
            assert routed_exit_status_resolution(*self._OK, python_law=lambda: sentinel) == sentinel
        warns = [e for e in logs if e.get("reason") == "exit_status_resolution_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.exit_status_resolution = lambda *args: (2.0, None, False)  # wrong start
        _install_crate(monkeypatch, crate)
        oracle = (100.5, None, False)
        with capture_logs() as logs:
            assert routed_exit_status_resolution(*self._OK, python_law=lambda: oracle) == oracle
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_exit_status_resolution"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> tuple[float, float | None, bool]:
            raise AssertionError("steady state must not consult the oracle")

        assert routed_exit_status_resolution(*self._OK, python_law=forbidden_oracle) == (
            100.5,
            None,
            False,
        )


class TestRoutedQuarantineNextOrdinal:
    @pytest.mark.parametrize(
        ("names", "prefix", "suffix", "expected"),
        [
            ([], "run-", "", 1),
            (["run-1", "run-7", "run-3"], "run-", "", 8),
            (["other-9", "run-x", "run-", "run-4"], "run-", "", 5),
            (["1.json", "12.json", "x.json"], "", ".json", 13),
            (["ab"], "ab", "b", 1),  # overlapping prefix/suffix — empty-slice arm
            (["007"], "", "", 8),  # leading zeros
        ],
    )
    def test_parity_with_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        names: list[str],
        prefix: str,
        suffix: str,
        expected: int,
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert (
            routed_quarantine_next_ordinal(names, prefix, suffix, python_law=lambda: expected)
            == expected
        )

    @pytest.mark.parametrize(
        ("names", "prefix", "suffix"),
        [
            (["run-٢٢"], "run-", ""),  # Unicode digits (Python int() accepts)
            (["run-12\n"], "run-", ""),  # trailing newline (re `$` accepts)
            (["run-1"], "run-ß", ""),  # non-ASCII prefix
            (["run-1"], "run-", "\n"),  # newline suffix
            ((), "run-", ""),  # tuple, not list
            ([b"run-1"], "run-", ""),  # non-str name
        ],
    )
    def test_input_refused_to_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        names: Any,
        prefix: str,
        suffix: str,
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            assert (
                routed_quarantine_next_ordinal(names, prefix, suffix, python_law=lambda: 23) == 23
            )
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "quarantine_next_ordinal_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize("result", ["1", 0, -3, True])
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.quarantine_next_ordinal = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_quarantine_next_ordinal([], "run-", "", python_law=lambda: 1) == 1
        warns = [e for e in logs if e.get("reason") == "quarantine_ordinal_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.quarantine_next_ordinal = lambda *args: 99  # wrong ordinal
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_quarantine_next_ordinal(["run-1"], "run-", "", python_law=lambda: 2) == 2
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_quarantine_ordinal"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> int:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_quarantine_next_ordinal(["run-6"], "run-", "", python_law=forbidden_oracle) == 7
        )


class TestRoutedCheckpointRecordDecision:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ((-1, -1, 10), "reject_below_zero"),
            ((10, -1, 10), "reject_beyond_total"),
            ((3, 3, 10), "noop"),
            ((2, 3, 10), "noop"),
            ((4, 3, 10), "advance"),
            ((0, -1, 1), "advance"),
            ((-1, -1, 0), "reject_below_zero"),  # guard order at total==0
        ],
    )
    def test_parity_with_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[int, ...], expected: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_checkpoint_record_decision(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (True, -1, 10),  # bool index
            (0, -1.0, 10),  # float last
            (0, -1, "10"),  # str total
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            assert routed_checkpoint_record_decision(*args, python_law=lambda: "noop") == "noop"
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "checkpoint_record_input_refused"]
        assert len(warns) == 1

    def test_out_of_i64_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The REAL wheel's pyo3 i64 conversion raises OverflowError on an
        # out-of-band index (parity-suite-proven); the fake mirrors it here
        # via a raising stand-in (the slice-2 overflow-test idiom).
        _enable(monkeypatch)
        crate = _good_crate()

        def raising(*args: Any) -> str:
            raise OverflowError("out of i64")

        crate.checkpoint_record_decision = raising
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_checkpoint_record_decision(2**64, -1, 2**65, python_law=lambda: "advance")
                == "advance"
            )
        warns = [e for e in logs if e.get("reason") == "checkpoint_record_error"]
        assert len(warns) == 1

    @pytest.mark.parametrize("result", [7, "banana", None])
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.checkpoint_record_decision = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert routed_checkpoint_record_decision(4, 3, 10, python_law=lambda: "advance") == (
                "advance"
            )
        warns = [e for e in logs if e.get("reason") == "checkpoint_record_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.checkpoint_record_decision = lambda *args: "noop"  # silent-rewind poison
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_checkpoint_record_decision(4, 3, 10, python_law=lambda: "advance")
                == "advance"
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_checkpoint_record"


class TestRoutedCheckpointAdoptionVerdict:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("a", 10, "b", 10), "foreign"),
            (("a", 10, "b", 99), "foreign"),  # foreign wins over total drift
            (("a", 10, "a", 99), "total_mismatch"),
            (("a", 10, "a", 10), "adopt"),
        ],
    )
    def test_parity_with_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...], expected: str
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        assert routed_checkpoint_adoption_verdict(*args, python_law=lambda: expected) == expected

    @pytest.mark.parametrize(
        "args",
        [
            (b"a", 10, "a", 10),  # non-str id
            ("a", True, "a", 10),  # bool total
            ("a", 10, "a", 10.0),  # float total
        ],
    )
    def test_input_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, args: tuple[Any, ...]
    ) -> None:
        _enable(monkeypatch)
        calls = _install_import_bomb(monkeypatch)
        with capture_logs() as logs:
            assert routed_checkpoint_adoption_verdict(*args, python_law=lambda: "adopt") == "adopt"
        assert calls == []
        warns = [e for e in logs if e.get("reason") == "checkpoint_adoption_input_refused"]
        assert len(warns) == 1

    @pytest.mark.parametrize("result", [7, "quarantine", None])
    def test_malformed_result_refused(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.checkpoint_adoption_verdict = lambda *args: result
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_checkpoint_adoption_verdict("a", 10, "a", 10, python_law=lambda: "adopt")
                == "adopt"
            )
        warns = [e for e in logs if e.get("reason") == "checkpoint_adoption_result_refused"]
        assert len(warns) == 1

    def test_burn_in_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _good_crate()
        crate.checkpoint_adoption_verdict = lambda *args: "adopt"  # tamper-blessing poison
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            assert (
                routed_checkpoint_adoption_verdict(
                    "a", 10, "a", 99, python_law=lambda: "total_mismatch"
                )
                == "total_mismatch"
            )
        reverts = [e for e in logs if e["event"] == "sweep_ops_rust.autoreverted"]
        assert reverts[0]["reason"] == "burn_in_mismatch_checkpoint_adoption"

    def test_steady_state_skips_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _good_crate())
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_checkpoint_adoption_verdict("a", 10, "b", 10, python_law=forbidden_oracle)
            == "foreign"
        )


class TestSliceThreeDriftPins:
    def test_run_state_tokens_mirror_enum(self) -> None:
        from brutex.engine.sweep_exit_status import RunState

        assert {s.value for s in RunState} == sweep_ops_rust._RUN_STATE_TOKENS

    def test_record_decision_tokens_cover_the_wiring_arms(self) -> None:
        from brutex.engine.sweep_checkpoint import _record_decision_python

        produced = {
            _record_decision_python(-1, -1, 10),
            _record_decision_python(10, -1, 10),
            _record_decision_python(3, 3, 10),
            _record_decision_python(4, 3, 10),
        }
        assert produced == sweep_ops_rust._RECORD_DECISION_TOKENS

    def test_adoption_tokens_cover_the_wiring_arms(self) -> None:
        from brutex.engine.sweep_checkpoint import _adoption_verdict_python

        produced = {
            _adoption_verdict_python("a", 10, "b", 10),
            _adoption_verdict_python("a", 10, "a", 99),
            _adoption_verdict_python("a", 10, "a", 10),
        }
        assert produced == sweep_ops_rust._ADOPTION_TOKENS
