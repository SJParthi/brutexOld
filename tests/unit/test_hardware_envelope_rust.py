"""Tests for :mod:`brutex.config.hardware_envelope_rust` — the default-OFF
hardware-envelope derivation Rust seam.

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, slice T35 milestone 2).
Every fail-closed layer is exercised WHEEL-FREE (fake crate modules
injected via ``sys.modules``), so the 100% line+branch gate on the facade
never depends on a built ``brutex_core``; the ON-arm parity legs
(:class:`TestRealCrateParity`) skip cleanly when the wheel is absent and
run for real in the ``core-rust-parity.yml`` lane.

HONESTY (the seam-split record): DETECTION stays Python — there is no
"Rust probe vs Python probe" parity to run. The ON-arm parity here is
BEHAVIORAL: the real detection feeds the routed DERIVATIONS and the
flag-ON public results must equal the flag-OFF oracles on this box, plus
the hypothesis differential over the laws' int/string domains.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.config import hardware_envelope as he
from brutex.config import hardware_envelope_rust as her
from brutex.observability.bench_baseline import KEY_SLUG_RE

if TYPE_CHECKING:
    from collections.abc import Callable

# The CLAUDE.md §8 reference-box envelope (M4 Pro) + fingerprint.
_M4 = he.envelope_from(
    {"logical_cores": 14, "physical_cores": 14, "performance_cores": 10, "efficiency_cores": 4}
)
_M4_FP = he.HardwareFingerprint(
    model_slug="mac16-8",
    arch="arm64",
    os_family="darwin",
    physical_cores=14,
    performance_cores=10,
    efficiency_cores=4,
    total_ram_gib=48,
)
_48_GIB = 48 * 2**30

# ── Harness ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-unset with a fresh latch/burn-in/memo."""
    monkeypatch.delenv(her.HWENV_RUST_ENV, raising=False)
    her.reset_hwenv_rust_for_tests()
    yield
    her.reset_hwenv_rust_for_tests()


def _fake_crate(**overrides: object) -> types.ModuleType:
    """A fake ``brutex_core`` carrying the hwenv API (oracle-echo defaults)."""
    crate = types.ModuleType("brutex_core")
    crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    defaults: dict[str, Callable[..., object]] = {
        "hwenv_pool_workers": he._pool_workers_python,  # pyright: ignore[reportPrivateUsage]
        "hwenv_library_thread_budget": he._library_thread_budget_python,  # pyright: ignore[reportPrivateUsage]
        "hwenv_memory_budget": he._memory_budget_python,  # pyright: ignore[reportPrivateUsage]
        "hwenv_hardware_key": he._hardware_key_python,  # pyright: ignore[reportPrivateUsage]
    }
    for name, fn in defaults.items():
        setattr(crate, name, overrides.get(name, fn))
    for name, value in overrides.items():
        setattr(crate, name, value)
    return crate


def _bomb_crate() -> types.ModuleType:
    """A crate whose every call is a test failure (the OFF-arm / gate spy)."""

    def _boom(*_a: object) -> object:
        raise AssertionError("the crate must not be called on this path")

    return _fake_crate(
        hwenv_pool_workers=_boom,
        hwenv_library_thread_budget=_boom,
        hwenv_memory_budget=_boom,
        hwenv_hardware_key=_boom,
    )


def _arm(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType | None) -> None:
    """Set the flag ON and install *crate* as ``brutex_core``."""
    monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_core", crate)


def _pin_ram(monkeypatch: pytest.MonkeyPatch, total: int) -> None:
    """Pin the RAM detection the public ``memory_budget_bytes`` consults."""
    monkeypatch.setattr(he, "detect_total_ram_bytes", lambda: total)


