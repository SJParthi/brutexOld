"""Tests for the ``fno_symbol_rust`` routing facade (ADR-0020 slice T10 PR-1).

DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940). Yields 100% line + branch on
``brutex/data/fno_symbol_rust.py`` ALONE (the discovery-gate contract): the
flag matrix (incl. junk-SET warn-once), the OFF-never-imports-crate bomb spy,
the ASCII pre-gate, the crate-absent / crate-stale latches, the LOCALE-GUARD
battery (real es_ES/de_DE arm with honest skip + the always-on synthesized
probe arm + the English-locale MUST-ENGAGE control — the vacuous-pin-OFF
killer), every degrade arm, the escape-set re-raise, the burn-in mismatch
autorevert + steady state, EVERY refuse-poison gate row, the strike-reject
slice-identity arm, the reset seam, the §11 ``env_flag_tristate`` identity
pin, the single-call-site grep pin, and the mutation-workflow wiring pin.
"""

from __future__ import annotations

import importlib
import locale
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex.data.fno_symbol_rust as fsr
import pytest
from brutex.data import fno_symbol, lake_rust_render
from brutex.data.fno_symbol import (
    EXPIRY_YEAR_MAX,
    EXPIRY_YEAR_MIN,
    FnoSymbolParts,
    _parse_fno_symbol_python,
    parse_fno_symbol,
    parse_strike_token,
)
from structlog.testing import capture_logs

REPO_ROOT = Path(__file__).resolve().parents[2]

FUT_SYM = "NSE-NIFTY-30Sep25-FUT"
CE_SYM = "NSE-NIFTY-30Sep25-24650-CE"

#: The CORRECT crate answer for FUT_SYM (the real 9-slot layout).
FUT_COORDS = (0, 3, 4, 9, 2025, 9, 30, 0, 0)
#: The CORRECT crate answer for CE_SYM.
CE_COORDS = (1, 3, 4, 9, 2025, 9, 30, 18, 23)

#: A frozen mini-corpus for the OFF-arm bomb pin (oracle values inline).
BOMB_CORPUS = (
    FUT_SYM,
    CE_SYM,
    "NSE-BAJAJ-AUTO-30Sep25-6000-CE",
    "NSE-NIFTY-FUT",
    "junk",
    "",
)


def _routed(symbol: str) -> FnoSymbolParts | None:
    """Call the facade with the REAL oracle injection triple."""
    return fsr.routed_parse_fno_symbol(
        symbol,
        python_law=_parse_fno_symbol_python,
        strike_law=parse_strike_token,
        build_parts=FnoSymbolParts,
    )


def _fake_crate(
    *,
    coords: Any = None,
    version: str = "0.1.0-fake",
) -> SimpleNamespace:
    """A stand-in ``brutex_data`` module exposing the T10 API surface.

    ``coords`` may be a fixed return value, an exception INSTANCE to raise,
    or a callable ``(symbol) -> coords``; ``None`` means "the correct
    answer for the two well-known symbols, else None".
    """

    def _default(symbol: str) -> tuple[int, ...] | None:
        if symbol == FUT_SYM:
            return FUT_COORDS
        if symbol == CE_SYM:
            return CE_COORDS
        return None

    def _raiser(symbol: str) -> tuple[int, ...] | None:
        assert isinstance(coords, BaseException)
        raise coords

    def _fixed(symbol: str) -> Any:
        return coords

    fn: Any
    if coords is None:
        fn = _default
    elif isinstance(coords, BaseException):
        fn = _raiser
    elif callable(coords):
        fn = coords
    else:
        fn = _fixed

    return SimpleNamespace(parse_fno_symbol_coords=fn, __version__=version)


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``importlib.import_module`` at the fake crate.

    Foreign names delegate to the REAL import machinery — the panic belt's
    guarded ``pyo3_runtime`` resolve must keep working under the patch.
    """
    real_import_module = fsr.importlib.import_module

    def _import(name: str) -> object:
        if name != "brutex_data":
            return real_import_module(name)
        return crate

    monkeypatch.setattr(fsr.importlib, "import_module", _import)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fsr.DATA_RUST_ENV, "1")


def _english_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the locale probe to the English table (the engage-ready state)."""
    monkeypatch.setattr(fsr, "_live_month_table", lambda: fsr._ENGLISH_MONTHS)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh latch/locale/burn-in/memo state + flag unset around every test."""
    monkeypatch.delenv(fsr.DATA_RUST_ENV, raising=False)
    fsr.reset_fno_symbol_rust_for_tests()
    yield
    fsr.reset_fno_symbol_rust_for_tests()


class TestFlagResolution:
    def test_default_unset_is_off_without_warn(self) -> None:
        with capture_logs() as logs:
            assert fsr.data_rust_enabled() is False
        assert logs == []

    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "1")
        assert fsr.data_rust_enabled() is True

    def test_falsy_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "0")
        with capture_logs() as logs:
            assert fsr.data_rust_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "banana")
        with capture_logs() as logs:
            assert fsr.data_rust_enabled() is False
            assert fsr.data_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "fno_symbol_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"

    def test_blank_whitespace_is_off_without_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "   ")
        with capture_logs() as logs:
            assert fsr.data_rust_enabled() is False
        assert logs == []

    def test_env_flag_tristate_is_the_sibling_object(self) -> None:
        # §11 COMMON identity pin — ONE flag vocabulary, never a copy.
        assert fsr.env_flag_tristate is lake_rust_render.env_flag_tristate

    def test_year_window_mirror_pins_oracle_constants(self) -> None:
        # The facade's no-import mirror must track the oracle module.
        assert fsr.EXPIRY_YEAR_MIN_MIRROR == EXPIRY_YEAR_MIN
        assert fsr.EXPIRY_YEAR_MAX_MIRROR == EXPIRY_YEAR_MAX


class TestOffArmInertness:
    def test_off_never_imports_or_touches_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The design's bomb-crate spy: raises on ANY attribute access AND on
        # import — the flag-OFF/unset public path must never reach either.
        class _Bomb:
            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"flag-OFF path touched crate attr {name!r}")

        monkeypatch.setitem(__import__("sys").modules, "brutex_data", _Bomb())

        def _bomb_import(name: str) -> object:
            raise AssertionError(f"flag-OFF path imported {name!r}")

        monkeypatch.setattr(fsr.importlib, "import_module", _bomb_import)
        for symbol in BOMB_CORPUS:
            assert parse_fno_symbol(symbol) == _parse_fno_symbol_python(symbol)

    def test_falsy_set_is_equally_inert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "0")

        def _bomb_import(name: str) -> object:
            raise AssertionError(f"flag-falsy path imported {name!r}")

        monkeypatch.setattr(fsr.importlib, "import_module", _bomb_import)
        assert _routed(CE_SYM) == _parse_fno_symbol_python(CE_SYM)

    def test_latched_process_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        fsr._disable("crate_unavailable")

        def _bomb_import(name: str) -> object:
            raise AssertionError("latched path re-imported the crate")

        monkeypatch.setattr(fsr.importlib, "import_module", _bomb_import)
        assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert fsr.data_rust_disabled() is True


class TestAsciiPreGate:
    def test_non_ascii_routes_to_oracle_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _bomb_import(name: str) -> object:
            raise AssertionError("non-ASCII symbol reached the crate resolve")

        monkeypatch.setattr(fsr.importlib, "import_module", _bomb_import)
        # Unicode-digit year: the ORACLE accepts it (strptime \d + int());
        # the facade must serve exactly the oracle with NO log line.
        sym = "NSE-NIFTY-30Sep٢٥-FUT"
        with capture_logs() as logs:
            result = _routed(sym)
        assert result == _parse_fno_symbol_python(sym)
        assert result is not None  # the oracle genuinely accepts (year 2025)
        assert logs == []

    def test_non_ascii_reject_also_matches_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        sym = "NSE-NIFTY-٣٠Sep25-FUT"  # non-ASCII day-first-char: oracle None
        assert _routed(sym) is None
        assert _parse_fno_symbol_python(sym) is None


class TestCrateGuards:
    def test_crate_absent_latches_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)

        def _import(name: str) -> object:
            raise ImportError("No module named 'brutex_data'")

        monkeypatch.setattr(fsr.importlib, "import_module", _import)
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert fsr.data_rust_disabled() is True
        events = [e for e in logs if e["event"] == "fno_symbol_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_unavailable"

    def test_crate_stale_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, SimpleNamespace(__version__="0.0.1"))
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        events = [e for e in logs if e["event"] == "fno_symbol_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_stale"

    def test_crate_stale_noncallable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, SimpleNamespace(parse_fno_symbol_coords="not-callable"))
        assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert fsr.data_rust_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        fsr._disable("crate_unavailable")
        fsr._disable("crate_stale")  # repeat: keeps the FIRST reason
        assert fsr._DISABLED_REASON[0] == "crate_unavailable"


class TestLocaleGuard:
    """The round-1 MAJOR-1 battery — real-locale, synthesized, and control."""

    _CANDIDATE_LOCALES = ("es_ES.UTF-8", "es_ES", "de_DE.UTF-8", "de_DE")

    def test_real_locale_refuses_to_engage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # (a) the real-locale arm — honest skip when no non-English locale
        # is installed (CI images often ship C-only; the synthesized arm
        # below is the always-on tooth).
        original = locale.setlocale(locale.LC_TIME)
        chosen: str | None = None
        for cand in self._CANDIDATE_LOCALES:
            try:
                locale.setlocale(locale.LC_TIME, cand)
            except locale.Error:
                continue
            chosen = cand
            break
        if chosen is None:
            pytest.skip("no es_ES/de_DE locale installed — synthesized arm covers the guard")
        try:
            _flag_on(monkeypatch)
            _install_crate(monkeypatch, _fake_crate())
            corpus = [*BOMB_CORPUS, "NSE-NIFTY-30ene25-FUT"]
            with capture_logs() as logs:
                for sym in corpus:
                    # Parity, not a frozen value: BOTH arms return whatever
                    # the LIVE (locale-variable) oracle says.
                    assert _routed(sym) == _parse_fno_symbol_python(sym)
            assert fsr.data_rust_disabled() is True
            assert fsr._DISABLED_REASON[0] == "locale_unsupported"
            warns = [
                e
                for e in logs
                if e["event"] == "fno_symbol_rust.fallback" and e["reason"] == "locale_unsupported"
            ]
            assert len(warns) == 1
        finally:
            locale.setlocale(locale.LC_TIME, original)

    def test_synthesized_non_english_table_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # (b) always-on: monkeypatch the probe seam to a non-English table.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        spanish = (
            "ene",
            "feb",
            "mar",
            "abr",
            "may",
            "jun",
            "jul",
            "ago",
            "sep",
            "oct",
            "nov",
            "dic",
        )
        monkeypatch.setattr(fsr, "_live_month_table", lambda: spanish)
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
            assert _routed(CE_SYM) == _parse_fno_symbol_python(CE_SYM)
        assert fsr._DISABLED_REASON[0] == "locale_unsupported"
        warns = [e for e in logs if e["event"] == "fno_symbol_rust.fallback"]
        assert len(warns) == 1

    def test_probe_raising_refuses_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _boom() -> tuple[str, ...]:
            raise RuntimeError("LocaleTime surface drifted")

        monkeypatch.setattr(fsr, "_live_month_table", _boom)
        assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert fsr._DISABLED_REASON[0] == "locale_unsupported"

    def test_probe_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())

        def _oom() -> tuple[str, ...]:
            raise MemoryError

        monkeypatch.setattr(fsr, "_live_month_table", _oom)
        with pytest.raises(MemoryError):
            _routed(FUT_SYM)
        assert fsr.data_rust_disabled() is False  # never a silent latch

    def test_english_control_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # (c) the MUST-ENGAGE control — the vacuous-pin-OFF killer: an
        # always-False compare (e.g. the list == tuple probe type slip)
        # fails THIS arm loudly instead of shipping a seam that silently
        # never engages. Uses the REAL probe under the live test locale
        # (the independent skip-guard computes the locale truth via the
        # same stdlib surface, resolved dynamically — no _strptime stub in
        # the strict type gates).
        strptime_mod = importlib.import_module("_strptime")
        live = tuple(str(m) for m in strptime_mod.LocaleTime().a_month[1:])
        if live != fsr._ENGLISH_MONTHS:
            pytest.skip(f"test runner LC_TIME is not C/English ({live[:3]}…)")
        _flag_on(monkeypatch)
        calls: list[str] = []

        def _spy(symbol: str) -> tuple[int, ...]:
            calls.append(symbol)
            return FUT_COORDS

        _install_crate(monkeypatch, _fake_crate(coords=_spy))
        assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert calls == [FUT_SYM]  # the crate genuinely engaged
        assert fsr.data_rust_disabled() is False
        assert fsr._LOCALE_OK[0] is True

    def test_locale_probe_memoises_positive_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        probes: list[int] = []

        def _probe() -> tuple[str, ...]:
            probes.append(1)
            return fsr._ENGLISH_MONTHS

        monkeypatch.setattr(fsr, "_live_month_table", _probe)
        _routed(FUT_SYM)
        _routed(CE_SYM)
        assert len(probes) == 1  # LocaleTime never re-constructed per call

    def test_reset_clears_positive_locale_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The full-virgin reset law (round-2 CORR NIT-R2-1): a prior
        # English-passed state must not leak into a real-locale arm.
        _flag_on(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        _english_probe(monkeypatch)
        _routed(FUT_SYM)
        assert fsr._LOCALE_OK[0] is True
        fsr.reset_fno_symbol_rust_for_tests()
        assert fsr._LOCALE_OK[0] is False
        assert fsr._DISABLED_REASON[0] is None
        assert fsr._BURN_IN_REMAINING["parse"] == fsr.BURN_IN_SAMPLES
        assert set() == fsr._OBSERVABILITY_MEMO


class TestDegradeArms:
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad"),
            RuntimeError("panic-mapped"),
            TypeError("conv"),
            OverflowError("big"),
            AttributeError("attr"),
        ],
        ids=["ValueError", "RuntimeError", "TypeError", "OverflowError", "AttributeError"],
    )
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=exc))
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        warns = [e for e in logs if e["event"] == "fno_symbol_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "parse_error"
        assert fsr.data_rust_disabled() is False  # per-call degrade, no latch

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=MemoryError()))
        with pytest.raises(MemoryError):
            _routed(FUT_SYM)
        assert fsr.data_rust_disabled() is False

    def test_keyboard_interrupt_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            _routed(FUT_SYM)

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polars.exceptions import PanicException

        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=PanicException("kernel panicked")))
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        assert fsr._DISABLED_REASON[0] == "crate_panic"
        events = [e for e in logs if e["event"] == "fno_symbol_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_panic"


class TestRefusePoisonGates:
    """Every §3.4 step-5 row: a poison tuple is refused to the oracle."""

    @pytest.mark.parametrize(
        ("poison", "label"),
        [
            ("not-a-tuple", "non_tuple"),
            ((0, 3, 4, 9, 2025, 9, 30), "wrong_arity"),
            ((0, 3, 4, 9, 2025.0, 9, 30, 0, 0), "non_int_member"),
            ((0, 3, 4, 9, 2025, True, 30, 0, 0), "bool_member"),
            ((3, 3, 4, 9, 2025, 9, 30, 0, 0), "bad_type_code"),
            ((0, 0, 1, 9, 2025, 9, 30, 0, 0), "zero_exchange_end"),
            ((0, 3, 5, 9, 2025, 9, 30, 0, 0), "underlying_start_not_adjacent"),
            ((0, 3, 4, 4, 2025, 9, 30, 0, 0), "empty_underlying_span"),
            ((0, 3, 4, 999, 2025, 9, 30, 0, 0), "underlying_span_out_of_bounds"),
            ((0, 999, 1000, 1001, 2025, 9, 30, 0, 0), "exchange_span_out_of_bounds"),
            ((0, 3, 4, 9, 1975, 6, 30, 0, 0), "out_of_window_year_1975"),
            ((0, 3, 4, 9, 2060, 9, 30, 0, 0), "out_of_window_year_2060"),
            ((0, 3, 4, 9, 2025, 0, 30, 0, 0), "month_zero"),
            ((0, 3, 4, 9, 2025, 13, 30, 0, 0), "month_thirteen"),
            ((0, 3, 4, 9, 2025, 9, 0, 0, 0), "day_zero"),
            ((0, 3, 4, 9, 2025, 9, 32, 0, 0), "day_thirty_two"),
            ((0, 3, 4, 9, 2025, 9, 30, 5, 7), "fut_with_strike_span"),
            ((1, 3, 4, 9, 2025, 9, 30, 0, 0), "option_empty_strike_span"),
            ((1, 3, 4, 9, 2025, 9, 30, 18, 999), "strike_span_out_of_bounds"),
        ],
    )
    def test_poison_row_refused_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any, label: str
    ) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=poison))
        sym = FUT_SYM if not label.startswith(("option", "strike")) else CE_SYM
        with capture_logs() as logs:
            assert _routed(sym) == _parse_fno_symbol_python(sym)
        warns = [e for e in logs if e["event"] == "fno_symbol_rust.fallback"]
        assert warns[0]["reason"] == "result_refused"
        assert fsr.data_rust_disabled() is False  # refuse, never latch

    def test_date_constructor_backstop_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # m/d in range but calendar-impossible (31 Apr) — the date() guard.
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=(0, 3, 4, 9, 2025, 4, 31, 0, 0)))
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
        warns = [e for e in logs if e["event"] == "fno_symbol_rust.fallback"]
        assert warns[0]["reason"] == "result_refused"


class TestRoutedFlow:
    def test_engaged_accept_matches_oracle_and_logs_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        with capture_logs() as logs:
            assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)
            assert _routed(CE_SYM) == _parse_fno_symbol_python(CE_SYM)
        engaged = [e for e in logs if e["event"] == "fno_symbol_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"

    def test_engaged_none_matches_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        assert _routed("NSE-NIFTY-FUT") is None  # the crate's structural None

    def test_strike_reject_returns_none_by_slice_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The disclosed pre-burn-in return: junk strike token — the crate
        # reports the span, the SHARED strike law rejects, facade None ⟺
        # oracle None by slice identity.
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        sym = "NSE-NIFTY-30Sep25-abc-CE"
        _install_crate(monkeypatch, _fake_crate(coords=(1, 3, 4, 9, 2025, 9, 30, 18, 21)))
        assert _routed(sym) is None
        assert _parse_fno_symbol_python(sym) is None

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        # Wrong-but-structurally-valid span: underlying span covers "NIFT".
        _install_crate(monkeypatch, _fake_crate(coords=(0, 3, 4, 8, 2025, 9, 30, 0, 0)))
        with capture_logs() as logs:
            first = _routed(FUT_SYM)
        assert first == _parse_fno_symbol_python(FUT_SYM)  # the ORACLE value ships
        assert fsr._DISABLED_REASON[0] == "burn_in_mismatch_parse"
        errors = [e for e in logs if e["event"] == "fno_symbol_rust.autoreverted"]
        assert len(errors) == 1

        # Later calls short-circuit to the oracle without touching the crate.
        def _bomb_import(name: str) -> object:
            raise AssertionError("latched path re-imported the crate")

        monkeypatch.setattr(fsr.importlib, "import_module", _bomb_import)
        assert _routed(FUT_SYM) == _parse_fno_symbol_python(FUT_SYM)

    def test_burn_in_none_mismatch_autoreverts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Rust false-REJECT is as wrong as a false-accept.
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate(coords=lambda s: None))
        result = _routed(FUT_SYM)
        assert result == _parse_fno_symbol_python(FUT_SYM)
        assert fsr._DISABLED_REASON[0] == "burn_in_mismatch_parse"

    def test_burn_in_strict_strike_representation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Dataclass __eq__ compares Decimal numerically; the STRICT compare
        # pins str(strike) — exercised via an injected python_law whose
        # strike representation differs.
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        sym = "NSE-NIFTY-30Sep25-24000.5-CE"
        _install_crate(monkeypatch, _fake_crate(coords=(1, 3, 4, 9, 2025, 9, 30, 18, 25)))

        def _law_variant(symbol: str) -> FnoSymbolParts | None:
            parts = _parse_fno_symbol_python(symbol)
            assert parts is not None
            return FnoSymbolParts(
                exchange=parts.exchange,
                underlying=parts.underlying,
                expiry=parts.expiry,
                instrument_type=parts.instrument_type,
                strike=Decimal("24000.50"),  # numerically equal, repr differs
                option_type=parts.option_type,
            )

        result = fsr.routed_parse_fno_symbol(
            sym,
            python_law=_law_variant,
            strike_law=parse_strike_token,
            build_parts=FnoSymbolParts,
        )
        assert result is not None
        assert str(result.strike) == "24000.50"  # the ORACLE value shipped
        assert fsr._DISABLED_REASON[0] == "burn_in_mismatch_parse"

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        fsr.reset_fno_symbol_rust_for_tests(remaining=2)
        oracle_calls: list[str] = []

        def _spy_law(symbol: str) -> FnoSymbolParts | None:
            oracle_calls.append(symbol)
            return _parse_fno_symbol_python(symbol)

        def _run(symbol: str) -> FnoSymbolParts | None:
            return fsr.routed_parse_fno_symbol(
                symbol,
                python_law=_spy_law,
                strike_law=parse_strike_token,
                build_parts=FnoSymbolParts,
            )

        with capture_logs() as logs:
            _run(FUT_SYM)
            _run(CE_SYM)  # burn-in exhausts here
            _run(FUT_SYM)  # steady state: the oracle is OUT of the path
        assert oracle_calls == [FUT_SYM, CE_SYM]
        done = [e for e in logs if e["event"] == "fno_symbol_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["samples"] == fsr.BURN_IN_SAMPLES

    def test_steady_state_ships_rust_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        _install_crate(monkeypatch, _fake_crate())
        fsr.reset_fno_symbol_rust_for_tests(remaining=0)

        def _law_bomb(symbol: str) -> FnoSymbolParts | None:
            raise AssertionError("steady state must not call the oracle")

        result = fsr.routed_parse_fno_symbol(
            CE_SYM,
            python_law=_law_bomb,
            strike_law=parse_strike_token,
            build_parts=FnoSymbolParts,
        )
        assert result == _parse_fno_symbol_python(CE_SYM)


class TestPartsEqualStrict:
    def test_both_none(self) -> None:
        assert fsr._parts_equal_strict(None, None) is True

    def test_one_none(self) -> None:
        parts = _parse_fno_symbol_python(FUT_SYM)
        assert fsr._parts_equal_strict(parts, None) is False
        assert fsr._parts_equal_strict(None, parts) is False

    def test_equal_futures(self) -> None:
        a = _parse_fno_symbol_python(FUT_SYM)
        b = _parse_fno_symbol_python(FUT_SYM)
        assert fsr._parts_equal_strict(a, b) is True  # str(None) == str(None)


class TestWiringPins:
    def test_single_call_site_pin(self) -> None:
        # Risk #12: `routed_parse_fno_symbol` is called from EXACTLY ONE
        # production site — fno_symbol.parse_fno_symbol (the injected
        # callables are always that module's own oracle helpers).
        pkg = REPO_ROOT / "brutex"
        callers: list[str] = []
        for path in sorted(pkg.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"routed_parse_fno_symbol\(", text):
                callers.append(str(path.relative_to(REPO_ROOT)))
        assert callers == ["brutex/data/fno_symbol.py", "brutex/data/fno_symbol_rust.py"]

    def test_public_seam_injects_the_oracle_triple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The public entry routes through the facade with the real oracle
        # injection (an integration pin on the fno_symbol.py delegation).
        _flag_on(monkeypatch)
        _english_probe(monkeypatch)
        seen: dict[str, Any] = {}

        def _spy(symbol: str, **kwargs: Any) -> FnoSymbolParts | None:
            seen.update(kwargs)
            return _parse_fno_symbol_python(symbol)

        monkeypatch.setattr(fno_symbol.fno_symbol_rust, "routed_parse_fno_symbol", _spy)
        assert parse_fno_symbol(CE_SYM) == _parse_fno_symbol_python(CE_SYM)
        assert seen["python_law"] is _parse_fno_symbol_python
        assert seen["strike_law"] is parse_strike_token
        assert seen["build_parts"] is FnoSymbolParts


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``TestBenchWorkflowWiring`` precedent (#1797 pattern; the #1914
    ``lake_rust_stats`` shape): the module:suite pair sits in the workflow's
    per-module ``mutmut`` loop with the EMPTY third (timeout) field — this
    deterministic pin fails if the pair is ever dropped. HONEST: it pins the
    WIRING only; the weekly run executes on its own schedule.
    """

    _WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/data/fno_symbol_rust.py:tests/unit/test_fno_symbol_rust.py:"' in text, (
            "fno_symbol_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )
