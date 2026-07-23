"""Tests for the ``notify_rust`` routing facade (DEC-NOTIFY-RUST-CORE-001).

MIGRATION.md #1957. Yields 100% line + branch on
``brutex/notifications/notify_rust.py`` ALONE (the discovery-gate contract):
the flag matrix, the ``_TRUTHY``/``_FALSY`` mirror pin against
``sweep_lookahead``'s sets, the OFF-never-imports-crate bomb spy (poisoned
``sys.modules`` + a bombed ``import_module``), the crate-absent /
crate-stale latches, every degrade arm, the ``MemoryError``/``SystemExit``
re-raise pins, the burn-in mismatch autorevert + steady state, the
refuse-poison gates, the panic belt, the reset seam, the routed
``sweep_alerts``/``telegram`` definition-site integration, the wheel-free
per-site ENGAGEMENT spies (burn-in-budget + engaged-INFO — a silently
un-wired definition site fails loudly; hostile-review round 1), and —
gated on the REAL built wheel — the hypothesis differential per law, the
lone-surrogate degrade, the FFI shape pins and the flag-ON-vs-OFF
byte-identity A/B through the real ``SweepAlerter`` builders (whose ON arm
asserts genuine crate engagement, never byte-identity alone).
"""

from __future__ import annotations

