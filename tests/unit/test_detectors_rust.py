"""Tests for the ``detectors_rust`` routing facade (SLICE T24 PR-1).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). Yields 100%
line + branch on ``brutex/data/detectors_rust.py`` ALONE (the
discovery-gate contract): the flag matrix ×3, the never-imports-crate
latched spy, the crate-absent / crate-stale latches, every degrade arm,
the escape-set re-raise, the panic belt, the burn-in mismatch autorevert +
steady state, the refuse-poison gates (hex / runs / verdict fields), the
partial-window ABSTAIN arm, the reset seam, the §11 ``env_flag_tristate``
identity pin, and the hypothesis differentials against the REAL built
wheel (skipped when absent).
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import brutex.data.detectors_rust as dr
import pytest
from brutex.data import lake_rust_render
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from tests.fakes.detectors_crate import (
    fake_classify_partial_window_verdict,
    fake_detectors_crate,
    fake_interior_barless_runs,
)

_ALL_FLAGS = (
    dr.LAKE_RUST_RAW_INTEGRITY_ENV,
    dr.LAKE_RUST_INTERIOR_GAP_ENV,
    dr.LAKE_RUST_PARTIAL_WINDOW_ENV,
)


def _install_crate(monkeypatch: pytest.MonkeyPatch, crate: object) -> None:
    """Route the facade's ``importlib.import_module`` at the fake crate."""

    def _import(name: str) -> object:
        assert name == "brutex_lake"
        return crate

    monkeypatch.setattr(dr.importlib, "import_module", _import)


def _bomb_import(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    """Any crate import fails the test loudly (the OFF/latched inertness spy)."""

    def _bomb(name: str) -> object:
        raise AssertionError(f"{label} imported {name!r}")

    monkeypatch.setattr(dr.importlib, "import_module", _bomb)


def _hash_oracle_for(canonical: bytes) -> Any:
    return lambda: hashlib.blake2b(canonical, digest_size=32).hexdigest()


def _runs_payload(covered: tuple[int, ...], interior: tuple[int, ...]) -> Any:
    return lambda: dr.InteriorRunsPayload(
        covered_ordinals=covered, interior_session_ordinals=interior
    )


#: A Monday ordinal anchor (2024-01-01) for grid-shaped payloads.
_MON = date(2024, 1, 1).toordinal()


def _weekday_flags(start: int, length: int) -> tuple[bool, ...]:
    return tuple((start + i - 1) % 7 < 5 for i in range(length))


def _partial_payload(**overrides: Any) -> Any:
    """A truncation-shaped PartialWindowPayload factory (overridable)."""
    fields: dict[str, Any] = {
        "last_bar_ordinal": _MON,
        "window_end_ordinal": _MON + 28,
        "today_ordinal": _MON + 42,
        "expiry_ordinal": None,
        "coverage_ok": True,
        "tolerance": 5,
        "live_edge": 3,
        "span_start_ordinal": _MON - 10,
        "trading_flags": _weekday_flags(_MON - 10, 80),
    }
    fields.update(overrides)
    payload = dr.PartialWindowPayload(**fields)
    return lambda: payload


def _partial_oracle() -> tuple[str, int | None, int | None, int]:
    """The truncation-shaped payload's expected verdict fields (fake law)."""
    factory = _partial_payload()
    payload = factory()
    fields = fake_classify_partial_window_verdict(
        payload.last_bar_ordinal,
        payload.window_end_ordinal,
        payload.today_ordinal,
        payload.expiry_ordinal,
        payload.coverage_ok,
        payload.tolerance,
        payload.live_edge,
        payload.span_start_ordinal,
        payload.trading_flags,
    )
    assert fields is not None
    return fields


def _identity_materialize(
    reason: str, last_bar: int | None, expected_end: int | None, missing: int
) -> tuple[str, int | None, int | None, int]:
    return (reason, last_bar, expected_end, missing)


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
    """Fresh latch/burn-in/memo state + flags unset around every test."""
    for env in _ALL_FLAGS:
        monkeypatch.delenv(env, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


class TestFlagResolution:
    @pytest.mark.parametrize(
        ("enabled", "env"),
        [
            (dr.raw_integrity_rust_enabled, dr.LAKE_RUST_RAW_INTEGRITY_ENV),
            (dr.interior_gap_rust_enabled, dr.LAKE_RUST_INTERIOR_GAP_ENV),
            (dr.partial_window_rust_enabled, dr.LAKE_RUST_PARTIAL_WINDOW_ENV),
        ],
    )
    def test_flag_matrix(self, enabled: Any, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            # unset → ON, silent (DEC-RUST-DEFAULT-ON-WAVE-001, operator
            # directive 2026-07-23)
            assert enabled() is True
        assert logs == []
        monkeypatch.setenv(env, "1")
        assert enabled() is True
        monkeypatch.setenv(env, "0")
        with capture_logs() as logs:
            assert enabled() is False  # the kill switch, silent
        assert logs == []
        monkeypatch.setenv(env, "   ")
        with capture_logs() as logs:
            assert enabled() is True  # blank ≡ unset → the default ON, silent
        assert logs == []

    def test_junk_is_loud_off_once_per_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, "banana")
        monkeypatch.setenv(dr.LAKE_RUST_INTERIOR_GAP_ENV, "banana")
        monkeypatch.setenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV, "banana")
        with capture_logs() as logs:
            assert dr.raw_integrity_rust_enabled() is False
            assert dr.raw_integrity_rust_enabled() is False
            assert dr.interior_gap_rust_enabled() is False
            assert dr.partial_window_rust_enabled() is False
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert len(warns) == 3  # once per VARIABLE, memoised per process
        assert {w["env_var"] for w in warns} == {
            dr.LAKE_RUST_RAW_INTEGRITY_ENV,
            dr.LAKE_RUST_INTERIOR_GAP_ENV,
            dr.LAKE_RUST_PARTIAL_WINDOW_ENV,
        }

    def test_env_flag_tristate_is_the_sibling_object(self) -> None:
        # §11 COMMON identity pin — ONE flag vocabulary, never a copy.
        assert dr.env_flag_tristate is lake_rust_render.env_flag_tristate


class TestLatchAndGuards:
    def test_latched_process_serves_oracle_without_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dr._disable("test_latch")
        assert dr.rust_detectors_disabled() is True
        _bomb_import(monkeypatch, "latched path")
        canonical = b"[[1,2]]"
        assert (
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
            == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )
        assert dr.routed_interior_barless_runs(
            payload_factory=_runs_payload((1, 5), (2, 3, 4)),
            python_law=lambda: [(date.fromordinal(2), date.fromordinal(4), 3)],
        ) == [(date.fromordinal(2), date.fromordinal(4), 3)]
        assert dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: ("oracle", None, None, 0),
            materialize=_identity_materialize,
        ) == ("oracle", None, None, 0)

    def test_crate_absent_latches_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(dr.importlib, "import_module", _import)
        canonical = b"[]"
        with capture_logs() as logs:
            result = dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
        assert result == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        assert dr.rust_detectors_disabled() is True
        events = [e for e in logs if e["event"] == "detectors_rust.rust_path_disabled"]
        assert len(events) == 1
        assert events[0]["reason"] == "crate_unavailable"

    def test_crate_stale_missing_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = fake_detectors_crate()
        del crate.classify_partial_window_verdict  # a pre-T24 wheel shape
        _install_crate(monkeypatch, crate)
        with capture_logs() as logs:
            result = dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=lambda: [],
            )
        assert result == []
        assert dr.rust_detectors_disabled() is True
        events = [e for e in logs if e["event"] == "detectors_rust.rust_path_disabled"]
        assert events[0]["reason"] == "crate_stale"

    def test_crate_stale_noncallable_symbol_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = fake_detectors_crate()
        crate.raw_integrity_blake2b_hex = "not callable"
        _install_crate(monkeypatch, crate)
        canonical = b"x"
        assert (
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
            == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )
        assert dr.rust_detectors_disabled() is True

    def test_crate_absent_partial_window_serves_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(dr.importlib, "import_module", _import)
        result = dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: ("oracle", None, None, 0),
            materialize=_identity_materialize,
        )
        assert result == ("oracle", None, None, 0)
        assert dr.rust_detectors_disabled() is True

    def test_disable_repeat_is_noop(self) -> None:
        dr._disable("first")
        dr._disable("second")
        assert dr._DISABLED_REASON[0] == "first"

    def test_shared_latch_disables_all_three_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The disclosed PR-2-precedent design: one law's trip serves the
        # oracle for every detector law thereafter.
        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(dr.importlib, "import_module", _import)
        dr.routed_hash_payload_hex(b"x", python_law=lambda: "aa")
        assert dr.rust_detectors_disabled() is True
        _bomb_import(monkeypatch, "post-latch sibling law")
        assert dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: ("oracle", None, None, 0),
            materialize=_identity_materialize,
        ) == ("oracle", None, None, 0)

    def test_warn_once_repeat_is_memoised(self) -> None:
        with capture_logs() as logs:
            dr._warn_once("some_reason")
            dr._warn_once("some_reason")
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert len(warns) == 1


class TestHashRouting:
    def test_degrade_error_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(data: bytes, digest_size: int) -> str:
            raise ValueError("ffi refusal")

        _install_crate(monkeypatch, fake_detectors_crate(blake2b_hex=_boom))
        canonical = b"[[1]]"
        expected = hashlib.blake2b(canonical, digest_size=32).hexdigest()
        with capture_logs() as logs:
            for _ in range(2):
                assert (
                    dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
                    == expected
                )
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "hash_error"
        assert dr.rust_detectors_disabled() is False  # degrade, not latch

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(data: bytes, digest_size: int) -> str:
            raise MemoryError

        _install_crate(monkeypatch, fake_detectors_crate(blake2b_hex=_oom))
        with pytest.raises(MemoryError):
            dr.routed_hash_payload_hex(b"x", python_law=lambda: "aa")

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(Exception):
            pass

        def _panic(data: bytes, digest_size: int) -> str:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, fake_detectors_crate(blake2b_hex=_panic))
        monkeypatch.setattr(dr, "_panic_belt_errors", lambda: (FakePanic,))
        canonical = b"[[2]]"
        assert (
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
            == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )
        assert dr._DISABLED_REASON[0] == "crate_panic"

    def test_non_str_result_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _poison(data: bytes, digest_size: int) -> object:
            return 42

        _install_crate(monkeypatch, fake_detectors_crate(blake2b_hex=_poison))
        canonical = b"[[3]]"
        with capture_logs() as logs:
            result = dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
        assert result == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert warns[0]["reason"] == "hash_result_refused"

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _wrong(data: bytes, digest_size: int) -> str:
            return "f" * 64

        _install_crate(monkeypatch, fake_detectors_crate(blake2b_hex=_wrong))
        canonical = b"[[4]]"
        with capture_logs() as logs:
            result = dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
        assert result == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        assert dr.rust_detectors_disabled() is True
        errors = [e for e in logs if e["event"] == "detectors_rust.autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "burn_in_mismatch_hash"

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        dr.reset_detectors_rust_for_tests(remaining=1)
        canonical = b"[[5]]"
        with capture_logs() as logs:
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
        done = [e for e in logs if e["event"] == "detectors_rust.burn_in_complete"]
        assert len(done) == 1
        assert done[0]["law"] == "hash"

        def _oracle_bomb() -> str:
            raise AssertionError("steady state consulted the oracle")

        assert dr.routed_hash_payload_hex(canonical, python_law=_oracle_bomb) == (
            hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )

    def test_engaged_info_logs_once_across_laws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        canonical = b"[[6]]"
        with capture_logs() as logs:
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
            dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=lambda: [(date.fromordinal(2), date.fromordinal(4), 3)],
            )
        engaged = [e for e in logs if e["event"] == "detectors_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"] == "0.1.0-fake"


