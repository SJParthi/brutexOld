"""Wheel-FREE unit matrix for the ``lake_keys_rust`` injection-leaf facade.

Every test runs WITHOUT the ``brutex_data`` wheel: crates are faked via
``monkeypatch`` on the facade's ``importlib.import_module`` seam.  The
matrix pins (design doc §6.3, T10 PR-2 step B):

- OFF-arm inertness (bomb crate never touched through the PUBLIC
  ``lake_keys`` entries with the flag OFF/unset);
- the ASCII pre-gate ordering (gate runs BEFORE ``_resolve_crate()``);
- crate guards (``crate_unavailable`` / ``crate_stale`` all-or-nothing);
- degrade arms per law + the pyo3 panic belt + the RAW escape set;
- every refuse-poison gate row per law, incl. the LEGAL RAW-identity
  direction (a stripped-equal-but-raw-unequal recovery entry SHIPS);
- burn-in accounting (independent per-law budgets; facade-WIDE latch;
  cross-facade isolation vs ``fno_symbol_rust``);
- routed-flow observability (engaged INFO once, burn_in_complete,
  steady-state uncompared serve);
- wiring pins (single call site per routed fn; ``_CRATE_API``; the leaf
  import law; fresh-subprocess import-order acyclicity).
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import os
import subprocess
import sys
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from brutex.data import fno_symbol_rust, lake_keys, lake_keys_rust

REPO_ROOT = Path(__file__).resolve().parents[2]
FACADE_PATH = REPO_ROOT / "brutex" / "data" / "lake_keys_rust.py"
LAKE_KEYS_PATH = REPO_ROOT / "brutex" / "data" / "lake_keys.py"

_CRATE_NAME = "brutex_data"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _BombCrate:
    """A crate whose every attribute access is recorded; calls explode."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        self.touched.append(name)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"bomb crate called: {name}")

        return _boom


class _FakeCrate:
    """A configurable fake exposing exactly the 3-symbol `_CRATE_API`."""

    def __init__(
        self,
        *,
        fold: Any = None,
        candidates: Any = None,
        recovery: Any = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fold = fold
        self._candidates = candidates
        self._recovery = recovery

    def stock_drift_fold(self, symbol: str) -> Any:
        self.calls.append(("stock_drift_fold", (symbol,)))
        if callable(self._fold):
            return self._fold(symbol)
        return self._fold

    def spot_key_candidates(self, symbol: str, exchange: str) -> Any:
        self.calls.append(("spot_key_candidates", (symbol, exchange)))
        if callable(self._candidates):
            return self._candidates(symbol, exchange)
        return self._candidates

    def spot_recovery_candidates(
        self,
        underlying: str,
        exchange: str,
        alias_pairs: list[tuple[str, str]],
        drift_bucket: list[str],
    ) -> Any:
        self.calls.append(
            ("spot_recovery_candidates", (underlying, exchange, alias_pairs, drift_bucket))
        )
        if callable(self._recovery):
            return self._recovery(underlying, exchange, alias_pairs, drift_bucket)
        return self._recovery


class _OracleCrate(_FakeCrate):
    """A fake crate that mirrors the Python oracles bit-for-bit."""

    def __init__(self) -> None:
        super().__init__(
            fold=lake_keys._stock_drift_fold_python,
            candidates=lake_keys._spot_key_candidates_python,
            recovery=lambda u, e, p, b: lake_keys._spot_recovery_candidates_python(
                u, e, alias_pairs=p, drift_bucket=b
            ),
        )


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's crate import at the fake; foreign names real."""

    real_import = importlib.import_module

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == _CRATE_NAME:
            return crate
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lake_keys_rust.importlib, "import_module", _fake_import)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("BRUTEX_DATA_RUST", raising=False)
    lake_keys_rust.reset_lake_keys_rust_for_tests()
    fno_symbol_rust.reset_fno_symbol_rust_for_tests()
    yield
    lake_keys_rust.reset_lake_keys_rust_for_tests()
    fno_symbol_rust.reset_fno_symbol_rust_for_tests()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_DATA_RUST", "1")