import importlib
import json
import math
import struct
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex.notifications.notify_rust as nr
import pytest
from brutex.notifications.sweep_alerts import (
    SweepAlerter,
    _count_or_question,
    _count_or_question_python,
    _escape_markdown_v2_python,
    _short_id,
    _truncate_id_12_python,
    escape_markdown_v2,
)
from brutex.notifications.telegram import (
    TelegramBucket,
    TelegramEvent,
    TelegramEventKind,
    _dedupe_should_drop_python,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

# ── Helpers ─────────────────────────────────────────────────────────────


def _bits_to_float(bits: int) -> float:
    """Inverse of the facade's ``_f64_bits`` (the fake-crate side)."""
    unpacked: tuple[Any, ...] = struct.unpack("<d", struct.pack("<Q", bits))
    return float(unpacked[0])


def _default_escape(text: str) -> str:
    return _escape_markdown_v2_python(text)


def _default_count(value: int | None) -> str:
    return "?" if value is None else str(value)


def _default_dedupe(window_bits: int, last_seen_bits: int | None, now_bits: int) -> bool:
    if last_seen_bits is None:
        return False
    return (_bits_to_float(now_bits) - _bits_to_float(last_seen_bits)) < _bits_to_float(window_bits)


def _default_truncate(text: str) -> str:
    return text[:12]


def _fake_crate(
    *,
    escape: Callable[[str], object] | None = None,
    count: Callable[[int | None], object] | None = None,
    dedupe: Callable[[int, int | None, int], object] | None = None,
    truncate: Callable[[str], object] | None = None,
    drop_symbols: tuple[str, ...] = (),
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_notify`` module exposing the four-law API surface."""
    crate = SimpleNamespace(
        escape_markdown_v2=escape or _default_escape,
        count_or_question=count or _default_count,
        dedupe_should_drop=dedupe or _default_dedupe,
        truncate_id_12=truncate or _default_truncate,
        __version__=version,
    )
    for name in drop_symbols:
        delattr(crate, name)
    return crate


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``importlib.import_module`` at the fake crate.

    Every OTHER name (the panic belt's ``pyo3_runtime`` /
    ``polars.exceptions`` probes) delegates to the REAL importer.
    """
    real_import = importlib.import_module

    def _import(name: str) -> object:
        if name == "brutex_notify":
            return crate
        return real_import(name)

    monkeypatch.setattr(nr.importlib, "import_module", _import)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(nr.NOTIFY_RUST_ENV, "1")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh flag/latch/burn-in/memo state per test (the sibling convention)."""
    monkeypatch.delenv(nr.NOTIFY_RUST_ENV, raising=False)
    nr.reset_notify_rust_for_tests()
    yield
    nr.reset_notify_rust_for_tests()


# ── Flag matrix (Pattern A) ─────────────────────────────────────────────


class TestFlagMatrix:
    """``BRUTEX_NOTIFY_RUST`` resolution: default ON, junk loud-OFF.

    DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive 2026-07-23): unset /
    blank are ON; explicit falsy is the kill switch; the junk arm is
    unchanged (loud-OFF once).
    """

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE ", "On"])
    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, value)
        assert nr.notify_rust_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
    def test_falsy_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, value)
        with capture_logs() as logs:
            assert nr.notify_rust_enabled() is False
        assert logs == []

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_is_on_without_warn(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, value)
        with capture_logs() as logs:
            assert nr.notify_rust_enabled() is True
        assert logs == []

    def test_unset_is_on(self) -> None:
        # The DEC-RUST-DEFAULT-ON-WAVE-001 default pin: unset ⇒ ON.
        assert nr.notify_rust_enabled() is True

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert nr.notify_rust_enabled() is False
            assert nr.notify_rust_enabled() is False  # second read: silent
        warns = [rec for rec in logs if rec["event"] == "notify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_explicit_raw_bypasses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, "1")
        assert nr.notify_rust_enabled("0") is False
        assert nr.notify_rust_enabled("yes") is True


class TestTristateMirrorPin:
    """The inline ``_TRUTHY``/``_FALSY`` vocabulary mirrors the engine's."""

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (importing brutex.engine
        # pulls the fno_math-wheel-bearing chain into the deliberately
        # wheel-free notifications package, and engine/dashboard modules
        # import sweep_alerts at module level — an outright cycle) — so its
        # inline vocabulary is mirror-PINNED instead (the feed_rust shape;
        # the engine import happens ONLY inside this test body).
        #
        # importorskip (the test_backup_rust.py house pattern): the engine
        # __init__ chain now imports the REQUIRED `brutex_identity` wheel at
        # module scope (DEC-RUST-RUNID-STORES-001) — in a wheel-less parity
        # lane that raises ModuleNotFoundError, so this pin skips gracefully
        # there while staying live in main CI (where every wheel is built).
        sweep_lookahead = pytest.importorskip("brutex.engine.sweep_lookahead")

        assert nr._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert nr._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]


# ── OFF arm — byte-identity + never-imports ─────────────────────────────


class TestOffArm:
    """Explicit-falsy kill switch: the crate is NEVER imported; every law is the verbatim oracle.

    Retargeted from the pre-flip unset arm onto the explicit ``"0"`` kill
    switch (DEC-RUST-DEFAULT-ON-WAVE-001) — the assertions are unchanged.
    """

    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, "0")
        # Belt AND suspenders (the feed_rust None-poison house shape —
        # CodeQL-clean, no special-method raise): the bombed import_module
        # explodes loudly on the facade's REAL lazy-import path, and the
        # poisoned ``None`` sys.modules entry turns even a hypothetical
        # plain ``import brutex_notify`` rewrite into an ImportError → the
        # crate_unavailable latch — which the trailing no-latch / zero-log
        # assertions then catch (teeth verified: deleting the OFF-arm guard
        # in ``_routed`` fails this test loudly).
        def _bomb(name: str) -> object:
            raise AssertionError(f"flag-OFF path imported {name!r}")

        monkeypatch.setattr(nr.importlib, "import_module", _bomb)
        monkeypatch.setitem(sys.modules, "brutex_notify", None)

        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("a._x!", python_law=_escape_markdown_v2_python)
                == "a\\.\\_x\\!"
            )
            assert nr.routed_count_or_question(None, python_law=_count_or_question_python) == "?"
            assert nr.routed_count_or_question(7, python_law=_count_or_question_python) == "7"
            assert (
                nr.routed_dedupe_should_drop(
                    60.0, 100.0, 120.0, python_law=_dedupe_should_drop_python
                )
                is True
            )
            assert (
                nr.routed_truncate_id_12("0123456789abcdef", python_law=_truncate_id_12_python)
                == "0123456789ab"
            )
        assert nr.rust_notify_disabled() is False  # never latched — the crate was never touched
        assert logs == []  # zero fallback/disable/engaged events on the OFF arm

    def test_latched_process_serves_oracle_without_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        nr._disable("test_latch")  # pyright: ignore[reportPrivateUsage]
        assert nr.rust_notify_disabled() is True

        def _bomb(name: str) -> object:
            raise AssertionError(f"latched path imported {name!r}")

        monkeypatch.setattr(nr.importlib, "import_module", _bomb)
        assert nr.routed_escape_markdown_v2(".", python_law=_escape_markdown_v2_python) == "\\."

    def test_disable_repeat_is_noop(self) -> None:
        nr._disable("first")  # pyright: ignore[reportPrivateUsage]
        nr._disable("second")  # pyright: ignore[reportPrivateUsage]
        assert nr._DISABLED_REASON[0] == "first"  # pyright: ignore[reportPrivateUsage]