# ── Flag matrix ─────────────────────────────────────────────────────────────


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on ", "On"])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, raw)
        assert her.hwenv_rust_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, raw)
        assert her.hwenv_rust_enabled() is False

    def test_unset_and_whitespace_default_on_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The DEC-RUST-DEFAULT-ON-WAVE-001 default pin: unset/blank ⇒ ON.
        assert her.hwenv_rust_enabled() is True
        monkeypatch.setenv(her.HWENV_RUST_ENV, "   ")
        assert her.hwenv_rust_enabled() is True

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, "onn")
        events: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: events.append(f"{event}:{kw.get('reason')}"),
        )
        assert her.hwenv_rust_enabled() is False
        assert her.hwenv_rust_enabled() is False  # second call — memoised
        assert events == ["hwenv_rust.fallback:flag_unrecognised"]

    def test_env_name_and_burn_in_literals_pinned(self) -> None:
        # The canonical BRUTEX_<CRATE>_RUST_<SEAM> form + the #1531 budget.
        assert her.HWENV_RUST_ENV == "BRUTEX_CORE_RUST_HWENV"
        assert her.BURN_IN_SAMPLES == 16

    def test_leaf_import_discipline_ast_pin(self) -> None:
        # hardware_envelope_rust must stay a CONFIG-leaf module: stdlib +
        # structlog ONLY at module scope (brutex.data / brutex.engine
        # would cycle or drag the fno_math wheel under every config
        # consumer). AST pin — the money_rust/feed_rust precedent.
        import ast
        from pathlib import Path

        source = Path(her.__file__).read_text(encoding="utf-8")
        allowed = {"importlib", "math", "os", "typing", "structlog", "__future__"}
        allowed_from = allowed | {"collections.abc", "types"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                assert node.module in allowed_from, node.module

    def test_truthy_falsy_mirror_money_rust(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (module docstring), so
        # its inline vocabulary is mirror-PINNED against the sibling
        # money_rust leaf's sets (which are themselves mirror-pinned to
        # engine.sweep_lookahead) — three homes, one vocabulary.
        from brutex import money_rust

        assert her._TRUTHY == money_rust._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert her._FALSY == money_rust._FALSY  # pyright: ignore[reportPrivateUsage]

    def test_gate_bounds_mirror_the_crate_constants(self) -> None:
        # The facade's eligibility bounds must equal the crate's checked
        # bounds (src/hwenv.rs MAX_COUNT_MAGNITUDE / MAX_DETECTED_RAM_BYTES)
        # so an eligible input can never be crate-refused for magnitude.
        assert her._MAX_COUNT == 2**31  # pyright: ignore[reportPrivateUsage]
        assert her._MAX_DETECTED_RAM == 2**62  # pyright: ignore[reportPrivateUsage]


# ── Reset seam + latch + warn-once ──────────────────────────────────────────


class TestResetSeamAndLatch:
    def test_reset_restores_fresh_process_view(self) -> None:
        her._disable("some_reason")  # pyright: ignore[reportPrivateUsage]
        assert her.hwenv_rust_disabled() is True
        her.reset_hwenv_rust_for_tests()
        assert her.hwenv_rust_disabled() is False
        for law in her._BURN_IN_REMAINING:  # pyright: ignore[reportPrivateUsage]
            assert her._BURN_IN_REMAINING[law] == her.BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        her.reset_hwenv_rust_for_tests(remaining=0)
        budgets = set(her._BURN_IN_REMAINING.values())  # pyright: ignore[reportPrivateUsage]
        assert budgets == {0}

    def test_warn_once_suppresses_repeats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: events.append(str(kw.get("reason"))),
        )
        her._warn_once("r1", extra=1)  # pyright: ignore[reportPrivateUsage]
        her._warn_once("r1")  # pyright: ignore[reportPrivateUsage]
        her._warn_once("r2")  # pyright: ignore[reportPrivateUsage]
        assert events == ["r1", "r2"]

    def test_disable_latches_once_and_repeat_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warned: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        her._disable("first")  # pyright: ignore[reportPrivateUsage]
        her._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert her.hwenv_rust_disabled() is True
        assert warned == ["first"]

    def test_disable_error_logs_autoreverted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        errors: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append(event),
        )
        her._disable("mismatch", error=True)  # pyright: ignore[reportPrivateUsage]
        assert errors == ["hwenv_rust.autoreverted"]


# ── OFF-arm bomb-crate spy ──────────────────────────────────────────────────


class TestFlagOffNeverTouchesCrate:
    def test_off_arm_is_byte_identical_and_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001) + a bomb crate installed + the
        # resolver itself booby-trapped: the OFF arm must never even resolve.
        monkeypatch.setenv(her.HWENV_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())

        def _resolver_bomb() -> types.ModuleType | None:
            raise AssertionError("_resolve_crate must not run flag-OFF")

        monkeypatch.setattr(her, "_resolve_crate", _resolver_bomb)
        _pin_ram(monkeypatch, _48_GIB)
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert he.library_thread_budget({}, 2, envelope=_M4) == 7
        assert he.polars_thread_budget({}, envelope=_M4) == 14
        assert he.duckdb_thread_budget({}, envelope=_M4) == 7
        assert (
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}) == 8_589_934_592
        )
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"
        # TEETH vs a flag-routing mutant: had any call entered the seam,
        # the resolver bomb would have fired — the pristine latch + memo
        # are the proof the OFF arm never touched the apparatus.
        assert her.hwenv_rust_disabled() is False
        assert not her._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_explicit_falsy_arm_is_off_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert her.hwenv_rust_disabled() is False
        assert not her._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]


# ── Import guard ────────────────────────────────────────────────────────────


class TestImportGuard:
    def test_absent_crate_latches_and_serves_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, None)  # sys.modules[name] = None → ImportError
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert her.hwenv_rust_disabled() is True
        assert her._DISABLED_REASON[0] == "crate_unavailable"  # pyright: ignore[reportPrivateUsage]

    def test_module_missing_api_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A money-only (pre-hwenv) wheel must latch stale for THIS seam.
        money_only = types.ModuleType("brutex_core")
        money_only.round_money_str = lambda s: s  # type: ignore[attr-defined]
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_core", money_only)
        assert he.library_thread_budget({}, 2, envelope=_M4) == 7
        assert her._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    def test_non_callable_api_attr_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _fake_crate(hwenv_memory_budget="not-callable"))
        _pin_ram(monkeypatch, _48_GIB)
        assert (
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}) == 8_589_934_592
        )
        assert her._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    def test_latched_process_serves_python_without_resolving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _arm(monkeypatch, _bomb_crate())
        her._disable("prior_reason")  # pyright: ignore[reportPrivateUsage]
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"


# ── Eligibility gate (layer 2 — crate never touched, bomb-pinned) ───────────


class TestEligibilityGate:
    def test_bool_envelope_fields_route_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        env_bool = he.HardwareEnvelope(
            logical_cores=True,  # type: ignore[arg-type]
            physical_cores=True,  # type: ignore[arg-type]
            performance_cores=True,  # type: ignore[arg-type]
            efficiency_cores=0,
            source="override",
        )
        # bool is an int subclass — exact-type gate routes to the oracle.
        assert he.cpu_pool_workers({}, envelope=env_bool) == 1
        assert he.library_thread_budget({}, 2, envelope=env_bool) == 1

    def test_out_of_magnitude_counts_route_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _arm(monkeypatch, _bomb_crate())
        huge = he.envelope_from({"logical_cores": 2**31 + 1})
        assert he.cpu_pool_workers({}, envelope=huge) == 2**31 - 1
        assert he.library_thread_budget({}, 1, envelope=huge) == 2**31 + 1
        assert he.cpu_pool_workers({}, reserve=-(2**31 + 1), envelope=_M4) == 10 + 2**31 + 1

    def test_sub_one_cap_routes_to_oracle_and_keeps_the_oracle_law(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A < 1 cap is nonsense caller input whose ORACLE result is < 1
        # (`min(max(1, ...), cap)`) — the gate keeps it off the crate so
        # the poison gate's `>= 1` law stays sharp, and the served value
        # is the oracle's verbatim.
        _arm(monkeypatch, _bomb_crate())
        assert he.cpu_pool_workers({}, cap=0, envelope=_M4) == 0
        assert he.cpu_pool_workers({}, cap=-5, envelope=_M4) == -5
        assert he.cpu_pool_workers({}, cap=2**31 + 1, envelope=_M4) == 8

    def test_non_float_fraction_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An int / Fraction fraction runs EXACT-rational CPython arithmetic
        # (a different law than the crate's f64 chain) — oracle, always.
        _arm(monkeypatch, _bomb_crate())
        _pin_ram(monkeypatch, _48_GIB)
        assert he.memory_budget_bytes(fraction=1, floor_bytes=7, env={}) == _48_GIB  # type: ignore[arg-type]
        assert (
            he.memory_budget_bytes(fraction=Fraction(1, 6), floor_bytes=7, env={})  # type: ignore[arg-type]
            == _48_GIB // 6
        )

    def test_oversized_detected_ram_routes_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # >= 2**62 totals are the f64 trunc→i64 saturation hazard class.
        _arm(monkeypatch, _bomb_crate())
        _pin_ram(monkeypatch, 2**62)
        assert he.memory_budget_bytes(fraction=0.5, floor_bytes=7, env={}) == 2**61

    def test_direct_route_gate_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The remaining gate conjuncts, driven at the facade seam (the
        # public functions cannot construct these shapes).
        _arm(monkeypatch, _bomb_crate())
        law = he._memory_budget_python  # pyright: ignore[reportPrivateUsage]
        # floor_bytes out of the crossing bound → oracle.
        assert her.route_memory_budget(0, 0.5, 2**62, 1, law) == 2**62
        # workers out of bound → oracle.
        assert her.route_memory_budget(4, 0.5, 1, 2**31 + 1, law) == 1
        # non-finite / out-of-domain float fraction → oracle law value.
        assert her.route_memory_budget(0, float("nan"), 9, 1, law) == 9
        # prefer_performance not an exact bool → oracle.
        pool_law = he._pool_workers_python  # pyright: ignore[reportPrivateUsage]
        assert her.route_pool_workers(10, 14, 1, 2, None, pool_law) == 8  # type: ignore[arg-type]
        # non-str / non-ASCII / oversized key fields → oracle.
        key_law = he._hardware_key_python  # pyright: ignore[reportPrivateUsage]
        assert her.route_hardware_key(None, 1, 1, 0, 1, "arm64", key_law) == "none-1c1p0e-1g-arm64"  # type: ignore[arg-type]
        assert her.route_hardware_key("mäc", 1, 1, 0, 1, "arm64", key_law) == "m-c-1c1p0e-1g-arm64"
        assert (
            her.route_hardware_key("a" * 201, 1, 1, 0, 1, "arm64", key_law)
            == "a" * 201 + "-1c1p0e-1g-arm64"
        )
        # An ineligible count field on the key law → oracle.
        assert (
            her.route_hardware_key("m", 2**31 + 1, 1, 0, 1, "a", key_law)
            == f"m-{2**31 + 1}c1p0e-1g-a"
        )

    def test_eligibility_true_paths(self) -> None:
        assert her._eligible_pool(10, 14, True, 2, None) is True  # pyright: ignore[reportPrivateUsage]
        assert her._eligible_pool(10, 14, True, 2, 8) is True  # pyright: ignore[reportPrivateUsage]
        assert her._eligible_library(14, 2) is True  # pyright: ignore[reportPrivateUsage]
        assert her._eligible_memory(_48_GIB, 1 / 6, 7, 1) is True  # pyright: ignore[reportPrivateUsage]
        assert her._eligible_memory(0, 1 / 6, 7, 1) is True  # pyright: ignore[reportPrivateUsage]
        assert her._eligible_key("mac16-8", 14, 10, 4, 48, "arm64") is True  # pyright: ignore[reportPrivateUsage]


# ── Degrade paths + escape set + panic belt ─────────────────────────────────


class TestDegradePaths:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("v"),
            RuntimeError("r"),
            TypeError("t"),
            OverflowError("o"),
            AttributeError("a"),
        ],
    )
    def test_crate_errors_degrade_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _boom(*_a: object) -> object:
            raise exc

        _arm(monkeypatch, _fake_crate(hwenv_pool_workers=_boom))
        warned: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert he.cpu_pool_workers({}, envelope=_M4, cap=4) == 4
        assert warned == ["pool_workers_error"]
        assert her.hwenv_rust_disabled() is False  # degrade ≠ latch

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_propagates_bare(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _boom(*_a: object) -> object:
            raise exc_type()

        _arm(monkeypatch, _fake_crate(hwenv_library_thread_budget=_boom))
        with pytest.raises(exc_type):
            he.library_thread_budget({}, 2, envelope=_M4)

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object) -> object:
            raise ZeroDivisionError("broken boundary")

        _arm(monkeypatch, _fake_crate(hwenv_memory_budget=_boom))
        _pin_ram(monkeypatch, _48_GIB)
        assert (
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}) == 8_589_934_592
        )
        assert her._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]

    def test_panic_exception_class_latches_via_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_pyo3 = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake_pyo3.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake_pyo3)

        def _boom(*_a: object) -> object:
            raise PanicException("panicked past catch_unwind")

        _arm(monkeypatch, _fake_crate(hwenv_hardware_key=_boom))
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"
        assert her._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]


class TestPanicBelt:
    def test_belt_always_carries_exception_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)  # → ImportError arm
        assert her._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_belt_folds_a_real_panic_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")

        class PanicException(BaseException):
            pass

        fake.PanicException = PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert her._panic_belt_errors() == (Exception, PanicException)  # pyright: ignore[reportPrivateUsage]

    def test_non_class_and_non_baseexception_attrs_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        assert her._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]
        fake2 = types.ModuleType("pyo3_runtime")
        fake2.PanicException = dict  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake2)
        assert her._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]


# ── Refuse-poison gate ──────────────────────────────────────────────────────


class TestRefusePoison:
    @pytest.mark.parametrize("poison", [True, 0, -3, "8", 8.0, None])
    def test_nonsense_numeric_returns_refused(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        # A bool (equality-punning int subclass), a < 1 count, or a
        # wrong-typed value never ships — the oracle serves.
        _arm(monkeypatch, _fake_crate(hwenv_pool_workers=lambda *_a: poison))
        warned: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "warning",
            lambda event, **kw: warned.append(str(kw.get("reason"))),
        )
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert warned == ["pool_workers_result_refused"]
        assert her.hwenv_rust_disabled() is False

    @pytest.mark.parametrize(
        "poison",
        [
            None,
            b"mac16-8",
            "",
            "-leading",
            "trailing-",
            "UPPER-1c1p0e-1g-a",
            "with space",
            "dot.key",  # KEY_SLUG_RE admits '.', the render range does not
            "unié",
        ],
    )
    def test_malformed_key_returns_refused(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _arm(monkeypatch, _fake_crate(hwenv_hardware_key=lambda *_a: poison))
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"

    def test_valid_key_shapes_accepted_and_imply_the_slug_law(self) -> None:
        valid = her._valid_key_str  # pyright: ignore[reportPrivateUsage]
        for good in [
            "mac16-8-14c10p4e-48g-arm64",
            "unknown-4c4p0e-unkg-x86-64",
            "a",
            "0-b-9",
        ]:
            assert valid(good), good
            # Every shape-law-accepted key satisfies the ONE bench-baseline
            # slug law (the mirror pin — a key the facade adopts can never
            # be refused downstream by KEY_SLUG_RE).
            assert KEY_SLUG_RE.fullmatch(good) is not None, good


# ── Burn-in + autorevert ────────────────────────────────────────────────────


class TestBurnIn:
    def test_clean_burn_in_ships_rust_value_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        her.reset_hwenv_rust_for_tests(remaining=2)
        _arm(monkeypatch, _fake_crate())
        infos: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "info",
            lambda event, **kw: infos.append(event),
        )
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert he.cpu_pool_workers({}, envelope=_M4, prefer_performance=False) == 12
        assert her._BURN_IN_REMAINING["pool_workers"] == 0  # pyright: ignore[reportPrivateUsage]
        assert "hwenv_rust.engaged" in infos
        assert "hwenv_rust.burn_in_complete" in infos
        # Third call — steady state, no further oracle compare needed.
        assert he.cpu_pool_workers({}, envelope=_M4, reserve=4) == 6

    def test_burn_in_budgets_are_per_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        her.reset_hwenv_rust_for_tests(remaining=1)
        _arm(monkeypatch, _fake_crate())
        assert he.library_thread_budget({}, 2, envelope=_M4) == 7
        budgets = her._BURN_IN_REMAINING  # pyright: ignore[reportPrivateUsage]
        assert budgets["library_thread_budget"] == 0
        assert budgets["pool_workers"] == 1  # untouched sibling budget

    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wrong-but-well-shaped crate value: the burn-in must catch it,
        # ship the ORACLE value and latch the process OFF.
        _arm(monkeypatch, _fake_crate(hwenv_pool_workers=lambda *_a: 99))
        errors: list[str] = []
        monkeypatch.setattr(
            her._LOG,  # pyright: ignore[reportPrivateUsage]
            "error",
            lambda event, **kw: errors.append(str(kw.get("reason"))),
        )
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert errors == ["burn_in_mismatch_pool_workers"]
        assert her.hwenv_rust_disabled() is True
        # And the NEXT call short-circuits at the latch (bomb-safe).
        monkeypatch.setitem(sys.modules, "brutex_core", _bomb_crate())
        assert he.cpu_pool_workers({}, envelope=_M4) == 8

    def test_post_burn_in_poison_is_caught_by_shape_not_burn_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Steady state (remaining=0): a WELL-SHAPED wrong value would ship
        # (the documented burn-in envelope); the SHAPE gate still refuses
        # nonsense ones. Pin the envelope honestly.
        her.reset_hwenv_rust_for_tests(remaining=0)
        _arm(monkeypatch, _fake_crate(hwenv_pool_workers=lambda *_a: -7))
        assert he.cpu_pool_workers({}, envelope=_M4) == 8  # refused → oracle
        her.reset_hwenv_rust_for_tests(remaining=0)
        _arm(monkeypatch, _fake_crate(hwenv_pool_workers=lambda *_a: 99))
        assert he.cpu_pool_workers({}, envelope=_M4) == 99  # the honest envelope

    def test_sentinel_memory_burn_in_compares_floor_exactly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The floor-EXACT sentinel law under burn-in: a crate whose
        # sentinel arm drifts by ONE byte autoreverts loudly.
        _arm(monkeypatch, _fake_crate(hwenv_memory_budget=lambda d, f, fl, w: fl + 1))
        _pin_ram(monkeypatch, 0)
        assert he.memory_budget_bytes(fraction=1 / 6, floor_bytes=7 * 2**20, env={}) == 7 * 2**20
        assert her._DISABLED_REASON[0] == "burn_in_mismatch_memory_budget"  # pyright: ignore[reportPrivateUsage]


# ── Routed wiring (each law's success arm through hardware_envelope) ────────


class TestRoutedWiring:
    def test_each_public_law_routes_through_its_crate_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, tuple[object, ...]]] = []

        def _spy(name: str, fn: Callable[..., object]) -> Callable[..., object]:
            def inner(*args: object) -> object:
                calls.append((name, args))
                return fn(*args)

            return inner

        crate = _fake_crate()
        for name in her._CRATE_API:  # pyright: ignore[reportPrivateUsage]
            setattr(crate, name, _spy(name, getattr(crate, name)))
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_core", crate)
        her.reset_hwenv_rust_for_tests(remaining=0)
        _pin_ram(monkeypatch, _48_GIB)

        assert he.cpu_pool_workers({}, envelope=_M4, cap=6) == 6
        assert he.library_thread_budget({}, 2, envelope=_M4) == 7
        assert he.memory_budget_bytes(fraction=0.5, floor_bytes=8 * 2**30, env={}) == 25_769_803_776
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"
        assert calls == [
            ("hwenv_pool_workers", (10, 14, True, 2, 6)),
            ("hwenv_library_thread_budget", (14, 2)),
            ("hwenv_memory_budget", (_48_GIB, 0.5, 8 * 2**30, 1)),
            ("hwenv_hardware_key", ("mac16-8", 14, 10, 4, 48, "arm64")),
        ]

    def test_polars_and_duckdb_budgets_inherit_the_library_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, ...]] = []

        def _capture(*args: object) -> object:
            seen.append(args)
            return he._library_thread_budget_python(*args)  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]

        _arm(monkeypatch, _fake_crate(hwenv_library_thread_budget=_capture))
        her.reset_hwenv_rust_for_tests(remaining=0)
        assert he.polars_thread_budget({}, envelope=_M4) == 14
        assert he.duckdb_thread_budget({}, envelope=_M4) == 7
        assert seen == [(14, 1), (14, 2)]

    def test_env_override_short_circuits_before_the_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fail-loud env laws stay Python: a CPU_WORKERS override never
        # consults the crate (the bomb proves it).
        _arm(monkeypatch, _bomb_crate())
        assert he.cpu_pool_workers({he.CPU_WORKERS_ENV: "5"}, envelope=_M4) == 5
        _pin_ram(monkeypatch, _48_GIB)
        assert (
            he.memory_budget_bytes(
                fraction=0.5,
                floor_bytes=7,
                env={"X_BUDGET": "1234"},
                env_var="X_BUDGET",
            )
            == 1234
        )
        with pytest.raises(ValueError, match="X_BUDGET"):
            he.memory_budget_bytes(
                fraction=0.5, floor_bytes=7, env={"X_BUDGET": "junk"}, env_var="X_BUDGET"
            )
        assert not her._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_memory_budget_flag_on_still_logs_derivation_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The observability law stays Python-side and identical flag-ON.
        _arm(monkeypatch, _fake_crate())
        her.reset_hwenv_rust_for_tests(remaining=0)
        he.reset_hardware_envelope_cache()
        _pin_ram(monkeypatch, _48_GIB)
        infos: list[str] = []
        monkeypatch.setattr(
            he._LOG,  # pyright: ignore[reportPrivateUsage]
            "info",
            lambda event, **kw: infos.append(event),
        )
        assert he.memory_budget_bytes(fraction=0.5, floor_bytes=7, env={}) == 25_769_803_776
        assert infos == ["hardware_envelope.budget_derived"]
        he.reset_hardware_envelope_cache()


# ── ON-arm parity with the REAL wheel (skips cleanly when absent) ───────────


class TestRealCrateParity:
    """Flag-ON legs against the real ``brutex_core`` wheel.

    Detection stays Python (the seam split) — parity here is BEHAVIORAL:
    the real detected envelope/RAM feed the routed derivations and the
    flag-ON results must equal the flag-OFF oracles, on this box and over
    the hypothesis domains.
    """

    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip(
            "brutex_core", reason="brutex-core wheel not built in this env (parity lane covers it)"
        )

    def test_reference_box_goldens_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        her.reset_hwenv_rust_for_tests(remaining=0)  # steady state — no oracle cushion
        _pin_ram(monkeypatch, _48_GIB)
        # The CLAUDE.md §8 M4-Pro worked examples, adopted from the crate.
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert he.cpu_pool_workers({}, envelope=_M4, prefer_performance=False) == 12
        assert he.library_thread_budget({}, 2, envelope=_M4) == 7
        assert (
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}) == 8_589_934_592
        )
        assert (
            he.memory_budget_bytes(fraction=1 / 3, floor_bytes=16 * 2**30, env={}) == 17_179_869_184
        )
        assert he.hardware_key({}, fingerprint=_M4_FP) == "mac16-8-14c10p4e-48g-arm64"

    def test_this_box_behavioral_parity_real_detection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REAL detection (envelope + RAM + fingerprint) → flag-ON must
        # serve exactly the flag-OFF numbers/keys on this machine.
        off = (
            he.cpu_pool_workers({}),
            he.library_thread_budget({}, 4),
            he.polars_thread_budget({}),
            he.duckdb_thread_budget({}),
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}),
            he.hardware_key({}),
        )
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        her.reset_hwenv_rust_for_tests(remaining=0)
        on = (
            he.cpu_pool_workers({}),
            he.library_thread_budget({}, 4),
            he.polars_thread_budget({}),
            he.duckdb_thread_budget({}),
            he.memory_budget_bytes(fraction=1 / 6, floor_bytes=8 * 2**30, env={}),
            he.hardware_key({}),
        )
        assert on == off
        assert her.hwenv_rust_disabled() is False

    def test_real_crate_burn_in_completes_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        her.reset_hwenv_rust_for_tests()  # full 16-sample budgets live
        for _ in range(her.BURN_IN_SAMPLES + 1):
            assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert her.hwenv_rust_disabled() is False
        assert her._BURN_IN_REMAINING["pool_workers"] == 0  # pyright: ignore[reportPrivateUsage]

    def test_engagement_spy_proves_the_crate_serves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex_core

        calls: list[tuple[object, ...]] = []
        real = brutex_core.hwenv_pool_workers

        def _spy(
            performance_cores: int,
            logical_cores: int,
            prefer_performance: bool,
            reserve: int,
            cap: int | None,
        ) -> int:
            calls.append((performance_cores, logical_cores, prefer_performance, reserve, cap))
            return real(performance_cores, logical_cores, prefer_performance, reserve, cap)

        monkeypatch.setattr(brutex_core, "hwenv_pool_workers", _spy)
        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")
        her.reset_hwenv_rust_for_tests(remaining=0)
        assert he.cpu_pool_workers({}, envelope=_M4) == 8
        assert calls == [(10, 14, True, 2, None)]  # genuinely ENGAGED

    def test_hypothesis_differential_all_four_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        monkeypatch.setenv(her.HWENV_RUST_ENV, "1")

        counts = st.integers(min_value=-64, max_value=512)
        caps = st.one_of(st.none(), st.integers(min_value=-4, max_value=64))
        rams = st.one_of(
            st.just(0),  # the sentinel arm
            st.integers(min_value=1, max_value=2**50),
            st.just(_48_GIB),
        )
        fractions = st.one_of(
            st.sampled_from([1 / 6, 1 / 3, 0.5, 0.25, 1.0]),
            st.floats(min_value=1e-12, max_value=1.0, allow_nan=False, exclude_min=True),
        )
        floors = st.integers(min_value=1, max_value=2**40)
        workers = st.integers(min_value=-4, max_value=256)
        slugs = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)

        pool_law = he._pool_workers_python  # pyright: ignore[reportPrivateUsage]
        lib_law = he._library_thread_budget_python  # pyright: ignore[reportPrivateUsage]
        mem_law = he._memory_budget_python  # pyright: ignore[reportPrivateUsage]
        key_law = he._hardware_key_python  # pyright: ignore[reportPrivateUsage]

        @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
        @given(
            perf=counts,
            logical=counts,
            prefer=st.booleans(),
            reserve=counts,
            cap=caps,
            physical=counts,
            div=workers,
            ram=rams,
            fraction=fractions,
            floor=floors,
            mem_workers=st.integers(min_value=1, max_value=64),
            model=slugs,
            arch=slugs,
            key_ram=st.integers(min_value=-8, max_value=4096),
        )
        def _differential(
            perf: int,
            logical: int,
            prefer: bool,
            reserve: int,
            cap: int | None,
            physical: int,
            div: int,
            ram: int,
            fraction: float,
            floor: int,
            mem_workers: int,
            model: str,
            arch: str,
            key_ram: int,
        ) -> None:
            her.reset_hwenv_rust_for_tests(remaining=0)
            assert her.route_pool_workers(perf, logical, prefer, reserve, cap, pool_law) == (
                pool_law(perf, logical, prefer, reserve, cap)
            )
            assert her.route_library_thread_budget(physical, div, lib_law) == lib_law(physical, div)
            assert her.route_memory_budget(ram, fraction, floor, mem_workers, mem_law) == (
                mem_law(ram, fraction, floor, mem_workers)
            )
            assert her.route_hardware_key(
                model, physical, perf, logical, key_ram, arch, key_law
            ) == key_law(model, physical, perf, logical, key_ram, arch)
            assert her.hwenv_rust_disabled() is False, (
                "a latch trip inside the differential marks a real divergence"
            )

        _differential()


# ── Workflow wiring pins ────────────────────────────────────────────────────


class TestWorkflowWiring:
    """Deterministic pins on the seam's CI wiring (the
    ``TestMutationWorkflowWiring`` precedent — they pin the WIRING only;
    the weekly mutation run and the parity lane execute on their own
    schedules/triggers).
    """

    _WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

    def test_facade_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = (self._WORKFLOWS / "mutation.yml").read_text(encoding="utf-8")
        pair = (
            '"brutex/config/hardware_envelope_rust.py:tests/unit/test_hardware_envelope_rust.py:"'
        )
        assert pair in text, (
            "hardware_envelope_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )

    def test_parity_lane_carries_the_hwenv_flag_on_leg(self) -> None:
        text = (self._WORKFLOWS / "core-rust-parity.yml").read_text(encoding="utf-8")
        assert "BRUTEX_CORE_RUST_HWENV" in text, "core-rust-parity.yml lost the hwenv flag-ON leg"
        assert "tests/unit/test_hardware_envelope_rust.py" in text, (
            "core-rust-parity.yml no longer runs the hwenv facade suite"
        )