@contextlib.contextmanager
def _refuse_gate(monkeypatch: pytest.MonkeyPatch, crate: object, reason: str) -> Generator[None]:
    """Install *crate*, force STEADY-STATE (remaining=0), assert the refuse warn.

    The r1-MAJOR fix: at the default burn-in budget the burn-in COMPARE
    (not the refuse gate) ships the oracle, so a dropped refuse gate
    survived every negative test.  Steady-state means the ONLY thing
    standing between the poison value and the caller is the refuse gate
    itself; the exit assert (belt + braces) pins the specific
    ``result_refused_<law>`` warn-once fallback reason.
    """
    _install_crate(monkeypatch, crate)
    lake_keys_rust.reset_lake_keys_rust_for_tests(remaining=0)
    with structlog.testing.capture_logs() as logs:
        yield
    assert any(
        e["event"] == "lake_keys_rust.fallback" and e.get("reason") == reason for e in logs
    ), logs


_ALIAS_PAIRS: tuple[tuple[str, str], ...] = (("NIFTYJR", "NIFTYNXT50"),)
_BUCKET: tuple[str, ...] = ("BAJAJ-AUTO",)


def _oracle_recovery(underlying: str, exchange: str) -> tuple[str, ...]:
    return lake_keys._spot_recovery_candidates_python(
        underlying, exchange, alias_pairs=_ALIAS_PAIRS, drift_bucket=_BUCKET
    )


# ---------------------------------------------------------------------------
# OFF-arm inertness
# ---------------------------------------------------------------------------


class TestOffArmInertness:
    def test_public_entries_never_touch_crate_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OFF via the explicit "0" kill switch (default flipped ON — DEC-RUST-DEFAULT-ON-WAVE-001).
        monkeypatch.setenv("BRUTEX_DATA_RUST", "0")
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"
        assert lake_keys.spot_key_candidates(" NSE-NIFTY ", "NSE") == (
            "NSE-NIFTY",
            "NIFTY",
        )
        assert lake_keys.spot_recovery_candidates("NIFTY", "NSE") == (
            lake_keys._spot_recovery_candidates_python(
                "NIFTY",
                "NSE",
                alias_pairs=lake_keys.fno_index_alias_pairs(),
                drift_bucket=(),
            )
        )
        assert bomb.touched == []

    def test_public_entries_never_touch_crate_flag_falsy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_DATA_RUST", "0")
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        assert lake_keys.stock_drift_fold("M&M ") == "M&M"
        assert bomb.touched == []

    def test_flag_junk_is_off_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_DATA_RUST", "banana")
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        with structlog.testing.capture_logs() as logs:
            assert lake_keys.stock_drift_fold("m&m ") == "M&M"
            assert lake_keys.stock_drift_fold("m&m ") == "M&M"
        warns = [
            e
            for e in logs
            if e["event"] == "fno_symbol_rust.fallback" and e.get("reason") == "flag_unrecognised"
        ]
        assert len(warns) == 1
        assert bomb.touched == []

    def test_off_arm_matches_oracle_bytes(self) -> None:
        for sym in ("BAJAJ-AUTO", "m&m", " nifty "):
            assert lake_keys.stock_drift_fold(sym) == lake_keys._stock_drift_fold_python(sym)
        assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
            lake_keys._spot_key_candidates_python("NIFTY", "NSE")
        )


# ---------------------------------------------------------------------------
# ASCII pre-gate (runs BEFORE _resolve_crate)
# ---------------------------------------------------------------------------


