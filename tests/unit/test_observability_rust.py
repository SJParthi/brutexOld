"""Unit matrix for :mod:`brutex.observability.observability_rust` (DEC-RUST-OBSERVABILITY-001).

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
import dataclasses
import importlib
import importlib.util
import inspect
import itertools
import json
import math
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
from brutex.engine.sweep_progress import STATE_RUNNING, SweepProgressSnapshot
from brutex.observability import observability_rust as obs
from brutex.observability import run_tripwires
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
        # The tripwire crate fns share the routed-law signatures, so the
        # test-local law oracles ARE the delegating fakes (burn-in clean).
        "tripwire_reconcile_counts": _tw_reconcile_law,
        "tripwire_throughput_watch": _tw_throughput_law,
        "tripwire_memory_budget": _tw_budget_law,
        "tripwire_memory_slope": _tw_slope_law,
        "tripwire_summary_classify": _tw_summary_law,
        "tripwire_stage_missing": _tw_stage_law,
        "tripwire_ru_maxrss_bytes": _tw_rss_law,
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


# ── Test-local tripwire law oracles (DEC-RUST-OBSERVABILITY-TRIPWIRES-002) ──
#
# Spec-faithful INDEPENDENT implementations of the routed tripwire-law
# signatures (deliberately literal-coded — never reading the facade's TW_*
# consts, so a garbled facade constant diverges loudly). The S2-S4 seams
# will feed `run_tripwires.py`'s own `_*_python` oracles through the same
# signatures; until then these are BOTH the fake-crate delegates and the
# differential oracles.


def _tw_reconcile_law(
    in_flight: int,
    enumerated: int,
    evaluated: int,
    skipped: int,
    survivors: int,
    total: int,
    prev_evaluated: int | None,
    is_done: bool,
) -> int:
    mask = 0
    if in_flight < 0:
        mask |= 1
    if enumerated != total:
        mask |= 2
    if skipped > evaluated:
        mask |= 4
    if survivors > evaluated:
        mask |= 8
    if evaluated + in_flight > total:
        mask |= 16
    if prev_evaluated is not None and evaluated < prev_evaluated:
        mask |= 32
    if is_done and evaluated != total:
        mask |= 64
    return mask


def _tw_throughput_law(
    rate: float, peak: float, below: int, fraction: float, dwell: int
) -> tuple[float, int, float, int]:
    new_peak = max(peak, rate)  # the argument-order mirror (NaN rate keeps peak)
    if new_peak <= 0.0:
        return (new_peak, below, 0.0, 0)
    floor = fraction * new_peak
    if rate < floor:
        new_below = below + 1
        return (new_peak, new_below, floor, 2 if new_below == dwell else 0)
    return (new_peak, 0, floor, 1)


def _tw_budget_law(rss: int, ram_total: int, fraction: float) -> tuple[float, int]:
    budget = fraction * float(ram_total)
    return (budget, 2 if float(rss) > budget else 1)


def _tw_slope_law(
    rss: int,
    evaluated: int,
    prev_rss: int,
    prev_evaluated: int,
    slope_threshold: float,
    slope_high: int,
    dwell: int,
) -> tuple[float, int, int]:
    slope = (rss - prev_rss) / (evaluated - prev_evaluated)
    if slope > slope_threshold:
        new_high = slope_high + 1
        return (slope, new_high, 2 if new_high == dwell else 0)
    return (slope, 0, 1)


def _tw_summary_law(
    values: list[float], screened: list[bool], absurd_threshold: float
) -> tuple[list[int], list[int]]:
    nan_codes: list[int] = []
    absurd_codes: list[int] = []
    for value, is_screened in zip(values, screened, strict=True):
        if not math.isfinite(value):
            nan_codes.append(2)
            absurd_codes.append(0)
            continue
        nan_codes.append(1)
        if is_screened:
            absurd_codes.append(2 if abs(value) > absurd_threshold else 1)
        else:
            absurd_codes.append(0)
    return (nan_codes, absurd_codes)


def _tw_stage_law(required_sorted: list[str], present: list[str]) -> list[bool]:
    return [token not in present for token in required_sorted]


def _tw_rss_law(ru_maxrss: int, platform: str) -> int:
    # The REAL production ORACLE (the S3 seam's extracted `_ru_maxrss_python`
    # — the pure law body; the public `normalized_ru_maxrss_bytes` is now
    # the branch-free ROUTER over it, so delegating to the router here
    # would recurse through the facade).
    return run_tripwires._ru_maxrss_python(ru_maxrss, platform)


def _call_tw_reconcile(python_law: Any = _tw_reconcile_law) -> int:
    """Canonical routed T2 call — in_flight −1 ⇒ bitmask 1 (bit 0)."""
    return obs.routed_tripwire_reconcile(-1, 10, 5, 0, 0, 10, None, False, python_law=python_law)


def _call_tw_throughput(python_law: Any = _tw_throughput_law) -> tuple[float, int, float, int]:
    """Canonical routed T3 call — the dwell FIRE edge (100.0, 5, 20.0, 2).

    Pairwise-UNEQUAL slots (100.0 vs 20.0; 5 vs 2) so a pack/unpack index
    garble fabricates a visible divergence (the FIX-ROUND-1 asymmetric
    burn-in fixture discipline).
    """
    return obs.routed_tripwire_throughput(10.0, 100.0, 4, 0.2, 5, python_law=python_law)


def _call_tw_budget(python_law: Any = _tw_budget_law) -> tuple[float, int]:
    """Canonical routed T4a call — one byte over budget ⇒ (500.0, FIRE)."""
    return obs.routed_tripwire_memory_budget(501, 1000, 0.5, python_law=python_law)


def _call_tw_slope(python_law: Any = _tw_slope_law) -> tuple[float, int, int]:
    """Canonical routed T4b call — the dwell FIRE edge (100.0, 5, 2)."""
    return obs.routed_tripwire_memory_slope(2000, 20, 1000, 10, 50.0, 4, 5, python_law=python_law)


def _call_tw_summary(python_law: Any = _tw_summary_law) -> tuple[list[int], list[int]]:
    """Canonical routed T5 call — mixed batch ([1, 2, 1], [0, 0, 2])."""
    return obs.routed_tripwire_summary(
        [1.0, float("nan"), 99.0], [False, True, True], 10.0, python_law=python_law
    )


def _call_tw_stage(python_law: Any = _tw_stage_law) -> list[bool]:
    """Canonical routed T6 call — one missing, one present ([True, False])."""
    return obs.routed_tripwire_stage(["stage2", "stage3"], ["stage3"], python_law=python_law)


def _call_tw_rss(python_law: Any = _tw_rss_law) -> int:
    """Canonical routed T9 call — 1024 KiB on linux ⇒ 1_048_576 bytes."""
    return obs.routed_ru_maxrss_bytes(1024, "linux", python_law=python_law)


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
    _LawCase(
        "tw_reconcile",
        "tripwire_reconcile_counts",
        _call_tw_reconcile,
        lambda: _call_tw_reconcile(_oracle_bomb),
        lambda: 1,
        True,  # bool bitmask (int subclass) — refused
        64,  # shape-valid wrong mask (bit 6 vs the oracle's bit 0)
    ),
    _LawCase(
        "tw_throughput",
        "tripwire_throughput_watch",
        _call_tw_throughput,
        lambda: _call_tw_throughput(_oracle_bomb),
        lambda: (100.0, 5, 20.0, 2),
        (100.0, 5, 20.0, 3),  # out-of-vocabulary code — refused
        (100.0, 5, 20.0, 0),  # shape-valid wrong code
    ),
    _LawCase(
        "tw_budget",
        "tripwire_memory_budget",
        _call_tw_budget,
        lambda: _call_tw_budget(_oracle_bomb),
        lambda: (500.0, 2),
        (500.0, 0),  # TW_NONE — the budget arm always decides; refused
        (500.0, 1),  # shape-valid wrong code
    ),
    _LawCase(
        "tw_slope",
        "tripwire_memory_slope",
        _call_tw_slope,
        lambda: _call_tw_slope(_oracle_bomb),
        lambda: (100.0, 5, 2),
        (100.0, 5.0, 2),  # float high — refused
        (100.0, 5, 0),  # shape-valid wrong code
    ),
    _LawCase(
        "tw_summary",
        "tripwire_summary_classify",
        _call_tw_summary,
        lambda: _call_tw_summary(_oracle_bomb),
        lambda: ([1, 2, 1], [0, 0, 2]),
        ([1, 2, 1], [0, 0]),  # truncated absurd array — refused
        ([1, 1, 1], [0, 0, 2]),  # shape-valid wrong nan codes
    ),
    _LawCase(
        "tw_stage",
        "tripwire_stage_missing",
        _call_tw_stage,
        lambda: _call_tw_stage(_oracle_bomb),
        lambda: [True, False],
        [True, 1],  # int flag — refused
        [False, False],  # shape-valid wrong mask
    ),
    _LawCase(
        "tw_rss",
        "tripwire_ru_maxrss_bytes",
        _call_tw_rss,
        lambda: _call_tw_rss(_oracle_bomb),
        lambda: 1_048_576,
        True,  # bool — refused
        7,  # shape-valid wrong bytes
    ),
)

_LAW_IDS: Final[tuple[str, ...]] = tuple(case.law for case in _LAW_CASES)

#: Every burn-in budget key — the A-D five + the seven tripwire laws
#: (== _LAW_IDS; kept a separate literal so a `_LAW_CASES` edit cannot
#: silently shrink the reset-seam pins).
_ALL_LAW_KEYS: Final[tuple[str, ...]] = (
    "loop",
    "watchdog",
    "sla",
    "stamp_shape",
    "stamp_verdict",
    "tw_reconcile",
    "tw_throughput",
    "tw_budget",
    "tw_slope",
    "tw_summary",
    "tw_stage",
    "tw_rss",
)


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


class TestCycleFreeTypeSurface:
    """The CodeQL py/cyclic-import fix: consumer types are structural stand-ins.

    The facade must carry NO import of its consumer modules — not even a
    type-only ``if TYPE_CHECKING:`` import (still a module-level import
    statement to a cyclic-import scan) — and the TypeVar surface that
    replaced those imports is pinned at runtime: ``TypeVar(...)`` calls are
    runtime expressions (unlike PEP 563 annotations), so their name strings
    and variance booleans mint mutmut mutants that are ONLY visible via the
    TypeVar objects' ``__name__``/``__bound__``/``__covariant__``/
    ``__contravariant__`` attributes.
    """

    def test_facade_source_carries_no_brutex_import(self) -> None:
        # The module docstring's law made mechanical: EVERY import statement
        # in the facade source (TYPE_CHECKING blocks included) is stdlib or
        # structlog — never a `brutex.*` module (absolute or relative), so
        # the consumer→facade edge can never become a cycle again.
        source = Path(obs.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    alias.name
                    for alias in node.names
                    if alias.name == "brutex" or alias.name.startswith("brutex.")
                )
            elif isinstance(node, ast.ImportFrom):
                if node.level != 0:  # any relative import is a brutex edge
                    offenders.append(f"level-{node.level} relative import")
                elif node.module == "brutex" or (node.module or "").startswith("brutex."):
                    offenders.append(str(node.module))
        assert offenders == []

    def test_typevar_names_bounds_variance_exact(self) -> None:
        # (bound, covariant, contravariant) per TypeVar — exact pins.
        expected: dict[str, tuple[type, bool, bool]] = {
            "_StateT_contra": (obs.WatchdogStateLike, False, True),
            "_StateT_co": (obs.WatchdogStateLike, True, False),
            "_VerdictT_co": (obs.WatchdogVerdictLike, True, False),
            "_PredicateT_contra": (str, False, True),
            "_StateT": (obs.WatchdogStateLike, False, False),
            "_VerdictT": (obs.WatchdogVerdictLike, False, False),
            "_PredicateT": (str, False, False),
        }
        for name, (bound, covariant, contravariant) in expected.items():
            tv = getattr(obs, name)
            assert tv.__name__ == name
            assert tv.__bound__ is bound
            assert tv.__covariant__ is covariant
            assert tv.__contravariant__ is contravariant

    @pytest.mark.parametrize(
        ("standin_name", "members"),
        [
            (
                "WatchdogStateLike",
                ("tier", "stall_started_beat", "last_logged_at", "last_alerted_at", "episodes"),
            ),
            (
                "WatchdogVerdictLike",
                (
                    "state",
                    "lag_s",
                    "warn_edge",
                    "critical_edge",
                    "relog",
                    "realert",
                    "recovered",
                    "stall_total_s",
                ),
            ),
        ],
    )
    def test_standin_members_are_read_only_properties(
        self, standin_name: str, members: tuple[str, ...]
    ) -> None:
        # mutmut's decorator mutation strips a stub's `@property`, turning
        # the member into a plain method — invisible to the isinstance
        # probes (runtime_checkable checks attribute PRESENCE only), so the
        # descriptor KIND is pinned member-by-member. Read-only properties
        # are also the load-bearing typing choice: a plain protocol
        # variable would demand a SETTABLE attribute and the frozen
        # consumer dataclasses would stop satisfying the stand-ins.
        standin = getattr(obs, standin_name)
        for name in members:
            assert isinstance(inspect.getattr_static(standin, name), property), name

    def test_production_types_satisfy_the_structural_standins(self) -> None:
        # Drift pins BOTH directions: the consumer dataclasses' field sets
        # are exactly the stand-ins' member sets (a rename/addition on
        # either side fails here before the burn-in ever could), and the
        # runtime_checkable isinstance probes prove attribute presence.
        state_fields = {f.name for f in dataclasses.fields(WatchdogState)}
        assert state_fields == {
            "tier",
            "stall_started_beat",
            "last_logged_at",
            "last_alerted_at",
            "episodes",
        }
        verdict_fields = {f.name for f in dataclasses.fields(WatchdogVerdict)}
        assert verdict_fields == {
            "state",
            "lag_s",
            "warn_edge",
            "critical_edge",
            "relog",
            "realert",
            "recovered",
            "stall_total_s",
        }
        assert isinstance(WatchdogState(), obs.WatchdogStateLike)
        assert isinstance(
            WatchdogVerdict(state=WatchdogState(), lag_s=0.0), obs.WatchdogVerdictLike
        )
        # Law C's stand-in is `bound=str`: the StrEnum IS a str subclass.
        assert issubclass(SLAPredicate, str)


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
        fresh = dict.fromkeys(_ALL_LAW_KEYS, obs.BURN_IN_SAMPLES)
        assert fresh == obs._BURN_IN_REMAINING

    def test_reset_remaining_zero_is_the_steady_state(self) -> None:
        obs.reset_observability_rust_for_tests(remaining=0)
        assert not obs._DISABLED_REASON  # empty latch = live
        assert not obs._OBSERVABILITY_MEMO
        assert all(budget == 0 for budget in obs._BURN_IN_REMAINING.values())

    def test_fresh_module_load_starts_unlatched(self) -> None:
        # The import-time INITIALIZER semantics: a fresh process must start
        # live (un-latched, empty memo, full budgets). Every in-suite test
        # runs AFTER the autouse reset — which would mask a poisoned
        # initializer — so execute the module SOURCE fresh under a
        # throwaway name and probe the pristine module-level state.
        spec = importlib.util.spec_from_file_location("obs_fresh_load_probe", obs.__file__)
        assert spec is not None and spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        assert not fresh._DISABLED_REASON  # empty latch = live
        assert fresh.rust_observability_disabled() is False
        assert not fresh._OBSERVABILITY_MEMO
        budgets = dict.fromkeys(_ALL_LAW_KEYS, fresh.BURN_IN_SAMPLES)
        assert budgets == fresh._BURN_IN_REMAINING

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
        # EXACT remedy wording (the sibling-facade discipline —
        # test_lake_rust_resample pins remedy == the literal): key-presence
        # alone let mutmut's XX-wrap string mutants survive, and a substring
        # assertion cannot kill an XX-wrap (the original stays a substring).
        assert events[0]["remedy"] == (
            "pip install rust/brutex-observability (or `maturin develop --release` "
            "inside rust/brutex-observability with the project venv active)"
        )
        # The latch arm must return None — the caller takes the oracle branch
        # DIRECTLY, so no degrade warn ever fires on the unavailable path.
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []

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
        # EXACT message wording (see the crate_unavailable pin above for why
        # substring/key-presence assertions have no mutation teeth here).
        assert events[0]["error_detail"] == (
            "brutex_observability.stamp_verdict missing or not callable — "
            "the installed wheel predates the observability laws (or is foreign/broken)"
        )
        assert events[0]["remedy"] == (
            "rebuild + reinstall rust/brutex-observability "
            "(`maturin develop --release` with the project venv active)"
        )
        # The stale arm also returns None — oracle branch, no degrade warn.
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []

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


def _tw_snapshot(
    *,
    enumerated: int = 100,
    evaluated: int = 0,
    survivors: int = 0,
    skipped: int = 0,
    state: str = STATE_RUNNING,
    started_at: float = 0.0,
    updated_at: float | None = None,
) -> SweepProgressSnapshot:
    """A real :class:`SweepProgressSnapshot` for the seam entry-point pins."""
    return SweepProgressSnapshot(
        state=state,
        enumerated=enumerated,
        evaluated=evaluated,
        survivors=survivors,
        skipped=skipped,
        current="",
        detail="",
        started_at=started_at,
        updated_at=started_at + 10.0 if updated_at is None else updated_at,
    )


def _tw_rate_snapshot(rate: float, evaluated: int) -> SweepProgressSnapshot:
    """A snapshot whose ``combos_per_sec`` reads exactly *rate*."""
    return _tw_snapshot(
        enumerated=10_000, evaluated=evaluated, started_at=0.0, updated_at=evaluated / rate
    )


def _tw_monitor(
    *,
    config: run_tripwires.TripwireConfig | None = None,
    total: int = 100,
    ram: int = 2**30,
    probe: Callable[[], int] | None = None,
) -> tuple[run_tripwires.TripwireMonitor, list[run_tripwires.TripwireEvent]]:
    """A REAL every-tick-samples monitor (memory watches disabled by default)."""
    events: list[run_tripwires.TripwireEvent] = []
    counter = itertools.count(1)
    monitor = run_tripwires.TripwireMonitor(
        config=run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        if config is None
        else config,
        enumerated_total=total,
        clock=lambda: float(next(counter)),
        on_trip=events.append,
        ram_total_bytes=ram,
        rss_probe=(lambda: 1) if probe is None else probe,
    )
    return monitor, events


#: The exact pre-seam T2 overflow detail — the byte pin for the moved template.
_TW_RECONCILE_DETAIL: Final[str] = "evaluated 90 + in_flight 11 > enumerated 100"

#: The exact pre-seam T4a budget detail (rss 501 · RAM 1000 · fraction 0.5).
_TW_BUDGET_DETAIL: Final[str] = "RSS 501 bytes exceeds budget 500 bytes (0.5 × 1000)"

#: The exact pre-seam T4b slope detail (+200 B/tuple > 100 · dwell 2).
_TW_SLOPE_DETAIL: Final[str] = (
    "RSS growing 200 bytes/tuple > 100 bytes/tuple for 2 consecutive samples"
)

#: The exact pre-seam T3 fire detail (rate 1.0 · peak 10.0 · fraction 0.2 · dwell 3).
_TW_THROUGHPUT_DETAIL: Final[str] = (
    "throughput 1.000/s below floor 2.000/s (peak 10.000/s × 0.2) for 3 consecutive samples"
)


def _tw_throughput_config() -> run_tripwires.TripwireConfig:
    return run_tripwires.TripwireConfig(
        reconcile_interval_s=1e-9,
        throughput_floor_fraction=0.2,
        throughput_dwell=3,
        rss_budget_fraction=0.0,
        memory_slope_bytes_per_tuple=0,
    )


def _tw_drive_throughput_fire(monitor: run_tripwires.TripwireMonitor) -> None:
    """Peak 10.0 then three below-floor samples — the dwell FIRE edge."""
    monitor.on_progress(_tw_rate_snapshot(10.0, 100))
    monitor.on_progress(_tw_rate_snapshot(1.0, 200))
    monitor.on_progress(_tw_rate_snapshot(1.0, 300))
    monitor.on_progress(_tw_rate_snapshot(1.0, 400))


class TestTripwireSeamEntryPoints:
    """The T2/T3 seam pins through the REAL :class:`TripwireMonitor` entry
    points (DEC-RUST-OBSERVABILITY-TRIPWIRES-002 S2): OFF-arm byte-identity
    with the crate import BOMBED, ON-arm engagement with the run_tripwires
    ORACLE bombed (a correct event proves the fake crate decided), and the
    I-3 MemoryError-landing INVERSION pinned on BOTH arms."""

    def test_off_reconcile_trip_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monitor, events = _tw_monitor()
        monitor.on_progress(_tw_snapshot(evaluated=90), in_flight=11)
        assert [e.detail for e in events] == [_TW_RECONCILE_DETAIL]
        assert events[0].name == "count_reconciliation"
        assert events[0].values == {
            "enumerated": 100.0,
            "evaluated": 90.0,
            "skipped": 0.0,
            "survivors": 0.0,
            "in_flight": 11.0,
        }
        assert monitor.suspect is True

    def test_off_throughput_fire_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monitor, events = _tw_monitor(config=_tw_throughput_config(), total=10_000)
        _tw_drive_throughput_fire(monitor)
        assert [e.detail for e in events] == [_TW_THROUGHPUT_DETAIL]
        assert events[0].values == {"rate": 1.0, "floor": 2.0, "peak": 10.0}

    def test_off_throughput_recovery_rearms_and_refires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fire → recover (the TW_CLEAR apply arm re-arms the key) → a fresh
        # dwell run fires AGAIN — the re-arm law the apply's CLEAR arm owns.
        # dwell=1 keeps NONE samples out of the sequence, so ONLY the CLEAR
        # arm can re-arm — killing the `TW_CLEAR → TW_NONE` apply-compare
        # mutant class the protected 81-test file leaves unpinned.
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.2,
            throughput_dwell=1,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = _tw_monitor(config=config, total=10_000)
        monitor.on_progress(_tw_rate_snapshot(10.0, 100))  # peak
        monitor.on_progress(_tw_rate_snapshot(1.0, 200))  # below #1 → fire #1
        monitor.on_progress(_tw_rate_snapshot(9.0, 300))  # recovery → re-arm
        monitor.on_progress(_tw_rate_snapshot(1.0, 400))  # below #1 → fire #2
        assert [e.name for e in events] == ["throughput_floor", "throughput_floor"]

    def test_on_reconcile_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_reconcile_decide_python", _oracle_bomb)
        monitor, events = _tw_monitor()
        monitor.on_progress(_tw_snapshot(evaluated=90), in_flight=11)
        assert [e.detail for e in events] == [_TW_RECONCILE_DETAIL]
        assert monitor.suspect is True

    def test_on_throughput_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_throughput_decide_python", _oracle_bomb)
        monitor, events = _tw_monitor(config=_tw_throughput_config(), total=10_000)
        _tw_drive_throughput_fire(monitor)
        assert [e.detail for e in events] == [_TW_THROUGHPUT_DETAIL]

    def test_memory_error_inversion_crate_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The facade re-raises MemoryError RAW; the I-3 blanket except
        # CATCHES it (an Exception subclass) → observe_failed — the
        # documented OOM-landing INVERSION vs the loop-watchdog lane.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(tripwire_reconcile_counts=_raiser(MemoryError("oom"))))
        obs.reset_observability_rust_for_tests(remaining=0)
        monitor, events = _tw_monitor()
        with capture_logs() as logs:
            monitor.on_progress(_tw_snapshot(evaluated=90), in_flight=11)
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [(w["method"], w["error_type"]) for w in warns] == [("on_progress", "MemoryError")]
        assert events == []
        assert monitor.suspect is False

    def test_memory_error_inversion_oracle_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Flag OFF, the ORACLE OOMs — identical observable behavior.
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monkeypatch.setattr(run_tripwires, "_reconcile_decide_python", _raiser(MemoryError("oom")))
        monitor, events = _tw_monitor()
        with capture_logs() as logs:
            monitor.on_progress(_tw_snapshot(evaluated=90), in_flight=11)
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [(w["method"], w["error_type"]) for w in warns] == [("on_progress", "MemoryError")]
        assert events == []
        assert monitor.suspect is False

    def test_keyboard_interrupt_escapes_the_observation_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The escape trio propagates PAST the I-3 blanket on both arms.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(tripwire_reconcile_counts=_raiser(KeyboardInterrupt())))
        obs.reset_observability_rust_for_tests(remaining=0)
        monitor, _events = _tw_monitor()
        with pytest.raises(KeyboardInterrupt):
            monitor.on_progress(_tw_snapshot(evaluated=90), in_flight=11)

    # ── S3 — the T4a/T4b/T5/T9 seams ──────────────────────────────────────

    @staticmethod
    def _budget_config() -> run_tripwires.TripwireConfig:
        return run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.5,
            memory_slope_bytes_per_tuple=0,
        )

    @staticmethod
    def _slope_config() -> run_tripwires.TripwireConfig:
        return run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=100,
            memory_dwell=2,
        )

    def test_off_memory_budget_trip_never_imports_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monitor, events = _tw_monitor(config=self._budget_config(), ram=1000, probe=lambda: 501)
        monitor.on_progress(_tw_snapshot(evaluated=1))
        assert [e.detail for e in events] == [_TW_BUDGET_DETAIL]
        assert events[0].values == {"rss_bytes": 501.0, "budget_bytes": 500.0}

    def test_on_memory_budget_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_memory_budget_decide_python", _oracle_bomb)
        monitor, events = _tw_monitor(config=self._budget_config(), ram=1000, probe=lambda: 501)
        monitor.on_progress(_tw_snapshot(evaluated=1))
        assert [e.detail for e in events] == [_TW_BUDGET_DETAIL]

    def test_on_memory_slope_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_memory_slope_decide_python", _oracle_bomb)
        readings = iter([1000, 1200, 1400])
        monitor, events = _tw_monitor(
            config=self._slope_config(), ram=10**12, probe=lambda: next(readings)
        )
        monitor.on_progress(_tw_snapshot(evaluated=1))  # first sample: no prev
        monitor.on_progress(_tw_snapshot(evaluated=2))  # over #1
        monitor.on_progress(_tw_snapshot(evaluated=3))  # over #2 → the dwell edge
        assert [e.detail for e in events] == [_TW_SLOPE_DETAIL]
        assert events[0].values == {"slope_bytes_per_tuple": 200.0, "rss_bytes": 1400.0}

    def test_off_slope_recovery_rearms_and_refires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fire → recover (the TW_CLEAR apply arm re-arms the key) → a fresh
        # over-slope sample fires AGAIN. memory_dwell=1 keeps NONE samples
        # out of the sequence, so ONLY the CLEAR arm can re-arm — killing
        # the slope `TW_CLEAR → TW_NONE` apply-compare mutant class the
        # protected 81-test file leaves unpinned.
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=100,
            memory_dwell=1,
        )
        readings = iter([1000, 1200, 1200, 1400])
        monitor, events = _tw_monitor(config=config, ram=10**12, probe=lambda: next(readings))
        monitor.on_progress(_tw_snapshot(evaluated=1))  # first sample: no prev
        monitor.on_progress(_tw_snapshot(evaluated=2))  # +200/t → fire #1
        monitor.on_progress(_tw_snapshot(evaluated=3))  # flat → recovery re-arms
        monitor.on_progress(_tw_snapshot(evaluated=4))  # +200/t → fire #2
        assert [e.name for e in events] == ["memory_slope", "memory_slope"]

    def test_off_summary_nan_trip_never_imports_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monitor, events = _tw_monitor()
        monitor.on_tuple_summary({"sharpe": math.nan})
        assert [e.detail for e in events] == ["non-finite metric-summary value: sharpe=nan"]
        assert monitor.suspect is True

    def test_on_summary_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_summary_classify_python", _oracle_bomb)
        monitor, events = _tw_monitor()
        monitor.on_tuple_summary({"sharpe": math.nan, "net_pnl_paisa": 2e15})
        # Sorted iteration: net_pnl_paisa (absurd) precedes sharpe (NaN).
        assert [e.name for e in events] == ["absurd_value", "nan_metric"]
        assert events[0].detail == (
            "absurd metric-summary magnitude: net_pnl_paisa=2000000000000000.0 "
            "(|value| > 1000000000000000 paisa)"
        )
        assert events[1].detail == "non-finite metric-summary value: sharpe=nan"

    def test_off_ru_maxrss_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=1000, platform="linux") == (
            1_024_000
        )
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=1000, platform="darwin") == 1000

    # ── S4 — the T6 seam ──────────────────────────────────────────────────

    def test_off_stage_trip_never_imports_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        monitor, events = _tw_monitor()
        monitor.on_stage("artifacts", stamps_present=frozenset({"cascade_manifest"}))
        assert [e.detail for e in events] == [
            "stage 'artifacts' entered without prerequisite stamp(s): ['cascade_report']"
        ]
        assert events[0].values == {"missing": 1.0}
        assert monitor.suspect is True

    def test_on_stage_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_stage_missing_python", _oracle_bomb)
        monitor, events = _tw_monitor()
        monitor.on_stage("cascade", stamps_present=frozenset())
        assert [e.detail for e in events] == [
            "stage 'cascade' entered without prerequisite stamp(s): ['stage3']"
        ]
        # A satisfied stage decides an all-False mask — clear, no event.
        monitor.on_stage("stage3", stamps_present=frozenset({"stage2"}))
        assert len(events) == 1

    def test_on_ru_maxrss_served_by_fake_crate_oracle_bombed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        # A LITERAL crate fn (the default delegate calls the very oracle we
        # bomb below).
        _install(
            monkeypatch,
            _fake_crate(
                tripwire_ru_maxrss_bytes=lambda ru, plat: (
                    int(ru) if plat == "darwin" else int(ru) * 1024
                )
            ),
        )
        obs.reset_observability_rust_for_tests(remaining=0)
        monkeypatch.setattr(run_tripwires, "_ru_maxrss_python", _oracle_bomb)
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=1000, platform="linux") == (
            1_024_000
        )
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=7, platform="darwin") == 7


class TestRunGateSupplementalPins:
    """Run_gate remediation killers (DEC-RUST-OBSERVABILITY-TRIPWIRES-002 S6).

    The ci.yml `run_tripwires` mutation run_gate (floor 0.75) mutates
    `run_tripwires.py` under its runner. The S2-S4 seams ADDED the
    oracle/materializer/bit-const mutant surface, and its kills need
    EXACT-equality asserts (the FIX-ROUND-1 C3 lesson — mutmut's XX-wrap
    keeps every inner substring intact, so a substring assert passes on a
    wrapped template) which the PROTECTED 81-test file cannot gain (its
    test list is frozen — restructure-not-new-tests applies to COVERAGE,
    not to a mutation-score regression). MEASURED at the seam head
    (CI-replica, mutmut 2.5.1, wheel-free): 406 mutants / 115 survived
    under the 81 alone = 71.7% — BELOW the floor. Remedy = the
    DEC-CONFIG-RUST-LEAVES-001 money.py precedent: the run_gate runner
    ALSO drives THIS file, and this class carries the killers (direct
    oracle calls + literal pins — wheel-free, flag-independent). The one
    disclosed equivalent survivor: the FIRST `mask |=` in
    `_reconcile_decide_python` (`mask` is still 0 there, so `|=` ≡ `=`).
    """

    def test_reconcile_bit_constants_exact(self) -> None:
        # Bit i == predicate i in the pre-seam append order — the detail
        # materializer + the crate mirror both key on these exact values.
        assert run_tripwires._RECONCILE_BIT_NEGATIVE_IN_FLIGHT == 1
        assert run_tripwires._RECONCILE_BIT_ENUMERATED_DRIFT == 2
        assert run_tripwires._RECONCILE_BIT_SKIPPED_OVERFLOW == 4
        assert run_tripwires._RECONCILE_BIT_SURVIVORS_OVERFLOW == 8
        assert run_tripwires._RECONCILE_BIT_IN_FLIGHT_OVERFLOW == 16
        assert run_tripwires._RECONCILE_BIT_BACKWARDS == 32
        assert run_tripwires._RECONCILE_BIT_DONE_INCOMPLETE == 64

    def test_reconcile_decide_all_seven_violations_fold(self) -> None:
        # Every predicate true at once → the full 7-bit mask; a `mask =`
        # (fold-dropping) mutant on any non-first arm loses an earlier bit.
        mask = run_tripwires._reconcile_decide_python(-1, 90, 200, 500, 300, 100, 300, True)
        assert mask == 0b1111111

    @pytest.mark.parametrize(
        ("mask", "kwargs", "expected"),
        [
            (1, {"in_flight": -1}, "in_flight -1 < 0"),
            (
                2,
                {"enumerated": 90, "total": 100},
                "snapshot enumerated 90 != armed total 100",
            ),
            (
                4,
                {"skipped": 5, "evaluated": 2},
                "skipped 5 > evaluated 2 (skipped ⊆ evaluated)",
            ),
            (8, {"survivors": 3, "evaluated": 2}, "survivors 3 > evaluated 2"),
            (
                16,
                {"evaluated": 90, "in_flight": 11, "total": 100},
                "evaluated 90 + in_flight 11 > enumerated 100",
            ),
            (
                32,
                {"evaluated": 49, "prev_evaluated": 50},
                "evaluated went backwards: 49 < 50",
            ),
            (
                64,
                {"evaluated": 2, "total": 100},
                "run done but evaluated 2 != enumerated 100",
            ),
        ],
    )
    def test_reconcile_details_single_violation_exact(
        self, mask: int, kwargs: dict[str, int], expected: str
    ) -> None:
        # EXACT equality (never a substring): kills the per-template
        # XX-wrap garbles AND the `mask & → mask |` bit-test garbles (a
        # `|`-test is always truthy, so extra templates would append).
        defaults: dict[str, int | None] = {
            "in_flight": 0,
            "enumerated": 100,
            "evaluated": 0,
            "skipped": 0,
            "survivors": 0,
            "total": 100,
            "prev_evaluated": None,
        }
        defaults.update(kwargs)
        assert run_tripwires._reconcile_details(mask, **defaults) == expected  # type: ignore[arg-type]

    def test_reconcile_details_joined_in_bit_order_exact(self) -> None:
        detail = run_tripwires._reconcile_details(
            1 | 64,
            in_flight=-1,
            enumerated=100,
            evaluated=2,
            skipped=0,
            survivors=0,
            total=100,
            prev_evaluated=None,
        )
        assert detail == "in_flight -1 < 0; run done but evaluated 2 != enumerated 100"

    def test_throughput_decide_zero_peak_arm_exact(self) -> None:
        # `new_peak <= 0.0` (not `<`): the all-zero regime decides NOTHING
        # — `below` carried untouched, floor exactly 0.0, code TW_NONE.
        assert run_tripwires._throughput_decide_python(0.0, 0.0, 3, 0.5, 5) == (
            0.0,
            3,
            0.0,
            obs.TW_NONE,
        )

    def test_throughput_decide_positive_peak_boundary_exact(self) -> None:
        # A positive peak ≤ 1.0 must NOT take the zero-peak arm; at-floor
        # rate is the CLEAR arm with the exact floor value.
        assert run_tripwires._throughput_decide_python(0.5, 0.5, 0, 0.4, 5) == (
            0.5,
            0,
            0.2,
            obs.TW_CLEAR,
        )

    def test_slope_decide_divides_never_multiplies(self) -> None:
        # An evaluated-diff of 2 distinguishes `/` from `*` (the protected
        # matrix only ever divides by 1, where they coincide).
        assert run_tripwires._memory_slope_decide_python(1300, 3, 1100, 1, 150.0, 0, 1) == (
            100.0,
            0,
            obs.TW_CLEAR,
        )

    def test_summary_classify_strict_zip_refuses_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="zip"):
            run_tripwires._summary_classify_python([1.0, 2.0], [True], 0.5)

    def test_summary_classify_nonfinite_continues_never_breaks(self) -> None:
        nan_codes, absurd_codes = run_tripwires._summary_classify_python(
            [float("nan"), 1.0], [False, False], 0.5
        )
        assert nan_codes == [obs.TW_FIRE, obs.TW_CLEAR]
        assert absurd_codes == [obs.TW_NONE, obs.TW_NONE]

    def test_on_tuple_summary_strict_zip_over_routed_code_arrays(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The seam belt: a length-mismatched routed code array (only a
        # poisoned facade could mint one — the output gate refuses it from
        # a crate) must raise into the I-3 blanket, never silently truncate
        # the screen walk.
        monkeypatch.setattr(
            obs, "routed_tripwire_summary", lambda *a, **k: ([obs.TW_CLEAR], [obs.TW_NONE])
        )
        monitor, events = _tw_monitor()
        with capture_logs() as logs:
            monitor.on_tuple_summary({"a": 1.0, "b": 2.0})
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [(w["method"], w["error_type"]) for w in warns] == [
            ("on_tuple_summary", "ValueError")
        ]
        assert events == []

    def test_on_stage_strict_zip_over_routed_missing_mask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(obs, "routed_tripwire_stage", lambda *a, **k: [])
        monitor, events = _tw_monitor()
        with capture_logs() as logs:
            monitor.on_stage("cascade", stamps_present=frozenset())
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [(w["method"], w["error_type"]) for w in warns] == [("on_stage", "ValueError")]
        assert events == []

    def test_env_knob_names_exact(self) -> None:
        # The from_env contract — a garbled knob name silently orphans the
        # operator's env value.
        assert run_tripwires.ENV_RECONCILE_INTERVAL_S == (
            "BRUTEX_SWEEP_TRIPWIRE_RECONCILE_INTERVAL_S"
        )
        assert run_tripwires.ENV_THROUGHPUT_FLOOR == "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_FLOOR"
        assert run_tripwires.ENV_THROUGHPUT_DWELL == "BRUTEX_SWEEP_TRIPWIRE_THROUGHPUT_DWELL"
        assert run_tripwires.ENV_RSS_BUDGET_FRACTION == (
            "BRUTEX_SWEEP_TRIPWIRE_RSS_BUDGET_FRACTION"
        )
        assert run_tripwires.ENV_MEMORY_SLOPE_BYTES == "BRUTEX_SWEEP_TRIPWIRE_MEMORY_SLOPE_BYTES"
        assert run_tripwires.ENV_MEMORY_DWELL == "BRUTEX_SWEEP_TRIPWIRE_MEMORY_DWELL"
        assert run_tripwires.ENV_ABSURD_PNL_PAISA == "BRUTEX_SWEEP_TRIPWIRE_ABSURD_PNL_PAISA"


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


class _StrSub(str):
    """A str subclass — could override ``__eq__``; the T6 exact-str gate refuses it."""


def _tw_slope_sentinel(
    rss: int,
    evaluated: int,
    prev_rss: int,
    prev_evaluated: int,
    slope_threshold: float,
    slope_high: int,
    dwell: int,
) -> tuple[float, int, int]:
    """A sentinel T4b oracle for refused-arm pins whose REAL law would divide
    by zero (the consumer's progress gate normally excludes those shapes —
    the facade must still serve the oracle VERBATIM on the refusal arm)."""
    return (-7.0, -7, -7)


def _tw_summary_sentinel(
    values: list[float], screened: list[bool], absurd_threshold: float
) -> tuple[list[int], list[int]]:
    """A sentinel T5 oracle for the length-mismatch refusal (the REAL law's
    strict zip would raise — today's I-3 fail-soft input, preserved)."""
    return ([-7], [-7])


#: (id, routed call, the ORACLE's expected value, the warn-once reason) — one
#: row per tripwire input-gate refusal class. The out-of-i64 / counter-cap
#: rows pin the UNBOUNDED-int honesty: Python computes the exact answer the
#: crate's i64 cannot, so the gate must serve it, never truncate.
_TW_INPUT_REFUSALS: Final[tuple[tuple[str, Callable[[], object], object, str], ...]] = (
    (
        "reconcile_out_of_i64_counter",
        lambda: obs.routed_tripwire_reconcile(
            2**63, 10, 5, 0, 0, 10, None, False, python_law=_tw_reconcile_law
        ),
        16,  # evaluated + in_flight > total — the exact unbounded-int compare
        "tw_reconcile_input_refused",
    ),
    (
        "reconcile_bool_prev_evaluated",
        lambda: obs.routed_tripwire_reconcile(
            0, 10, 5, 0, 0, 10, True, False, python_law=_tw_reconcile_law
        ),
        0,
        "tw_reconcile_input_refused",
    ),
    (
        "reconcile_int_is_done",
        lambda: obs.routed_tripwire_reconcile(
            0, 10, 5, 0, 0, 10, None, cast("bool", 1), python_law=_tw_reconcile_law
        ),
        64,  # truthy-int is_done drives the oracle's done-mismatch bit
        "tw_reconcile_input_refused",
    ),
    (
        "throughput_int_rate",
        lambda: obs.routed_tripwire_throughput(
            cast("float", 10), 100.0, 4, 0.2, 5, python_law=_tw_throughput_law
        ),
        (100.0, 5, 20.0, 2),
        "tw_throughput_input_refused",
    ),
    (
        "throughput_negative_below",
        lambda: obs.routed_tripwire_throughput(
            10.0, 100.0, -1, 0.2, 5, python_law=_tw_throughput_law
        ),
        (100.0, 0, 20.0, 0),
        "tw_throughput_input_refused",
    ),
    (
        "throughput_zero_dwell",
        lambda: obs.routed_tripwire_throughput(
            10.0, 100.0, 4, 0.2, 0, python_law=_tw_throughput_law
        ),
        (100.0, 5, 20.0, 0),
        "tw_throughput_input_refused",
    ),
    (
        "throughput_below_at_counter_cap",
        lambda: obs.routed_tripwire_throughput(
            10.0, 100.0, 2**63 - 1, 0.2, 5, python_law=_tw_throughput_law
        ),
        (100.0, 2**63, 20.0, 0),  # the unbounded increment — checked_add unreachable
        "tw_throughput_input_refused",
    ),
    (
        "budget_bool_rss",
        lambda: obs.routed_tripwire_memory_budget(
            cast("int", True), 1000, 0.5, python_law=_tw_budget_law
        ),
        (500.0, 1),
        "tw_budget_input_refused",
    ),
    (
        "budget_int_fraction",
        lambda: obs.routed_tripwire_memory_budget(
            501, 1000, cast("float", 1), python_law=_tw_budget_law
        ),
        (1000.0, 1),
        "tw_budget_input_refused",
    ),
    (
        "slope_rss_diff_past_exactness_fence",
        lambda: obs.routed_tripwire_memory_slope(
            2**53 + 1, 20, 0, 10, 50.0, 4, 5, python_law=_tw_slope_law
        ),
        ((2**53 + 1) / 10, 5, 2),  # Python's correctly-rounded int/int division
        "tw_slope_input_refused",
    ),
    (
        "slope_no_progress",
        lambda: obs.routed_tripwire_memory_slope(
            2000, 10, 1000, 10, 50.0, 4, 5, python_law=_tw_slope_sentinel
        ),
        (-7.0, -7, -7),
        "tw_slope_input_refused",
    ),
    (
        "slope_evaluated_diff_past_exactness_fence",
        lambda: obs.routed_tripwire_memory_slope(
            2000, 2**53 + 11, 1000, 10, 50.0, 4, 5, python_law=_tw_slope_law
        ),
        (1000 / (2**53 + 1), 0, 1),
        "tw_slope_input_refused",
    ),
    (
        "summary_length_mismatch",
        lambda: obs.routed_tripwire_summary(
            [1.0, 2.0], [False], 10.0, python_law=_tw_summary_sentinel
        ),
        ([-7], [-7]),
        "tw_summary_input_refused",
    ),
    (
        "summary_int_value",
        lambda: obs.routed_tripwire_summary(
            cast("list[float]", [1]), [False], 10.0, python_law=_tw_summary_law
        ),
        ([1], [0]),
        "tw_summary_input_refused",
    ),
    (
        "summary_int_screened_flag",
        lambda: obs.routed_tripwire_summary(
            [1.0], cast("list[bool]", [1]), 10.0, python_law=_tw_summary_law
        ),
        ([1], [1]),  # the oracle treats the truthy int as screened — preserved
        "tw_summary_input_refused",
    ),
    (
        "stage_int_required_token",
        lambda: obs.routed_tripwire_stage(
            cast("list[str]", ["a", 2]), [], python_law=_tw_stage_law
        ),
        [True, True],
        "tw_stage_input_refused",
    ),
    (
        "stage_str_subclass_member",
        lambda: obs.routed_tripwire_stage(["a"], [_StrSub("a")], python_law=_tw_stage_law),
        [False],  # Python's own membership semantics, preserved verbatim
        "tw_stage_input_refused",
    ),
    (
        "rss_bool_ru_maxrss",
        lambda: obs.routed_ru_maxrss_bytes(cast("int", True), "linux", python_law=_tw_rss_law),
        1024,
        "tw_rss_input_refused",
    ),
    (
        "rss_out_of_i64",
        lambda: obs.routed_ru_maxrss_bytes(2**63, "linux", python_law=_tw_rss_law),
        2**63 * 1024,  # the oracle's unbounded-int product
        "tw_rss_input_refused",
    ),
    (
        "rss_bytes_platform",
        lambda: obs.routed_ru_maxrss_bytes(1, cast("str", b"linux"), python_law=_tw_rss_law),
        1024,
        "tw_rss_input_refused",
    ),
)


class TestTripwireInputGates:
    """The tripwire exact-type / i64 / counter-cap / 2^53 INPUT gates.

    Every refusal serves the ORACLE with one named warn and NEVER touches
    the crate (import bombed); refusals never latch.
    """

    @pytest.mark.parametrize(
        "case", _TW_INPUT_REFUSALS, ids=[entry[0] for entry in _TW_INPUT_REFUSALS]
    )
    def test_gate_refuses_to_oracle_without_crate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case: tuple[str, Callable[[], object], object, str],
    ) -> None:
        _flag_on(monkeypatch)
        monkeypatch.setattr(obs.importlib, "import_module", _bomb_import)
        _, call, expected, reason = case
        with capture_logs() as logs:
            assert call() == expected
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == reason
        assert obs.rust_observability_disabled() is False  # a refusal never latches


class TestTripwireGateBoundaryAcceptance:
    """The ACCEPT side of every routed tripwire gate BOUNDARY.

    Each legal-boundary input must take the CRATE path — proven by the
    burn-in budget decrement (the oracle/refusal arms never decrement) plus
    ZERO fallback warns. These are the named killers for the routed-level
    gate-argument mutants (`minimum=0→1` / `1→2`, `<=`→`<`, `0 <`→`1 <`,
    `abs(rss - prev)`→`abs(rss + prev)`) the helper truth tables cannot see.
    """

    def test_throughput_below_zero_and_dwell_one_are_routed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # below == 0 (minimum=0 boundary) AND dwell == 1 (minimum=1
        # boundary) in ONE call — either refusal mutant fails this.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_tripwire_throughput(
                10.0, 100.0, 0, 0.2, 1, python_law=_tw_throughput_law
            )
        assert result == (100.0, 1, 20.0, 2)  # first below-floor sample fires at dwell 1
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []
        assert obs._BURN_IN_REMAINING["tw_throughput"] == obs.BURN_IN_SAMPLES - 1

    def test_slope_all_low_boundaries_are_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # slope_high == 0, dwell == 1, |rss − prev_rss| == 2**53 EXACTLY
        # (the closed fence) and evaluated − prev_evaluated == 1 (the open
        # progress floor) in ONE call — any of the four boundary mutants
        # flips this to a refusal.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_tripwire_memory_slope(
                2**53, 11, 0, 10, 50.0, 0, 1, python_law=_tw_slope_law
            )
        assert result == (float(2**53), 1, 2)  # fires at dwell 1
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []
        assert obs._BURN_IN_REMAINING["tw_slope"] == obs.BURN_IN_SAMPLES - 1

    def test_slope_evaluated_diff_at_fence_is_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # evaluated − prev_evaluated == 2**53 EXACTLY (the closed fence).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_tripwire_memory_slope(
                1000, 2**53 + 10, 0, 10, 50.0, 4, 5, python_law=_tw_slope_law
            )
        assert result == (1000 / 2**53, 0, 1)
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []
        assert obs._BURN_IN_REMAINING["tw_slope"] == obs.BURN_IN_SAMPLES - 1

    def test_slope_large_equal_rss_values_are_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # rss == prev_rss == 2**52 + 1: the DIFF is 0 (accepted) while the
        # SUM is past the fence — the `abs(rss + prev_rss)` operator mutant
        # refuses exactly this shape.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_tripwire_memory_slope(
                2**52 + 1, 20, 2**52 + 1, 10, 50.0, 0, 5, python_law=_tw_slope_law
            )
        assert result == (0.0, 0, 1)
        assert [e for e in logs if e["event"] == "observability_rust.fallback"] == []
        assert obs._BURN_IN_REMAINING["tw_slope"] == obs.BURN_IN_SAMPLES - 1


class TestTripwireGateHelpers:
    """Direct truth tables over the tripwire input/output gate helpers."""

    def test_is_gated_int_boundaries(self) -> None:
        assert obs._is_gated_int(0) is True
        assert obs._is_gated_int(-(2**63)) is True
        assert obs._is_gated_int(2**63 - 1) is True
        assert obs._is_gated_int(2**63) is False
        assert obs._is_gated_int(-(2**63) - 1) is False
        assert obs._is_gated_int(True) is False
        assert obs._is_gated_int(0.0) is False

    def test_is_gated_counter_boundaries(self) -> None:
        assert obs._is_gated_counter(1, minimum=1) is True
        assert obs._is_gated_counter(0, minimum=1) is False
        assert obs._is_gated_counter(0, minimum=0) is True
        assert obs._is_gated_counter(-1, minimum=0) is False
        assert obs._is_gated_counter(2**63 - 2, minimum=0) is True
        assert obs._is_gated_counter(2**63 - 1, minimum=0) is False
        assert obs._is_gated_counter(True, minimum=0) is False

    @pytest.mark.parametrize(
        ("mask", "prev_missing", "ok"),
        [
            (0, True, True),
            (64, True, True),
            (95, True, True),  # every bit EXCEPT bit 5 — legal without prev
            (127, False, True),
            (32, True, False),  # bit 5 without a prev sample — impossible-by-law
            (127, True, False),
            (-1, False, False),
            (128, False, False),
            (True, False, False),  # bool bitmask (int subclass)
            (1.0, False, False),
        ],
    )
    def test_reconcile_result_gate(self, mask: object, prev_missing: bool, ok: bool) -> None:
        assert obs._tw_reconcile_result_ok(mask, prev_missing=prev_missing) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [
            ((100.0, 5, 20.0, 2), True),
            ((100.0, 0, 20.0, 0), True),
            ((100.0, 5, 20.0, 3), False),
            ((100.0, 5, 20.0, True), False),
            ((100.0, True, 20.0, 2), False),
            ((100.0, 5, 20, 2), False),
            ((100, 5, 20.0, 2), False),
            ((100.0, 5, 20.0), False),
            ([100.0, 5, 20.0, 2], False),
        ],
    )
    def test_throughput_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_throughput_result_ok(value) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [
            ((500.0, 1), True),
            ((500.0, 2), True),
            ((500.0, 0), False),  # TW_NONE — the budget arm always decides
            ((500, 1), False),
            ((500.0, True), False),
            ((500.0, 1, 0), False),
            ([500.0, 1], False),
        ],
    )
    def test_budget_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_budget_result_ok(value) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [
            ((100.0, 5, 2), True),
            ((100.0, 0, 1), True),
            ((100.0, 5, 3), False),
            ((100.0, 5.0, 2), False),
            ((100.0, 5, True), False),
            ((100, 5, 2), False),
            ((100.0, 5), False),
            ([100.0, 5, 2], False),
        ],
    )
    def test_slope_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_slope_result_ok(value) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [
            (([1, 2], [0, 1]), True),
            (([1, 2], [0]), False),
            (([1, 2, 0], [0, 1]), False),
            (((1, 2), [0, 1]), False),
            (([1, 3], [0, 1]), False),
            (([1, True], [0, 1]), False),
            (([1, 2], [0, 1], []), False),
            ([[1, 2], [0, 1]], False),
        ],
    )
    def test_summary_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_summary_result_ok(value, expected_len=2) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [
            ([True, False], True),
            ([], False),
            ([True], False),
            ([True, 1], False),
            ((True, False), False),
        ],
    )
    def test_stage_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_stage_result_ok(value, expected_len=2) is ok

    @pytest.mark.parametrize(
        ("value", "ok"),
        [(5, True), (0, True), (-5, True), (True, False), (5.0, False), ("5", False)],
    )
    def test_rss_result_gate(self, value: object, ok: bool) -> None:
        assert obs._tw_rss_result_ok(value) is ok


class TestTripwireOutputGates:
    """Routed-level output refusals BEYOND the shared per-law poison arm."""

    def test_reconcile_bit5_without_prev_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A poisoned crate minting the evaluated-went-backwards bit while NO
        # prev sample exists (impossible-by-law) is refused wholesale — a
        # foreign crate can degrade, never fire a phantom violation.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(tripwire_reconcile_counts=lambda *a: 32))
        with capture_logs() as logs:
            assert _call_tw_reconcile() == 1  # the oracle's bit-0 mask
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "tw_reconcile_result_refused"
        assert obs.rust_observability_disabled() is False

    def test_reconcile_bit5_with_prev_is_legal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        obs.reset_observability_rust_for_tests(remaining=0)  # steady state
        _install(monkeypatch, _fake_crate(tripwire_reconcile_counts=lambda *a: 32))
        result = obs.routed_tripwire_reconcile(
            0, 10, 5, 0, 0, 10, 10, False, python_law=_oracle_bomb
        )
        assert result == 32  # evaluated 5 < prev 10 — the crate value ships

    @pytest.mark.parametrize("mask", [-1, 128, 2**63])
    def test_reconcile_out_of_vocabulary_mask_refused(
        self, monkeypatch: pytest.MonkeyPatch, mask: int
    ) -> None:
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(tripwire_reconcile_counts=lambda *a: mask))
        assert _call_tw_reconcile() == 1
        assert "tw_reconcile_result_refused" in obs._OBSERVABILITY_MEMO

    def test_summary_inflated_code_array_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The truncated twin rides the shared poison arm; the INFLATED array
        # (input-aligned law broken the other way) must refuse too.
        _flag_on(monkeypatch)
        _install(
            monkeypatch,
            _fake_crate(tripwire_summary_classify=lambda *a: ([1, 2, 1, 1], [0, 0, 2, 0])),
        )
        assert _call_tw_summary() == ([1, 2, 1], [0, 0, 2])
        assert "tw_summary_result_refused" in obs._OBSERVABILITY_MEMO

    def test_pre_tripwire_wheel_latches_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A -001-era wheel (all five detector/stamp fns, NO tripwire fns)
        # fails the all-or-nothing handshake — EVERY law (the A-D five
        # included) stays on the oracle; the crate_stale detail names the
        # missing tripwire symbol. The -002 crate_stale teeth.
        _flag_on(monkeypatch)
        crate = _fake_crate()
        delattr(crate, "tripwire_reconcile_counts")
        _install(monkeypatch, crate)
        with capture_logs() as logs:
            assert _call_sla() == (True, True)
        assert obs._DISABLED_REASON[0] == "crate_stale"
        events = [e for e in logs if e["event"] == "observability_rust.rust_path_disabled"]
        assert "tripwire_reconcile_counts" in events[0]["error_detail"]


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

    def test_burn_in_clean_ships_the_crate_value_not_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A CLEAN burn-in compare must ship the CRATE value (`_burn_in_check`
        # returns True), not fall back to the oracle: on law A's OK→OK arm
        # the two are distinguishable by OBJECT identity — the oracle's
        # verdict carries the input state object ITSELF, the crate path
        # recomposes a fresh (equal) one. This kills the `return True` →
        # `return False` mutant, which the value-equality burn-in pins above
        # cannot see (both arms are value-equal during a clean burn-in).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        assert obs._BURN_IN_REMAINING["loop"] > 0  # genuinely inside burn-in
        verdict = _call_loop()
        assert verdict == _oracle_loop()
        assert verdict.state is not _STATE_OK  # the recomposed CRATE object shipped

    def test_watchdog_burn_in_clean_when_hung_differs_from_edge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A burn-in-clean compare whose oracle triple has hung != edge
        # (steady-hung: prev_hung=True, still hung → (20.0, True, False)).
        # The canonical (20.0, True, True) case leaves the two bool slots
        # EQUAL, so a pack-leg index garble (`oracle[1]` → `oracle[2]`) was
        # invisible — a real mutmut survivor pre-fix. A garbled pack leg
        # fabricates a mismatch → a FALSE autorevert; pin the compare CLEAN.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_watchdog_decide(
                20.0, 0.0, 15.0, True, python_law=_worker_decide_python
            )
        assert result == (20.0, True, False)
        assert obs.rust_observability_disabled() is False
        assert [e for e in logs if e["event"] == "observability_rust.autoreverted"] == []
        assert obs._BURN_IN_REMAINING["watchdog"] == obs.BURN_IN_SAMPLES - 1

    def test_sla_burn_in_clean_when_breached_differs_from_edge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sla twin: a still-breached observation (prev_breach=True) has
        # (breached, edge) == (True, False) — the two slots DIFFER, so a
        # pack-leg index garble (`oracle[0]` → `oracle[1]`) fabricates a
        # mismatch → a FALSE autorevert (a real mutmut survivor pre-fix).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            result = obs.routed_sla_decide(
                SLAPredicate.GREATER_THAN, 150.0, 100.0, True, python_law=_sla_decide_python
            )
        assert result == (True, False)
        assert obs.rust_observability_disabled() is False
        assert [e for e in logs if e["event"] == "observability_rust.autoreverted"] == []
        assert obs._BURN_IN_REMAINING["sla"] == obs.BURN_IN_SAMPLES - 1


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
        # The once-only latch lives in the SHARED warn memo under the
        # documented dunder-namespaced literal (namespaced APART from the
        # fallback reasons, so neither can silence the other) — pinned by
        # VALUE so a memo-key garble cannot survive.
        assert "__engaged__" in obs._OBSERVABILITY_MEMO

    @pytest.mark.parametrize("case", _LAW_CASES, ids=_LAW_IDS)
    def test_engaged_info_carries_crate_version_from_every_law(
        self, monkeypatch: pytest.MonkeyPatch, case: _LawCase
    ) -> None:
        # EVERY routed fn must hand the RESOLVED crate to _log_engaged_once
        # — per CALL SITE (fresh memo per case, so each law's own site mints
        # the INFO): a `_log_engaged_once(None)` garble reads crate_version
        # None (four real mutmut survivors pre-fix — every site the shared
        # once-across-laws test did not reach FIRST).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert case.call_real() == case.oracle()
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"

    def test_engaged_crate_version_defaults_none_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `__version__` is NOT in the _CRATE_API handshake (it is metadata,
        # not a law) — an engaged crate without it reads the honest None,
        # never a fabricated token.
        _flag_on(monkeypatch)
        crate = _fake_crate()
        delattr(crate, "__version__")
        _install(monkeypatch, crate)
        with capture_logs() as logs:
            _call_loop()
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert engaged[0]["crate_version"] is None


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

    def test_loop_critical_edge_values_recomposed_from_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CRITICAL-tier mirror of the warn-edge pin above: a delegating
        # crate return carrying tier "critical" must pass the _LOOP_TIERS
        # output gate and ship (oracle BOMBED — a gate refusal would consult
        # it and fail). This is the one wheel-free drive of the gate's
        # "critical" member: without it, garbling that vocabulary token
        # silently degrades every critical-tier verdict to the oracle (a
        # confirmed mutmut survivor in BOTH envs pre-fix — the wheel-present
        # differentials also hold through the refusal fallback).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        obs.reset_observability_rust_for_tests(remaining=0)
        verdict = obs.routed_loop_check_once(
            40.0,  # lag 40 ≥ critical_after_s 30 — straight past both edges
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
        assert verdict.critical_edge is True
        assert verdict.warn_edge is True  # one starved sample fires both edges
        assert verdict.lag_s == 40.0
        assert verdict.state.tier == "critical"
        assert verdict.state.last_alerted_at == 40.0
        assert verdict.state.episodes == 1
        assert "loop_result_refused" not in obs._OBSERVABILITY_MEMO

    @pytest.mark.parametrize(
        "poison",
        [(1, True), (True, 1), 42, (True, True, True)],
        ids=["int_slot0", "int_slot1", "non_tuple", "wrong_arity"],
    )
    def test_sla_result_per_slot_type_gate_refuses(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        # Per-CLAUSE teeth for the sla output gate: (1, True) is refused
        # ONLY by the slot-0 `type(...) is bool` clause, (True, 1) only by
        # the slot-1 clause — the shared matrix's (1, 0) poison alone let a
        # single-clause drop survive (a confirmed hand-mutant escape: a
        # foreign crate's (1, True) was served RAW). The non-tuple and
        # wrong-arity shapes drive _sla_result_ok's FIRST arm individually
        # (the shared matrix never fed the sla gate a non-pair).
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(sla_decide=lambda *a: poison))
        assert _call_sla() == (True, True)
        assert "sla_result_refused" in obs._OBSERVABILITY_MEMO

    @pytest.mark.parametrize(
        "poison",
        [(20, True, True), (20.0, True, 1), 7, (20.0, True)],
        ids=["int_elapsed", "int_edge", "non_tuple", "wrong_arity"],
    )
    def test_watchdog_result_per_slot_type_gate_refuses(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        # The watchdog twin of the per-slot sla matrix: the shared (20.0, 1,
        # True) poison pins only slot 1 — these pin the slot-0 float clause,
        # the slot-2 bool clause, and _watchdog_result_ok's first arm
        # (non-tuple / wrong arity) individually.
        _flag_on(monkeypatch)
        _install(monkeypatch, _fake_crate(watchdog_decide=lambda *a: poison))
        assert _call_watchdog() == (20.0, True, True)
        assert "watchdog_result_refused" in obs._OBSERVABILITY_MEMO

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
            "tripwire_reconcile_counts",
            "tripwire_throughput_watch",
            "tripwire_memory_budget",
            "tripwire_memory_slope",
            "tripwire_summary_classify",
            "tripwire_stage_missing",
            "tripwire_ru_maxrss_bytes",
            "PanicException",  # the panic-belt anchor — the crate_stale teeth
        )
        # The tripwire code vocabulary — defined ONCE facade-side (§3.7).
        assert (obs.TW_NONE, obs.TW_CLEAR, obs.TW_FIRE) == (0, 1, 2)

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

    # Lone surrogates are deliberately OUTSIDE this domain (`st.text()`
    # never emits surrogate code points): they cannot cross the pyo3 &str
    # edge, so they DEGRADE to the oracle instead of differentiating — the
    # dedicated test_lone_surrogate_string_degrades_to_oracle pins that arm.
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
        # HONEST LIMIT: both sides run through the facade's OWN _pack_loop,
        # so this differential cannot detect a _pack_loop bug (both arms
        # transform identically); the pack legs are exercised for real by
        # the burn-in compare pins (clean-decrement / mismatch-autorevert)
        # and _pack_loop's own lines sit under the zero-survivor mutmut pair.
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

    _I64_INTS = st.integers(min_value=-(2**63), max_value=2**63 - 1)

    @settings(max_examples=200, deadline=None)
    @given(
        in_flight=_I64_INTS,
        enumerated=_I64_INTS,
        evaluated=_I64_INTS,
        skipped=_I64_INTS,
        survivors=_I64_INTS,
        total=_I64_INTS,
        prev_evaluated=st.none() | _I64_INTS,
        is_done=st.booleans(),
    )
    def test_tw_reconcile_differential(
        self,
        in_flight: int,
        enumerated: int,
        evaluated: int,
        skipped: int,
        survivors: int,
        total: int,
        prev_evaluated: int | None,
        is_done: bool,
    ) -> None:
        # The FULL i64 lattice — the bit-4 sum (`evaluated + in_flight`) can
        # exceed i64 both ways; the crate's i128 widening must match
        # Python's unbounded compare bit-for-bit (oracle bombed).
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_reconcile(
                in_flight,
                enumerated,
                evaluated,
                skipped,
                survivors,
                total,
                prev_evaluated,
                is_done,
                python_law=_oracle_bomb,
            )
        assert routed == _tw_reconcile_law(
            in_flight, enumerated, evaluated, skipped, survivors, total, prev_evaluated, is_done
        )

    def test_tw_reconcile_small_lattice_exhaustive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        vals = (-1, 0, 1, 2)
        for in_flight, evaluated, total in itertools.product(vals, repeat=3):
            for prev_evaluated in (None, 0, 2):
                for is_done in (False, True):
                    routed = obs.routed_tripwire_reconcile(
                        in_flight,
                        1,
                        evaluated,
                        1,
                        1,
                        total,
                        prev_evaluated,
                        is_done,
                        python_law=_oracle_bomb,
                    )
                    assert routed == _tw_reconcile_law(
                        in_flight, 1, evaluated, 1, 1, total, prev_evaluated, is_done
                    )

    @settings(max_examples=200, deadline=None)
    @given(
        rate=_FLOATS,
        peak=_FLOATS,
        below=st.integers(min_value=0, max_value=10) | st.sampled_from([2**63 - 2]),
        fraction=_FLOATS,
        dwell=st.integers(min_value=1, max_value=10) | st.sampled_from([2**63 - 2]),
    )
    def test_tw_throughput_differential(
        self, rate: float, peak: float, below: int, fraction: float, dwell: int
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_throughput(
                rate, peak, below, fraction, dwell, python_law=_oracle_bomb
            )
        oracle = _tw_throughput_law(rate, peak, below, fraction, dwell)
        assert obs._pack_throughput(tuple(routed)) == obs._pack_throughput(tuple(oracle))

    def test_tw_throughput_nan_rate_keeps_peak(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `max(peak, rate)` with a NaN rate KEEPS the peak (the Python
        # argument-order law) — the crate mirrors it, never NaN-poisons.
        self._require_wheel(monkeypatch)
        routed = obs.routed_tripwire_throughput(
            float("nan"), 100.0, 0, 0.2, 5, python_law=_oracle_bomb
        )
        assert routed == (100.0, 0, 20.0, 1)  # NaN < floor is False → the clear arm

    def test_tw_throughput_below_at_cap_minus_one_serves_through_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Ok boundary sibling of the counter-cap input refusal: at
        # 2**63 − 2 the crate's increment still fits i64 and SERVES.
        self._require_wheel(monkeypatch)
        routed = obs.routed_tripwire_throughput(
            0.5, 100.0, 2**63 - 2, 0.2, 5, python_law=_oracle_bomb
        )
        assert routed == (100.0, 2**63 - 1, 20.0, 0)

    @settings(max_examples=200, deadline=None)
    @given(rss=_I64_INTS, ram_total=_I64_INTS, fraction=_FLOATS)
    def test_tw_budget_differential(self, rss: int, ram_total: int, fraction: float) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_memory_budget(
                rss, ram_total, fraction, python_law=_oracle_bomb
            )
        oracle = _tw_budget_law(rss, ram_total, fraction)
        assert obs._pack_budget(tuple(routed)) == obs._pack_budget(tuple(oracle))

    @settings(max_examples=200, deadline=None)
    @given(
        prev_rss=st.integers(min_value=-(2**53), max_value=2**53),
        rss_diff=st.integers(min_value=-(2**53), max_value=2**53),
        prev_evaluated=st.integers(min_value=0, max_value=2**53),
        ev_diff=st.integers(min_value=1, max_value=2**53),
        slope_threshold=_FLOATS,
        slope_high=st.integers(min_value=0, max_value=1000),
        dwell=st.integers(min_value=1, max_value=1000),
    )
    def test_tw_slope_differential(
        self,
        prev_rss: int,
        rss_diff: int,
        prev_evaluated: int,
        ev_diff: int,
        slope_threshold: float,
        slope_high: int,
        dwell: int,
    ) -> None:
        # The gate-passing domain: both diffs inside the 2^53 exactness
        # fence, positive progress — the crate's exact-f64 division must
        # equal Python's correctly-rounded int/int true division.
        rss = prev_rss + rss_diff
        evaluated = prev_evaluated + ev_diff
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_memory_slope(
                rss,
                evaluated,
                prev_rss,
                prev_evaluated,
                slope_threshold,
                slope_high,
                dwell,
                python_law=_oracle_bomb,
            )
        oracle = _tw_slope_law(
            rss, evaluated, prev_rss, prev_evaluated, slope_threshold, slope_high, dwell
        )
        assert obs._pack_slope(tuple(routed)) == obs._pack_slope(tuple(oracle))

    @settings(max_examples=200, deadline=None)
    @given(
        pairs=st.lists(st.tuples(_FLOATS, st.booleans()), max_size=8),
        absurd_threshold=_FLOATS,
    )
    def test_tw_summary_differential(
        self, pairs: list[tuple[float, bool]], absurd_threshold: float
    ) -> None:
        values = [pair[0] for pair in pairs]
        screened = [pair[1] for pair in pairs]
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_summary(
                values, screened, absurd_threshold, python_law=_oracle_bomb
            )
        assert routed == _tw_summary_law(values, screened, absurd_threshold)

    def test_tw_summary_threshold_boundary_strict_gt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # |value| EXACTLY at the threshold is NOT absurd (strict `>`); one
        # ulp above fires — the boundary law pinned through the crate.
        self._require_wheel(monkeypatch)
        one_ulp_over = math.nextafter(10.0, math.inf)
        routed = obs.routed_tripwire_summary(
            [10.0, -10.0, one_ulp_over], [True, True, True], 10.0, python_law=_oracle_bomb
        )
        assert routed == ([1, 1, 1], [1, 1, 2])

    @settings(max_examples=200, deadline=None)
    @given(
        required_sorted=st.lists(_D2_STR, max_size=6),
        present=st.lists(_D2_STR, max_size=6),
    )
    def test_tw_stage_differential(self, required_sorted: list[str], present: list[str]) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_tripwire_stage(required_sorted, present, python_law=_oracle_bomb)
        assert routed == _tw_stage_law(required_sorted, present)

    def test_tw_stage_lone_surrogate_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lone surrogate cannot cross the pyo3 &str edge — the degrade set
        # serves the oracle's Python-side membership answer.
        self._require_wheel(monkeypatch)
        with capture_logs() as logs:
            routed = obs.routed_tripwire_stage(["\ud800"], [], python_law=_tw_stage_law)
        assert routed == [True]
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "tw_stage_error"

    @settings(max_examples=200, deadline=None)
    @given(
        ru_maxrss=_I64_INTS | st.sampled_from([2**53, 2**62, 2**63 - 1, -(2**63)]),
        platform=st.sampled_from(["darwin", "linux", "win32", "", "Darwin"]),
    )
    def test_tw_rss_differential(self, ru_maxrss: int, platform: str) -> None:
        # python_law is the REAL production law here (not bombed): an
        # in-i64 input whose ×1024 product overflows takes the crate's
        # checked_mul ValueError → the degrade set → the oracle — equality
        # must hold across BOTH the crate-served and degraded arms.
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = obs.routed_ru_maxrss_bytes(ru_maxrss, platform, python_law=_tw_rss_law)
        assert routed == _tw_rss_law(ru_maxrss, platform)

    def test_tw_rss_platform_law_through_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._require_wheel(monkeypatch)
        assert obs.routed_ru_maxrss_bytes(1024, "darwin", python_law=_oracle_bomb) == 1024
        assert obs.routed_ru_maxrss_bytes(1024, "linux", python_law=_oracle_bomb) == 1_048_576
        assert obs.routed_ru_maxrss_bytes(1024, "Darwin", python_law=_oracle_bomb) == 1_048_576

    def test_tw_rss_checked_mul_overflow_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An in-i64 reading whose ×1024 product overflows i64: the crate's
        # checked_mul refuses with ValueError → the degrade set serves the
        # oracle's unbounded-int product (the episodes+1 containment
        # precedent). One warn, no latch, no autorevert.
        self._require_wheel(monkeypatch)
        with capture_logs() as logs:
            result = obs.routed_ru_maxrss_bytes(2**62, "linux", python_law=_tw_rss_law)
        assert result == 2**62 * 1024
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "tw_rss_error"
        assert "overflow" in warns[0]["error_detail"]
        assert obs.rust_observability_disabled() is False
        assert [e for e in logs if e["event"] == "observability_rust.autoreverted"] == []

    def test_wheel_tripwire_err_arms_raise_value_error(self) -> None:
        # The Err arms STRUCTURALLY unreachable through the facade (the
        # counter-cap input gates + the length-equality gate sit in front) —
        # pinned at the wheel surface so the crate-side refusal messages
        # stay ValueError, never a new exception class.
        wheel = pytest.importorskip("brutex_observability")
        with pytest.raises(ValueError, match="overflow"):
            wheel.tripwire_throughput_watch(0.5, 100.0, 2**63 - 1, 0.2, 5)
        with pytest.raises(ValueError, match="overflow"):
            wheel.tripwire_memory_slope(2000, 20, 1000, 10, 0.0, 2**63 - 1, 5)
        with pytest.raises(ValueError, match="mismatch"):
            wheel.tripwire_summary_classify([1.0], [True, False], 10.0)
