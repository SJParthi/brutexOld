"""Tests for the Wave-2F pull-progress derived-metrics seam (`DEC-RUST-PULL-STATE-001`).

Covers BOTH sides of the seam:

* the facade accessor ``brutex.pull_state_rust.rust_progress_derived`` — the
  SILENT count/float/bool domain gate (a legal Python construction outside
  the kernel's ``2**53`` exactness envelope degrades to the oracle with NO
  warn), the shared degrade tiers (``wheel_missing`` / ``wheel_stale`` /
  ``kernel_error``), the refuse-poison result gate (``kernel_invalid``) and
  the ``eta_present`` → ``float | None`` fold;
* the ``ProgressSnapshot`` property wiring in
  ``brutex/dashboard/pull_progress.py`` — flag OFF byte-identical with the
  wheel provably NEVER touched (the recording-bomb pin), flag ON serving the
  kernel values, and every degrade falling back to the oracle;
* flag ON + the REAL wheel — the frozen pre-conversion golden replayed
  through the LIVE properties (``importorskip`` + a symbol capability probe:
  SKIPPED until the installed wheel carries the Wave-2F symbol; the crate's
  fixture-driven ``cargo test`` carries kernel parity meanwhile), plus a
  hypothesis differential over the float/int domain including the banker's
  rounding tie grids (the #1 numeric hazard of this module).
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.pull_progress import STATE_DONE, STATE_RUNNING, ProgressSnapshot
from structlog.testing import capture_logs

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_progress_derived.json"


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Any:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError`` (the CodeQL-standard shape ``getattr``/
    ``hasattr`` swallow — the teeth live in ``touches`` staying EMPTY)."""

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touches").append(name)
        raise AttributeError(name)


def _snap(
    *,
    state: str = STATE_RUNNING,
    total: int = 16,
    completed: int = 6,
    base: int = 4,
    updated_at: float = 1056.0,
    started_at: float = 1000.0,
    phase_started_at: float = 1024.0,
    open_ended: bool = False,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        state=state,
        total=total,
        completed=completed,
        failed=0,
        bars=0,
        current="",
        detail="",
        updated_at=updated_at,
        started_at=started_at,
        phase_started_at=phase_started_at,
        phase_base_completed=base,
        open_ended=open_ended,
    )


def _derived(**overrides: Any) -> tuple[float, float, float, float | None] | None:
    kwargs: dict[str, Any] = {
        "state_running": True,
        "open_ended": False,
        "total": 16,
        "completed": 6,
        "phase_base_completed": 4,
        "updated_at": 1056.0,
        "started_at": 1000.0,
        "phase_started_at": 1024.0,
    }
    kwargs.update(overrides)
    return pull_state_rust.rust_progress_derived(**kwargs)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _bits(x: float) -> str:
    return struct.pack("<d", x).hex()


# ---------------------------------------------------------------------------
# Flag OFF — byte-identical oracle, wheel provably never touched.
# ---------------------------------------------------------------------------


class TestFlagOffNeverImports:
    @pytest.mark.parametrize("flag", [None, "0"])
    def test_properties_are_oracle_and_wheel_untouched(
        self, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        if flag is None:
            monkeypatch.delenv(FLAG_ENV, raising=False)
        else:
            monkeypatch.setenv(FLAG_ENV, flag)
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        snap = _snap()
        assert snap.percent == 37.5
        assert snap.elapsed_seconds == 56.0
        assert snap.units_per_sec == 0.0625
        assert snap.eta_seconds == 160.0
        payload = snap.to_dict()
        assert payload["percent"] == 37.5
        assert payload["units_per_sec"] == 0.0625
        assert payload["eta_seconds"] == 160.0
        # Tie + clamp arms stay oracle-served too.
        assert _snap(total=16, completed=1, base=0).percent == 6.2
        assert _snap(total=4, completed=9, base=0).percent == 100.0
        assert _snap(state=STATE_DONE).eta_seconds is None
        assert bomb.touches == []


# ---------------------------------------------------------------------------
# The SILENT domain gate (legal inputs, oracle-authoritative — no warn).
# ---------------------------------------------------------------------------


class TestDomainGate:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"total": -1},
            {"completed": -7},
            {"phase_base_completed": -1},
            {"total": 2**53 + 1},
            {"completed": 2**53 + 1},
            {"phase_base_completed": 2**53 + 1},
            {"total": True},  # bool-is-int pun
            {"updated_at": 1056},  # int-typed instant (exact-int semantics)
            {"started_at": 1000},
            {"phase_started_at": 1024},
            {"open_ended": 1},  # truthy pun — oracle truthiness, no pyo3 raise
        ],
    )
    def test_out_of_domain_is_silent_none(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
    ) -> None:
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with capture_logs() as logs:
            assert _derived(**overrides) is None
        assert logs == []  # a legal input is NOT a fault — no warn, no noise
        assert bomb.touches == []  # gated BEFORE any kernel resolve

    def test_ceiling_is_inclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            seen.append(args)
            return (1.5, 2.5, 3.5, True, 4.5)

        _install(monkeypatch, progress_derived=kernel)
        assert _derived(total=2**53, completed=2**53, phase_base_completed=2**53) == (
            1.5,
            2.5,
            3.5,
            4.5,
        )
        assert seen == [(True, False, 2**53, 2**53, 2**53, 1056.0, 1000.0, 1024.0)]

    def test_nan_inf_floats_are_in_domain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (0.0, 0.0, 0.0, False, 0.0))
        assert _derived(updated_at=math.nan) is not None
        assert _derived(updated_at=math.inf) is not None


# ---------------------------------------------------------------------------
# Degrade tiers + the refuse-poison gate.
# ---------------------------------------------------------------------------


class TestDegrades:
    def test_missing_wheel_degrades_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        with capture_logs() as logs:
            assert _derived() is None
        (event,) = logs
        assert event["reason"] == "wheel_missing"

    def test_stale_wheel_degrades_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch)  # importable, but pre-Wave-2F surface
        with capture_logs() as logs:
            assert _derived() is None
        (event,) = logs
        assert event["reason"] == "wheel_stale"
        assert event["missing"] == "progress_derived"

    def test_kernel_raise_degrades_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            raise ValueError("count outside the pull-progress kernel domain")

        _install(monkeypatch, progress_derived=kernel)
        with capture_logs() as logs:
            assert _derived() is None
        (event,) = logs
        assert event["reason"] == "kernel_error"
        assert event["symbol"] == "progress_derived"

    @pytest.mark.parametrize(
        ("poison", "detail"),
        [
            (None, "NoneType"),
            (37.5, "float"),
            ((1.0, 2.0, 3.0, True), "tuple"),  # wrong arity
            ((1.0, 2.0, 3.0, True, 4.0, 5.0), "tuple"),
            ((True, 2.0, 3.0, True, 4.0), "tuple"),  # bool pun in a float slot
            ((1.0, 2.0, 3.0, 1, 4.0), "tuple"),  # int pun in the bool slot
            ((1.0, 2.0, 3.0, True, "4.0"), "tuple"),  # str in the eta slot
            ([1.0, 2.0, 3.0, True, 4.0], "list"),  # list pun
            ((1, 2.0, 3.0, True, 4.0), "tuple"),  # int in the percent slot
        ],
    )
    def test_poison_results_degrade_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object, detail: str
    ) -> None:
        _install(monkeypatch, progress_derived=lambda *a: poison)
        with capture_logs() as logs:
            assert _derived() is None
        (event,) = logs
        assert event["reason"] == "kernel_invalid"
        assert event["symbol"] == "progress_derived"
        assert event["detail"] == detail

    def test_eta_present_folds_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (1.5, 2.5, 3.5, False, 0.0))
        assert _derived() == (1.5, 2.5, 3.5, None)

    def test_nan_results_pass_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (0.0, 0.0, math.nan, True, math.nan))
        out = _derived()
        assert out is not None
        assert math.isnan(out[2])
        assert out[3] is not None and math.isnan(out[3])


# ---------------------------------------------------------------------------
# The property wiring (flag ON, fake wheel) — kernel served, degrades fall back.
# ---------------------------------------------------------------------------


class TestPropertySeam:
    def test_flag_on_serves_the_kernel_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        calls: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            calls.append(args)
            return (11.5, 22.5, 33.5, True, 44.5)

        _install(monkeypatch, progress_derived=kernel)
        snap = _snap()
        assert snap.percent == 11.5
        assert snap.elapsed_seconds == 22.5
        assert snap.units_per_sec == 33.5
        assert snap.eta_seconds == 44.5
        # ONE consult per property evaluation, exact FFI call shape.
        assert calls == [(True, False, 16, 6, 4, 1056.0, 1000.0, 1024.0)] * 4

    def test_flag_on_eta_absent_serves_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, progress_derived=lambda *a: (0.0, 0.0, 0.0, False, 0.0))
        assert _snap().eta_seconds is None

    def test_flag_on_degrade_falls_back_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch)  # stale wheel: every consult degrades
        snap = _snap()
        assert snap.percent == 37.5
        assert snap.elapsed_seconds == 56.0
        assert snap.units_per_sec == 0.0625
        assert snap.eta_seconds == 160.0

    def test_flag_on_out_of_domain_snapshot_is_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        snap = _snap(total=2**53 + 1, completed=2**54)
        assert snap.percent == 100.0  # the oracle's min-clamp
        assert bomb.touches == []  # silently gated before any resolve

    def test_flag_on_string_state_stays_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kernel receives the PRE-COMPUTED running verdict, never a string."""
        monkeypatch.setenv(FLAG_ENV, "1")
        calls: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            calls.append(args)
            return (0.0, 0.0, 0.0, False, 0.0)

        _install(monkeypatch, progress_derived=kernel)
        _ = _snap(state=STATE_DONE).eta_seconds
        assert calls[0][0] is False  # state_running verdict, not "done"


# ---------------------------------------------------------------------------
# Flag ON + the REAL wheel — golden replay + hypothesis differential
# (skipped until the installed wheel carries the Wave-2F symbol; the crate's
# fixture-driven `cargo test` carries kernel parity meanwhile).
# ---------------------------------------------------------------------------


def _require_wave2f_wheel() -> None:
    wheel = pytest.importorskip("brutex_pull_state")
    if not hasattr(wheel, "progress_derived"):
        pytest.skip("brutex_pull_state wheel predates the Wave-2F pull_progress symbol")


def _f64(cell: dict[str, Any]) -> float:
    value: float = struct.unpack("<d", bytes.fromhex(cell["bits"]))[0]
    return value


def _assert_metric(got: float, want: float, what: str, case: dict[str, Any]) -> None:
    if math.isnan(want):
        assert math.isnan(got), f"{what}: expected NaN on {case}"
    else:
        assert _bits(got) == _bits(want), f"{what}: got {got!r} want {want!r} on {case}"


def test_real_wheel_replays_the_frozen_golden_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag-ON properties ≡ the frozen pre-conversion oracle, row for row."""
    _require_wave2f_wheel()
    monkeypatch.setenv(FLAG_ENV, "1")
    frozen = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    for row in frozen["rows"]:
        case = row["case"]
        out = row["out"]
        snap = _snap(
            state=case["state"],
            total=case["total"],
            completed=case["completed"],
            base=case["phase_base_completed"],
            updated_at=_f64(case["updated_at"]),
            started_at=_f64(case["started_at"]),
            phase_started_at=_f64(case["phase_started_at"]),
            open_ended=case["open_ended"],
        )
        _assert_metric(snap.percent, _f64(out["percent"]), "percent", case)
        _assert_metric(snap.elapsed_seconds, _f64(out["elapsed_seconds"]), "elapsed", case)
        _assert_metric(snap.units_per_sec, _f64(out["units_per_sec"]), "units_per_sec", case)
        eta = snap.eta_seconds
        if out["eta_present"]:
            assert eta is not None, f"eta absent on {case}"
            _assert_metric(eta, _f64(out["eta_seconds"]), "eta", case)
        else:
            assert eta is None, f"eta fabricated on {case}"


def test_real_wheel_hypothesis_differential() -> None:
    """The REAL kernel ≡ the Python oracle, bit-for-bit, over the full domain.

    Tie-biased count grids (1/16 + 1/2500 land exact banker's ties / the
    round-up-to-clamp boundary — the #1 numeric hazard) + arbitrary floats
    (NaN/inf included). The suite runs flag-OFF by default, so the snapshot
    properties ARE the oracle; the kernel is consulted DIRECTLY through the
    facade accessor.
    """
    _require_wave2f_wheel()
    from hypothesis import given, settings
    from hypothesis import strategies as st

    assert not pull_state_rust.pull_state_rust_enabled()
    counts = st.one_of(
        st.integers(min_value=0, max_value=64),
        st.integers(min_value=0, max_value=4000),
        st.integers(min_value=0, max_value=2**53),
        st.sampled_from([16, 400, 2500, 2**53 - 1, 2**53]),
    )
    instants = st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.sampled_from([0.0, 1024.0, 1024.25, 1056.0, 1e9, -0.0, math.nan, math.inf]),
    )

    @settings(max_examples=400, deadline=None)
    @given(
        state_running=st.booleans(),
        open_ended=st.booleans(),
        total=counts,
        completed=counts,
        base=counts,
        updated_at=instants,
        started_at=instants,
        phase_started_at=instants,
    )
    def check(
        state_running: bool,
        open_ended: bool,
        total: int,
        completed: int,
        base: int,
        updated_at: float,
        started_at: float,
        phase_started_at: float,
    ) -> None:
        got = pull_state_rust.rust_progress_derived(
            state_running=state_running,
            open_ended=open_ended,
            total=total,
            completed=completed,
            phase_base_completed=base,
            updated_at=updated_at,
            started_at=started_at,
            phase_started_at=phase_started_at,
        )
        assert got is not None  # every drawn input is in the kernel domain
        snap = _snap(
            state=STATE_RUNNING if state_running else STATE_DONE,
            total=total,
            completed=completed,
            base=base,
            updated_at=updated_at,
            started_at=started_at,
            phase_started_at=phase_started_at,
            open_ended=open_ended,
        )
        case = {"t": total, "c": completed, "b": base}
        _assert_metric(got[0], snap.percent, "percent", case)
        _assert_metric(got[1], snap.elapsed_seconds, "elapsed", case)
        _assert_metric(got[2], snap.units_per_sec, "units_per_sec", case)
        eta = snap.eta_seconds
        if eta is None:
            assert got[3] is None
        else:
            assert got[3] is not None
            _assert_metric(got[3], eta, "eta", case)

    check()  # pyright: ignore[reportCallIssue] — hypothesis @given rebinds the signature