class TestAsciiPreGate:
    def test_fold_non_ascii_symbol_routes_oracle_before_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        sym = "NIFTY₹"
        assert lake_keys.stock_drift_fold(sym) == lake_keys._stock_drift_fold_python(sym)
        assert bomb.touched == []

    def test_candidates_non_ascii_exchange_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        got = lake_keys_rust.routed_spot_key_candidates(
            "NIFTY",
            "NSEé",
            python_law=lake_keys._spot_key_candidates_python,
        )
        assert got == lake_keys._spot_key_candidates_python("NIFTY", "NSEé")
        assert bomb.touched == []

    def test_recovery_non_ascii_pair_key_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        pairs = (("NIFTYé", "NIFTYNXT50"),)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=pairs,
            drift_bucket=(),
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == lake_keys._spot_recovery_candidates_python(
            "NIFTY", "NSE", alias_pairs=pairs, drift_bucket=()
        )
        assert bomb.touched == []

    def test_recovery_non_ascii_pair_value_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        pairs = (("NIFTYJR", "NIFTY₹"),)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=pairs,
            drift_bucket=(),
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == lake_keys._spot_recovery_candidates_python(
            "NIFTY", "NSE", alias_pairs=pairs, drift_bucket=()
        )
        assert bomb.touched == []

    def test_recovery_non_ascii_bucket_entry_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        bucket = ("BAJAJÉ",)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=(),
            drift_bucket=bucket,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == lake_keys._spot_recovery_candidates_python(
            "NIFTY", "NSE", alias_pairs=(), drift_bucket=bucket
        )
        assert bomb.touched == []


# ---------------------------------------------------------------------------
# Crate guards
# ---------------------------------------------------------------------------


class TestCrateGuards:
    def test_crate_unavailable_disables_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)

        def _raise_import(name: str, *a: object, **k: object) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(lake_keys_rust.importlib, "import_module", _raise_import)
        assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_unavailable"

    def test_pr1_shaped_crate_is_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        class _Pr1Crate:
            @staticmethod
            def parse_fno_symbol_coords(symbol: str) -> None:
                return None

        _install_crate(monkeypatch, _Pr1Crate())
        assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_stale"

    @pytest.mark.parametrize("missing", lake_keys_rust._CRATE_API)
    def test_each_missing_symbol_is_stale(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        _enable(monkeypatch)
        crate = _OracleCrate()

        class _Partial:
            pass

        partial = _Partial()
        for name in lake_keys_rust._CRATE_API:
            if name != missing:
                setattr(partial, name, getattr(crate, name))
        _install_crate(monkeypatch, partial)
        assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_stale"

    def test_disabled_latch_short_circuits_resolve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _BombCrate())
        lake_keys_rust._disable("test_latch")
        assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"
        assert lake_keys_rust._DISABLED_REASON[0] == "test_latch"


# ---------------------------------------------------------------------------
# Degrade arms + panic belt + escape set
# ---------------------------------------------------------------------------


