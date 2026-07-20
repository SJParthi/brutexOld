"""Tests for the ``combo_rust`` routing facade (T16 PR-1).

DEC-STRATEGIES-RUST-COMBO-001. Yields 100% line + branch on
``brutex/strategies/combo_rust.py`` ALONE (the discovery-gate contract),
WHEEL-FREE — every crate interaction is a ``sys.modules`` / import fake, so
the suite fully covers the facade in a crate-absent environment: the flag
matrix, the OFF-never-imports-crate bomb spy, the crate-absent / crate-stale
latches, every degrade arm (incl. the ``"non-exact str"`` key-marker split),
the escape-set re-raise trio, the burn-in mismatch autorevert + steady
state, the refuse-poison gates (non-str canonical render; non-int /
bool / negative / out-of-range scan index), the ON-arm materialization law,
the OFF-arm laziness law, the local-twin equality pins
(``env_flag_tristate`` vs the engine's; ``_panic_belt_errors`` vs
``lake_rust_render``'s), the reset seam, and the conftest hermeticity-belt
grep pin.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex.strategies.combo_rust as cr
import pytest
from brutex.data import lake_rust_render
from brutex.engine import sweep_lookahead
from brutex.strategies.combo import _first_offending_field, _stdlib_canonical_json
from structlog.testing import capture_logs

#: A representative LOWERED params tree (post-``_canonicalise_value`` — plain
#: data, float-free by law; floats/Decimals arrive as already-rendered strings).
LOWERED: dict[str, Any] = {
    "sl_paisa": 2000,
    "tp_paisa": None,
    "prior_n": 20,
    "proximity_bps": [10, 15, 25],
    "fib_anchor": "prev_day",
    "sigma": "0.5",
    "flag": True,
}

#: The pre-seam recorded canonical render of :data:`LOWERED` (the verbatim
#: ``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)``).
CANON: str = (
    '{"fib_anchor":"prev_day","flag":true,"prior_n":20,"proximity_bps":[10,15,25],'
    '"sigma":"0.5","sl_paisa":2000,"tp_paisa":null}'
)

#: Clean scan fields (no offender).
CLEAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("strategy_id", "mask:1f"),
    ("direction", "bull"),
)

#: Fields whose FIRST offender is ``b`` (a second offender follows — the
#: first-offender law's teeth).
OFFENDING_FIELDS: tuple[tuple[str, str], ...] = (
    ("a", "clean"),
    ("b", "x\x1fy"),
    ("c", "\x1f"),
)


def _fake_crate(
    *,
    canonical: Any = None,
    scan: Any = None,
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_strategies`` module exposing the T16 API surface."""

    def _default_canonical(tree: Mapping[str, Any]) -> str:
        return json.dumps(tree, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _default_scan(values: list[str]) -> int | None:
        for i, value in enumerate(values):
            if "\x1f" in value:
                return i
        return None

    if canonical is None:
        canonical = _default_canonical
    if scan is None:
        scan = _default_scan

    return SimpleNamespace(
        canonical_json=canonical,
        first_unit_separator=scan,
        __version__=version,
    )


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``importlib.import_module`` at the fake crate."""

    def _import(name: str) -> object:
        assert name == "brutex_strategies"
        return crate

    monkeypatch.setattr(cr.importlib, "import_module", _import)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "1")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh latch/burn-in/memo state + flag unset around every test."""
    monkeypatch.delenv(cr.STRATEGIES_RUST_COMBO_ENV, raising=False)
    cr.reset_combo_rust_for_tests()
    yield
    cr.reset_combo_rust_for_tests()


class TestFlagResolution:
    def test_default_unset_is_off_without_warn(self) -> None:
        with capture_logs() as logs:
            assert cr.combo_rust_enabled() is False
        assert logs == []

    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "1")
        assert cr.combo_rust_enabled() is True

    def test_falsy_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "off")
        with capture_logs() as logs:
            assert cr.combo_rust_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "banana")
        with capture_logs() as logs:
            assert cr.combo_rust_enabled() is False
            assert cr.combo_rust_enabled() is False  # the warn-once memo
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_blank_whitespace_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.STRATEGIES_RUST_COMBO_ENV, "   ")
        with capture_logs() as logs:
            assert cr.combo_rust_enabled() is False
        assert logs == []


class TestLocalTwins:
    """The two local twins are equality-PINNED against their originals."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "1",
            "true",
            "TRUE",
            " Yes ",
            "on",
            "0",
            "false",
            "No",
            " OFF ",
            "banana",
            "2",
            "onn",
            "ture",
        ],
    )
    def test_env_flag_tristate_equals_engine_twin(self, raw: str) -> None:
        assert cr.env_flag_tristate(raw) == sweep_lookahead.env_flag_tristate(raw)

    def test_vocabulary_sets_equal_engine_twin(self) -> None:
        assert cr._TRUTHY == sweep_lookahead._TRUTHY
        assert cr._FALSY == sweep_lookahead._FALSY

    def test_panic_belt_equals_lake_rust_render_twin(self) -> None:
        assert cr._panic_belt_errors() == lake_rust_render._panic_belt_errors()

    def test_panic_belt_starts_with_exception_and_skips_absent_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deterministic ImportError arm: pyo3_runtime absent → `continue`.
        real_import = importlib.import_module

        def _imp(name: str) -> object:
            if name == "pyo3_runtime":
                raise ImportError("absent")
            return real_import(name)

        monkeypatch.setattr(cr.importlib, "import_module", _imp)
        belt = cr._panic_belt_errors()
        assert belt[0] is Exception

    def test_panic_belt_home_without_class_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A home module that IMPORTS but carries no PanicException → the
        # compound-condition False arm.
        empty = types.ModuleType("pyo3_runtime")
        monkeypatch.setitem(sys.modules, "pyo3_runtime", empty)
        belt = cr._panic_belt_errors()
        assert belt[0] is Exception

    def test_panic_belt_dedupes_shared_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # pyo3_runtime resolves the SAME class polars re-exports → the
        # `panic not in belt` dedupe arm on the second home.
        polars_exc = importlib.import_module("polars.exceptions")
        panic_cls = polars_exc.PanicException
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = panic_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        belt = cr._panic_belt_errors()
        assert belt.count(panic_cls) == 1


class TestOffArmInertness:
    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(name: str) -> object:
            raise AssertionError(f"flag-OFF path imported {name!r}")

        monkeypatch.setattr(cr.importlib, "import_module", _bomb)
        assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        assert (
            cr.routed_first_offending_field(OFFENDING_FIELDS, python_law=_first_offending_field)
            == "b"
        )
        assert (
            cr.routed_first_offending_field(CLEAN_FIELDS, python_law=_first_offending_field) is None
        )

    def test_off_arm_preserves_lazy_single_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The OFF arm hands the UNTOUCHED iterable to the verbatim lazy loop:
        # a generator is consumed only to the first offender (the historical
        # inline-loop behaviour, byte- and laziness-identical).
        def _bomb(name: str) -> object:
            raise AssertionError("flag-OFF path imported the crate")

        monkeypatch.setattr(cr.importlib, "import_module", _bomb)

        def _gen() -> Iterator[tuple[str, str]]:
            yield ("a", "x\x1fy")
            raise AssertionError("OFF arm consumed past the first offender")

        assert cr.routed_first_offending_field(_gen(), python_law=_first_offending_field) == "a"

    def test_latched_process_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        cr._disable("test_latch")
        assert cr.rust_combo_disabled() is True

        def _bomb(name: str) -> object:
            raise AssertionError("latched path imported the crate")

        monkeypatch.setattr(cr.importlib, "import_module", _bomb)
        assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        assert (
            cr.routed_first_offending_field(OFFENDING_FIELDS, python_law=_first_offending_field)
            == "b"
        )


class TestCrateGuards:
    def test_crate_absent_latches_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(cr.importlib, "import_module", _import)
        with capture_logs() as logs:
            result = cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json)
        assert result == CANON
        assert cr.rust_combo_disabled() is True
        events = [e for e in logs if e["event"] == "combo_rust.rust_path_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "crate_unavailable"

    def test_crate_absent_scan_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(cr.importlib, "import_module", _import)

        # The crate-None scan path hands the UNTOUCHED iterable to the lazy
        # oracle — no materialization happened yet.
        def _gen() -> Iterator[tuple[str, str]]:
            yield ("a", "x\x1fy")
            raise AssertionError("crate-None arm consumed past the first offender")

        assert cr.routed_first_offending_field(_gen(), python_law=_first_offending_field) == "a"
        assert cr.rust_combo_disabled() is True

    def test_crate_stale_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        del crate.first_unit_separator  # a pre-T16 wheel shape
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            result = cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json)
        assert result == CANON
        assert cr.rust_combo_disabled() is True
        events = [e for e in logs if e["event"] == "combo_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_stale"

    def test_crate_stale_noncallable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        crate.canonical_json = "not callable"
        _install_crate(monkeypatch, crate)
        result = cr.routed_first_offending_field(
            OFFENDING_FIELDS, python_law=_first_offending_field
        )
        assert result == "b"
        assert cr.rust_combo_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        cr._disable("first")
        cr._disable("second")
        assert cr._DISABLED_REASON[0] == "first"

    def test_reset_seam_restores_live_state(self) -> None:
        cr._disable("test_latch")
        cr._warn_once("some_reason")
        cr._BURN_IN_REMAINING[cr._LAW_CANON] = 0
        cr.reset_combo_rust_for_tests()
        assert cr.rust_combo_disabled() is False
        assert cr._BURN_IN_REMAINING[cr._LAW_CANON] == cr.BURN_IN_SAMPLES
        assert cr._BURN_IN_REMAINING[cr._LAW_SCAN] == cr.BURN_IN_SAMPLES
        assert not cr._OBSERVABILITY_MEMO


class TestCanonicalRouting:
    @pytest.mark.parametrize(
        "exc_factory",
        [ValueError, RuntimeError, OverflowError, AttributeError],
    )
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc_factory: type[Exception]
    ) -> None:
        _flag_on(monkeypatch)

        def _boom(tree: Mapping[str, Any]) -> str:
            raise exc_factory("ffi refusal")

        _install_crate(monkeypatch, _fake_crate(canonical=_boom))
        with capture_logs() as logs:
            for _ in range(2):
                assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "canonical_json_error"
        assert cr.rust_combo_disabled() is False  # degrade, not latch

    def test_type_error_without_marker_is_generic_degrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _boom(tree: Mapping[str, Any]) -> str:
            raise TypeError("unsupported node type: float")

        _install_crate(monkeypatch, _fake_crate(canonical=_boom))
        with capture_logs() as logs:
            assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_json_error"

    def test_nonexact_key_marker_gets_its_own_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _refuse(tree: Mapping[str, Any]) -> str:
            raise TypeError(
                "canonical_json: dict key is a str subclass (non-exact str) — refused to the oracle"
            )

        _install_crate(monkeypatch, _fake_crate(canonical=_refuse))
        with capture_logs() as logs:
            assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "nonexact_key_refused"
        assert cr.rust_combo_disabled() is False  # refuse, not latch

    @pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, escape: type[BaseException]
    ) -> None:
        _flag_on(monkeypatch)

        def _boom(tree: Mapping[str, Any]) -> str:
            raise escape

        _install_crate(monkeypatch, _fake_crate(canonical=_boom))
        with pytest.raises(escape):
            cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json)
        assert cr.rust_combo_disabled() is False

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        class FakePanic(Exception):
            pass

        def _panic(tree: Mapping[str, Any]) -> str:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, _fake_crate(canonical=_panic))
        monkeypatch.setattr(cr, "_panic_belt_errors", lambda: (FakePanic,))
        assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        assert cr._DISABLED_REASON[0] == "crate_panic"

    def test_non_str_result_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _poison(tree: Mapping[str, Any]) -> object:
            return b'{"a":1}'

        _install_crate(monkeypatch, _fake_crate(canonical=_poison))
        with capture_logs() as logs:
            assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "canonical_result_refused"
        assert warns[0]["result_type"] == "bytes"

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _wrong(tree: Mapping[str, Any]) -> str:
            return '{"WRONG":1}'

        _install_crate(monkeypatch, _fake_crate(canonical=_wrong))
        with capture_logs() as logs:
            result = cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json)
        assert result == CANON  # the ORACLE value ships
        assert cr.rust_combo_disabled() is True
        errors = [e for e in logs if e["event"] == "combo_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch_canonical"

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        done = [e for e in logs if e["event"] == "combo_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "canonical_json"
        # Steady state: the oracle must never be consulted again.

        def _oracle_bomb(lowered: Mapping[str, Any]) -> str:
            raise AssertionError("steady state consulted the oracle")

        assert cr.routed_canonical_json(LOWERED, python_law=_oracle_bomb) == CANON

    def test_burn_in_decrement_without_completion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=2)
        with capture_logs() as logs:
            assert cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json) == CANON
        assert [e for e in logs if e["event"] == "combo_rust.burn_in_complete"] == []
        assert cr._BURN_IN_REMAINING[cr._LAW_CANON] == 1

    def test_engaged_info_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            cr.routed_canonical_json(LOWERED, python_law=_stdlib_canonical_json)
            cr.routed_first_offending_field(CLEAN_FIELDS, python_law=_first_offending_field)
        engaged = [e for e in logs if e["event"] == "combo_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"


class TestScanRouting:
    def test_on_arm_materializes_the_iterable_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The DISCLOSED ON-arm behavioral note: the whole iterable is
        # consumed up front (the Python law stops at the first offender).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=0)  # steady state — no oracle
        consumed: list[tuple[str, str]] = []

        def _gen() -> Iterator[tuple[str, str]]:
            for pair in [("a", "x\x1fy"), ("b", "ok"), ("c", "also ok")]:
                consumed.append(pair)
                yield pair

        assert cr.routed_first_offending_field(_gen(), python_law=_first_offending_field) == "a"
        assert len(consumed) == 3

    def test_first_offender_at_index_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=0)
        fields = (("first", "\x1fx"), ("second", "\x1fy"))
        assert cr.routed_first_offending_field(fields, python_law=_first_offending_field) == "first"

    def test_no_offender_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=0)
        assert (
            cr.routed_first_offending_field(CLEAN_FIELDS, python_law=_first_offending_field) is None
        )

    def test_malformed_pair_first_degrades_and_oracle_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The R3 interleave law, malformed-first: the ON-arm materialization
        # unpack fails → degrade → the oracle re-runs over the materialized
        # pairs and raises ITS OWN error with today's class + message bytes.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        fields = (("bad", "x", "y"), ("b", "x\x1fy"))
        with capture_logs() as logs, pytest.raises(ValueError, match="too many values to unpack"):
            cr.routed_first_offending_field(
                fields,  # type: ignore[arg-type]
                python_law=_first_offending_field,
            )
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "scan_error"

    def test_offender_before_malformed_pair_returns_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The R3 interleave law, offender-first: the degrade path's oracle
        # re-run is the LAZY loop over the materialized pairs, so it returns
        # the offender's name before ever unpacking the malformed pair —
        # exactly the OFF-arm outcome.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        fields = (("b", "x\x1fy"), ("bad", "x", "y"))
        result = cr.routed_first_offending_field(
            fields,  # type: ignore[arg-type]
            python_law=_first_offending_field,
        )
        assert result == "b"

    def test_crate_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _boom(values: list[str]) -> int | None:
            raise TypeError("Vec<String> extraction failed")

        _install_crate(monkeypatch, _fake_crate(scan=_boom))
        with capture_logs() as logs:
            result = cr.routed_first_offending_field(
                OFFENDING_FIELDS, python_law=_first_offending_field
            )
        assert result == "b"
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "scan_error"
        assert cr.rust_combo_disabled() is False

    @pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, escape: type[BaseException]
    ) -> None:
        _flag_on(monkeypatch)

        def _boom(values: list[str]) -> int | None:
            raise escape

        _install_crate(monkeypatch, _fake_crate(scan=_boom))
        with pytest.raises(escape):
            cr.routed_first_offending_field(OFFENDING_FIELDS, python_law=_first_offending_field)

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        class FakePanic(Exception):
            pass

        def _panic(values: list[str]) -> int | None:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, _fake_crate(scan=_panic))
        monkeypatch.setattr(cr, "_panic_belt_errors", lambda: (FakePanic,))
        result = cr.routed_first_offending_field(
            OFFENDING_FIELDS, python_law=_first_offending_field
        )
        assert result == "b"
        assert cr._DISABLED_REASON[0] == "crate_panic"

    @pytest.mark.parametrize(
        "poison",
        ["0", True, -1, 3],  # non-int / bool / negative / out-of-range (len == 3)
        ids=["str_index", "bool_index", "negative_index", "past_end_index"],
    )
    def test_poison_index_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(scan=lambda values: poison))
        with capture_logs() as logs:
            result = cr.routed_first_offending_field(
                OFFENDING_FIELDS, python_law=_first_offending_field
            )
        assert result == "b"  # the oracle's answer, never a poisoned lookup
        warns = [e for e in logs if e["event"] == "combo_rust.fallback"]
        assert warns[0]["reason"] == "scan_result_refused"
        assert cr.rust_combo_disabled() is False

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        # A miscompiled scan returning None on a REAL offender — the
        # fail-open shape the burn-in compare catches while armed.
        _install_crate(monkeypatch, _fake_crate(scan=lambda values: None))
        with capture_logs() as logs:
            result = cr.routed_first_offending_field(
                OFFENDING_FIELDS, python_law=_first_offending_field
            )
        assert result == "b"  # the ORACLE name ships
        assert cr.rust_combo_disabled() is True
        errors = [e for e in logs if e["event"] == "combo_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch_scan"

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            assert (
                cr.routed_first_offending_field(OFFENDING_FIELDS, python_law=_first_offending_field)
                == "b"
            )
        done = [e for e in logs if e["event"] == "combo_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "scan_sep"

        def _oracle_bomb(fields: Iterable[tuple[str, str]]) -> str | None:
            raise AssertionError("steady state consulted the oracle")

        assert cr.routed_first_offending_field(OFFENDING_FIELDS, python_law=_oracle_bomb) == "b"

    def test_burn_in_decrement_without_completion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_combo_rust_for_tests(remaining=2)
        with capture_logs() as logs:
            assert (
                cr.routed_first_offending_field(CLEAN_FIELDS, python_law=_first_offending_field)
                is None
            )
        assert [e for e in logs if e["event"] == "combo_rust.burn_in_complete"] == []
        assert cr._BURN_IN_REMAINING[cr._LAW_SCAN] == 1


class TestConftestBelt:
    def test_hermeticity_belt_line_is_pinned(self) -> None:
        # The §3.1 conftest belt (the #1692/#1774/#1793 setdefault idiom): a
        # runner exporting the flag truthy must not RED the OFF-asserting
        # suite; ON-arm tests override via monkeypatch.setenv.
        conftest = Path(__file__).resolve().parents[1] / "conftest.py"
        source = conftest.read_text(encoding="utf-8")
        assert 'os.environ.setdefault(STRATEGIES_RUST_COMBO_ENV, "0")' in source
        assert "from brutex.strategies import combo_rust as _combo_rust" in source

    def test_wheel_less_fallback_literal_matches_the_constant(self) -> None:
        # WHEEL-FREE DISCIPLINE drift pin: conftest.py must stay importable
        # in the wheel-LESS lanes (lake-rust-parity installs only the
        # brutex-lake wheel; redteam-nightly installs only ``.[dev]``) —
        # importing ``brutex.strategies.combo_rust`` there raises
        # ``ModuleNotFoundError: No module named 'fno_math'`` via the
        # package __init__ (reproduced red: actions run 29599656943,
        # 2026-07-17). The belt therefore guards the import and falls back
        # to a literal; THIS pin (running in the wheel-PRESENT lanes) is
        # what keeps the fallback literal equal to the real constant.
        conftest = Path(__file__).resolve().parents[1] / "conftest.py"
        source = conftest.read_text(encoding="utf-8")
        assert "except ModuleNotFoundError:" in source
        assert "if _combo_rust is None" in source
        # The quoted fallback literal (the belt comment uses backticks, so the
        # double-quoted form appears ONLY in the fallback expression).
        assert f'"{cr.STRATEGIES_RUST_COMBO_ENV}"' in source, (
            "conftest's wheel-less fallback literal drifted from the combo_rust constant"
        )


class TestMutationWorkflowWiring:
    """Pin the DEC-STRATEGIES-RUST-COMBO-001 wiring INTO the mutation workflow.

    A routing leaf shipped WITHOUT its weekly mutation wiring would repeat
    the DEC-LAKE-STATS-MANIFEST-001 round-8 refuter class (an unwired gate
    presented as a deferred one — Golden Rule #10), and the design's §4.4
    round-1 fold proved the cargo-mutants JOB alone does not GATE: without
    the verdict ``needs:`` fan-in + result clauses + the issue-open step a
    red run would neither fail the verdict nor page. These deterministic
    pins (the ``TestMutationWorkflowWiring`` precedent) fail the moment any
    of the five touch points is dropped. HONEST: they pin the WIRING only —
    the weekly run executes on its own schedule/push triggers.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def _text(self) -> str:
        return self._WORKFLOW.read_text(encoding="utf-8")

    def test_facade_pair_is_wired_into_the_mutmut_loop(self) -> None:
        # 3-field ``module:testfile:timeout`` pair, EMPTY third field = the
        # historical runner (the DEC-MUTATION-RUNTIME-003 pair format).
        assert (
            '"brutex/strategies/combo_rust.py:tests/unit/test_strategies_combo_rust.py:"'
            in self._text()
        ), "combo_rust.py is not wired into mutation.yml's per-module mutmut loop"

    def test_crate_path_trigger_is_wired(self) -> None:
        assert '- "rust/brutex-strategies/**"' in self._text(), (
            "rust/brutex-strategies/** is not a mutation.yml push path trigger"
        )

    def test_cargo_mutants_job_feeds_the_verdict_fan_in(self) -> None:
        text = self._text()
        assert "rust-mutation-brutex-strategies:" in text, (
            "the rust-mutation-brutex-strategies cargo-mutants job is missing"
        )
        assert (
            "needs: [python-mutation, rust-mutation, rust-mutation-trading-core, "
            "rust-mutation-brutex-feed, rust-mutation-brutex-pull-state, "
            "rust-mutation-brutex-strategies]" in text
        ), "the mutation-verdict needs: list does not fan in the brutex-strategies job"

    def test_verdict_result_clauses_and_issue_step(self) -> None:
        text = self._text()
        # The issue-open step (a red/cancelled run must PAGE, with the honest
        # timeout-vs-survivor split in the body).
        assert "Open GitHub issue — brutex-strategies gate red" in text
        # The success gate (auto-close) + the failure fold both consult the
        # new job's result.
        assert "needs.rust-mutation-brutex-strategies.result == 'success'" in text
        assert (
            'contains(fromJSON(\'["failure","cancelled"]\'), '
            "needs.rust-mutation-brutex-strategies.result)" in text
        )