class TestInteriorRunsRouting:
    def test_success_materializes_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        expected = [(date.fromordinal(2), date.fromordinal(4), 3)]
        result = dr.routed_interior_barless_runs(
            payload_factory=_runs_payload((1, 5), (2, 3, 4)),
            python_law=lambda: expected,
        )
        assert result == expected

    def test_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(covered: object, interior: object) -> object:
            raise OverflowError("out of i64")

        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=_boom))
        with capture_logs() as logs:
            result = dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=lambda: [],
            )
        assert result == []
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert warns[0]["reason"] == "interior_runs_error"

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(covered: object, interior: object) -> object:
            raise MemoryError

        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=_oom))
        with pytest.raises(MemoryError):
            dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2,)), python_law=lambda: []
            )

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(Exception):
            pass

        def _panic(covered: object, interior: object) -> object:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=_panic))
        monkeypatch.setattr(dr, "_panic_belt_errors", lambda: (FakePanic,))
        assert (
            dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2,)), python_law=lambda: []
            )
            == []
        )
        assert dr._DISABLED_REASON[0] == "crate_panic"

    @pytest.mark.parametrize(
        "poison",
        [
            "not a list",
            [(1, 2)],  # wrong tuple arity
            [[1, 2, 3]],  # list, not tuple
            [(1, "2", 3)],  # non-int member
            [(0, 2, 1)],  # first ordinal below date.min
            [(1, 3_652_060, 1)],  # last ordinal above date.max
        ],
    )
    def test_poisoned_result_shapes_refused(
        self, poison: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=lambda c, i: poison))
        with capture_logs() as logs:
            result = dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=lambda: [],
            )
        assert result == []
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert warns[0]["reason"] == "interior_runs_result_refused"

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, fake_detectors_crate(interior_runs=lambda c, i: [(2, 2, 1)]))
        oracle = [(date.fromordinal(2), date.fromordinal(4), 3)]
        with capture_logs() as logs:
            result = dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=lambda: oracle,
            )
        assert result == oracle  # the ORACLE runs ship
        errors = [e for e in logs if e["event"] == "detectors_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_interior_runs"
        assert dr.rust_detectors_disabled() is True

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        dr.reset_detectors_rust_for_tests(remaining=1)
        expected = [(date.fromordinal(2), date.fromordinal(4), 3)]
        with capture_logs() as logs:
            assert (
                dr.routed_interior_barless_runs(
                    payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                    python_law=lambda: expected,
                )
                == expected
            )
        done = [e for e in logs if e["event"] == "detectors_rust.burn_in_complete"]
        assert done[0]["law"] == "interior_runs"

        def _oracle_bomb() -> list[tuple[date, date, int]]:
            raise AssertionError("steady state consulted the oracle")

        assert (
            dr.routed_interior_barless_runs(
                payload_factory=_runs_payload((1, 5), (2, 3, 4)),
                python_law=_oracle_bomb,
            )
            == expected
        )


class TestPartialWindowRouting:
    def test_success_routes_and_materializes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        expected = _partial_oracle()
        assert expected[0] == "truncated"  # the anti-vacuity tooth
        result = dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: expected,
            materialize=_identity_materialize,
        )
        assert result == expected

    def test_abstain_none_serves_oracle_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=lambda *a: None))
        with capture_logs() as logs:
            results = [
                dr.routed_classify_partial_window(
                    payload_factory=_partial_payload(),
                    python_law=lambda: ("oracle", None, None, 0),
                    materialize=_identity_materialize,
                )
                for _ in range(2)
            ]
        assert results == [("oracle", None, None, 0)] * 2
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "partial_window_abstain"
        assert dr.rust_detectors_disabled() is False  # an envelope miss, not a fault

    def test_degrade_error_serves_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object) -> object:
            raise TypeError("conversion failure")

        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=_boom))
        with capture_logs() as logs:
            result = dr.routed_classify_partial_window(
                payload_factory=_partial_payload(),
                python_law=lambda: ("oracle", None, None, 0),
                materialize=_identity_materialize,
            )
        assert result == ("oracle", None, None, 0)
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert warns[0]["reason"] == "partial_window_error"

    def test_memory_error_reraises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(*args: object) -> object:
            raise MemoryError

        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=_oom))
        with pytest.raises(MemoryError):
            dr.routed_classify_partial_window(
                payload_factory=_partial_payload(),
                python_law=lambda: ("oracle", None, None, 0),
                materialize=_identity_materialize,
            )

    def test_panic_belt_latches_crate_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakePanic(Exception):
            pass

        def _panic(*args: object) -> object:
            raise FakePanic("rust panicked")

        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=_panic))
        monkeypatch.setattr(dr, "_panic_belt_errors", lambda: (FakePanic,))
        result = dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: ("oracle", None, None, 0),
            materialize=_identity_materialize,
        )
        assert result == ("oracle", None, None, 0)
        assert dr._DISABLED_REASON[0] == "crate_panic"

    @pytest.mark.parametrize(
        "poison",
        [
            "not a tuple",
            ("truncated", 1, 2),  # wrong arity
            (42, 1, 2, 3),  # non-str reason
            ("truncated", "1", 2, 3),  # non-int ordinal
            ("truncated", 0, 2, 3),  # ordinal below date.min
            ("truncated", 1, 3_652_060, 3),  # ordinal above date.max
            ("truncated", 1, 2, "3"),  # non-int missing
        ],
    )
    def test_poisoned_fields_refused(self, poison: object, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=lambda *a: poison))
        with capture_logs() as logs:
            result = dr.routed_classify_partial_window(
                payload_factory=_partial_payload(),
                python_law=lambda: ("oracle", None, None, 0),
                materialize=_identity_materialize,
            )
        assert result == ("oracle", None, None, 0)
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert warns[0]["reason"] == "partial_window_result_refused"

    def test_none_ordinals_are_well_formed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The empty-arm shape (None ordinals) passes the shape gate.
        _install_crate(
            monkeypatch,
            fake_detectors_crate(classify_partial=lambda *a: ("empty", None, None, 0)),
        )
        result = dr.routed_classify_partial_window(
            payload_factory=_partial_payload(last_bar_ordinal=None),
            python_law=lambda: ("empty", None, None, 0),
            materialize=_identity_materialize,
        )
        assert result == ("empty", None, None, 0)

    def test_unknown_reason_token_refused_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Round-1 review hardening: a crate speaking an UNKNOWN reason
        # vocabulary (a stale/foreign wheel) is refused like any other
        # shape failure — the ORACLE verdict serves, one WARN per process,
        # never a latch.
        _install_crate(
            monkeypatch,
            fake_detectors_crate(classify_partial=lambda *a: ("bogus_reason", 1, 2, 3)),
        )
        with capture_logs() as logs:
            results = [
                dr.routed_classify_partial_window(
                    payload_factory=_partial_payload(),
                    python_law=lambda: ("oracle", None, None, 0),
                    materialize=_identity_materialize,
                )
                for _ in range(2)
            ]
        assert results == [("oracle", None, None, 0)] * 2
        warns = [e for e in logs if e["event"] == "detectors_rust.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "partial_window_result_refused"
        assert dr.rust_detectors_disabled() is False  # degrade, never a latch

    def test_known_partial_reasons_mirror_the_oracle_source(self) -> None:
        # The vocabulary frozenset is defined FACADE-side (importing the
        # target there would be the G6 verdict-dataclass import cycle) —
        # MIRROR-PIN it against the ``reason="…"`` literals the oracle
        # actually mints, so a new Python-side arm can never silently
        # widen the vocabulary past the refuse-poison gate.
        import brutex.data.partial_window_detect as pwd

        minted = set(re.findall(r'reason="(\w+)"', inspect.getsource(pwd)))
        assert minted == set(dr._KNOWN_PARTIAL_REASONS)
        assert len(minted) == 7  # anti-vacuity: the regex genuinely harvested

    def test_burn_in_mismatch_ships_oracle_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_crate(
            monkeypatch,
            fake_detectors_crate(classify_partial=lambda *a: ("complete", 1, 2, 0)),
        )
        oracle = ("truncated", 1, 2, 9)
        with capture_logs() as logs:
            result = dr.routed_classify_partial_window(
                payload_factory=_partial_payload(),
                python_law=lambda: oracle,
                materialize=_identity_materialize,
            )
        assert result == oracle  # the ORACLE verdict ships
        errors = [e for e in logs if e["event"] == "detectors_rust.autoreverted"]
        assert errors[0]["reason"] == "burn_in_mismatch_partial_window"
        assert dr.rust_detectors_disabled() is True

    def test_burn_in_completes_then_steady_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_crate(monkeypatch, fake_detectors_crate())
        dr.reset_detectors_rust_for_tests(remaining=1)
        expected = _partial_oracle()
        with capture_logs() as logs:
            assert (
                dr.routed_classify_partial_window(
                    payload_factory=_partial_payload(),
                    python_law=lambda: expected,
                    materialize=_identity_materialize,
                )
                == expected
            )
        done = [e for e in logs if e["event"] == "detectors_rust.burn_in_complete"]
        assert done[0]["law"] == "partial_window"

        def _oracle_bomb() -> tuple[str, int | None, int | None, int]:
            raise AssertionError("steady state consulted the oracle")

        assert (
            dr.routed_classify_partial_window(
                payload_factory=_partial_payload(),
                python_law=_oracle_bomb,
                materialize=_identity_materialize,
            )
            == expected
        )


class TestRealWheelDifferential:
    """Hypothesis differentials vs the REAL built wheel (skipped when absent)."""

    @staticmethod
    def _require_wheel() -> None:
        pytest.importorskip("brutex_lake")
        dr.reset_detectors_rust_for_tests(remaining=0)  # steady state

    @settings(max_examples=200, deadline=None)
    @given(data=st.binary(max_size=4096))
    def test_hash_differential_full_byte_domain(self, data: bytes) -> None:
        self._require_wheel()
        expected = hashlib.blake2b(data, digest_size=32).hexdigest()
        assert dr.routed_hash_payload_hex(data, python_law=lambda: expected) == expected

    @settings(max_examples=200, deadline=None)
    @given(
        covered=st.sets(st.integers(min_value=737_000, max_value=739_000), max_size=24),
        holes=st.sets(st.integers(min_value=737_000, max_value=739_000), max_size=24),
    )
    def test_interior_runs_differential(self, covered: set[int], holes: set[int]) -> None:
        self._require_wheel()
        covered_sorted = tuple(sorted(covered))
        if len(covered_sorted) >= 2:
            interior = tuple(
                s for s in sorted(covered | holes) if covered_sorted[0] < s < covered_sorted[-1]
            )
        else:
            interior = ()
        oracle = [
            (date.fromordinal(first), date.fromordinal(last), n)
            for first, last, n in fake_interior_barless_runs(covered_sorted, interior)
        ]
        assert (
            dr.routed_interior_barless_runs(
                payload_factory=_runs_payload(covered_sorted, interior),
                python_law=lambda: oracle,
            )
            == oracle
        )

    @settings(max_examples=200, deadline=None)
    @given(
        last_bar_off=st.one_of(st.none(), st.integers(min_value=-40, max_value=40)),
        window_end_off=st.integers(min_value=-40, max_value=60),
        today_off=st.integers(min_value=-10, max_value=60),
        expiry_off=st.one_of(st.none(), st.integers(min_value=-40, max_value=60)),
        coverage_ok=st.booleans(),
        tolerance=st.integers(min_value=0, max_value=10),
        live_edge=st.integers(min_value=0, max_value=6),
    )
    def test_partial_window_differential(
        self,
        last_bar_off: int | None,
        window_end_off: int,
        today_off: int,
        expiry_off: int | None,
        coverage_ok: bool,
        tolerance: int,
        live_edge: int,
    ) -> None:
        self._require_wheel()
        span_start = _MON - 90
        flags = _weekday_flags(span_start, 260)
        payload = dr.PartialWindowPayload(
            last_bar_ordinal=(_MON + last_bar_off if last_bar_off is not None else None),
            window_end_ordinal=_MON + window_end_off,
            today_ordinal=_MON + today_off,
            expiry_ordinal=(_MON + expiry_off if expiry_off is not None else None),
            coverage_ok=coverage_ok,
            tolerance=tolerance,
            live_edge=live_edge,
            span_start_ordinal=span_start,
            trading_flags=flags,
        )
        oracle_fields = fake_classify_partial_window_verdict(
            payload.last_bar_ordinal,
            payload.window_end_ordinal,
            payload.today_ordinal,
            payload.expiry_ordinal,
            payload.coverage_ok,
            payload.tolerance,
            payload.live_edge,
            payload.span_start_ordinal,
            payload.trading_flags,
        )
        assert oracle_fields is not None  # the span covers every walk here
        assert (
            dr.routed_classify_partial_window(
                payload_factory=lambda: payload,
                python_law=lambda: oracle_fields,
                materialize=_identity_materialize,
            )
            == oracle_fields
        )


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``TestBenchWorkflowWiring`` precedent (#1797 pattern; the
    lake_rust_stats twin): the module:suite pair sits in the workflow's
    per-module ``mutmut`` loop with the EMPTY third (timeout) field — this
    deterministic pin fails if the pair is ever dropped. HONEST: it pins
    the WIRING only; the weekly run executes on its own schedule.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/data/detectors_rust.py:tests/unit/test_detectors_rust.py:"' in text, (
            "detectors_rust.py is not wired into mutation.yml's per-module mutmut loop"
        )