class TestDegradeArms:
    @pytest.mark.parametrize("exc_type", lake_keys_rust._DEGRADE_ERRORS)
    def test_fold_degrades_per_error(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _enable(monkeypatch)

        def _raise(symbol: str) -> str:
            raise exc_type("boom")

        _install_crate(monkeypatch, _FakeCrate(fold=_raise))
        assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"

    @pytest.mark.parametrize("exc_type", lake_keys_rust._DEGRADE_ERRORS)
    def test_candidates_degrades_per_error(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _enable(monkeypatch)

        def _raise(symbol: str, exchange: str) -> object:
            raise exc_type("boom")

        _install_crate(monkeypatch, _FakeCrate(candidates=_raise))
        assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
            lake_keys._spot_key_candidates_python("NIFTY", "NSE")
        )

    @pytest.mark.parametrize("exc_type", lake_keys_rust._DEGRADE_ERRORS)
    def test_recovery_degrades_per_error(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        _enable(monkeypatch)

        def _raise(*a: object, **k: object) -> object:
            raise exc_type("boom")

        _install_crate(monkeypatch, _FakeCrate(recovery=_raise))
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == _oracle_recovery("NIFTY", "NSE")

    def test_pyo3_panic_belt_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        class _PanicException(Exception):
            pass

        fake_pyo3 = type(sys)("pyo3_runtime")
        fake_pyo3.PanicException = _PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake_pyo3)

        def _panic(symbol: str) -> str:
            raise _PanicException("kernel panicked")

        _install_crate(monkeypatch, _FakeCrate(fold=_panic))
        assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def _oom(symbol: str) -> str:
            raise MemoryError

        _install_crate(monkeypatch, _FakeCrate(fold=_oom))
        with pytest.raises(MemoryError):
            lake_keys.stock_drift_fold("NIFTY")

    def test_keyboard_interrupt_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def _intr(symbol: str) -> str:
            raise KeyboardInterrupt

        _install_crate(monkeypatch, _FakeCrate(fold=_intr))
        with pytest.raises(KeyboardInterrupt):
            lake_keys.stock_drift_fold("NIFTY")


# ---------------------------------------------------------------------------
# Refuse-poison gates
# ---------------------------------------------------------------------------


class TestRefusePoisonGates:
    def test_fold_non_str_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(monkeypatch, _FakeCrate(fold=lambda s: 42), "result_refused_fold"):
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"

    def test_fold_non_ascii_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(monkeypatch, _FakeCrate(fold=lambda s: "NIFTY₹"), "result_refused_fold"):
            assert lake_keys.stock_drift_fold("NIFTY") == "NIFTY"

    def test_candidates_non_list_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(candidates=lambda s, e: "NSE-NIFTY"),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_candidates_non_str_entry_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(candidates=lambda s, e: ["NIFTY", 7]),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_candidates_empty_entry_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(candidates=lambda s, e: ["NIFTY", ""]),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_candidates_identity_echo_refused_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        # An entry ``==`` the RAW symbol param is the identity echo — refused.
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(candidates=lambda s, e: [s, "OTHER"]),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_candidates_stripped_form_legal_ships(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The RAW direction: the STRIPPED form is rung 1's LEGAL value — ships."""
        _enable(monkeypatch)
        _install_crate(monkeypatch, _FakeCrate(candidates=lambda s, e: [s.strip()]))
        lake_keys_rust.reset_lake_keys_rust_for_tests(remaining=0)
        raw = " NIFTY "
        got = lake_keys_rust.routed_spot_key_candidates(
            raw, "NSE", python_law=lake_keys._spot_key_candidates_python
        )
        assert got == ("NIFTY",) == lake_keys._spot_key_candidates_python(raw, "NSE")

    def test_candidates_length_ceiling_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch,
            # Deliberately NO identity/dup/empty poison — the LENGTH gate
            # must be the ONLY refusing clause (mutant isolation).
            _FakeCrate(candidates=lambda s, e: [f"X{i}" for i in range(65)]),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_recovery_non_list_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(recovery=lambda *a: None), "result_refused_recovery"
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")

    def test_recovery_non_str_entry_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(recovery=lambda *a: ["OK", 3]), "result_refused_recovery"
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")

    def test_recovery_length_ceiling_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        ceiling = 2 * len(_ALIAS_PAIRS) + 3 + len(_BUCKET)
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(recovery=lambda *a: [f"C{i}" for i in range(ceiling + 1)]),
            "result_refused_recovery",
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")

    def test_recovery_empty_entry_legal_ships(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EMPTY recovery entries are NOT refused (design §3.2)."""
        _enable(monkeypatch)
        value = ["", "NSE-NIFTY"]
        _install_crate(monkeypatch, _FakeCrate(recovery=lambda *a: list(value)))
        lake_keys_rust.reset_lake_keys_rust_for_tests(remaining=0)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == tuple(value)

    def test_recovery_stripped_equal_raw_unequal_entry_legal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #C6 shape: ' NSE-NIFTY ' style entries are LEGAL and ship."""
        _enable(monkeypatch)
        value = [" NSE-NIFTY ", "NSE-NIFTY"]
        _install_crate(monkeypatch, _FakeCrate(recovery=lambda *a: list(value)))
        lake_keys_rust.reset_lake_keys_rust_for_tests(remaining=0)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == tuple(value)


# ---------------------------------------------------------------------------
# Burn-in accounting
# ---------------------------------------------------------------------------


class TestBurnInAccounting:
    def test_budgets_are_independent_per_law(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _OracleCrate())
        lake_keys.stock_drift_fold("bajaj-auto")
        remaining = dict(lake_keys_rust._BURN_IN_REMAINING)
        assert remaining[lake_keys_rust._LAW_FOLD] == lake_keys_rust.BURN_IN_SAMPLES - 1
        assert remaining[lake_keys_rust._LAW_CANDIDATES] == lake_keys_rust.BURN_IN_SAMPLES
        assert remaining[lake_keys_rust._LAW_RECOVERY] == lake_keys_rust.BURN_IN_SAMPLES

    def test_fold_mismatch_latches_all_three_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _OracleCrate()
        crate._fold = lambda s: "WRONGVALUE"
        _install_crate(monkeypatch, crate)
        with structlog.testing.capture_logs() as logs:
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"
        reason = lake_keys_rust._DISABLED_REASON[0]
        assert reason == f"burn_in_mismatch_{lake_keys_rust._LAW_FOLD}"
        autorevert = [e for e in logs if e["event"] == "lake_keys_rust.autoreverted"]
        assert len(autorevert) == 1
        # Every later call on EVERY law short-circuits to the oracle.
        crate.calls.clear()
        assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
            lake_keys._spot_key_candidates_python("NIFTY", "NSE")
        )
        assert lake_keys.spot_recovery_candidates("NIFTY", "NSE") == (
            lake_keys.spot_recovery_candidates("NIFTY", "NSE")
        )
        assert crate.calls == []

    def test_candidates_mismatch_ships_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _OracleCrate()
        crate._candidates = lambda s, e: ["BOGUS"]
        _install_crate(monkeypatch, crate)
        assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
            lake_keys._spot_key_candidates_python("NIFTY", "NSE")
        )
        assert lake_keys_rust._DISABLED_REASON[0] == (
            f"burn_in_mismatch_{lake_keys_rust._LAW_CANDIDATES}"
        )

    def test_recovery_mismatch_ships_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        crate = _OracleCrate()
        crate._recovery = lambda u, e, p, b: ["NSE-BOGUS"]
        _install_crate(monkeypatch, crate)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == _oracle_recovery("NIFTY", "NSE")
        assert lake_keys_rust._DISABLED_REASON[0] == (
            f"burn_in_mismatch_{lake_keys_rust._LAW_RECOVERY}"
        )

    def test_cross_facade_isolation_lake_keys_latch_leaves_fno_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        lake_keys_rust._disable("test_latch")
        assert fno_symbol_rust.data_rust_disabled() is False

    def test_cross_facade_isolation_fno_symbol_latch_leaves_lake_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        fno_symbol_rust._disable("test_latch")
        assert lake_keys_rust.lake_keys_rust_disabled() is False


# ---------------------------------------------------------------------------
# Routed flow / observability
# ---------------------------------------------------------------------------


class TestRoutedFlow:
    def test_engaged_info_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _OracleCrate())
        with structlog.testing.capture_logs() as logs:
            lake_keys.stock_drift_fold("NIFTY")
            lake_keys.stock_drift_fold("BANKNIFTY")
            lake_keys.spot_key_candidates("NIFTY", "NSE")
        engaged = [e for e in logs if e["event"] == "lake_keys_rust.engaged"]
        assert len(engaged) == 1

    def test_burn_in_completes_then_steady_state_serves_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        crate = _OracleCrate()
        _install_crate(monkeypatch, crate)
        for i in range(lake_keys_rust.BURN_IN_SAMPLES):
            lake_keys.stock_drift_fold(f"SYM{i}")
        assert lake_keys_rust._BURN_IN_REMAINING[lake_keys_rust._LAW_FOLD] == 0
        assert lake_keys_rust.lake_keys_rust_disabled() is False
        # Steady state: a distinguishable-but-LEGAL crate value ships
        # uncompared (the burn-in no longer runs).
        crate._fold = lambda s: "STEADYVALUE"
        assert lake_keys.stock_drift_fold("anything") == "STEADYVALUE"
        assert lake_keys_rust.lake_keys_rust_disabled() is False

    def test_reset_remaining_zero_skips_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _FakeCrate(candidates=lambda s, e: ["STRIPPED", "LEGAL"]))
        lake_keys_rust.reset_lake_keys_rust_for_tests(remaining=0)
        got = lake_keys_rust.routed_spot_key_candidates(
            "NIFTY", "NSE", python_law=lake_keys._spot_key_candidates_python
        )
        assert got == ("STRIPPED", "LEGAL")

    def test_reset_restores_full_virgin_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        lake_keys_rust._disable("test_latch")
        lake_keys_rust._BURN_IN_REMAINING[lake_keys_rust._LAW_FOLD] = 0
        lake_keys_rust.reset_lake_keys_rust_for_tests()
        assert lake_keys_rust.lake_keys_rust_disabled() is False
        assert lake_keys_rust._BURN_IN_REMAINING == {
            lake_keys_rust._LAW_FOLD: lake_keys_rust.BURN_IN_SAMPLES,
            lake_keys_rust._LAW_CANDIDATES: lake_keys_rust.BURN_IN_SAMPLES,
            lake_keys_rust._LAW_RECOVERY: lake_keys_rust.BURN_IN_SAMPLES,
        }
        assert set() == lake_keys_rust._OBSERVABILITY_MEMO


