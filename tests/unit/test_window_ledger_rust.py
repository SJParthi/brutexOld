"""Tests for the ``window_ledger_rust`` routing facade (Wave C-3, slice G8).

DEC-LAKE-RUST-WINDOW-LEDGER-001 (MIGRATION.md #1915). Yields 100% line +
branch on ``brutex/data/window_ledger_rust.py`` ALONE (the discovery-gate
contract): the flag matrix, the OFF-never-imports-crate spy, the
crate-absent / crate-stale latches, every degrade arm, the escape-set
re-raise, the burn-in mismatch autorevert + steady state, the refuse-poison
gates, the exact-type ``date`` gate, the tuple-identity pin, the reset seam,
the §11 ``env_flag_tristate`` identity pin, the ON-arm integration through
the REAL ``window_status`` / ``window_ledger`` laws, and the hypothesis
differential against the REAL built wheel (skipped when absent).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import brutex.data.window_ledger_rust as wlr
import pytest
from brutex.data import lake_rust_render
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_status import (
    WindowStatusLog,
    _classify_window_status_python,
    classify_window_status,
    window_key_python,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

KEY = WindowKey(
    market="groww",
    security_id="NSE-NIFTY",
    interval="1minute",
    start=date(2024, 1, 2),
    end=date(2024, 1, 3),
)

EXPECTED_KEY = ("groww", "NSE-NIFTY", "1minute", "2024-01-02", "2024-01-03")


def _fake_crate(
    *,
    classify: Any = None,
    isoformat: Any = None,
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_lake`` module exposing the G8 API surface."""

    def _default_classify(raw_bars: int, error_type: str | None, degraded: bool) -> str:
        return _classify_window_status_python(raw_bars, error_type, degraded=degraded)

    def _default_isoformat(year: int, month: int, day: int) -> str:
        return f"{year:04}-{month:02}-{day:02}"

    if classify is None:
        classify = _default_classify
    if isoformat is None:
        isoformat = _default_isoformat

    return SimpleNamespace(
        classify_window_status=classify,
        date_isoformat=isoformat,
        __version__=version,
    )


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``importlib.import_module`` at the fake crate."""

    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(wlr.importlib, "import_module", _import)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "1")


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

    Uses the SAME shared ``monkeypatch`` object as the sibling autouse fixtures
    (never its own ``MonkeyPatch.context()``): one object means one LIFO undo
    stack, so a ``TestStrictRefusal`` ``delenv`` on that same object cannot be
    restored out of order and leak the value past this module.
    """
    from brutex.config import rust_strict

    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh latch/burn-in/memo state + flag unset around every test."""
    monkeypatch.delenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, raising=False)
    wlr.reset_window_ledger_rust_for_tests()
    yield
    wlr.reset_window_ledger_rust_for_tests()


class TestFlagResolution:
    def test_default_unset_is_on_without_warn(self) -> None:
        # Default ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
        # 2026-07-23) — unset resolves True silently.
        with capture_logs() as logs:
            assert wlr.window_ledger_rust_enabled() is True
        assert logs == []

    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "1")
        assert wlr.window_ledger_rust_enabled() is True

    def test_falsy_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "0")
        with capture_logs() as logs:
            assert wlr.window_ledger_rust_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "banana")
        with capture_logs() as logs:
            assert wlr.window_ledger_rust_enabled() is False
            assert wlr.window_ledger_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_blank_whitespace_is_on_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Whitespace-blank ≡ unset ⇒ the default ON, silently
        # (DEC-RUST-DEFAULT-ON-WAVE-001).
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "   ")
        with capture_logs() as logs:
            assert wlr.window_ledger_rust_enabled() is True
        assert logs == []

    def test_env_flag_tristate_is_the_sibling_object(self) -> None:
        # §11 COMMON identity pin — ONE flag vocabulary, never a copy.
        assert wlr.env_flag_tristate is lake_rust_render.env_flag_tristate


class TestOffArmInertness:
    def test_off_never_imports_crate_and_matches_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-RUST-DEFAULT-ON-WAVE-001: unset now means ON — the OFF-arm
        # inertness property is addressed via the explicit kill switch ("0").
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "0")

        def _bomb(name: str) -> object:
            raise AssertionError(f"flag-OFF path imported {name!r}")

        monkeypatch.setattr(wlr.importlib, "import_module", _bomb)
        assert (
            wlr.routed_classify_window_status(
                5, None, degraded=False, python_law=_classify_window_status_python
            )
            == "SUCCESS"
        )
        assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY

    def test_latched_process_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        wlr._disable("test_latch")
        assert wlr.rust_window_ledger_disabled() is True

        def _bomb(name: str) -> object:
            raise AssertionError("latched path imported the crate")

        monkeypatch.setattr(wlr.importlib, "import_module", _bomb)
        assert (
            wlr.routed_classify_window_status(
                0, "X", degraded=False, python_law=_classify_window_status_python
            )
            == "FAILED"
        )
        assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY


class TestCrateGuards:
    def test_crate_absent_latches_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(wlr.importlib, "import_module", _import)
        with capture_logs() as logs:
            result = wlr.routed_classify_window_status(
                3, None, degraded=False, python_law=_classify_window_status_python
            )
        assert result == "SUCCESS"
        assert wlr.rust_window_ledger_disabled() is True
        events = [e for e in logs if e["event"] == "window_ledger_rust.rust_path_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "crate_unavailable"

    def test_crate_stale_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        del crate.date_isoformat  # a pre-G8 wheel shape
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            result = wlr.routed_window_merge_key(KEY, python_law=window_key_python)
        assert result == EXPECTED_KEY
        assert wlr.rust_window_ledger_disabled() is True
        events = [e for e in logs if e["event"] == "window_ledger_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_stale"

    def test_crate_stale_noncallable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        crate = _fake_crate()
        crate.classify_window_status = "not callable"
        _install_crate(monkeypatch, crate)
        result = wlr.routed_classify_window_status(
            1, None, degraded=True, python_law=_classify_window_status_python
        )
        assert result == "FAILED"
        assert wlr.rust_window_ledger_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        wlr._disable("first")
        wlr._disable("second")
        assert wlr._DISABLED_REASON[0] == "first"


class TestClassifyRouting:
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _boom(raw_bars: int, error_type: str | None, degraded: bool) -> str:
            raise ValueError("ffi refusal")

        _install_crate(monkeypatch, _fake_crate(classify=_boom))
        with capture_logs() as logs:
            for _ in range(2):
                assert (
                    wlr.routed_classify_window_status(
                        7, None, degraded=False, python_law=_classify_window_status_python
                    )
                    == "SUCCESS"
                )
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "classify_error"
        assert wlr.rust_window_ledger_disabled() is False  # degrade, not latch

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _oom(raw_bars: int, error_type: str | None, degraded: bool) -> str:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(classify=_oom))
        with pytest.raises(MemoryError):
            wlr.routed_classify_window_status(
                1, None, degraded=False, python_law=_classify_window_status_python
            )

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        class FakePanic(Exception):
            pass

        def _panic(raw_bars: int, error_type: str | None, degraded: bool) -> str:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, _fake_crate(classify=_panic))
        monkeypatch.setattr(wlr, "_panic_belt_errors", lambda: (FakePanic,))
        result = wlr.routed_classify_window_status(
            2, None, degraded=False, python_law=_classify_window_status_python
        )
        assert result == "SUCCESS"
        assert wlr.rust_window_ledger_disabled() is True
        assert wlr._DISABLED_REASON[0] == "crate_panic"

    def test_non_str_result_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _poison(raw_bars: int, error_type: str | None, degraded: bool) -> object:
            return 42

        _install_crate(monkeypatch, _fake_crate(classify=_poison))
        with capture_logs() as logs:
            result = wlr.routed_classify_window_status(
                0, None, degraded=False, python_law=_classify_window_status_python
            )
        assert result == "EMPTY"
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert warns[0]["reason"] == "classify_result_refused"

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _wrong(raw_bars: int, error_type: str | None, degraded: bool) -> str:
            return "WRONG"

        _install_crate(monkeypatch, _fake_crate(classify=_wrong))
        with capture_logs() as logs:
            result = wlr.routed_classify_window_status(
                9, None, degraded=False, python_law=_classify_window_status_python
            )
        assert result == "SUCCESS"  # the ORACLE value ships
        assert wlr.rust_window_ledger_disabled() is True
        errors = [e for e in logs if e["event"] == "window_ledger_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch_classify"

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        wlr.reset_window_ledger_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            assert (
                wlr.routed_classify_window_status(
                    4, None, degraded=False, python_law=_classify_window_status_python
                )
                == "SUCCESS"
            )
        done = [e for e in logs if e["event"] == "window_ledger_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "classify"
        # Steady state: the oracle must never be consulted again.

        def _oracle_bomb(raw_bars: int, error_type: str | None, *, degraded: bool = False) -> str:
            raise AssertionError("steady state consulted the oracle")

        assert (
            wlr.routed_classify_window_status(4, None, degraded=False, python_law=_oracle_bomb)
            == "SUCCESS"
        )

    def test_engaged_info_logs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            wlr.routed_classify_window_status(
                1, None, degraded=False, python_law=_classify_window_status_python
            )
            wlr.routed_window_merge_key(KEY, python_law=window_key_python)
        engaged = [e for e in logs if e["event"] == "window_ledger_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"


class TestMergeKeyRouting:
    def test_exact_type_date_gate_refuses_datetime_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _bomb(name: str) -> object:
            raise AssertionError("nondate key reached the crate import")

        monkeypatch.setattr(wlr.importlib, "import_module", _bomb)
        key = WindowKey(
            market="groww",
            security_id="NSE-NIFTY",
            interval="1minute",
            start=datetime(2024, 1, 2, 9, 15, tzinfo=UTC),  # type: ignore[arg-type]
            end=date(2024, 1, 3),
        )
        with capture_logs() as logs:
            result = wlr.routed_window_merge_key(key, python_law=window_key_python)
        # The oracle serves the subclass isoformat honestly.
        assert result[3] == "2024-01-02T09:15:00+00:00"
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert warns[0]["reason"] == "merge_key_nondate_refused"

    def test_exact_type_date_gate_refuses_datetime_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        key = WindowKey(
            market="groww",
            security_id="NSE-NIFTY",
            interval="1minute",
            start=date(2024, 1, 2),
            end=datetime(2024, 1, 3, 15, 30),  # type: ignore[arg-type]
        )
        result = wlr.routed_window_merge_key(key, python_law=window_key_python)
        assert result[4] == "2024-01-03T15:30:00"
        assert wlr.rust_window_ledger_disabled() is False

    def test_crate_absent_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(wlr.importlib, "import_module", _import)
        assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY
        assert wlr.rust_window_ledger_disabled() is True

    def test_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _boom(year: int, month: int, day: int) -> str:
            raise ValueError("bad date")

        _install_crate(monkeypatch, _fake_crate(isoformat=_boom))
        with capture_logs() as logs:
            result = wlr.routed_window_merge_key(KEY, python_law=window_key_python)
        assert result == EXPECTED_KEY
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert warns[0]["reason"] == "merge_key_error"

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _oom(year: int, month: int, day: int) -> str:
            raise MemoryError

        _install_crate(monkeypatch, _fake_crate(isoformat=_oom))
        with pytest.raises(MemoryError):
            wlr.routed_window_merge_key(KEY, python_law=window_key_python)

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        class FakePanic(Exception):
            pass

        def _panic(year: int, month: int, day: int) -> str:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, _fake_crate(isoformat=_panic))
        monkeypatch.setattr(wlr, "_panic_belt_errors", lambda: (FakePanic,))
        assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY
        assert wlr._DISABLED_REASON[0] == "crate_panic"

    def test_non_str_start_render_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _poison(year: int, month: int, day: int) -> object:
            return 20240102

        _install_crate(monkeypatch, _fake_crate(isoformat=_poison))
        with capture_logs() as logs:
            assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert warns[0]["reason"] == "merge_key_result_refused"

    def test_non_str_end_render_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        calls: list[tuple[int, int, int]] = []

        def _mixed(year: int, month: int, day: int) -> object:
            calls.append((year, month, day))
            if len(calls) == 1:
                return f"{year:04}-{month:02}-{day:02}"
            return None

        _install_crate(monkeypatch, _fake_crate(isoformat=_mixed))
        assert wlr.routed_window_merge_key(KEY, python_law=window_key_python) == EXPECTED_KEY

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)

        def _wrong(year: int, month: int, day: int) -> str:
            return "9999-99-99"

        _install_crate(monkeypatch, _fake_crate(isoformat=_wrong))
        with capture_logs() as logs:
            result = wlr.routed_window_merge_key(KEY, python_law=window_key_python)
        assert result == EXPECTED_KEY  # the ORACLE key ships
        errors = [e for e in logs if e["event"] == "window_ledger_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_merge_key"
        assert wlr.rust_window_ledger_disabled() is True

    def test_burn_in_completes_then_steady_state_with_identity_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        wlr.reset_window_ledger_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            first = wlr.routed_window_merge_key(KEY, python_law=window_key_python)
        assert first == EXPECTED_KEY
        done = [e for e in logs if e["event"] == "window_ledger_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "merge_key"

        def _oracle_bomb(key: WindowKey) -> tuple[str, str, str, str, str]:
            raise AssertionError("steady state consulted the oracle")

        steady = wlr.routed_window_merge_key(KEY, python_law=_oracle_bomb)
        assert steady == EXPECTED_KEY
        # Identity pins: the three identity strings are the ORIGINAL Python
        # objects — they never crossed the FFI.
        assert steady[0] is KEY.market
        assert steady[1] is KEY.security_id
        assert steady[2] is KEY.interval


class TestOnArmIntegration:
    """The REAL public laws routed through a fake crate, flag ON."""

    def test_classify_window_status_public_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        assert classify_window_status(5, None) == "SUCCESS"
        assert classify_window_status(5, None, degraded=True) == "FAILED"
        assert classify_window_status(0, None) == "EMPTY"
        assert classify_window_status(0, "GrowwTimeoutError") == "FAILED"

    def test_both_key_staticmethods_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        assert WindowStatusLog._key(KEY) == EXPECTED_KEY
        assert WindowLedger._key(KEY) == EXPECTED_KEY

    def test_flag_off_public_laws_never_import_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # DEC-RUST-DEFAULT-ON-WAVE-001: the kill switch ("0") keeps the
        # public laws crate-free — the SAME never-imports teeth survive.
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "0")

        def _bomb(name: str) -> object:
            raise AssertionError("flag-OFF public law imported the crate")

        monkeypatch.setattr(wlr.importlib, "import_module", _bomb)
        assert classify_window_status(1, None) == "SUCCESS"
        assert WindowStatusLog._key(KEY) == EXPECTED_KEY
        assert WindowLedger._key(KEY) == EXPECTED_KEY


class TestRealWheelDifferential:
    """Hypothesis differential vs the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_lake")
        monkeypatch.setenv(wlr.LAKE_RUST_WINDOW_LEDGER_ENV, "1")
        wlr.reset_window_ledger_rust_for_tests(remaining=0)  # steady state

    @settings(max_examples=200, deadline=None)
    @given(
        raw_bars=st.integers(min_value=-(2**63), max_value=2**63 - 1),
        error_type=st.one_of(st.none(), st.text(max_size=40)),
        degraded=st.booleans(),
    )
    def test_classify_differential_i64_domain(
        self, raw_bars: int, error_type: str | None, degraded: bool
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = wlr.routed_classify_window_status(
                raw_bars,
                error_type,
                degraded=degraded,
                python_law=_classify_window_status_python,
            )
        assert routed == _classify_window_status_python(raw_bars, error_type, degraded=degraded)

    @settings(max_examples=200, deadline=None)
    @given(
        start=st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)),
        end=st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)),
    )
    def test_merge_key_differential_full_date_domain(self, start: date, end: date) -> None:
        key = WindowKey(
            market="groww",
            security_id="NSE-NIFTY",
            interval="1minute",
            start=start,
            end=end,
        )
        with pytest.MonkeyPatch.context() as mp:
            self._require_wheel(mp)
            routed = wlr.routed_window_merge_key(key, python_law=window_key_python)
        assert routed == window_key_python(key)

    def test_out_of_i64_raw_bars_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Production-unreachable (the vendor never returns >9.2e18 bars) —
        # the honest scope note for the i64-domain differential above.
        self._require_wheel(monkeypatch)
        with capture_logs() as logs:
            result = wlr.routed_classify_window_status(
                2**63,
                None,
                degraded=False,
                python_law=_classify_window_status_python,
            )
        assert result == "SUCCESS"
        warns = [e for e in logs if e["event"] == "window_ledger_rust.fallback"]
        assert warns[0]["reason"] == "classify_error"
