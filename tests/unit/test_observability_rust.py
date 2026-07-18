"""Unit matrix for :mod:`brutex.observability.observability_rust` (DEC-OBS-RUST-DETECTOR-CORE-001).

The wheel-FREE facade battery: flag tristate matrix / import guard + SYMBOL
handshake (``crate_unavailable`` / ``crate_stale``) / per-law burn-in
decrement + mismatch autorevert (ship-the-ORACLE value) / every degrade-set
member / the panic-belt latch / the MemoryError-KeyboardInterrupt-SystemExit
escape pins / every refuse-poison input + output gate / the warn-once memo /
the engaged-INFO / the reset seam (incl. ``remaining=0`` steady state) / the
OFF-arm bomb-crate inertness + identity pins / the two mirror pins vs
``lake_rust_render`` (tristate vocabulary equality + the panic-belt FOLD law:
the sibling fold is a pinned SUBSET of ours, the crate's OWN per-extension
``PanicException`` the one sanctioned extra — the panic-containment fix) /
the crate_stale teeth for a pre-fix wheel missing the ``PanicException``
export / thread-fuzz on latch + memo — every path exercised WITHOUT the
wheel (fake crate modules injected via ``sys.modules``), so the 100%
line+branch discovery gate holds on wheel-less CI.

``TestRealWheelDifferential`` (the hypothesis differentials per law at
``remaining=0`` + the D1 all-64 exhaustive sweep + the D2 truth table) runs
ONLY where the real ``brutex_observability`` wheel is importable (skipped
cleanly otherwise — the window_ledger_rust precedent); the dedicated
``observability-rust-parity.yml`` lane is where it has teeth every push.
"""

from __future__ import annotations

import ast
import importlib
import itertools
import json
import re
import struct
import sys
import threading
import types
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple, NoReturn, cast