# ---------------------------------------------------------------------------
# Wiring pins
# ---------------------------------------------------------------------------


def _iter_call_sites(tree: ast.AST, attr: str) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == attr) or (
                isinstance(fn, ast.Name) and fn.id == attr
            ):
                out.append(node)
    return out


class TestWiringPins:
    @pytest.mark.parametrize(
        "routed_name",
        [
            "routed_stock_drift_fold",
            "routed_spot_key_candidates",
            "routed_spot_recovery_candidates",
        ],
    )
    def test_single_call_site_pin(self, routed_name: str) -> None:
        """Each routed fn is called from EXACTLY lake_keys.py in brutex/."""
        hits: list[Path] = []
        for path in (REPO_ROOT / "brutex").rglob("*.py"):
            if path == FACADE_PATH:
                continue
            source = path.read_text(encoding="utf-8")
            if routed_name not in source:
                continue
            tree = ast.parse(source)
            if _iter_call_sites(tree, routed_name):
                hits.append(path)
        assert hits == [LAKE_KEYS_PATH]

    def test_crate_api_is_exactly_three_symbols(self) -> None:
        assert lake_keys_rust._CRATE_API == (
            "stock_drift_fold",
            "spot_key_candidates",
            "spot_recovery_candidates",
        )

    def test_facade_is_a_leaf_import_wise(self) -> None:
        """The facade imports NO lake_keys and no sibling data modules
        beyond the sanctioned flag/panic-belt sources."""
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        brutex_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("brutex"):
                    brutex_imports.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("brutex"):
                        brutex_imports.add(alias.name)
        allowed = {
            "brutex.data.fno_symbol_rust",
            "brutex.data.lake_rust_render",
        }
        assert brutex_imports <= allowed
        assert not any(
            "lake_keys" in m and m != "brutex.data.lake_keys_rust" for m in brutex_imports
        )

    def test_seam_oracles_match_public_defaults(self) -> None:
        """§5.2 surgery-faithfulness: the seam oracles equal the public
        fns' flag-OFF behavior over a hostile corpus."""
        corpus = [
            ("BAJAJ-AUTO", "NSE"),
            ("m&m ", "NSE"),
            (" NIFTY ", "BSE"),
            ("NIFTYJR", "NSE"),
            ("MIDCPNIFTY", "NSE"),
            ("", "NSE"),
        ]
        for sym, exch in corpus:
            assert lake_keys.stock_drift_fold(sym) == (lake_keys._stock_drift_fold_python(sym))
            assert lake_keys.spot_key_candidates(sym, exch) == (
                lake_keys._spot_key_candidates_python(sym, exch)
            )
            assert lake_keys.spot_recovery_candidates(sym, exch) == (
                lake_keys._spot_recovery_candidates_python(
                    sym,
                    exch,
                    alias_pairs=lake_keys.fno_index_alias_pairs(),
                    drift_bucket=(),
                )
            )