# ── Import guard / SYMBOL handshake ─────────────────────────────────────


class TestHandshake:
    """Crate-absent / crate-stale latches (fail-closed, one loud WARN)."""

    def test_absent_wheel_latches_crate_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError(f"No module named {name!r}")

        monkeypatch.setattr(nr.importlib, "import_module", _import)
        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("x.", python_law=_escape_markdown_v2_python) == "x\\."
            )
        assert nr._DISABLED_REASON[0] == "crate_unavailable"  # pyright: ignore[reportPrivateUsage]
        events = [rec["event"] for rec in logs]
        assert "notify_rust.rust_path_disabled" in events

    def test_stale_wheel_missing_symbol_latches_crate_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(drop_symbols=("truncate_id_12",)))
        assert nr.routed_count_or_question(3, python_law=_count_or_question_python) == "3"
        assert nr._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]

    def test_non_callable_symbol_latches_crate_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        crate.dedupe_should_drop = 42  # present but not callable
        _install_crate(monkeypatch, crate)
        assert nr.routed_truncate_id_12("abcdef", python_law=_truncate_id_12_python) == "abcdef"
        assert nr._DISABLED_REASON[0] == "crate_stale"  # pyright: ignore[reportPrivateUsage]


# ── Degrade / escape / panic arms ───────────────────────────────────────


class TestDegradeArms:
    """The narrow degrade set ships the oracle; the escape set re-raises raw."""

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("boom"),
            RuntimeError("boom"),
            TypeError("boom"),
            OverflowError("out of i64"),
            AttributeError("broken internals"),
            UnicodeEncodeError("utf-8", "\ud800", 0, 1, "lone surrogate"),
        ],
    )
    def test_degrade_error_ships_oracle_warn_once(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        _flag_on(monkeypatch)

        def _raise(text: str) -> str:
            raise exc

        _install_crate(monkeypatch, _fake_crate(escape=_raise))
        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("a.b", python_law=_escape_markdown_v2_python)
                == "a\\.b"
            )
            assert (
                nr.routed_escape_markdown_v2("c!d", python_law=_escape_markdown_v2_python)
                == "c\\!d"
            )
        warns = [rec for rec in logs if rec["event"] == "notify_rust.fallback"]
        assert len(warns) == 1  # warn-once per reason per process
        assert warns[0]["reason"] == "escape_error"
        assert nr.rust_notify_disabled() is False  # degrade, never a latch

    @pytest.mark.parametrize("exc_type", [MemoryError, SystemExit, KeyboardInterrupt])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _flag_on(monkeypatch)

        def _raise(text: str) -> str:
            raise exc_type()

        _install_crate(monkeypatch, _fake_crate(escape=_raise))
        with pytest.raises(exc_type):
            nr.routed_escape_markdown_v2("x", python_law=_escape_markdown_v2_python)

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        class FakePanic(Exception):
            """Stands in for a leaked pyo3 panic / unknown crate defect."""

        def _raise(text: str) -> str:
            raise FakePanic("kernel panicked")

        _install_crate(monkeypatch, _fake_crate(escape=_raise))
        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("p.q", python_law=_escape_markdown_v2_python)
                == "p\\.q"
            )
        assert nr._DISABLED_REASON[0] == "crate_panic"  # pyright: ignore[reportPrivateUsage]
        events = [rec["event"] for rec in logs]
        assert "notify_rust.rust_path_disabled" in events


class TestPanicBelt:
    """The by-name ``PanicException`` resolver (local — the leaf may not
    import ``lake_rust_render``'s copy; module docstring)."""

    def test_belt_always_contains_exception(self) -> None:
        belt = nr._panic_belt_errors()  # pyright: ignore[reportPrivateUsage]
        assert Exception in belt

    def test_both_homes_unresolvable_yields_exception_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # None in sys.modules makes import_module raise ImportError for the
        # entry — both probe homes fail, the belt is (Exception,).
        monkeypatch.setitem(sys.modules, "pyo3_runtime", None)
        monkeypatch.setitem(sys.modules, "polars.exceptions", None)
        assert nr._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_home_without_panic_class_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "pyo3_runtime", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "polars.exceptions", SimpleNamespace(PanicException=7))
        assert nr._panic_belt_errors() == (Exception,)  # pyright: ignore[reportPrivateUsage]

    def test_resolved_panic_class_joins_belt_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(BaseException):
            """A stand-in pyo3 PanicException type."""

        monkeypatch.setitem(sys.modules, "pyo3_runtime", SimpleNamespace(PanicException=FakePanic))
        monkeypatch.setitem(
            sys.modules, "polars.exceptions", SimpleNamespace(PanicException=FakePanic)
        )
        # The SAME class from both homes joins ONCE (the dedupe arm).
        assert nr._panic_belt_errors() == (  # pyright: ignore[reportPrivateUsage]
            Exception,
            FakePanic,
        )


# ── Refuse-poison gates ─────────────────────────────────────────────────


class TestRefusePoison:
    """A wrong-typed crate return is refused to the oracle, never served."""

    def test_non_str_escape_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(escape=lambda text: b"bytes"))
        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("z.", python_law=_escape_markdown_v2_python) == "z\\."
            )
        warns = [rec for rec in logs if rec["event"] == "notify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "escape_result_refused"
        assert nr.rust_notify_disabled() is False

    def test_non_bool_dedupe_result_refused_exact_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bool IS an int subclass in Python — the gate is EXACT-type, so an
        # int 1 from a poisoned crate is refused, never punned to True.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(dedupe=lambda w, ls, n: 1))
        with capture_logs() as logs:
            assert (
                nr.routed_dedupe_should_drop(
                    60.0, 100.0, 200.0, python_law=_dedupe_should_drop_python
                )
                is False
            )
        warns = [rec for rec in logs if rec["event"] == "notify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "dedupe_result_refused"

    def test_none_count_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(count=lambda value: None))
        assert nr.routed_count_or_question(None, python_law=_count_or_question_python) == "?"


# ── Burn-in + autorevert ────────────────────────────────────────────────


class TestBurnIn:
    """The first 16 engaged calls per law bit-compare vs the live oracle."""

    def test_mismatch_ships_oracle_and_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(escape=lambda text: "WRONG"))
        with capture_logs() as logs:
            assert (
                nr.routed_escape_markdown_v2("a.b", python_law=_escape_markdown_v2_python)
                == "a\\.b"  # the ORACLE value ships, never the wrong Rust value
            )
        errors = [rec for rec in logs if rec["event"] == "notify_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch_escape"
        assert nr.rust_notify_disabled() is True
        # The latch holds: later calls are pure oracle (bomb the importer).
        monkeypatch.setattr(
            nr.importlib,
            "import_module",
            lambda name: (_ for _ in ()).throw(AssertionError("latched path imported")),
        )
        assert nr.routed_escape_markdown_v2("c!", python_law=_escape_markdown_v2_python) == "c\\!"

    def test_clean_burn_in_completes_then_ships_rust_uncompared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        oracle_calls = 0

        def _counting_oracle(text: str) -> str:
            nonlocal oracle_calls
            oracle_calls += 1
            return _escape_markdown_v2_python(text)

        with capture_logs() as logs:
            for _ in range(nr.BURN_IN_SAMPLES):
                assert nr.routed_escape_markdown_v2("m.", python_law=_counting_oracle) == "m\\."
        assert oracle_calls == nr.BURN_IN_SAMPLES
        done = [rec for rec in logs if rec["event"] == "notify_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "escape"
        engaged = [rec for rec in logs if rec["event"] == "notify_rust.engaged"]
        assert len(engaged) == 1  # once per process
        # Post-burn-in: the Rust value ships with NO oracle in the path.
        assert nr.routed_escape_markdown_v2("n!", python_law=_counting_oracle) == "n\\!"
        assert oracle_calls == nr.BURN_IN_SAMPLES
        assert nr.rust_notify_disabled() is False

    def test_steady_state_reset_skips_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        nr.reset_notify_rust_for_tests(remaining=0)

        def _bomb_oracle(text: str) -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert nr.routed_escape_markdown_v2("s.", python_law=_bomb_oracle) == "s\\."

    def test_burn_in_budgets_are_per_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A poisoned COUNT law autoreverts the process; the latch protects
        # every law (process-wide OFF — the conservative posture).
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(count=lambda value: "999"))
        assert nr.routed_count_or_question(1, python_law=_count_or_question_python) == "1"
        assert nr._DISABLED_REASON[0] == "burn_in_mismatch_count"  # pyright: ignore[reportPrivateUsage]
        assert nr.routed_escape_markdown_v2("q.", python_law=_escape_markdown_v2_python) == "q\\."


# ── ON arm — per-law parity via the compatible fake ─────────────────────


class TestOnArmParityFake:
    """Flag-ON routing returns law-exact bytes for every law (fake crate)."""

    def test_all_four_laws_match_oracle_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        body = "🛑 Sweep run-1 HALTED: disk_full. Fix + relaunch!"
        assert nr.routed_escape_markdown_v2(
            body, python_law=_escape_markdown_v2_python
        ) == _escape_markdown_v2_python(body)
        for value in (None, 0, -1, 2**62):
            assert nr.routed_count_or_question(
                value, python_law=_count_or_question_python
            ) == _count_or_question_python(value)
        for window, last_seen, now in (
            (60.0, None, 100.0),
            (60.0, 100.0, 159.9),
            (60.0, 100.0, 160.0),
            (300.0, 0.0, float("nan")),
        ):
            assert nr.routed_dedupe_should_drop(
                window, last_seen, now, python_law=_dedupe_should_drop_python
            ) is _dedupe_should_drop_python(window, last_seen, now)
        for text in ("", "short", "0123456789abcdef", "🛑" * 20):
            assert nr.routed_truncate_id_12(
                text, python_law=_truncate_id_12_python
            ) == _truncate_id_12_python(text)

    def test_f64_bits_round_trip(self) -> None:
        for value in (0.0, -0.0, 1.5, 60.0, float("inf"), float("-inf"), 1e-300):
            bits = nr._f64_bits(value)  # pyright: ignore[reportPrivateUsage]
            assert 0 <= bits < 2**64
            assert _bits_to_float(bits) == value or math.isnan(value)
        nan_bits = nr._f64_bits(float("nan"))  # pyright: ignore[reportPrivateUsage]
        recovered = _bits_to_float(nan_bits)
        assert math.isnan(recovered)  # NaN round-trips as NaN


class TestResetSeam:
    """``reset_notify_rust_for_tests`` restores latch + budgets + memo."""

    def test_reset_clears_latch_and_budgets(self) -> None:
        nr._disable("some_latch")  # pyright: ignore[reportPrivateUsage]
        nr._BURN_IN_REMAINING["escape"] = 0  # pyright: ignore[reportPrivateUsage]
        nr.reset_notify_rust_for_tests()
        assert nr.rust_notify_disabled() is False
        assert all(
            remaining == nr.BURN_IN_SAMPLES
            for remaining in nr._BURN_IN_REMAINING.values()  # pyright: ignore[reportPrivateUsage]
        )


# ── Definition-site integration (flag OFF byte-identity + fake ON) ──────


class TestDefinitionSites:
    """The routed public seams serve the verbatim oracle bytes."""

    def test_escape_markdown_v2_off_is_verbatim_oracle(self) -> None:
        body = "⚠️ pull degraded (rule=pull.stalled): achieved=0.40 < floor!"
        assert escape_markdown_v2(body) == _escape_markdown_v2_python(body)

    def test_count_and_short_id_off_are_verbatim(self) -> None:
        assert _count_or_question(None) == "?"
        assert _count_or_question(42) == "42"
        assert _short_id("5b020e892247deac0123") == "5b020e892247"

    def test_check_dedupe_locked_routes_predicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bucket = TelegramBucket()
        kind = str(TelegramEventKind.RATE_LIMIT_DOWNSHIFT)  # 60.0 s window
        # No stamp → never dropped (predicate's None arm).
        assert bucket.check_dedupe_locked(kind, "key", 100.0) is False
        bucket.record_dedupe_locked(kind, "key", 100.0)
        assert bucket.check_dedupe_locked(kind, "key", 159.9) is True
        assert bucket.check_dedupe_locked(kind, "key", 160.0) is False
        # Zero-window kinds short-circuit BEFORE the routed predicate.
        assert bucket.check_dedupe_locked(str(TelegramEventKind.TEST), "key", 0.0) is False
        # Flag ON through the fake crate: byte-identical decisions.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        assert bucket.check_dedupe_locked(kind, "key", 159.9) is True
        assert bucket.check_dedupe_locked(kind, "key", 160.0) is False


class TestDefinitionSiteEngagement:
    """ENGAGEMENT TEETH — the FOUR definition sites genuinely ROUTE.

    Hostile-review round 1 (MIGRATION.md #1957): the routing is
    byte-identical-by-design, so byte-equality alone cannot catch a
    definition site silently un-wired back onto its Python oracle (the
    executed round-1 mutation reverted all four sites and the whole battery
    stayed green). These pins observe the facade's SIDE CHANNEL instead
    (the ``feed_rust`` engagement-spy precedent): with the flag ON + a
    correct fake crate, a routed call decrements ITS law's burn-in budget
    and fires the once-per-process ``notify_rust.engaged`` INFO — an
    un-wired site leaves both untouched and fails here. Wheel-free (the
    fake crate), so the teeth bite in the isolated ci.yml arm too.
    """

    @staticmethod
    def _budgets() -> dict[str, int]:
        return dict(nr._BURN_IN_REMAINING)  # pyright: ignore[reportPrivateUsage]

    def test_escape_markdown_v2_site_engages_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert escape_markdown_v2("a._x!") == "a\\.\\_x\\!"
        assert [rec["event"] for rec in logs] == ["notify_rust.engaged"]
        # Per-LAW attribution: ONLY the escape budget was consumed.
        assert self._budgets() == {
            "escape": nr.BURN_IN_SAMPLES - 1,
            "count": nr.BURN_IN_SAMPLES,
            "dedupe": nr.BURN_IN_SAMPLES,
            "truncate": nr.BURN_IN_SAMPLES,
        }

    def test_count_or_question_site_engages_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert _count_or_question(None) == "?"
            assert _count_or_question(7) == "7"
        assert [rec["event"] for rec in logs] == ["notify_rust.engaged"]
        assert self._budgets() == {
            "escape": nr.BURN_IN_SAMPLES,
            "count": nr.BURN_IN_SAMPLES - 2,
            "dedupe": nr.BURN_IN_SAMPLES,
            "truncate": nr.BURN_IN_SAMPLES,
        }

    def test_short_id_site_engages_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert _short_id("0123456789abcdef") == "0123456789ab"
        assert [rec["event"] for rec in logs] == ["notify_rust.engaged"]
        assert self._budgets() == {
            "escape": nr.BURN_IN_SAMPLES,
            "count": nr.BURN_IN_SAMPLES,
            "dedupe": nr.BURN_IN_SAMPLES,
            "truncate": nr.BURN_IN_SAMPLES - 1,
        }

    def test_check_dedupe_locked_site_engages_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        bucket = TelegramBucket()
        kind = str(TelegramEventKind.RATE_LIMIT_DOWNSHIFT)  # 60.0 s window
        with capture_logs() as logs:
            # The no-stamp (last_seen=None) arm ROUTES — the predicate owns it.
            assert bucket.check_dedupe_locked(kind, "key", 100.0) is False
            bucket.record_dedupe_locked(kind, "key", 100.0)
            assert bucket.check_dedupe_locked(kind, "key", 159.9) is True
        assert [rec["event"] for rec in logs] == ["notify_rust.engaged"]
        assert self._budgets()["dedupe"] == nr.BURN_IN_SAMPLES - 2
        # The zero-window short-circuit stays BEFORE the facade: no route,
        # no budget consumption.
        assert bucket.check_dedupe_locked(str(TelegramEventKind.TEST), "key", 0.0) is False
        assert self._budgets() == {
            "escape": nr.BURN_IN_SAMPLES,
            "count": nr.BURN_IN_SAMPLES,
            "dedupe": nr.BURN_IN_SAMPLES - 2,
            "truncate": nr.BURN_IN_SAMPLES,
        }


# ── Real-wheel gates (skipped loudly when the wheel is absent) ──────────


def _real_wheel() -> object:
    return pytest.importorskip(
        "brutex_notify", reason="brutex_notify wheel not installed — build via maturin"
    )


class TestRealWheelFfi:
    """FFI shape pins over the REAL built wheel."""

    def test_law_surface_and_stamps(self) -> None:
        crate: Any = _real_wheel()
        for name in (
            "escape_markdown_v2",
            "count_or_question",
            "dedupe_should_drop",
            "truncate_id_12",
        ):
            assert callable(getattr(crate, name))
        digest = crate.SOURCE_DIGEST
        assert isinstance(digest, str)
        assert len(digest) == 64
        assert crate.__source_digest__ == digest

    def test_expected_build_json_mirrors_shim_symbols(self) -> None:
        crate: Any = _real_wheel()
        expected_path = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "brutex-notify"
            / "python"
            / "brutex_notify"
            / "EXPECTED_BUILD.json"
        )
        fingerprint = json.loads(expected_path.read_text(encoding="utf-8"))
        assert sorted(fingerprint["symbols"]) == sorted(crate._EXPECTED_COMPILED_SYMBOLS)
        assert fingerprint["crate_version"] == crate.__build_crate_version__

    def test_escape_exact_bytes(self) -> None:
        crate: Any = _real_wheel()
        assert crate.escape_markdown_v2("a._x! (b) #1 {c} 5.0 |d| ~e~ `f` >g< +h- =i=") == (
            "a\\.\\_x\\! \\(b\\) \\#1 \\{c\\} 5\\.0 \\|d\\| \\~e\\~ \\`f\\` \\>g< \\+h\\- \\=i\\="
        )
        assert crate.escape_markdown_v2("🛑⚠️ plain") == "🛑⚠️ plain"
        assert crate.escape_markdown_v2("\\") == "\\"  # backslash NOT escaped

    def test_count_dedupe_truncate_exact(self) -> None:
        crate: Any = _real_wheel()
        assert crate.count_or_question(None) == "?"
        assert crate.count_or_question(-(2**63)) == str(-(2**63))
        with pytest.raises(OverflowError):
            crate.count_or_question(2**63)  # out of i64 — the facade degrades
        bits = nr._f64_bits  # pyright: ignore[reportPrivateUsage]
        assert crate.dedupe_should_drop(bits(60.0), bits(100.0), bits(159.9)) is True
        assert crate.dedupe_should_drop(bits(60.0), bits(100.0), bits(160.0)) is False
        assert crate.dedupe_should_drop(bits(60.0), None, bits(0.0)) is False
        assert crate.truncate_id_12("🛑" * 13) == "🛑" * 12


class TestRealWheelDifferential:
    """Hypothesis differential per law — the REAL wheel vs the Python oracle."""

    @given(
        text=st.text(
            alphabet=st.characters(
                min_codepoint=0, max_codepoint=0x10FFFF, exclude_categories=("Cs",)
            ),
            max_size=256,
        )
    )
    @settings(max_examples=200, deadline=None)
    def test_escape_differential_hostile_unicode(self, text: str) -> None:
        # Full Unicode plane MINUS lone surrogates (they cannot cross the
        # pyo3 UTF-8 edge — the surrogate DEGRADE arm is pinned separately).
        crate: Any = _real_wheel()
        assert crate.escape_markdown_v2(text) == _escape_markdown_v2_python(text)

    @given(
        text=st.text(
            alphabet=st.characters(
                min_codepoint=0, max_codepoint=0x10FFFF, exclude_categories=("Cs",)
            ),
            max_size=64,
        )
    )
    @settings(max_examples=200, deadline=None)
    def test_truncate_differential(self, text: str) -> None:
        crate: Any = _real_wheel()
        assert crate.truncate_id_12(text) == text[:12]

    @given(value=st.one_of(st.none(), st.integers(min_value=-(2**63), max_value=2**63 - 1)))
    @settings(max_examples=200, deadline=None)
    def test_count_differential(self, value: int | None) -> None:
        crate: Any = _real_wheel()
        assert crate.count_or_question(value) == _count_or_question_python(value)

    @given(
        window=st.floats(allow_nan=True, allow_infinity=True),
        last_seen=st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
        now=st.floats(allow_nan=True, allow_infinity=True),
    )
    @settings(max_examples=300, deadline=None)
    def test_dedupe_differential_full_float_plane(
        self, window: float, last_seen: float | None, now: float
    ) -> None:
        crate: Any = _real_wheel()
        bits = nr._f64_bits  # pyright: ignore[reportPrivateUsage]
        assert crate.dedupe_should_drop(
            bits(window), None if last_seen is None else bits(last_seen), bits(now)
        ) is _dedupe_should_drop_python(window, last_seen, now)

    def test_routed_escape_steady_state_ships_rust_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The routed seam in the post-burn-in steady state: the REAL Rust
        # value ships (no oracle in the path) and equals the oracle
        # byte-for-byte over a deterministic hostile corpus.
        _real_wheel()
        _flag_on(monkeypatch)
        nr.reset_notify_rust_for_tests(remaining=0)
        corpus = [
            "",
            "\\",
            "_*[]()~`>#+-=|{}.!",
            "🛑⚠️✅🌙🔁🧹⏳🔔🔑🔐⛔❌📦⏭💀",
            "a\nb\tc\x00d\x1be",
            "x." * 5000,
            "𝕊𝕡𝕒𝕟 𝔞𝔰𝔱𝔯𝔞𝔩 (maths) — em–dash…",
            "NSE-BAJAJ-AUTO-30Sep25-FUT @ ₹8,010.00 (+0.25%)!",
        ]
        for text in corpus:
            assert nr.routed_escape_markdown_v2(
                text, python_law=_escape_markdown_v2_python
            ) == _escape_markdown_v2_python(text)

    def test_lone_surrogate_degrades_to_oracle_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lone surrogate (reachable via vendor-derived reason/detail text)
        # fails UTF-8 conversion at the pyo3 edge — the facade must ship the
        # oracle bytes, never crash (the notifier's never-raises contract).
        _real_wheel()
        _flag_on(monkeypatch)
        nr.reset_notify_rust_for_tests(remaining=0)
        hostile = "bad \ud800 vendor text."
        with capture_logs() as logs:
            assert nr.routed_escape_markdown_v2(
                hostile, python_law=_escape_markdown_v2_python
            ) == _escape_markdown_v2_python(hostile)
        warns = [rec for rec in logs if rec["event"] == "notify_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "escape_error"
        assert nr.rust_notify_disabled() is False


class _CapturingNotifier:
    """A NotifierLike fake capturing every published event."""

    def __init__(self) -> None:
        self.events: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.events.append(event)


def _drive_builders(alerter: SweepAlerter) -> list[tuple[str, str, str, str]]:
    """Drive a representative real-builder battery; return the wire tuples."""
    assert alerter.test_digest() is True
    assert alerter.pull_halted(reason="disk_full") is True
    assert alerter.auth_renew_failed(reason="totp (2 attempts)") is True
    assert (
        alerter.sweep_milestone(
            run_id="5b020e892247deac0123456789abcdef", pct=50, tuples_done=7, total=14
        )
        is True
    )
    assert (
        alerter.run_suspect(
            run_id="5b020e892247deac0123456789abcdef",
            tripwire="count_reconcile",
            detail="applied=3 != done=4 (Δ-1)",
        )
        is True
    )
    assert alerter.sweep_halted(run_id="run-id-🛑-astral-beyond-twelve", reason="disk_full") is True
    assert alerter.loop_stalled(stalled_s=7.5, threshold_s=5.0) is True
    assert alerter.vendor_down(failed_windows=12, distinct_symbols=5) is True
    notifier = alerter._explicit  # pyright: ignore[reportPrivateUsage]
    assert isinstance(notifier, _CapturingNotifier)
    return [
        (str(event.kind), str(event.severity), event.message, event.dedupe_key)
        for event in notifier.events
    ]


class TestRealWheelByteIdentityAB:
    """Flag ON vs OFF through the REAL SweepAlerter builders — bytes equal."""

    def test_builder_battery_bytes_identical_on_vs_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _real_wheel()
        # OFF arm (the default): the verbatim oracle bytes.
        monkeypatch.delenv(nr.NOTIFY_RUST_ENV, raising=False)
        nr.reset_notify_rust_for_tests()
        off_tuples = _drive_builders(SweepAlerter(notifier=_CapturingNotifier()))
        # ON arm with the REAL wheel, burn-in ACTIVE (each engaged call is
        # bit-compared against the live oracle — a mismatch would autorevert
        # AND fail the latch assertion below).
        monkeypatch.setenv(nr.NOTIFY_RUST_ENV, "1")
        nr.reset_notify_rust_for_tests()
        with capture_logs() as logs:
            on_tuples = _drive_builders(SweepAlerter(notifier=_CapturingNotifier()))
        assert on_tuples == off_tuples
        assert nr.rust_notify_disabled() is False  # no burn-in mismatch fired
        # ENGAGEMENT TEETH (hostile-review round 1): byte-identity alone is
        # vacuously satisfiable by an un-wired definition site, so the ON-arm
        # drive must also prove the REAL crate was genuinely engaged — the
        # engaged INFO fired and the battery's two driven laws (every builder
        # escapes; milestone/suspect/halted call `_short_id`) consumed
        # burn-in budget.
        assert "notify_rust.engaged" in [rec["event"] for rec in logs]
        budgets = dict(nr._BURN_IN_REMAINING)  # pyright: ignore[reportPrivateUsage]
        assert budgets["escape"] < nr.BURN_IN_SAMPLES
        assert budgets["truncate"] < nr.BURN_IN_SAMPLES
        # ON arm again in the post-burn-in steady state (Rust bytes ship
        # uncompared) — still byte-identical.
        nr.reset_notify_rust_for_tests(remaining=0)
        steady_tuples = _drive_builders(SweepAlerter(notifier=_CapturingNotifier()))
        assert steady_tuples == off_tuples
