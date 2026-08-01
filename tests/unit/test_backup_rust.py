"""Unit gate for :mod:`brutex.backup.backup_rust` — the ops-kernel facade.

DEC-BACKUP-RUST-OPS-001. 100% line+branch STANDALONE: the flag matrix, the
import/API-surface guard, the per-law burn-in + ship-the-oracle autorevert,
the decided-degrade (:data:`~brutex.backup.backup_rust.USE_ORACLE`) arm, the
refuse-poison capture, the panic belt / escape set, the warn-once registry,
and the bomb-module OFF pin (flag OFF never imports the crate).

Fake crates are installed via ``sys.modules`` (the ``test_lake_rust_render``
idiom); the REAL-wheel legs live in the per-module seam suites + the
hypothesis differential (``tests/property/test_backup_rust_differential.py``).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest
import structlog.testing
from brutex.backup import backup_rust
from brutex.backup.backup_rust import (
    BACKUP_RUST_ENV,
    BURN_IN_SAMPLES,
    USE_ORACLE,
    backup_rust_disabled,
    backup_rust_enabled,
    burn_in_remaining,
    default_outcome_key,
    disabled_reason,
    env_flag_tristate,
    reset_backup_rust_for_tests,
    route_law,
)
from brutex.backup.errors import BackupError
from brutex.config import rust_strict

# ── shared fixtures / helpers ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts AND ends with a fresh process view of the seam.

    Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ships ON, and this module
    exists to exercise the DEGRADE lattice — so it opts OUT explicitly here
    rather than weakening any assertion. ``TestStrictRefusal`` opts back IN
    per test, on THIS SAME shared ``monkeypatch`` object (never a nested
    ``MonkeyPatch.context()`` layered on top of a fixture that owns another
    one — LIFO teardown across two objects leaks the env past the module).
    """
    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
    reset_backup_rust_for_tests()
    yield
    reset_backup_rust_for_tests()


def _fake_crate(**overrides: object) -> ModuleType:
    """A fake ``brutex_ops`` module carrying the FULL API surface."""
    module = types.ModuleType("brutex_ops")
    setattr(module, "__version__", "0.0-test")  # noqa: B010 — fake-module stamp
    for name in (
        "build_backup_argv",
        "evaluate_deadman_decision",
        "parse_backup_summary_snapshot_id",
        "parse_ls_file_paths",
        "parse_ls_snapshot_time_decision",
        "parse_snapshots_latest_decision",
        "render_backup_plist",
        "render_cron_lines",
        "render_deadman_plist",
        "render_drill_plist",
    ):
        setattr(module, name, overrides.get(name, lambda *a, **k: None))
    for name, value in overrides.items():
        setattr(module, name, value)
    return module


def _install(monkeypatch: pytest.MonkeyPatch, module: ModuleType | None) -> None:
    """Install a fake crate (or a poisoned ``None`` → ImportError) in sys.modules."""
    monkeypatch.setitem(sys.modules, "brutex_ops", module)


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BACKUP_RUST_ENV, "1")


def _value_compute(value: object) -> Callable[[ModuleType], object]:
    """A compute returning a thunk that serves *value* (ignores the crate)."""
    return lambda crate: lambda: value


# ── the flag (local tristate twin) ─────────────────────────────────────────


class TestFlag:
    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy(self, raw: str) -> None:
        assert env_flag_tristate(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
    def test_falsy(self, raw: str) -> None:
        assert env_flag_tristate(raw) is False

    @pytest.mark.parametrize("raw", ["", "   ", "junk", "onn"])
    def test_neither(self, raw: str) -> None:
        assert env_flag_tristate(raw) is None

    def test_enabled_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The DEC-RUST-DEFAULT-ON-WAVE-001 default pin: unset ⇒ ON.
        monkeypatch.delenv(BACKUP_RUST_ENV, raising=False)
        assert backup_rust_enabled() is True

    def test_enabled_truthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        assert backup_rust_enabled() is True

    def test_enabled_explicit_falsy_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKUP_RUST_ENV, "off")
        with structlog.testing.capture_logs() as logs:
            assert backup_rust_enabled() is False
        assert logs == []

    def test_enabled_junk_warns_once_and_stays_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKUP_RUST_ENV, "ture")
        with structlog.testing.capture_logs() as logs:
            assert backup_rust_enabled() is False
            assert backup_rust_enabled() is False
        events = [log for log in logs if log["event"] == "backup_rust.fallback"]
        assert len(events) == 1
        assert events[0]["reason"] == "flag_unrecognised"

    def test_enabled_whitespace_empty_is_silent_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKUP_RUST_ENV, "   ")
        with structlog.testing.capture_logs() as logs:
            assert backup_rust_enabled() is True
        assert logs == []

    def test_vocabulary_matches_the_engine_twin(self) -> None:
        """The local twin is equality-pinned against the sweep flags' home."""
        sweep_lookahead = pytest.importorskip("brutex.engine.sweep_lookahead")
        assert backup_rust._TRUTHY == sweep_lookahead._TRUTHY
        assert backup_rust._FALSY == sweep_lookahead._FALSY


# ── OFF arm: byte-identical AND crate-untouched (the bomb pin) ─────────────


class TestOffArm:
    def test_off_serves_oracle_and_never_imports_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(BACKUP_RUST_ENV, "0")
        _install(monkeypatch, None)  # a poisoned entry: ANY import raises ImportError
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("rust")) == "python"
        # The spy: an attempted import would have latched crate_unavailable.
        assert disabled_reason() is None
        assert backup_rust_disabled() is False

    def test_off_propagates_the_oracle_backup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BACKUP_RUST_ENV, "0")

        def _oracle() -> str:
            raise BackupError("decided refusal")

        with pytest.raises(BackupError, match="decided refusal"):
            route_law("law", oracle=_oracle, compute=_value_compute("rust"))

    def test_latched_process_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        backup_rust._disable("test_latch")
        _install(monkeypatch, None)
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("rust")) == "python"
        assert disabled_reason() == "test_latch"


# ── the import + API-surface guard ─────────────────────────────────────────


class TestCrateGuard:
    def test_absent_crate_latches_and_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, None)
        with structlog.testing.capture_logs() as logs:
            assert (
                route_law("law", oracle=lambda: "python", compute=_value_compute("rust"))
                == "python"
            )
        assert disabled_reason() == "crate_unavailable"
        events = [log for log in logs if log["event"] == "backup_rust.rust_path_disabled"]
        assert len(events) == 1

    def test_stale_crate_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        crate = _fake_crate()
        delattr(crate, "render_cron_lines")
        _install(monkeypatch, crate)
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("rust")) == "python"
        assert disabled_reason() == "crate_unavailable"

    def test_non_callable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate(render_cron_lines="not callable"))
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("rust")) == "python"
        assert disabled_reason() == "crate_unavailable"

    def test_disable_is_a_latch_repeats_noop(self) -> None:
        backup_rust._disable("first")
        backup_rust._disable("second")
        assert disabled_reason() == "first"


# ── route_law: compute-side degrades / escapes / panic belt ────────────────


class TestComputeDegrades:
    def test_compute_degrade_error_warns_once_and_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _compute(crate: ModuleType) -> object:
            raise ValueError("crate refused")

        with structlog.testing.capture_logs() as logs:
            assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"
            assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"
        events = [log for log in logs if log.get("reason") == "law_error"]
        assert len(events) == 1
        assert disabled_reason() is None, "a degrade error never latches"

    @pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises_raw(
        self, monkeypatch: pytest.MonkeyPatch, escape: type[BaseException]
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _compute(crate: ModuleType) -> object:
            raise escape

        with pytest.raises(escape):
            route_law("law", oracle=lambda: "python", compute=_compute)
        assert disabled_reason() is None

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _compute(crate: ModuleType) -> object:
            raise ZeroDivisionError("panic-shaped")

        assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"
        assert disabled_reason() == "crate_panic"


# ── route_law: the decided-degrade + engaged/burn-in paths ─────────────────


class TestRouting:
    def test_use_oracle_sentinel_serves_oracle_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with structlog.testing.capture_logs() as logs:
            assert (
                route_law("law", oracle=lambda: "python", compute=lambda crate: USE_ORACLE)
                == "python"
            )
        assert logs == []
        assert burn_in_remaining("law") == BURN_IN_SAMPLES, "no burn-in charge on abstain"

    def test_burn_in_match_serves_rust_and_decrements(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with structlog.testing.capture_logs() as logs:
            assert route_law("law", oracle=lambda: "same", compute=_value_compute("same")) == "same"
        assert burn_in_remaining("law") == BURN_IN_SAMPLES - 1
        engaged = [log for log in logs if log["event"] == "backup_rust.engaged"]
        assert len(engaged) == 1

    def test_engaged_logged_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with structlog.testing.capture_logs() as logs:
            route_law("law", oracle=lambda: "same", compute=_value_compute("same"))
            route_law("law", oracle=lambda: "same", compute=_value_compute("same"))
        engaged = [log for log in logs if log["event"] == "backup_rust.engaged"]
        assert len(engaged) == 1

    def test_burn_in_mismatch_autoreverts_and_ships_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        with structlog.testing.capture_logs() as logs:
            assert (
                route_law("law", oracle=lambda: "python", compute=_value_compute("drifted"))
                == "python"
            )
        assert disabled_reason() == "burn_in_mismatch"
        autorevert = [log for log in logs if log["event"] == "backup_rust.autoreverted"]
        assert len(autorevert) == 1
        assert autorevert[0]["log_level"] == "error"
        # The latch holds: the next call never touches the crate path.
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("x")) == "python"

    def test_burn_in_completes_after_the_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        reset_backup_rust_for_tests(remaining=2)
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                assert (
                    route_law("law", oracle=lambda: "same", compute=_value_compute("same"))
                    == "same"
                )
        complete = [log for log in logs if log["event"] == "backup_rust.burn_in_complete"]
        assert len(complete) == 1
        assert burn_in_remaining("law") == 0

    def test_steady_state_never_runs_the_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        reset_backup_rust_for_tests(remaining=0)

        def _bomb_oracle() -> str:
            raise AssertionError("the steady state must not run the oracle")

        assert route_law("law", oracle=_bomb_oracle, compute=_value_compute("rust")) == "rust"

    def test_per_law_budgets_are_independent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        route_law("law_a", oracle=lambda: "same", compute=_value_compute("same"))
        assert burn_in_remaining("law_a") == BURN_IN_SAMPLES - 1
        assert burn_in_remaining("law_b") == BURN_IN_SAMPLES

    def test_decided_backup_error_matches_oracle_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both arms raising the SAME BackupError is a burn-in MATCH."""
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _oracle() -> str:
            raise BackupError("no summary")

        def _compute(crate: ModuleType) -> object:
            def _thunk() -> str:
                raise BackupError("no summary")

            return _thunk

        with pytest.raises(BackupError, match="no summary"):
            route_law("law", oracle=_oracle, compute=_compute)
        assert disabled_reason() is None
        assert burn_in_remaining("law") == BURN_IN_SAMPLES - 1

    def test_decided_backup_error_mismatch_autoreverts_to_oracle_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _compute(crate: ModuleType) -> object:
            def _thunk() -> str:
                raise BackupError("rust-only refusal")

            return _thunk

        assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"
        assert disabled_reason() == "burn_in_mismatch"

    def test_steady_state_decided_backup_error_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        reset_backup_rust_for_tests(remaining=0)

        def _compute(crate: ModuleType) -> object:
            def _thunk() -> str:
                raise BackupError("decided refusal")

            return _thunk

        with pytest.raises(BackupError, match="decided refusal"):
            route_law("law", oracle=lambda: "python", compute=_compute)

    def test_poisoned_thunk_is_refused_and_oracle_served(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _compute(crate: ModuleType) -> object:
            def _thunk() -> str:
                raise TypeError("crate returned junk")

            return _thunk

        with structlog.testing.capture_logs() as logs:
            assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"
        refused = [log for log in logs if log.get("reason") == "result_refused"]
        assert len(refused) == 1
        assert disabled_reason() is None

    def test_poisoned_thunk_in_steady_state_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        reset_backup_rust_for_tests(remaining=0)

        def _compute(crate: ModuleType) -> object:
            def _thunk() -> str:
                raise TypeError("crate returned junk")

            return _thunk

        assert route_law("law", oracle=lambda: "python", compute=_compute) == "python"


# ── outcome keys + capture helpers ─────────────────────────────────────────


class TestOutcomeMachinery:
    def test_default_outcome_key_ok_is_identity(self) -> None:
        outcome: tuple[str, object] = ("ok", ("a", 1))
        assert default_outcome_key(outcome) == outcome

    def test_default_outcome_key_raise_uses_type_and_message(self) -> None:
        exc = BackupError("boom")
        assert default_outcome_key(("raise", exc)) == ("raise", "BackupError", "boom")

    def test_capture_oracle_ok_and_raise(self) -> None:
        assert backup_rust._capture_oracle(lambda: 7) == ("ok", 7)
        kind, payload = backup_rust._capture_oracle(_raise_backup_error)
        assert kind == "raise"
        assert isinstance(payload, BackupError)

    def test_release_value_and_raise(self) -> None:
        assert backup_rust._release(("ok", "v")) == "v"
        with pytest.raises(BackupError, match="decided"):
            backup_rust._release(("raise", BackupError("decided")))

    def test_warn_once_dedupes_by_reason(self) -> None:
        with structlog.testing.capture_logs() as logs:
            backup_rust._warn_once("some_reason", extra=1)
            backup_rust._warn_once("some_reason", extra=2)
            backup_rust._warn_once("other_reason")
        assert len(logs) == 2

    def test_reset_restores_fresh_view(self) -> None:
        backup_rust._disable("x")
        backup_rust._warn_once("y")
        reset_backup_rust_for_tests(remaining=3)
        assert disabled_reason() is None
        assert burn_in_remaining("any") == 3


def _raise_backup_error() -> Any:
    raise BackupError("captured")


# ── Strict mode (DEC-RUST-STRICT-NO-FALLBACK-001) ──────────────────────────


class TestStrictRefusal:
    """Strict mode turns every ANNOUNCED fall-back into a refusal.

    Two choke points — :func:`~brutex.backup.backup_rust._warn_once` and
    :func:`~brutex.backup.backup_rust._disable` — carry the gate; every
    ``return oracle()`` in :func:`route_law` is preceded by one of them
    EXCEPT the :data:`USE_ORACLE` abstain arm, which never announces and is
    therefore structurally exempt (covered below).
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-arm strict mode on the SHARED monkeypatch the autouse fixture uses.

        Deliberately NOT a nested ``MonkeyPatch.context()``: two MonkeyPatch
        objects teardown LIFO and would leak ``BRUTEX_RUST_STRICT`` past this
        module.
        """
        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    def test_class_a_crate_unavailable_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS A — an absent wheel refuses instead of serving the oracle."""
        _on(monkeypatch)
        _install(monkeypatch, None)  # any import raises ImportError
        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused, match="crate_unavailable"):
            route_law("law", oracle=lambda: "python", compute=_value_compute("rust"))
        # The refusal fires BEFORE the latch write, so the latch stays clear
        # and the latched early-return in route_law is unreachable.
        assert disabled_reason() is None
        assert backup_rust_disabled() is False

    def test_same_path_serves_the_oracle_strict_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SAME class-A path degrades normally with the kill switch on."""
        _on(monkeypatch)
        _install(monkeypatch, None)
        assert rust_strict.strict_mode() is False  # set by the autouse fixture
        assert route_law("law", oracle=lambda: "python", compute=_value_compute("rust")) == "python"
        assert disabled_reason() == "crate_unavailable"

    def test_class_a_flag_unrecognised_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS A — an operator typo refuses rather than silently disarming."""
        monkeypatch.setenv(BACKUP_RUST_ENV, "ture")
        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused, match="flag_unrecognised"):
            backup_rust_enabled()

    def test_class_a_law_error_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS A — a raising crate call refuses instead of degrading."""
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())

        def _boom(_crate: ModuleType) -> object:
            raise ValueError("crate blew up")

        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused, match="law_error"):
            route_law("law", oracle=lambda: "python", compute=_boom)

    def test_class_b_burn_in_mismatch_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLASS B — the crate DISAGREED with the oracle: never papered over."""
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        self._strict_on(monkeypatch)
        with pytest.raises(rust_strict.RustFallbackRefused, match="burn_in_mismatch"):
            route_law("law", oracle=lambda: "python", compute=_value_compute("rust"))
        assert disabled_reason() is None  # refusal precedes the latch write

    def test_class_c_use_oracle_abstain_never_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The abstain-to-oracle law is CLASS C and is STRUCTURALLY exempt.

        Layer 3 (the module docstring): a crate decision whose Python
        spelling embeds a CPython repr, or any input serde parses
        differently than CPython's ``json`` (``NaN``/``Infinity`` literals,
        lone surrogates, ``\\x1c``-class whitespace), returns
        :data:`USE_ORACLE` and :func:`route_law` serves the Python oracle
        VERBATIM — no warn, no burn-in compare, so NEITHER choke point is
        reached and there is nothing to exempt in a frozenset. Refusing here
        would break backup rendering on a legitimate input; the honest fix
        is widening the crate until the abstain is unreachable.
        """
        _on(monkeypatch)
        _install(monkeypatch, _fake_crate())
        self._strict_on(monkeypatch)
        abstain: Callable[[ModuleType], object] = lambda crate: USE_ORACLE  # noqa: E731
        assert route_law("law", oracle=lambda: "python", compute=abstain) == "python"
        assert disabled_reason() is None
        assert backup_rust_disabled() is False


class TestCrateMemo:
    """The resolved-crate memo: what it caches, what it must NOT, and the
    reset that keeps every crate swap in this file visible.

    Motivation (measured on the sibling ``money_rust``,
    DEC-MONEY-RUST-WRAPPER-COST-001): the resolve cost 911 ns/call because
    ``importlib.import_module`` is ~574 ns even on a ``sys.modules`` HIT and
    the symbol handshake the rest. This seam pays it per routed call.

    Self-contained by design: it builds its fake crate from the facade's OWN
    API tuple and drives ``_resolve_crate`` directly, so it needs nothing
    from this file's routed-call harness.
    """

    @staticmethod
    def _mod() -> object:
        from brutex.backup import backup_rust as _memo_br

        return _memo_br

    @classmethod
    def _fake(cls, *, drop: str | None = None) -> object:
        import types as _t

        mod = cls._mod()
        crate = _t.SimpleNamespace()
        for name in mod._CRATE_API:  # type: ignore[attr-defined]
            if name != drop:
                setattr(crate, name, lambda *a, **k: None)
        return crate

    @classmethod
    def _route_import(cls, monkeypatch: pytest.MonkeyPatch, crate: object) -> list[str]:
        """Point the facade's importer at *crate*; return the call log."""
        import importlib as _il

        mod = cls._mod()
        calls: list[str] = []
        real = _il.import_module

        def _import(name: str, package: str | None = None) -> object:
            if name == "brutex_ops":
                calls.append(name)
                return crate
            return real(name, package)

        monkeypatch.setattr(mod.importlib, "import_module", _import)  # type: ignore[attr-defined]
        return calls

    @classmethod
    def _strict_off(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """This class exercises the DEGRADE lattice — pin strict OFF itself.

        Self-contained rather than relying on a module-level fixture, so the
        failed-resolve pin below cannot be silently turned into a strict-mode
        refusal by an unrelated harness change.
        """
        from brutex.config import rust_strict

        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")

    def test_success_is_memoized_and_import_runs_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._mod()
        crate = self._fake()
        calls = self._route_import(monkeypatch, crate)
        mod.reset_backup_rust_for_tests()  # type: ignore[attr-defined]
        for _ in range(5):
            assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        assert calls == ["brutex_ops"], "the crate must be imported exactly ONCE"
        assert mod._CRATE_MEMO[0] is crate  # type: ignore[attr-defined]

    def test_failed_resolve_is_never_memoized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A FAILED resolve must NOT be cached: `_disable` REFUSES before it
        # latches under strict mode, so caching failure would manufacture the
        # silently-latched steady state strict mode exists to forbid — and a
        # wheel rebuilt mid-process could never be picked up.
        mod = self._mod()
        self._strict_off(monkeypatch)
        self._route_import(monkeypatch, self._fake(drop="build_backup_argv"))
        mod.reset_backup_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is None  # type: ignore[attr-defined]
        assert mod._CRATE_MEMO[0] is None  # type: ignore[attr-defined]

    def test_reset_clears_the_memo_so_a_crate_swap_is_seen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE LOAD-BEARING PIN. This file's crate installers swap WITHOUT
        # resetting, so per-test isolation rests on the reset seam clearing
        # the memo (this file's autouse fixture calls it).
        mod = self._mod()
        first = self._fake()
        self._route_import(monkeypatch, first)
        mod.reset_backup_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is first  # type: ignore[attr-defined]
        second = self._fake()
        self._route_import(monkeypatch, second)
        assert mod._resolve_crate() is first, "warm memo still serves — no re-import"  # type: ignore[attr-defined]
        mod.reset_backup_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is second  # type: ignore[attr-defined]

    def test_memo_holds_the_module_not_a_bound_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every routed law reads `getattr(crate, name)` per call, so an
        # ATTRIBUTE monkeypatch on the memoized crate must still be seen —
        # a memo holding a bound callable would serve the pre-patch function
        # forever, silently breaking ~85 attribute-monkeypatch test sites.
        mod = self._mod()
        crate = self._fake()
        self._route_import(monkeypatch, crate)
        mod.reset_backup_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        sentinel = object()
        monkeypatch.setattr(crate, "build_backup_argv", lambda *a, **k: sentinel)
        served = mod._resolve_crate().build_backup_argv  # type: ignore[attr-defined]
        assert served() is sentinel