class TestImportGraphAcyclic:
    @pytest.mark.parametrize(
        "code",
        [
            "import brutex.data.lake_keys_rust; import brutex.data.lake_keys",
            "import brutex.data.lake_keys; import brutex.data.lake_keys_rust",
        ],
        ids=["facade_first", "lake_keys_first"],
    )
    def test_fresh_subprocess_import_orders(self, code: str) -> None:
        env = dict(os.environ)
        env.pop("BRUTEX_DATA_RUST", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Coverage completion — per-law guard/degrade arms + memo repeats
# ---------------------------------------------------------------------------


class TestPerLawGuardArms:
    def test_candidates_crate_unavailable_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)

        def _raise_import(name: str, *a: object, **k: object) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(lake_keys_rust.importlib, "import_module", _raise_import)
        assert lake_keys.spot_key_candidates(" NSE-NIFTY ", "NSE") == (
            lake_keys._spot_key_candidates_python(" NSE-NIFTY ", "NSE")
        )
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_unavailable"

    def test_recovery_crate_unavailable_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)

        def _raise_import(name: str, *a: object, **k: object) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(lake_keys_rust.importlib, "import_module", _raise_import)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == _oracle_recovery("NIFTY", "NSE")
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_unavailable"

    def test_recovery_non_ascii_underlying_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY\u20b9",
            "NSE",
            alias_pairs=(),
            drift_bucket=(),
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == lake_keys._spot_recovery_candidates_python(
            "NIFTY\u20b9", "NSE", alias_pairs=(), drift_bucket=()
        )
        assert bomb.touched == []

    def test_recovery_non_ascii_exchange_routes_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        bomb = _BombCrate()
        _install_crate(monkeypatch, bomb)
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE\u00e9",
            alias_pairs=(),
            drift_bucket=(),
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == lake_keys._spot_recovery_candidates_python(
            "NIFTY", "NSE\u00e9", alias_pairs=(), drift_bucket=()
        )
        assert bomb.touched == []

    def test_candidates_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def _oom(symbol: str, exchange: str) -> object:
            raise MemoryError

        _install_crate(monkeypatch, _FakeCrate(candidates=_oom))
        with pytest.raises(MemoryError):
            lake_keys.spot_key_candidates("NIFTY", "NSE")

    def test_recovery_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def _oom(*a: object) -> object:
            raise MemoryError

        _install_crate(monkeypatch, _FakeCrate(recovery=_oom))
        with pytest.raises(MemoryError):
            lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )

    def test_candidates_panic_belt_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        class _PanicException(Exception):
            pass

        fake_pyo3 = type(sys)("pyo3_runtime")
        fake_pyo3.PanicException = _PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake_pyo3)

        def _panic(symbol: str, exchange: str) -> object:
            raise _PanicException("kernel panicked")

        _install_crate(monkeypatch, _FakeCrate(candidates=_panic))
        assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
            lake_keys._spot_key_candidates_python("NIFTY", "NSE")
        )
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_panic"

    def test_recovery_panic_belt_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        class _PanicException(Exception):
            pass

        fake_pyo3 = type(sys)("pyo3_runtime")
        fake_pyo3.PanicException = _PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake_pyo3)

        def _panic(*a: object) -> object:
            raise _PanicException("kernel panicked")

        _install_crate(monkeypatch, _FakeCrate(recovery=_panic))
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTY",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == _oracle_recovery("NIFTY", "NSE")
        assert lake_keys_rust._DISABLED_REASON[0] == "crate_panic"


