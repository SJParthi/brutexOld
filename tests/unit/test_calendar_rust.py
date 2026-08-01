"""Tests for the ``calendar_rust`` routing facade (DEC-RUST-CALENDAR-FACADE-FLIP-001).

MIGRATION.md #2218; the Pattern-A facade flip of the crate landed INERT by
DEC-RUST-CALENDAR-LEAVES-001 (#1962). Yields 100% line + branch on
``brutex/calendars/calendar_rust.py`` ALONE (the discovery-gate contract, the
``notify_rust`` precedent) with the WHEEL-FREE fake-crate arms — the facade
degrades to the live Python oracle when the wheel is absent, so the whole
routing lattice is driven with a synthetic crate + synthetic caller laws:

* the flag matrix (default ON, explicit falsy kill switch, junk loud-OFF once);
* ``unwrap`` over both outcome tags;
* the OFF-never-imports-crate bomb spy (a bombed ``import_module`` + a
  rust_call that must never run);
* the crate-absent / crate-stale / non-callable latches;
* every degrade / escape / abstain / refuse-poison / panic arm;
* the ``MemoryError`` / ``SystemExit`` / ``KeyboardInterrupt`` re-raise pins;
* the burn-in mismatch autorevert, the clean-completion INFO, the steady state;
* the per-law burn-in budgets;
* the ``_disable`` repeat no-op + error/warn arms, the panic-belt resolver, the
  reset seam and the engaged-INFO memo.

Gated on the REAL built wheel (``importorskip`` — skipped in the wheel-free main
CI, run in the dedicated non-required ``calendar-rust-parity.yml`` lane): the
symbol-surface handshake + a real-FFI-vs-Python differential for the
settle-evidence law through ``routed`` in the post-burn-in steady state (the
Rust tuple ships uncompared and STILL equals the Python validator's fields).
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import date, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import brutex.calendars.calendar_rust as cr
import pytest
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

# ── Helpers ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module is the *degrade-lattice* suite — pin ``BRUTEX_RUST_STRICT=0``.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON by default, so every
    class-A (crate absent / stale / errored) and class-B (structurally refused
    crate result) reason now RAISES ``RustFallbackRefused`` instead of quietly
    serving the Python oracle.  The tests in this module exist precisely to
    exercise that oracle machinery — the still-supported ``BRUTEX_RUST_STRICT=0``
    mode — so the mode is DECLARED once, here, rather than by weakening any
    assertion.  Strict-ON refusal is pinned separately in ``TestStrictRefusal``
    at the end of this module.
    """
    from brutex.config import rust_strict

    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


def _oracle_value(value: str = "ORACLE") -> Callable[[], cr.LawOutcome[str]]:
    """A side-effect-free oracle returning ``Value(value)``."""

    def _oracle() -> cr.LawOutcome[str]:
        return cr.Value(value)

    return _oracle


def _oracle_raise(message: str) -> Callable[[], cr.LawOutcome[str]]:
    """A side-effect-free oracle returning ``Raise(message)``."""

    def _oracle() -> cr.LawOutcome[str]:
        return cr.Raise(message)

    return _oracle


def _fake_crate(
    *, drop_symbols: tuple[str, ...] = (), version: str = "0.1.0-fake"
) -> SimpleNamespace:
    """A stand-in ``brutex_calendar`` exposing the 3-callable handshake surface.

    ``routed`` marshalling is caller-owned (``rust_call`` / ``materialize`` /
    ``oracle``), so the fake's callables are inert placeholders — the handshake
    only checks they are *present + callable*.
    """
    crate = SimpleNamespace(
        classify_month_drift=lambda *a, **k: None,
        parse_observed_sidecar=lambda *a, **k: None,
        validate_settle_evidence=lambda *a, **k: None,
        __version__=version,
    )
    for name in drop_symbols:
        delattr(crate, name)
    return crate


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``import_module`` at the fake crate.

    Every OTHER name (the panic belt's ``pyo3_runtime`` / ``polars.exceptions``
    probes) delegates to the REAL importer.
    """
    real_import = importlib.import_module

    def _import(name: str) -> object:
        if name == "brutex_calendar":
            return crate
        return real_import(name)

    monkeypatch.setattr(importlib, "import_module", _import)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "1")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh flag/latch/burn-in/memo state per test (the sibling convention)."""
    monkeypatch.delenv(cr.CALENDAR_RUST_ENV, raising=False)
    cr.reset_calendar_rust_for_tests()
    yield
    cr.reset_calendar_rust_for_tests()


# ── Flag matrix (Pattern A: default ON, junk loud-OFF) ──────────────────


class TestFlagMatrix:
    """``BRUTEX_CALENDAR_RUST`` resolution (DEC-RUST-DEFAULT-ON-WAVE-001)."""

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE ", "On"])
    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, value)
        assert cr.calendar_rust_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
    def test_falsy_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, value)
        with capture_logs() as logs:
            assert cr.calendar_rust_enabled() is False
        assert logs == []

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_is_on_without_warn(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, value)
        with capture_logs() as logs:
            assert cr.calendar_rust_enabled() is True
        assert logs == []

    def test_unset_is_on(self) -> None:
        assert cr.calendar_rust_enabled() is True

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert cr.calendar_rust_enabled() is False
            assert cr.calendar_rust_enabled() is False  # second read: silent
        warns = [rec for rec in logs if rec["event"] == "calendar_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "banana"

    def test_explicit_raw_bypasses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "1")
        assert cr.calendar_rust_enabled("0") is False
        assert cr.calendar_rust_enabled("yes") is True

    def test_default_arg_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # raw=None reads CALENDAR_RUST_ENV from os.environ.
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "off")
        assert cr.calendar_rust_enabled() is False


# ── Outcome tags ────────────────────────────────────────────────────────


class TestUnwrap:
    def test_value_returns_value(self) -> None:
        assert cr.unwrap(cr.Value(7)) == 7

    def test_raise_reraises_byte_identical(self) -> None:
        with pytest.raises(ValueError, match=r"^exact byte message$"):
            cr.unwrap(cr.Raise("exact byte message"))

    def test_equal_raise_messages_compare_equal(self) -> None:
        # The burn-in relies on message-string ``==`` (identity-based
        # ValueError instances would never compare equal).
        assert cr.Raise("m") == cr.Raise("m")
        assert cr.Raise("m") != cr.Raise("n")


# ── Disable latch + reset seam ──────────────────────────────────────────


class TestDisableAndReset:
    def test_disabled_default_false(self) -> None:
        assert cr.rust_calendar_disabled() is False

    def test_disable_warn_arm(self) -> None:
        with capture_logs() as logs:
            cr._disable("guard_latch")  # pyright: ignore[reportPrivateUsage]
        assert cr.rust_calendar_disabled() is True
        assert [r["event"] for r in logs] == ["calendar_rust.rust_path_disabled"]

    def test_disable_error_arm(self) -> None:
        with capture_logs() as logs:
            cr._disable("boom", error=True, detail="x")  # pyright: ignore[reportPrivateUsage]
        assert [r["event"] for r in logs] == ["calendar_rust.autoreverted"]

    def test_disable_repeat_is_noop(self) -> None:
        cr._disable("first")  # pyright: ignore[reportPrivateUsage]
        with capture_logs() as logs:
            cr._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert logs == []
        assert cr._DISABLED_REASON[0] == "first"  # pyright: ignore[reportPrivateUsage]

    def test_reset_restores_latch_budgets_memo(self) -> None:
        cr._disable("latched")  # pyright: ignore[reportPrivateUsage]
        cr._warn_once("some_reason")  # pyright: ignore[reportPrivateUsage]
        cr._BURN_IN_REMAINING[cr._LAW_CLASSIFY] = 3  # pyright: ignore[reportPrivateUsage]
        cr.reset_calendar_rust_for_tests()
        assert cr.rust_calendar_disabled() is False
        assert cr._BURN_IN_REMAINING[cr._LAW_CLASSIFY] == cr.BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]
        assert not cr._OBSERVABILITY_MEMO  # pyright: ignore[reportPrivateUsage]

    def test_reset_steady_state(self) -> None:
        cr.reset_calendar_rust_for_tests(remaining=0)
        assert all(v == 0 for v in cr._BURN_IN_REMAINING.values())  # pyright: ignore[reportPrivateUsage]


class TestWarnOnce:
    def test_warn_once_logs_then_silent(self) -> None:
        with capture_logs() as logs:
            cr._warn_once("r", a=1)  # pyright: ignore[reportPrivateUsage]
            cr._warn_once("r", a=1)  # pyright: ignore[reportPrivateUsage]
        warns = [r for r in logs if r["event"] == "calendar_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "r"

    def test_warn_fallback_public_seam_delegates(self) -> None:
        with capture_logs() as logs:
            cr.warn_fallback("poison_shape", got="junk")
        warns = [r for r in logs if r["event"] == "calendar_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "poison_shape"


# ── Panic belt resolver (local by-name; the notify/lake template) ───────


class TestPanicBelt:
    def test_belt_always_contains_exception(self) -> None:
        assert Exception in cr._panic_belt_errors()  # pyright: ignore[reportPrivateUsage]

    def test_both_homes_unresolvable_yields_exception_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert cr._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_home_without_panic_class_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # getattr None (no attr) AND a present-but-non-type PanicException both
        # take the ``isinstance(panic, type)`` False arm.
        monkeypatch.setitem(sys.modules, "pyo3_runtime", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "polars.exceptions", SimpleNamespace(PanicException=7))
        assert cr._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_resolved_panic_class_joins_belt_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(BaseException):
            """A stand-in pyo3 PanicException type."""

        monkeypatch.setitem(sys.modules, "pyo3_runtime", SimpleNamespace(PanicException=FakePanic))
        monkeypatch.setitem(
            sys.modules, "polars.exceptions", SimpleNamespace(PanicException=FakePanic)
        )
        # SAME class from both homes joins ONCE (the ``panic not in belt`` dedupe).
        assert cr._panic_belt_errors() == (Exception, FakePanic)  # pyright: ignore[reportPrivateUsage]


# ── Crate resolution handshake ──────────────────────────────────────────


def _bomb_import(name: str) -> object:
    if name == "brutex_calendar":
        raise AssertionError("crate must NOT be imported on the OFF/oracle path")
    return importlib.import_module(name)


class TestHandshake:
    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError(f"No module named {name!r}")

        monkeypatch.setattr(importlib, "import_module", _import)
        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
                rust_call=lambda _c: "unreached",
                materialize=lambda _r: cr.Value("RUST"),
                oracle=_oracle_value(),
            )
        assert cr.unwrap(out) == "ORACLE"
        assert cr._DISABLED_REASON[0] == "crate_unavailable"  # pyright: ignore[reportPrivateUsage]
        assert "calendar_rust.rust_path_disabled" in [r["event"] for r in logs]

    def test_stale_wheel_missing_symbol_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(drop_symbols=("validate_settle_evidence",)))
        out = cr.routed(
            cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "unreached",
            materialize=lambda _r: cr.Value("RUST"),
            oracle=_oracle_value(),
        )
        assert cr.unwrap(out) == "ORACLE"
        assert cr._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    def test_non_callable_symbol_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        crate.parse_observed_sidecar = 42  # present but not callable
        _install_crate(monkeypatch, crate)
        out = cr.routed(
            cr._LAW_OBSERVED,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "unreached",
            materialize=lambda _r: cr.Value("RUST"),
            oracle=_oracle_value(),
        )
        assert cr.unwrap(out) == "ORACLE"
        assert cr._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]


# ── routed: OFF / latched / degrade / abstain / panic / burn-in ─────────


class TestRoutedOffAndLatched:
    def test_flag_off_serves_oracle_never_imports_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "0")
        monkeypatch.setattr(importlib, "import_module", _bomb_import)

        def _rust_call(_c: ModuleType) -> object:
            raise AssertionError("rust_call must NOT run on the OFF path")

        out = cr.routed(
            cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
            rust_call=_rust_call,
            materialize=lambda _r: cr.Value("RUST"),
            oracle=_oracle_value(),
        )
        assert cr.unwrap(out) == "ORACLE"
        assert cr.rust_calendar_disabled() is False  # never latched — crate untouched

    def test_pre_latched_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        cr._disable("prior")  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setattr(importlib, "import_module", _bomb_import)
        out = cr.routed(
            cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "x",
            materialize=lambda _r: cr.Value("RUST"),
            oracle=_oracle_value(),
        )
        assert cr.unwrap(out) == "ORACLE"