# ---------------------------------------------------------------------------
# DEC-RUST-STRICT-NO-FALLBACK-001 — strict-ON refusal pins
# ---------------------------------------------------------------------------


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Everything else in this module runs with ``BRUTEX_RUST_STRICT=0`` (the
    degrade lattice).  These tests DELETE the kill switch so strict mode takes
    its shipped default (ON) and pin that the choke points REFUSE instead of
    quietly handing the call to the Python oracle.

    ONE class-C exemption is held in ``_STRICT_EXEMPT_REASONS``:
    ``partial_window_abstain`` — the crate's DECLARED off-grid abstain
    (``classify_partial_window_verdict`` returns ``None``), i.e. an input
    outside its envelope.  Crate FAULTS surface separately as
    ``partial_window_error`` / ``partial_window_result_refused``, so the
    token is not overloaded and the exemption cannot mask a real fault.

    ``_strict_on`` uses the SAME shared ``monkeypatch`` object the autouse
    fixture used, so both live on one LIFO undo stack and the value cannot
    leak past this module.
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config import rust_strict

        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    def test_class_a_absent_crate_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(dr.importlib, "import_module", _import)
        canonical = b"[]"
        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))

    def test_same_path_serves_the_oracle_under_strict_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The autouse fixture already pinned BRUTEX_RUST_STRICT=0.
        def _import(name: str) -> object:
            raise ImportError("no wheel")

        monkeypatch.setattr(dr.importlib, "import_module", _import)
        canonical = b"[]"
        assert (
            dr.routed_hash_payload_hex(canonical, python_law=_hash_oracle_for(canonical))
            == hashlib.blake2b(canonical, digest_size=32).hexdigest()
        )

    def test_class_b_result_refused_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            dr._disable("hash_result_refused")

    def test_class_c_partial_window_abstain_is_exempt_under_strict_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crate's declared off-grid abstain is class C: no raise."""
        assert frozenset({"partial_window_abstain"}) == dr._STRICT_EXEMPT_REASONS
        _install_crate(monkeypatch, fake_detectors_crate(classify_partial=lambda *a: None))
        self._strict_on(monkeypatch)
        assert dr.routed_classify_partial_window(
            payload_factory=_partial_payload(),
            python_law=lambda: ("oracle", None, None, 0),
            materialize=_identity_materialize,
        ) == ("oracle", None, None, 0)


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
        from brutex.data import detectors_rust as _memo_dr

        return _memo_dr

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
            if name == "brutex_lake":
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
        mod.reset_detectors_rust_for_tests()  # type: ignore[attr-defined]
        for _ in range(5):
            assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        assert calls == ["brutex_lake"], "the crate must be imported exactly ONCE"
        assert mod._CRATE_MEMO[0] is crate  # type: ignore[attr-defined]

    def test_failed_resolve_is_never_memoized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A FAILED resolve must NOT be cached: `_disable` REFUSES before it
        # latches under strict mode, so caching failure would manufacture the
        # silently-latched steady state strict mode exists to forbid — and a
        # wheel rebuilt mid-process could never be picked up.
        mod = self._mod()
        self._strict_off(monkeypatch)
        self._route_import(monkeypatch, self._fake(drop="raw_integrity_blake2b_hex"))
        mod.reset_detectors_rust_for_tests()  # type: ignore[attr-defined]
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
        mod.reset_detectors_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is first  # type: ignore[attr-defined]
        second = self._fake()
        self._route_import(monkeypatch, second)
        assert mod._resolve_crate() is first, "warm memo still serves — no re-import"  # type: ignore[attr-defined]
        mod.reset_detectors_rust_for_tests()  # type: ignore[attr-defined]
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
        mod.reset_detectors_rust_for_tests()  # type: ignore[attr-defined]
        assert mod._resolve_crate() is crate  # type: ignore[attr-defined]
        sentinel = object()
        monkeypatch.setattr(crate, "raw_integrity_blake2b_hex", lambda *a, **k: sentinel)
        served = mod._resolve_crate().raw_integrity_blake2b_hex  # type: ignore[attr-defined]
        assert served() is sentinel