class TestMemoRepeats:
    def test_warn_once_repeats_are_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)

        def _raise(symbol: str) -> str:
            raise ValueError("boom")

        _install_crate(monkeypatch, _FakeCrate(fold=_raise))
        with structlog.testing.capture_logs() as logs:
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"
        warns = [
            e
            for e in logs
            if e["event"] == "lake_keys_rust.fallback" and e.get("reason") == "fold_error"
        ]
        assert len(warns) == 1

    def test_disable_repeat_is_a_no_op(self) -> None:
        with structlog.testing.capture_logs() as logs:
            lake_keys_rust._disable("first_reason")
            lake_keys_rust._disable("second_reason")
        assert lake_keys_rust._DISABLED_REASON[0] == "first_reason"
        disabled = [e for e in logs if e["event"] == "lake_keys_rust.rust_path_disabled"]
        assert len(disabled) == 1


class TestRemainingRefuseRows:
    def test_fold_space_or_hyphen_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(fold=lambda s: "BAJAJ AUTO"), "result_refused_fold"
        ):
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"
        with _refuse_gate(
            monkeypatch, _FakeCrate(fold=lambda s: "BAJAJ-AUTO"), "result_refused_fold"
        ):
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"

    def test_fold_lowercase_result_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(fold=lambda s: "bajajauto"), "result_refused_fold"
        ):
            assert lake_keys.stock_drift_fold("bajaj-auto") == "BAJAJAUTO"

    def test_candidates_duplicate_entries_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch,
            _FakeCrate(candidates=lambda s, e: ["DUP", "DUP"]),
            "result_refused_candidates",
        ):
            assert lake_keys.spot_key_candidates("NIFTY", "NSE") == (
                lake_keys._spot_key_candidates_python("NIFTY", "NSE")
            )

    def test_recovery_duplicate_entries_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(recovery=lambda *a: ["DUP", "DUP"]), "result_refused_recovery"
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")

    def test_recovery_non_ascii_entry_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(recovery=lambda *a: ["NIFTY\u20b9"]), "result_refused_recovery"
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")

    def test_recovery_raw_identity_echo_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        with _refuse_gate(
            monkeypatch, _FakeCrate(recovery=lambda u, e, p, b: [u]), "result_refused_recovery"
        ):
            got = lake_keys_rust.routed_spot_recovery_candidates(
                "NIFTY",
                "NSE",
                alias_pairs=_ALIAS_PAIRS,
                drift_bucket=_BUCKET,
                python_law=lake_keys._spot_recovery_candidates_python,
            )
            assert got == _oracle_recovery("NIFTY", "NSE")