class TestRoutedDegradeArms:
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
    def test_degrade_set_warns_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _rust_call(_c: ModuleType) -> object:
            raise exc

        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
                rust_call=_rust_call,
                materialize=lambda _r: cr.Value("RUST"),
                oracle=_oracle_value(),
            )
        assert cr.unwrap(out) == "ORACLE"
        assert cr.rust_calendar_disabled() is False  # degrade, never a latch
        warns = [r for r in logs if r["event"] == "calendar_rust.fallback"]
        assert warns and warns[0]["reason"] == "classify_error"

    def test_degrade_from_materialize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A malformed rust string reaching date.fromisoformat inside materialize
        # degrades (materialize runs INSIDE the guard).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _materialize(_r: object) -> cr.LawOutcome[str]:
            date.fromisoformat("not-a-date")  # ValueError
            return cr.Value("unreached")

        out = cr.routed(
            cr._LAW_OBSERVED,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "raw",
            materialize=_materialize,
            oracle=_oracle_value(),
        )
        assert cr.unwrap(out) == "ORACLE"

    @pytest.mark.parametrize("exc_type", [MemoryError, SystemExit, KeyboardInterrupt])
    def test_escape_set_reraises(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _rust_call(_c: ModuleType) -> object:
            raise exc_type

        with pytest.raises(exc_type):
            cr.routed(
                cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
                rust_call=_rust_call,
                materialize=lambda _r: cr.Value("RUST"),
                oracle=_oracle_value(),
            )
        assert cr.rust_calendar_disabled() is False  # never latched by the escape set

    def test_out_of_degrade_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        class FakePanic(Exception):
            """Stands in for a leaked pyo3 panic / unknown crate defect."""

        def _rust_call(_c: ModuleType) -> object:
            raise FakePanic("kernel panicked")

        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
                rust_call=_rust_call,
                materialize=lambda _r: cr.Value("RUST"),
                oracle=_oracle_value(),
            )
        assert cr.unwrap(out) == "ORACLE"
        assert cr._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]
        assert "calendar_rust.rust_path_disabled" in [r["event"] for r in logs]


class TestRoutedAbstain:
    def test_materialize_none_abstains_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The designed silent abstain (law-2 serde None) — no burn-in decrement,
        # no engaged INFO, no WARN.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        before = cr._BURN_IN_REMAINING[cr._LAW_SETTLE]  # pyright: ignore[reportPrivateUsage]
        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
                rust_call=lambda _c: None,
                materialize=lambda _r: None,
                oracle=_oracle_value(),
            )
        assert cr.unwrap(out) == "ORACLE"
        assert cr._BURN_IN_REMAINING[cr._LAW_SETTLE] == before  # pyright: ignore[reportPrivateUsage]
        assert logs == []  # silent abstain

    def test_refuse_poison_via_warn_fallback_then_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A refuse-poison surprise: materialize WARNs (public seam) then abstains.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _materialize(_r: object) -> cr.LawOutcome[str] | None:
            cr.warn_fallback("observed_poison", got="junk")
            return None

        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_OBSERVED,  # pyright: ignore[reportPrivateUsage]
                rust_call=lambda _c: "wrong-shape",
                materialize=_materialize,
                oracle=_oracle_value(),
            )
        assert cr.unwrap(out) == "ORACLE"
        assert [r["reason"] for r in logs if r["event"] == "calendar_rust.fallback"] == [
            "observed_poison"
        ]


class TestRoutedBurnIn:
    def test_burn_in_mismatch_autoreverts_and_ships_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
                rust_call=lambda _c: "raw",
                materialize=lambda _r: cr.Value("RUST"),
                oracle=_oracle_value("PY"),
            )
        assert cr.unwrap(out) == "PY"  # ships the ORACLE outcome
        errors = [r for r in logs if r["event"] == "calendar_rust.autoreverted"]
        assert errors and errors[0]["reason"] == "burn_in_mismatch_classify"
        assert cr.rust_calendar_disabled() is True

    def test_burn_in_mismatch_raise_outcome_reraises_oracle_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When both outcomes are Raise but the MESSAGE differs, the oracle Raise
        # ships (its byte-exact message wins).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        out = cr.routed(
            cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "raw",
            materialize=lambda _r: cr.Raise("rust msg"),
            oracle=_oracle_raise("python msg"),
        )
        with pytest.raises(ValueError, match=r"^python msg$"):
            cr.unwrap(out)
        assert cr.rust_calendar_disabled() is True

    def test_clean_burn_in_decrements_without_complete_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            out = cr.routed(
                cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
                rust_call=lambda _c: "raw",
                materialize=lambda _r: cr.Value("SAME"),
                oracle=_oracle_value("SAME"),
            )
        assert cr.unwrap(out) == "SAME"  # ships the (matching) Rust outcome
        assert cr._BURN_IN_REMAINING[cr._LAW_CLASSIFY] == cr.BURN_IN_SAMPLES - 1  # pyright: ignore[reportPrivateUsage]
        assert "calendar_rust.burn_in_complete" not in [r["event"] for r in logs]
        assert "calendar_rust.engaged" in [r["event"] for r in logs]

    def test_clean_burn_in_completes_then_ships_rust_uncompared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        # Exhaust exactly BURN_IN_SAMPLES clean calls; the last logs completion.
        oracle_calls = [0]

        def _oracle() -> cr.LawOutcome[str]:
            oracle_calls[0] += 1
            return cr.Value("SAME")

        with capture_logs() as logs:
            for _ in range(cr.BURN_IN_SAMPLES):
                out = cr.routed(
                    cr._LAW_OBSERVED,  # pyright: ignore[reportPrivateUsage]
                    rust_call=lambda _c: "raw",
                    materialize=lambda _r: cr.Value("SAME"),
                    oracle=_oracle,
                )
                assert cr.unwrap(out) == "SAME"
        assert cr._BURN_IN_REMAINING[cr._LAW_OBSERVED] == 0  # pyright: ignore[reportPrivateUsage]
        done = [r for r in logs if r["event"] == "calendar_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == cr._LAW_OBSERVED  # pyright: ignore[reportPrivateUsage]
        # The oracle was consulted on every burn-in call (side-effect-free proof).
        assert oracle_calls[0] == cr.BURN_IN_SAMPLES

        # One MORE call: steady state — oracle must NOT be consulted.
        def _oracle_bomb() -> cr.LawOutcome[str]:
            raise AssertionError("steady state must not consult the oracle")

        out2 = cr.routed(
            cr._LAW_OBSERVED,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "raw",
            materialize=lambda _r: cr.Value("RUST-ONLY"),
            oracle=_oracle_bomb,
        )
        assert cr.unwrap(out2) == "RUST-ONLY"
        assert cr.rust_calendar_disabled() is False

    def test_steady_state_reset_skips_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        cr.reset_calendar_rust_for_tests(remaining=0)

        def _oracle_bomb() -> cr.LawOutcome[str]:
            raise AssertionError("steady state must not consult the oracle")

        out = cr.routed(
            cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "raw",
            materialize=lambda _r: cr.Value("RUST"),
            oracle=_oracle_bomb,
        )
        assert cr.unwrap(out) == "RUST"

    def test_burn_in_budgets_are_per_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        # A mismatch on the SETTLE law latches the whole facade OFF, but the
        # decrement it would have made is scoped to SETTLE only.
        cr.routed(
            cr._LAW_CLASSIFY,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda _c: "raw",
            materialize=lambda _r: cr.Value("SAME"),
            oracle=_oracle_value("SAME"),
        )
        assert cr._BURN_IN_REMAINING[cr._LAW_CLASSIFY] == cr.BURN_IN_SAMPLES - 1  # pyright: ignore[reportPrivateUsage]
        assert cr._BURN_IN_REMAINING[cr._LAW_SETTLE] == cr.BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]
        assert cr._BURN_IN_REMAINING[cr._LAW_OBSERVED] == cr.BURN_IN_SAMPLES  # pyright: ignore[reportPrivateUsage]

    def test_engaged_info_logged_once_across_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            for law in (cr._LAW_CLASSIFY, cr._LAW_SETTLE):  # pyright: ignore[reportPrivateUsage]
                cr.routed(
                    law,
                    rust_call=lambda _c: "raw",
                    materialize=lambda _r: cr.Value("SAME"),
                    oracle=_oracle_value("SAME"),
                )
        engaged = [r for r in logs if r["event"] == "calendar_rust.engaged"]
        assert len(engaged) == 1


# ── Wheel-gated real-crate differential (parity lane only) ──────────────


def _real_crate() -> ModuleType:
    return cast(
        "ModuleType",
        pytest.importorskip(
            "brutex_calendar",
            reason="the compiled rust/brutex-calendar wheel is not installed "
            "(built + installed only in the calendar-rust-parity.yml lane)",
        ),
    )


def _valid_settle_bytes() -> tuple[bytes, str]:
    base = date(2026, 7, 6)
    days = [(base + timedelta(days=i)).isoformat() for i in range(5)]
    per_day = {
        day: {
            "15:35": {"records": 14, "all_pass": False, "failed_checks": ["count_ok"]},
            "15:45": {"records": 14, "all_pass": True, "failed_checks": []},
        }
        for day in days
    }
    payload = {
        "schema": 1,
        "kind": "groww-postclose-settle-evidence",
        "earliest_all_pass_ist": "15:45",
        "adopted_settle_ist": "16:00",
        "margin_minutes": 15,
        "distinct_trading_days": 5,
        "per_day": per_day,
        "generated_at_utc": "2026-07-13T12:31:04+00:00",
        "probe_script_version": "1.1",
        "checks": ["count_ok"],
        "slots_observed": ["15:35", "15:45"],
    }
    # The caller passes ``path_repr`` PRE-``repr()``'d to the crate (the facade
    # contract); the Python oracle ``repr()``s the RAW path itself.
    return json.dumps(payload).encode("utf-8"), repr("/tmp/evidence.json")


class TestRealWheel:
    """Runs ONLY where the compiled wheel is installed (the parity lane)."""

    def test_handshake_surface_present(self) -> None:
        crate = _real_crate()
        for name in cr._CRATE_API:  # pyright: ignore[reportPrivateUsage]
            assert callable(getattr(crate, name, None)), name

    def test_settle_law_real_ffi_valid(self) -> None:
        crate = _real_crate()
        payload_bytes, path_repr = _valid_settle_bytes()
        result = crate.validate_settle_evidence(payload_bytes, path_repr)
        assert result is not None
        valid, message, adopted_hhmm, generated_raw = result
        assert valid is True
        assert message == ""
        assert adopted_hhmm == "16:00"
        assert generated_raw == "2026-07-13T12:31:04+00:00"

    def test_settle_law_real_ffi_invalid_message_byte_exact(self) -> None:
        from brutex.calendars.settle_evidence import validate_evidence_payload

        crate = _real_crate()
        payload = {"schema": 999, "kind": "groww-postclose-settle-evidence"}
        raw_path = "/tmp/bad.json"
        result = crate.validate_settle_evidence(json.dumps(payload).encode("utf-8"), repr(raw_path))
        assert result is not None
        valid, message, adopted_hhmm, generated_raw = result
        assert valid is False
        assert adopted_hhmm is None and generated_raw is None
        # The crate's refusal message (fed the PRE-repr'd path) is BYTE-IDENTICAL
        # to the Python oracle's (which repr()s the RAW path itself).
        with pytest.raises(ValueError) as excinfo:
            validate_evidence_payload(payload, path=raw_path)
        assert message == str(excinfo.value)

    def test_routed_settle_steady_state_ships_real_rust_tuple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through routed() in the post-burn-in steady state: the REAL
        # crate tuple ships UNCOMPARED and STILL equals the Python validator's
        # fields (the genuine no-illusion real-FFI differential).
        from brutex.calendars.settle_evidence import validate_evidence_payload

        crate = _real_crate()
        monkeypatch.setenv(cr.CALENDAR_RUST_ENV, "1")
        cr.reset_calendar_rust_for_tests(remaining=0)
        payload_bytes, path_repr = _valid_settle_bytes()

        def _oracle_bomb() -> cr.LawOutcome[tuple[bool, str, str | None, str | None]]:
            raise AssertionError("steady state must not consult the oracle")

        def _materialize(
            raw: object,
        ) -> cr.LawOutcome[tuple[bool, str, str | None, str | None]] | None:
            if raw is None:
                return None
            assert isinstance(raw, tuple)
            return cr.Value(cast("tuple[bool, str, str | None, str | None]", raw))

        out = cr.routed(
            cr._LAW_SETTLE,  # pyright: ignore[reportPrivateUsage]
            rust_call=lambda c: c.validate_settle_evidence(payload_bytes, path_repr),
            materialize=_materialize,
            oracle=_oracle_bomb,
        )
        rust_tuple = cr.unwrap(out)
        oracle = validate_evidence_payload(json.loads(payload_bytes), path=path_repr)
        assert rust_tuple == (
            True,
            "",
            oracle.adopted.strftime("%H:%M"),
            oracle.generated_at_utc,
        )
        assert crate.validate_settle_evidence is not None  # crate genuinely engaged


# ── Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ───────────────────────────


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Everything else in this module runs with ``BRUTEX_RUST_STRICT=0`` (the
    degrade lattice). These tests DELETE the kill switch so strict mode takes
    its shipped default (ON) and pin that the choke points REFUSE instead of
    quietly handing the call to the Python oracle.
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config import rust_strict

        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    def test_class_a_warn_choke_point_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            cr._warn_once("crate_stale")

    def test_class_a_disable_choke_point_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            cr._disable("crate_unavailable")

    def test_class_c_exempt_reason_does_not_refuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Class C: the INPUT is outside the crate's envelope, so Python was
        # ALWAYS the intended handler — enumerated in the facade's
        # ``_STRICT_EXEMPT_REASONS`` frozenset. Must NOT raise.
        self._strict_on(monkeypatch)
        cr._warn_once("observed_error")