import pytest
from brutex.data import lake_rust_render
from brutex.observability import observability_rust as obs
from brutex.observability.loop_watchdog import (
    WatchdogState,
    WatchdogVerdict,
    _check_once_python,
    check_once,
)
from brutex.observability.sla import (
    SLADetector,
    SLAEvent,
    SLAPredicate,
    SLARule,
    _sla_decide_python,
)
from brutex.observability.stage_stamp import (
    StampVerdict,
    _stamp_shape_index_python,
    _stamp_verdict_python,
    read_stage_stamp,
    validate_stage_stamp,
    write_stage_stamp,
)
from brutex.observability.watchdog import (
    Watchdog,
    WorkerEvent,
    WorkerState,
    _worker_decide_python,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: The canonical OK state every law-A helper call reuses (frozen dataclass —
#: safe to share across tests).
_STATE_OK: Final[WatchdogState] = WatchdogState()

#: The crate tree's Python shim (parsed by AST — wheel-free contract pins).
_SHIM_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "rust"
    / "brutex-observability"
    / "python"
    / "brutex_observability"
    / "__init__.py"
)

_EXPECTED_BUILD_PATH: Final[Path] = _SHIM_PATH.parent / "EXPECTED_BUILD.json"

_CARGO_TOML_PATH: Final[Path] = _SHIM_PATH.parents[2] / "Cargo.toml"


def _oracle_bomb(*args: object, **kwargs: object) -> NoReturn:
    """A ``python_law`` stand-in that must NEVER be consulted (steady state)."""
    raise AssertionError("the routed path consulted the Python oracle")


def _bomb_import(name: str) -> types.ModuleType:
    """An ``importlib.import_module`` stand-in the OFF/refused arms must never hit."""
    raise AssertionError(f"the routed path imported {name!r}")


def _delegating_loop(
    now: float,
    last_beat: float,
    tier: str,
    stall_started_beat: float,
    last_logged_at: float,
    last_alerted_at: float,
    episodes: int,
    warn_after_s: float,
    critical_after_s: float,
    relog_every_s: float,
    realert_every_s: float,
) -> tuple[Any, ...]:
    """A law-A fake crate fn that mirrors the oracle exactly (burn-in clean)."""
    state = WatchdogState(
        tier=tier,
        stall_started_beat=stall_started_beat,
        last_logged_at=last_logged_at,
        last_alerted_at=last_alerted_at,
        episodes=episodes,
    )
    verdict = _check_once_python(
        now,
        last_beat,
        state,
        warn_after_s=warn_after_s,
        critical_after_s=critical_after_s,
        relog_every_s=relog_every_s,
        realert_every_s=realert_every_s,
    )
    return obs._loop_verdict_tuple(verdict)


class _FakeCratePanic(BaseException):
    """The fake crate's own per-extension panic class (a BaseException, like
    pyo3's real per-extension ``PanicException`` static — NOT an Exception)."""


def _fake_crate(**overrides: object) -> types.ModuleType:
    """A stand-in ``brutex_observability`` module exposing the lane API surface.

    Defaults DELEGATE to the Python oracles, so burn-in compares clean;
    overrides swap individual law fns for raisers / poison / wrong values.
    ``PanicException`` defaults to :class:`_FakeCratePanic` (the real wheel's
    per-extension-class shape — part of the ``_CRATE_API`` handshake).
    """
    module = types.ModuleType("brutex_observability")
    surface: dict[str, object] = {
        "loop_watchdog_check_once": _delegating_loop,
        "watchdog_decide": _worker_decide_python,
        "sla_decide": _sla_decide_python,
        "stamp_shape_index": _stamp_shape_index_python,
        "stamp_verdict": _stamp_verdict_python,
        "PanicException": _FakeCratePanic,
        "__version__": "0.1.0-fake",
    }
    surface.update(overrides)
    for name, value in surface.items():
        setattr(module, name, value)
    return module


def _install(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    """Route the facade's lazy crate import at *crate* (``None`` = absent wheel)."""
    monkeypatch.setitem(sys.modules, "brutex_observability", crate)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "1")


def _call_loop(python_law: Any = _check_once_python) -> WatchdogVerdict:
    """The canonical routed law-A call (now == last_beat → the OK→OK arm)."""
    return obs.routed_loop_check_once(
        0.0,
        0.0,
        _STATE_OK,
        warn_after_s=5.0,
        critical_after_s=30.0,
        relog_every_s=60.0,
        realert_every_s=300.0,
        python_law=python_law,
        state_factory=WatchdogState,
        verdict_factory=WatchdogVerdict,
    )


def _oracle_loop() -> WatchdogVerdict:
    return _check_once_python(
        0.0,
        0.0,
        _STATE_OK,
        warn_after_s=5.0,
        critical_after_s=30.0,
        relog_every_s=60.0,
        realert_every_s=300.0,
    )


def _call_watchdog(python_law: Any = _worker_decide_python) -> tuple[float, bool, bool]:
    return obs.routed_watchdog_decide(20.0, 0.0, 15.0, False, python_law=python_law)


def _call_sla(python_law: Any = _sla_decide_python) -> tuple[bool, bool]:
    return obs.routed_sla_decide(
        SLAPredicate.GREATER_THAN, 150.0, 100.0, False, python_law=python_law
    )


def _call_shape(python_law: Any = _stamp_shape_index_python) -> int:
    return obs.routed_stamp_shape_index(True, True, True, True, True, True, python_law=python_law)


def _call_verdict(python_law: Any = _stamp_verdict_python) -> str:
    return obs.routed_stamp_verdict(
        "h", "h", "s", "s", "r", None, None, None, python_law=python_law
    )


class _LawCase(NamedTuple):
    """One routed law's canonical call + oracle + poison/wrong crate returns."""

    law: str
    crate_fn: str
    call_real: Callable[[], object]
    call_bomb: Callable[[], object]
    oracle: Callable[[], object]
    poison: object
    wrong: object


#: A valid-SHAPE but WRONG-VALUE law-A crate return (lag 999.0 vs oracle 0.0).
_WRONG_LOOP: Final[tuple[Any, ...]] = (
    "ok",
    0.0,
    0.0,
    0.0,
    0,
    999.0,
    False,
    False,
    False,
    False,
    False,
    0.0,
)

_LAW_CASES: Final[tuple[_LawCase, ...]] = (
    _LawCase(
        "loop",
        "loop_watchdog_check_once",
        _call_loop,
        lambda: _call_loop(_oracle_bomb),
        _oracle_loop,
        42,
        _WRONG_LOOP,
    ),
    _LawCase(
        "watchdog",
        "watchdog_decide",
        _call_watchdog,
        lambda: _call_watchdog(_oracle_bomb),
        lambda: (20.0, True, True),
        (20.0, 1, True),
        (999.0, True, True),
    ),
    _LawCase(
        "sla",
        "sla_decide",
        _call_sla,
        lambda: _call_sla(_oracle_bomb),
        lambda: (True, True),
        (1, 0),
        (False, False),
    ),
    _LawCase(
        "stamp_shape",
        "stamp_shape_index",
        _call_shape,
        lambda: _call_shape(_oracle_bomb),
        lambda: -1,
        True,
        5,
    ),
    _LawCase(
        "stamp_verdict",
        "stamp_verdict",
        _call_verdict,
        lambda: _call_verdict(_oracle_bomb),
        lambda: "ok",
        "junk",
        "hash_mismatch",
    ),
)

_LAW_IDS: Final[tuple[str, ...]] = tuple(case.law for case in _LAW_CASES)


def _raiser(exc: BaseException) -> Callable[..., NoReturn]:
    def _raise(*args: object, **kwargs: object) -> NoReturn:
        raise exc

    return _raise


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh latch/burn-in/memo state + flag unset around every test."""
    monkeypatch.delenv(obs.OBSERVABILITY_RUST_ENV, raising=False)
    obs.reset_observability_rust_for_tests()
    yield
    obs.reset_observability_rust_for_tests()


class TestFlagResolution:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, raw)
        assert obs.observability_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", " OFF "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, raw)
        with capture_logs() as logs:
            assert obs.observability_rust_enabled() is False
        assert logs == []

    def test_unset_defaults_off_silently(self) -> None:
        with capture_logs() as logs:
            assert obs.observability_rust_enabled() is False
        assert logs == []

    def test_blank_whitespace_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "   ")
        with capture_logs() as logs:
            assert obs.observability_rust_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert obs.observability_rust_enabled() is False
            assert obs.observability_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "banana"

    def test_flag_is_read_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert obs.observability_rust_enabled() is False
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "1")
        assert obs.observability_rust_enabled() is True
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "0")
        assert obs.observability_rust_enabled() is False


class TestMirrorPins:
    """The documented no-import-edge law: LOCAL twins, equality-pinned."""

    def test_truthy_falsy_sets_match_lake_rust_render(self) -> None:
        assert obs._TRUTHY == lake_rust_render._TRUTHY
        assert obs._FALSY == lake_rust_render._FALSY

    @pytest.mark.parametrize(
        "raw",
        ["1", "true", "yes", "on", "0", "false", "no", "off", "", "  ", "banana", " TRUE ", "On"],
    )
    def test_tristate_function_matches_lake_rust_render(self, raw: str) -> None:
        assert obs._env_flag_tristate(raw) == lake_rust_render.env_flag_tristate(raw)

    def test_panic_belt_folds_the_lake_rust_render_twin(self) -> None:
        # The belt LAW after the panic-containment fix: ours = the sibling
        # name-based fold ∪ {the crate's OWN PanicException when the wheel
        # is loaded}. The shared fold stays pinned (a subset of ours — the
        # sibling-shape parity the fallback homes exist for); the ONE
        # sanctioned extra is the per-extension class the name-based homes
        # structurally cannot resolve (see _panic_belt_errors' docstring).
        ours = set(obs._panic_belt_errors())
        sibling = set(lake_rust_render._panic_belt_errors())
        assert sibling <= ours
        extras = ours - sibling
        try:
            crate = importlib.import_module("brutex_observability")
        except ImportError:
            assert extras == set()  # wheel absent → exact sibling-fold equality
        else:
            assert extras == {crate.PanicException}

    def test_crate_own_panic_class_joins_the_belt_when_wheel_present(self) -> None:
        # The wheel-gated half of the fold pin: the crate class JOINS.
        crate = pytest.importorskip("brutex_observability")
        belt = obs._panic_belt_errors()
        assert crate.PanicException in belt
        assert issubclass(crate.PanicException, BaseException)
        assert not issubclass(crate.PanicException, Exception)


def _block_belt_homes(monkeypatch: pytest.MonkeyPatch, *homes: str) -> None:
    """Make each named belt home unimportable (``None`` in ``sys.modules``)."""
    for home in homes:
        monkeypatch.setitem(sys.modules, home, None)


#: Every home `_panic_belt_errors` resolves — the crate's own FIRST.
_BELT_HOMES: Final[tuple[str, ...]] = ("brutex_observability", "pyo3_runtime", "polars.exceptions")


class TestPanicBeltResolution:
    def test_belt_always_carries_exception_first(self) -> None:
        assert obs._panic_belt_errors()[0] is Exception

    def test_no_homes_importable_yields_bare_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _block_belt_homes(monkeypatch, *_BELT_HOMES)
        assert obs._panic_belt_errors() == (Exception,)

    def test_crate_home_class_resolves_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The crate's OWN class is resolved BEFORE the name-based fallback
        # homes (the per-extension-class law — the fix's load-bearing order).
        class PyO3Panic(BaseException):
            pass

        home = types.ModuleType("pyo3_runtime")
        home.PanicException = PyO3Panic  # type: ignore[attr-defined]
        _install(monkeypatch, _fake_crate())
        monkeypatch.setitem(sys.modules, "pyo3_runtime", home)
        _block_belt_homes(monkeypatch, "polars.exceptions")
        assert obs._panic_belt_errors() == (Exception, _FakeCratePanic, PyO3Panic)

    def test_crate_home_without_panic_attr_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Belt-side grace for a crate module lacking the export (the belt
        # never crashes; the HANDSHAKE is what refuses such a wheel — see
        # test_pre_fix_wheel_missing_panic_class_latches_stale).
        crate = _fake_crate()
        delattr(crate, "PanicException")
        _install(monkeypatch, crate)
        _block_belt_homes(monkeypatch, "pyo3_runtime", "polars.exceptions")
        assert obs._panic_belt_errors() == (Exception,)

    def test_valid_panic_class_is_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(BaseException):
            pass

        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = FakePanic  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        _block_belt_homes(monkeypatch, "brutex_observability", "polars.exceptions")
        assert obs._panic_belt_errors() == (Exception, FakePanic)

    def test_non_class_panic_attr_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        _block_belt_homes(monkeypatch, "brutex_observability", "polars.exceptions")
        assert obs._panic_belt_errors() == (Exception,)

    def test_non_baseexception_class_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = type("NotAnError", (), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        _block_belt_homes(monkeypatch, "brutex_observability", "polars.exceptions")
        assert obs._panic_belt_errors() == (Exception,)

    def test_duplicate_class_across_homes_is_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The crate home and a name-based home carrying the SAME class fold
        # to one belt member (`panic not in belt`).
        name_home = types.ModuleType("pyo3_runtime")
        name_home.PanicException = _FakeCratePanic  # type: ignore[attr-defined]
        _install(monkeypatch, _fake_crate())
        monkeypatch.setitem(sys.modules, "pyo3_runtime", name_home)
        _block_belt_homes(monkeypatch, "polars.exceptions")
        belt = obs._panic_belt_errors()
        assert belt.count(_FakeCratePanic) == 1


class TestResetSeam:
    def test_reset_restores_fresh_process_view(self) -> None:
        obs._disable("poison")
        obs._warn_once("stale")
        obs.reset_observability_rust_for_tests()
        assert obs.rust_observability_disabled() is False
        assert not obs._OBSERVABILITY_MEMO
        fresh = dict.fromkeys(
            ("loop", "watchdog", "sla", "stamp_shape", "stamp_verdict"), obs.BURN_IN_SAMPLES
        )
        assert fresh == obs._BURN_IN_REMAINING

    def test_reset_remaining_zero_is_the_steady_state(self) -> None:
        obs.reset_observability_rust_for_tests(remaining=0)
        assert obs._DISABLED_REASON[0] is None
        assert not obs._OBSERVABILITY_MEMO
        assert all(budget == 0 for budget in obs._BURN_IN_REMAINING.values())

    def test_warn_once_suppresses_repeats(self) -> None:
        with capture_logs() as logs:
            obs._warn_once("reason_x", detail=1)
            obs._warn_once("reason_x", detail=2)
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["detail"] == 1

    def test_disable_repeat_is_noop_first_call_wins(self) -> None:
        obs._disable("first")
        obs._disable("second")
        assert obs._DISABLED_REASON[0] == "first"


class TestCrateGuards:
    def test_crate_absent_latches_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, None)  # None in sys.modules → ImportError
        with capture_logs() as logs:
            assert _call_sla() == (True, True)
        assert obs.rust_observability_disabled() is True
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "crate_unavailable"
        assert "remedy" in events[0]

    def test_crate_stale_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        delattr(crate, "stamp_verdict")  # a pre-lane wheel shape
        _install(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_watchdog() == (20.0, True, True)
        assert obs.rust_observability_disabled() is True
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_stale"

    def test_crate_stale_noncallable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(sla_decide="not callable"))
        assert _call_shape() == -1
        assert obs.rust_observability_disabled() is True
        assert obs._DISABLED_REASON[0] == "crate_stale"

    def test_pre_fix_wheel_missing_panic_class_latches_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The crate_stale TEETH of the panic-containment fix: a stale
        # PRE-FIX wheel carries all five law fns but NOT the PanicException
        # export — the all-or-nothing _CRATE_API handshake refuses it, so
        # the belt can never run against a wheel whose own panic class it
        # cannot resolve (an uncatchable per-extension panic would be back).
        _flag_on(monkeypatch)
        crate = _fake_crate()
        delattr(crate, "PanicException")
        _install(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_sla() == (True, True)
        assert obs._DISABLED_REASON[0] == "crate_stale"
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert "PanicException" in events[0]["error_detail"]

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_crate_absent_every_law_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, None)
        assert case.call_real() == case.oracle()
        assert obs._DISABLED_REASON[0] == "crate_unavailable"

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_latched_process_never_imports_crate(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        obs._disable("test_latch")
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        assert case.call_real() == case.oracle()


class TestOffArmInertness:
    def test_off_routed_calls_never_import_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        for case in _LAW_CASES:
            assert case.call_real() == case.oracle()

    def test_off_loop_ok_to_ok_preserves_state_object_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        verdict = _call_loop()
        assert verdict.state is _STATE_OK  # the oracle's own object, untouched

    def test_off_public_check_once_never_imports_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        state = WatchdogState()
        verdict = check_once(10.0, 0.0, state)
        assert verdict.warn_edge is True
        assert verdict.state.tier == "warned"
        assert verdict.state.episodes == 1

    def test_off_public_watchdog_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        times = [0.0, 20.0]
        events: list[WorkerEvent] = []
        dog = Watchdog(
            timeout_seconds=15.0,
            on_hung=events.append,
            on_recovery=events.append,
            clock=lambda: times.pop(0),
        )
        dog.register("w")
        fired = dog.check_once()
        assert [e.new_state for e in fired] == [WorkerState.HUNG]
        assert fired[0].last_beat_seconds_ago == 20.0
        assert events == list(fired)

    def test_off_public_sla_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        events: list[SLAEvent] = []
        detector = SLADetector(
            rules=(
                SLARule(
                    name="r",
                    value_provider=lambda: 150.0,
                    predicate=SLAPredicate.GREATER_THAN,
                    threshold=100.0,
                ),
            ),
            on_breach=events.append,
            on_recovery=events.append,
        )
        fired = detector.check_once()
        assert len(fired) == 1
        assert fired[0].observed_value == 150.0

    def test_off_public_stamp_laws_never_import_crate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        write_stage_stamp(artifact, stage="cascade_manifest", run_id="r1")
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_manifest")
        assert verdict is StampVerdict.OK
        assert stamp is not None and stamp.run_id == "r1"
        assert read_stage_stamp(artifact).stage == "cascade_manifest"


class TestInputGates:
    """Exact-type refuse-poison INPUT gates route exotic numerics to the oracle."""

    def test_loop_int_now_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        with capture_logs() as logs:
            verdict = obs.routed_loop_check_once(
                0,  # int, not float — Python compares it EXACTLY; f64 is lossy
                0.0,
                _STATE_OK,
                warn_after_s=5.0,
                critical_after_s=30.0,
                relog_every_s=60.0,
                realert_every_s=300.0,
                python_law=_check_once_python,
                state_factory=WatchdogState,
                verdict_factory=WatchdogVerdict,
            )
        assert verdict.lag_s == 0.0
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_input_refused"

    def test_loop_bool_episodes_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        state = WatchdogState(episodes=True)  # bool is an int SUBCLASS — refused
        with capture_logs() as logs:
            verdict = _call_loop_with_state(state)
        assert verdict.state is state  # OK→OK oracle identity even when refused
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_input_refused"

    def test_watchdog_int_now_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        with capture_logs() as logs:
            result = obs.routed_watchdog_decide(
                20, 0.0, 15.0, False, python_law=_worker_decide_python
            )
        assert result == (20.0, True, True)
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "watchdog_input_refused"

    def test_sla_int_value_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        with capture_logs() as logs:
            result = obs.routed_sla_decide(
                SLAPredicate.GREATER_THAN, 150, 100.0, False, python_law=_sla_decide_python
            )
        assert result == (True, True)
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "sla_input_refused"

    def test_sla_non_str_predicate_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)

        def _law(
            predicate: object, value: float, threshold: float, prev_breach: bool
        ) -> tuple[bool, bool]:
            return (True, True)

        result = obs.routed_sla_decide(
            cast("SLAPredicate", 5.0), 150.0, 100.0, False, python_law=_law
        )
        assert result == (True, True)
        assert "sla_input_refused" in obs._OBSERVABILITY_MEMO


def _call_loop_with_state(state: WatchdogState) -> WatchdogVerdict:
    return obs.routed_loop_check_once(
        0.0,
        0.0,
        state,
        warn_after_s=5.0,
        critical_after_s=30.0,
        relog_every_s=60.0,
        realert_every_s=300.0,
        python_law=_check_once_python,
        state_factory=WatchdogState,
        verdict_factory=WatchdogVerdict,
    )


def _call_loop_warn_edge(state: WatchdogState, *, python_law: Any) -> WatchdogVerdict:
    """A routed law-A call at a WARN edge (lag 10 ≥ warn_after 5)."""
    return obs.routed_loop_check_once(
        10.0,
        0.0,
        state,
        warn_after_s=5.0,
        critical_after_s=30.0,
        relog_every_s=60.0,
        realert_every_s=300.0,
        python_law=python_law,
        state_factory=WatchdogState,
        verdict_factory=WatchdogVerdict,
    )


class TestRoutedLawMatrix:
    """The shared per-law arms: degrade / escape / panic / refused / mismatch / steady."""

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(**{case.crate_fn: _raiser(ValueError("ffi refusal"))}))
        with capture_logs() as logs:
            for _ in range(2):
                assert case.call_real() == case.oracle()
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == f"{case.law}_error"
        assert obs.rust_observability_disabled() is False  # degrade, not latch

    @pytest.mark.parametrize(
        "exc_type", [ValueError, RuntimeError, TypeError, OverflowError, AttributeError]
    )
    def test_every_degrade_set_member_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(watchdog_decide=_raiser(exc_type("boom"))))
        assert _call_watchdog() == (20.0, True, True)
        assert "watchdog_error" in obs._OBSERVABILITY_MEMO
        assert obs.rust_observability_disabled() is False

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_memory_error_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(**{case.crate_fn: _raiser(MemoryError())}))
        with pytest.raises(MemoryError):
            case.call_real()
        assert obs.rust_observability_disabled() is False

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_interrupt_and_exit_reraise_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(watchdog_decide=_raiser(exc_type())))
        with pytest.raises(exc_type):
            _call_watchdog()
        assert obs.rust_observability_disabled() is False

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        # KeyError is OUTSIDE the degrade set: it falls through to the panic
        # belt's `Exception` member — the same route a pyo3 PanicException
        # takes (the belt is equality-pinned to resolve those classes too).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(**{case.crate_fn: _raiser(KeyError("rust panicked"))}))
        with capture_logs() as logs:
            assert case.call_real() == case.oracle()
        assert obs._DISABLED_REASON[0] == "crate_panic"
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_panic"

    def test_crate_own_panic_class_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The belt resolves the CRATE's own PanicException — end to end.

        A genuine per-extension-shaped panic (a bare BaseException raised
        under the crate's own ``PanicException`` name) is CONTAINED with the
        name-based homes blocked — only the crate home can resolve the
        class. No ``_panic_belt_errors`` patching: this drives the real
        belt-resolution logic (the fake-crate injection the fix mandates,
        since the checked_add refusal removed the last natural panic path).
        """
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(sla_decide=_raiser(_FakeCratePanic("rust panicked"))))
        _block_belt_homes(monkeypatch, "pyo3_runtime", "polars.exceptions")
        with capture_logs() as logs:
            assert _call_sla() == (True, True)
        assert obs._DISABLED_REASON[0] == "crate_panic"
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_panic"

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_result_poison_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(**{case.crate_fn: lambda *a: case.poison}))
        with capture_logs() as logs:
            assert case.call_real() == case.oracle()
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == f"{case.law}_result_refused"
        assert obs.rust_observability_disabled() is False

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(**{case.crate_fn: lambda *a: case.wrong}))
        with capture_logs() as logs:
            assert case.call_real() == case.oracle()  # the ORACLE value ships
        assert obs.rust_observability_disabled() is True
        errors = [e for e in logs if e["event"] == "observability_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == f"burn_in_mismatch_{case.law}"

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_steady_state_never_consults_oracle(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        assert case.call_bomb() == case.oracle()

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_burn_in_clean_decrements_budget(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert case.call_real() == case.oracle()
        assert obs._BURN_IN_REMAINING[case.law] == obs.BURN_IN_SAMPLES - 1
        assert [e for e in logs if e["event"] == "observability_rust.burn_in_complete"] == []


class TestBurnInLifecycle:
    def test_burn_in_completes_per_law_then_steady(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            for case in _LAW_CASES:
                assert case.call_real() == case.oracle()
        done = [e for e in logs if e["event"] == "observability_rust.burn_in_complete"]
        assert {e["law"] for e in done} == set(_LAW_IDS)
        assert all(e["samples"] == obs.BURN_IN_SAMPLES for e in done)
        # Steady state: the oracle must never be consulted again (any law).
        for case in _LAW_CASES:
            assert case.call_bomb() == case.oracle()

    def test_engaged_info_logs_once_across_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            _call_loop()
            _call_sla()
            _call_verdict()
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"


class TestLoopRoutingExtras:
    @pytest.mark.parametrize(
        "poison",
        [
            [1, 2, 3],  # not a tuple
            ("ok", 0.0, 0.0),  # wrong arity
            ("junk-tier", 0.0, 0.0, 0.0, 0, 0.0, False, False, False, False, False, 0.0),
            ("ok", 0, 0.0, 0.0, 0, 0.0, False, False, False, False, False, 0.0),  # int ssb
            ("ok", 0.0, 0.0, 0.0, 0, 0.0, 1, False, False, False, False, 0.0),  # int flag
        ],
        ids=["non_tuple", "wrong_arity", "junk_tier", "int_float_field", "int_bool_field"],
    )
    def test_loop_result_shape_gate_refuses(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(loop_watchdog_check_once=lambda *a: poison))
        assert _call_loop() == _oracle_loop()
        assert "loop_result_refused" in obs._OBSERVABILITY_MEMO

    def test_loop_steady_state_recomposes_equal_not_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        verdict = _call_loop(_oracle_bomb)
        oracle = _oracle_loop()
        assert verdict == oracle
        assert verdict.state is not _STATE_OK  # the documented flag-ON envelope

    def test_loop_warn_edge_values_recomposed_from_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        verdict = obs.routed_loop_check_once(
            10.0,
            0.0,
            _STATE_OK,
            warn_after_s=5.0,
            critical_after_s=30.0,
            relog_every_s=60.0,
            realert_every_s=300.0,
            python_law=_oracle_bomb,
            state_factory=WatchdogState,
            verdict_factory=WatchdogVerdict,
        )
        assert verdict.warn_edge is True
        assert verdict.lag_s == 10.0
        assert verdict.state.tier == "warned"
        assert verdict.state.stall_started_beat == 0.0
        assert verdict.state.last_logged_at == 10.0
        assert verdict.state.episodes == 1

    @pytest.mark.parametrize("poison", [True, 6, -2], ids=["bool", "past_max", "past_min"])
    def test_stamp_shape_index_vocabulary_gate_refuses(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(stamp_shape_index=lambda *a: poison))
        assert _call_shape() == -1
        assert "stamp_shape_result_refused" in obs._OBSERVABILITY_MEMO

    def test_stamp_verdict_non_str_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(stamp_verdict=lambda *a: b"ok"))
        assert _call_verdict() == "ok"
        assert "stamp_verdict_result_refused" in obs._OBSERVABILITY_MEMO


class TestThreadFuzz:
    """The documented UNLOCKED memo/latch exception — never a wrong value."""

    def test_degrade_storm_converges_without_latch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(sla_decide=_raiser(ValueError("ffi"))))
        results: list[tuple[bool, bool]] = []
        lock = threading.Lock()

        def _work() -> None:
            local: list[tuple[bool, bool]] = [_call_sla() for _ in range(25)]
            with lock:
                results.extend(local)

        with capture_logs():
            threads = [threading.Thread(target=_work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert results == [(True, True)] * 200
        assert "sla_error" in obs._OBSERVABILITY_MEMO
        assert obs.rust_observability_disabled() is False

    def test_panic_storm_latches_once_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(watchdog_decide=_raiser(KeyError("panic"))))
        results: list[tuple[float, bool, bool]] = []
        lock = threading.Lock()

        def _work() -> None:
            local: list[tuple[float, bool, bool]] = [_call_watchdog() for _ in range(25)]
            with lock:
                results.extend(local)

        with capture_logs():
            threads = [threading.Thread(target=_work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert results == [(20.0, True, True)] * 200
        assert obs._DISABLED_REASON[0] == "crate_panic"


class TestContractPins:
    def test_env_name_and_burn_in_literals_pinned(self) -> None:
        assert obs.OBSERVABILITY_RUST_ENV == "BRUTEX_OBSERVABILITY_RUST"
        assert obs.BURN_IN_SAMPLES == 16
        assert obs._CRATE_API == (
            "loop_watchdog_check_once",
            "watchdog_decide",
            "sla_decide",
            "stamp_shape_index",
            "stamp_verdict",
            "PanicException",  # the panic-belt anchor — the crate_stale teeth
        )

    def test_check_once_and_oracle_share_signature_and_defaults(self) -> None:
        # The defaults are DUPLICATED across the router and the moved law
        # body — this pin keeps them from drifting apart silently.
        assert signature(_check_once_python) == signature(check_once)

    def test_shim_symbols_match_expected_build_json(self) -> None:
        tree = ast.parse(_SHIM_PATH.read_text(encoding="utf-8"))
        shim_symbols: tuple[str, ...] | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_EXPECTED_COMPILED_SYMBOLS"
                and node.value is not None
            ):
                shim_symbols = tuple(ast.literal_eval(node.value))
        assert shim_symbols is not None, "_EXPECTED_COMPILED_SYMBOLS not found in the shim"
        build = json.loads(_EXPECTED_BUILD_PATH.read_text(encoding="utf-8"))
        assert sorted(shim_symbols) == build["symbols"]
        assert build["symbols"] == sorted(build["symbols"])  # the file stays sorted
        # Every facade-handshaken callable is part of the shim surface.
        assert set(obs._CRATE_API) <= set(shim_symbols)

    def test_expected_build_crate_version_matches_cargo_toml(self) -> None:
        build = json.loads(_EXPECTED_BUILD_PATH.read_text(encoding="utf-8"))
        manifest = _CARGO_TOML_PATH.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', manifest, flags=re.MULTILINE)
        assert match is not None
        assert build["crate_version"] == match.group(1)


class TestRealWheelDifferential:
    """Hypothesis differentials vs the REAL built wheel (skipped when absent).

    ``remaining=0`` = the post-burn-in steady state, so the crate value ships
    with NO oracle cushioning a divergence — the differential IS the oracle
    compare. The 20k-sample ad-hoc runs live in the IMPL handoff as evidence;
    these committed suites keep CI-sane example counts.
    """

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_observability")
        monkeypatch.setenv(obs.OBSERVABILITY_RUST_ENV, "1")
        obs.reset_observability_rust_for_tests(remaining=0)  # steady state

    _FLOATS = st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.sampled_from(
            [
                0.0,
                -0.0,
                5.0,
                15.0,
                30.0,
                60.0,
                300.0,
                float("inf"),
                float("-inf"),
                float("nan"),
                2.0**53,
                -(2.0**53),
                5e-324,
                -5e-324,
            ]
        ),
    )

    _TIERS = st.sampled_from(["ok", "warned", "critical", "junk-tier", ""])

    _PREDICATES: st.SearchStrategy[SLAPredicate] = st.sampled_from(
        [
            SLAPredicate.GREATER_THAN,
            SLAPredicate.LESS_THAN,
            SLAPredicate.EQUALS,
            cast("SLAPredicate", "junk_pred"),
            cast("SLAPredicate", ""),
        ]
    )

    _EPS_BOUNDARY = st.sampled_from([100.0, 100.0 + 1e-12, 100.0 + 1e-9, 100.0 - 1e-9, 1e-9])

    _D2_STR = st.sampled_from(["a", "b", "deadbeef", ""]) | st.text(max_size=8)

    @settings(max_examples=200, deadline=None)
    @given(
        now=_FLOATS,
        last_beat=_FLOATS,
        tier=_TIERS,
        stall_started_beat=_FLOATS,
        last_logged_at=_FLOATS,
        last_alerted_at=_FLOATS,
        # The FULL u64 domain, u64::MAX INCLUDED (+ the two boundary values
        # oversampled): a warn edge at episodes == 2**64 - 1 now takes the
        # crate's checked_add ValueError refusal → the degrade set → the
        # ORACLE value (Python ints are unbounded), so the differential
        # equality holds over the whole domain — the pre-fix belt-escaping
        # overflow panic is dead at the root (the flipped containment pin
        # below: test_u64_max_episodes_warn_edge_degrades_via_value_error).
        episodes=st.integers(min_value=0, max_value=2**64 - 1)
        | st.sampled_from([2**64 - 1, 2**64 - 2]),
        warn_after_s=_FLOATS,
        critical_after_s=_FLOATS,
        relog_every_s=_FLOATS,
        realert_every_s=_FLOATS,
    )
    def test_loop_differential_hostile_floats(
        self,
        now: float,
        last_beat: float,
        tier: str,
        stall_started_beat: float,
        last_logged_at: float,
        last_alerted_at: float,
        episodes: int,
        warn_after_s: float,
        critical_after_s: float,
        relog_every_s: float,
        realert_every_s: float,
    ) -> None:
        state = WatchdogState(
            tier=tier,
            stall_started_beat=stall_started_beat,
            last_logged_at=last_logged_at,
            last_alerted_at=last_alerted_at,
            episodes=episodes,
        )
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_loop_check_once(
                now,
                last_beat,
                state,
                warn_after_s=warn_after_s,
                critical_after_s=critical_after_s,
                relog_every_s=relog_every_s,
                realert_every_s=realert_every_s,
                python_law=_check_once_python,
                state_factory=WatchdogState,
                verdict_factory=WatchdogVerdict,
            )
        oracle = _check_once_python(
            now,
            last_beat,
            state,
            warn_after_s=warn_after_s,
            critical_after_s=critical_after_s,
            relog_every_s=relog_every_s,
            realert_every_s=realert_every_s,
        )
        assert obs._pack_loop(obs._loop_verdict_tuple(routed)) == obs._pack_loop(
            obs._loop_verdict_tuple(oracle)
        )

    @settings(max_examples=200, deadline=None)
    @given(now=_FLOATS, last_beat=_FLOATS, timeout_seconds=_FLOATS, prev_hung=st.booleans())
    def test_watchdog_differential_hostile_floats(
        self, now: float, last_beat: float, timeout_seconds: float, prev_hung: bool
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_watchdog_decide(
                now, last_beat, timeout_seconds, prev_hung, python_law=_worker_decide_python
            )
        oracle = _worker_decide_python(now, last_beat, timeout_seconds, prev_hung)
        assert (struct.pack("<d", routed[0]), routed[1], routed[2]) == (
            struct.pack("<d", oracle[0]),
            oracle[1],
            oracle[2],
        )

    def test_watchdog_strict_gt_boundary_not_hung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # elapsed EXACTLY at the timeout is NOT hung (strict `>` — the
        # deliberate law-A/law-B divergence, mirrored in the crate).
        self._require_wheel(monkeypatch)
        assert obs.routed_watchdog_decide(15.0, 0.0, 15.0, False, python_law=_oracle_bomb) == (
            15.0,
            False,
            False,
        )

    @settings(max_examples=200, deadline=None)
    @given(
        predicate=_PREDICATES,
        value=st.one_of(_FLOATS, _EPS_BOUNDARY),
        threshold=st.one_of(_FLOATS, _EPS_BOUNDARY),
        prev_breach=st.booleans(),
    )
    def test_sla_differential_hostile_floats(
        self, predicate: SLAPredicate, value: float, threshold: float, prev_breach: bool
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_sla_decide(
                predicate, value, threshold, prev_breach, python_law=_sla_decide_python
            )
        assert routed == _sla_decide_python(predicate, value, threshold, prev_breach)

    def test_stamp_shape_index_all_64_combinations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        for combo in itertools.product((False, True), repeat=6):
            routed = obs.routed_stamp_shape_index(*combo, python_law=_oracle_bomb)
            assert routed == _stamp_shape_index_python(*combo)

    def test_stamp_verdict_truth_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        run_arms: tuple[str | None, ...] = (None, "r", "other")
        snap_arms: tuple[tuple[str | None, str | None], ...] = (
            (None, None),
            (None, "s"),
            ("s", None),
            ("s", "s"),
            ("s", "x"),
        )
        for hash_ok, stage_ok, expected_run, (snap, expected_snap) in itertools.product(
            (True, False), (True, False), run_arms, snap_arms
        ):
            recomputed = "h" if hash_ok else "torn"
            expected_stage = "s" if stage_ok else "other-stage"
            routed = obs.routed_stamp_verdict(
                recomputed,
                "h",
                "s",
                expected_stage,
                "r",
                expected_run,
                snap,
                expected_snap,
                python_law=_oracle_bomb,
            )
            oracle = _stamp_verdict_python(
                recomputed, "h", "s", expected_stage, "r", expected_run, snap, expected_snap
            )
            assert routed == oracle
            mismatch = (
                not hash_ok
                or not stage_ok
                or expected_run == "other"
                or (snap == "s" and expected_snap == "x")
            )
            assert routed == ("hash_mismatch" if mismatch else "ok")

    @settings(max_examples=200, deadline=None)
    @given(
        recomputed=_D2_STR,
        content_hash=_D2_STR,
        stage=_D2_STR,
        expected_stage=_D2_STR,
        run_id=_D2_STR,
        expected_run_id=st.none() | _D2_STR,
        stamped_snapshot=st.none() | _D2_STR,
        expected_snapshot=st.none() | _D2_STR,
    )
    def test_stamp_verdict_differential(
        self,
        recomputed: str,
        content_hash: str,
        stage: str,
        expected_stage: str,
        run_id: str,
        expected_run_id: str | None,
        stamped_snapshot: str | None,
        expected_snapshot: str | None,
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_stamp_verdict(
                recomputed,
                content_hash,
                stage,
                expected_stage,
                run_id,
                expected_run_id,
                stamped_snapshot,
                expected_snapshot,
                python_law=_stamp_verdict_python,
            )
        assert routed == _stamp_verdict_python(
            recomputed,
            content_hash,
            stage,
            expected_stage,
            run_id,
            expected_run_id,
            stamped_snapshot,
            expected_snapshot,
        )

    def test_out_of_u64_episodes_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Production-unreachable (episodes only ever increments from 0): a
        # NEGATIVE int fails the u64 conversion with OverflowError at the
        # pyo3 edge → the degrade set → the oracle serves it (the in-u64
        # overflow at MAX is the separate checked_add ValueError pin above).
        self._require_wheel(monkeypatch)
        state = WatchdogState(episodes=-1)
        with capture_logs() as logs:
            verdict = _call_loop_with_state(state)
        assert verdict.state is state  # the oracle's OK→OK identity
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_error"

    def test_u64_max_episodes_warn_edge_degrades_via_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE FLIPPED CONTAINMENT PIN. Pre-fix this test pinned the ESCAPE:
        # the `episodes + 1` overflow-checks panic crossed the FFI as THIS
        # wheel's per-extension PanicException — a BaseException resolvable
        # from NEITHER name-based belt home — and propagated un-latched.
        # The fix kills the panic at the ROOT (the crate's checked_add maps
        # the warn-edge increment at u64::MAX to a ValueError naming the
        # field) so the facade's degrade set serves the ORACLE value —
        # honest, since Python ints are unbounded and the oracle computes
        # u64::MAX + 1 fine. One warn, no latch, no autorevert.
        self._require_wheel(monkeypatch)
        state = WatchdogState(episodes=2**64 - 1)
        with capture_logs() as logs:
            verdict = _call_loop_warn_edge(state, python_law=_check_once_python)
        oracle = _check_once_python(
            10.0,
            0.0,
            state,
            warn_after_s=5.0,
            critical_after_s=30.0,
            relog_every_s=60.0,
            realert_every_s=300.0,
        )
        assert verdict == oracle
        assert verdict.warn_edge is True
        assert verdict.state.episodes == 2**64  # the unbounded-int truth
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_error"
        assert "episodes counter overflow" in warns[0]["error_detail"]
        assert obs.rust_observability_disabled() is False  # degrade, never a latch
        assert [e for e in logs if e["event"] == "observability_rust.autoreverted"] == []

    def test_u64_max_minus_one_warn_edge_increments_through_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Ok boundary sibling: checked_add at u64::MAX − 1 still
        # increments and the CRATE serves it (oracle bombed, steady state).
        self._require_wheel(monkeypatch)
        state = WatchdogState(episodes=2**64 - 2)
        verdict = _call_loop_warn_edge(state, python_law=_oracle_bomb)
        assert verdict.warn_edge is True
        assert verdict.state.episodes == 2**64 - 1

    def test_wheel_panic_instance_is_contained_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No NATURAL panic path survives the checked_add fix, so a genuine
        # instance of the WHEEL's own per-extension PanicException is
        # injected through a fake crate law — proving end-to-end
        # containment of exactly the class that pre-fix ESCAPED: belt catch
        # → crate_panic latch → oracle served, never a propagating
        # BaseException out of a monitor tick.
        wheel = pytest.importorskip("brutex_observability")
        _flag_on(monkeypatch)
        obs.reset_observability_rust_for_tests()
        _install(
            monkeypatch,
            _fake_crate(
                sla_decide=_raiser(wheel.PanicException("injected rust panic")),
                PanicException=wheel.PanicException,
            ),
        )
        assert not issubclass(wheel.PanicException, Exception)  # the escape shape
        with capture_logs() as logs:
            assert _call_sla() == (True, True)  # the oracle's value, served
        assert obs._DISABLED_REASON[0] == "crate_panic"
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_panic"
        assert "injected rust panic" in events[0]["error_detail"]

    def test_lone_surrogate_string_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lone surrogate cannot cross the pyo3 &str edge: UnicodeEncodeError
        # ⊂ ValueError → the degrade set → the oracle compares Python-side.
        self._require_wheel(monkeypatch)
        with capture_logs() as logs:
            routed = obs.routed_stamp_verdict(
                "\ud800",
                "\ud800",
                "s",
                "s",
                "r",
                None,
                None,
                None,
                python_law=_stamp_verdict_python,
            )
        assert routed == "ok"
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "stamp_verdict_error"