class TestRecoveryBurnInPass:
    def test_recovery_burn_in_match_decrements_and_ships(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        _install_crate(monkeypatch, _OracleCrate())
        got = lake_keys_rust.routed_spot_recovery_candidates(
            "NIFTYJR",
            "NSE",
            alias_pairs=_ALIAS_PAIRS,
            drift_bucket=_BUCKET,
            python_law=lake_keys._spot_recovery_candidates_python,
        )
        assert got == _oracle_recovery("NIFTYJR", "NSE")
        assert lake_keys_rust._BURN_IN_REMAINING[lake_keys_rust._LAW_RECOVERY] == (
            lake_keys_rust.BURN_IN_SAMPLES - 1
        )
        assert lake_keys_rust.lake_keys_rust_disabled() is False


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``TestBenchWorkflowWiring`` precedent (#1797 pattern; the #1914
    ``lake_rust_stats`` shape mirrored by the PR-1 ``fno_symbol_rust`` pin):
    the module:suite pair sits in the workflow's per-module ``mutmut`` loop
    with the EMPTY third (timeout) field — this deterministic pin fails if
    the pair is ever dropped. HONEST: it pins the WIRING only; the weekly
    run executes on its own schedule.
    """

    _WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/data/lake_keys_rust.py:tests/unit/test_lake_keys_rust.py:"' in text, (
            "lake_keys_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )
