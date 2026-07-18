"""The pull-progress derived-metrics kernel seam (`DEC-RUST-PULL-STATE-001`, flip phase).

Covers BOTH sides of the seam post-flip (the Python oracle bodies are
DELETED — the kernel is THE decision plane, fail-closed):

* the facade accessor ``brutex.pull_state_rust.rust_progress_derived`` — the
  count/float/bool input-DOMAIN gate now REFUSES loudly (an input outside
  the spike-proven ``2**53`` exactness envelope raises ``kernel_invalid``
  BEFORE any wheel resolve), the fault tiers (``wheel_missing`` /
  ``wheel_stale`` / ``kernel_error``) raise typed, the refuse-poison result
  gate (``kernel_invalid``) raises, and the ``eta_present`` →
  ``float | None`` fold serves;
* the ``ProgressSnapshot`` property wiring in
  ``brutex/dashboard/pull_progress.py`` — kernel values SERVED, faults
  RAISE through the properties, a SET removed knob refuses loudly;
* the REAL wheel — the frozen pre-conversion golden replayed through the
  LIVE properties, direct kernel-law value pins, and a hypothesis
  differential against an IN-TEST reimplementation of the deleted oracle
  law (banker's-rounding tie grids included — the #1 numeric hazard; test
  code may re-state the law, production may not).
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import math
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.pull_progress import STATE_DONE, STATE_RUNNING, ProgressSnapshot
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "pull_progress_derived.json"


def _run_given(fn: Callable[..., object]) -> None:
    """Invoke a hypothesis ``@given``-wrapped inner check as a zero-arg runner.

    The decorator rebinds the signature (hypothesis supplies every argument),
    which static arity checkers (pyright reportCallIssue, CodeQL
    py/call/wrong-arguments) cannot see through on a DIRECT call — routing
    the call through this ``Callable[..., object]`` seam states the dynamic
    contract once instead of per-site ignore pragmas.
    """
    fn()


@pytest.fixture(autouse=True)
def _knob_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The removed opt-in knob must be UNSET for every non-refusal pin (a
    SET value refuses loudly by design — pinned in its own test below)."""
    monkeypatch.delenv(FLAG_ENV, raising=False)


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError`` (the facade's 3-arg ``getattr`` folds it into
    the ``wheel_stale`` refusal — the domain-gate teeth live in ``touches``
    staying EMPTY)."""

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        """Record *name* then raise (the stale-wheel shape)."""
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
    """A snapshot with the suite's canonical counter/instant defaults."""
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


def _derived(**overrides: Any) -> tuple[float, float, float, float | None]:
    """Drive the facade accessor with the canonical inputs + *overrides*."""
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
    """Plant a fake wheel exposing exactly *symbols*."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _bits(x: float) -> str:
    """The IEEE-754 bit pattern of *x* (little-endian hex)."""
    return struct.pack("<d", x).hex()


# ---------------------------------------------------------------------------
# The input-DOMAIN gate — now a LOUD refusal (the silent Python arm is gone).
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
            {"open_ended": 1},  # truthy pun — no genuine bool, no pyo3 pass
        ],
    )
    def test_out_of_domain_refuses_before_any_resolve(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
    ) -> None:
        """DEC-RUST-PULL-STATE-001 (flip phase): an input outside the
        kernel's modeled domain RAISES ``kernel_invalid`` naming the domain
        — the deleted Python arm no longer serves it — and the wheel is
        never even resolved (the bomb stays untouched)."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _derived(**overrides)
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
# Fault tiers + the refuse-poison gate — typed raises, fail-closed.
# ---------------------------------------------------------------------------


class TestKernelFaultsFailClosed:
    def test_missing_wheel_raises_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            _derived()

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch)  # importable, but pre-Wave-2F surface
        with pytest.raises(PullStateKernelError, match="progress_derived.*wheel_stale"):
            _derived()

    def test_kernel_raise_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            raise ValueError("count outside the pull-progress kernel domain")

        _install(monkeypatch, progress_derived=kernel)
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            _derived()

    @pytest.mark.parametrize(
        "poison",
        [
            None,
            37.5,
            (1.0, 2.0, 3.0, True),  # wrong arity
            (1.0, 2.0, 3.0, True, 4.0, 5.0),
            (True, 2.0, 3.0, True, 4.0),  # bool pun in a float slot
            (1.0, 2.0, 3.0, 1, 4.0),  # int pun in the bool slot
            (1.0, 2.0, 3.0, True, "4.0"),  # str in the eta slot
            [1.0, 2.0, 3.0, True, 4.0],  # list pun
            (1, 2.0, 3.0, True, 4.0),  # int in the percent slot
        ],
    )
    def test_poison_results_raise_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _install(monkeypatch, progress_derived=lambda *a: poison)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _derived()

    def test_eta_present_folds_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (1.5, 2.5, 3.5, False, 0.0))
        assert _derived() == (1.5, 2.5, 3.5, None)

    def test_nan_results_pass_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (0.0, 0.0, math.nan, True, math.nan))
        out = _derived()
        assert math.isnan(out[2])
        assert out[3] is not None and math.isnan(out[3])


# ---------------------------------------------------------------------------
# The property wiring — kernel served; faults raise through the properties.
# ---------------------------------------------------------------------------


class TestPropertySeam:
    def test_properties_serve_the_kernel_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_eta_absent_serves_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, progress_derived=lambda *a: (0.0, 0.0, 0.0, False, 0.0))
        assert _snap().eta_seconds is None

    def test_stale_wheel_raises_through_the_property(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No oracle fallback exists: a stale wheel FAILS the property read
        loudly (the pre-flip degrade-to-oracle arm went with the oracle)."""
        _install(monkeypatch)  # stale wheel: every consult raises
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            _ = _snap().percent

    def test_out_of_domain_snapshot_raises_before_any_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-built snapshot outside the kernel domain (unreachable from
        the clamped production tracker) refuses loudly, wheel untouched."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            _ = _snap(total=2**53 + 1, completed=2**54).percent
        assert bomb.touches == []

    def test_string_state_stays_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kernel receives the PRE-COMPUTED running verdict, never a string."""
        calls: list[tuple[Any, ...]] = []

        def kernel(*args: Any) -> tuple[float, float, float, bool, float]:
            calls.append(args)
            return (0.0, 0.0, 0.0, False, 0.0)

        _install(monkeypatch, progress_derived=kernel)
        _ = _snap(state=STATE_DONE).eta_seconds
        assert calls[0][0] is False  # state_running verdict, not "done"

    def test_set_removed_knob_refuses_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANY non-empty ``BRUTEX_PULL_STATE_RUST`` — truthy spellings
        included — refuses the property read (the knob is removed)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        with pytest.raises(PullStateFlagRemovedError, match=FLAG_ENV):
            _ = _snap().percent


# ---------------------------------------------------------------------------
# The REAL wheel — kernel-law value pins, golden replay, inline-law
# hypothesis differential.
# ---------------------------------------------------------------------------


def _f64(cell: dict[str, Any]) -> float:
    """Decode a fixture float cell from its IEEE-754 bit pattern."""
    value: float = struct.unpack("<d", bytes.fromhex(cell["bits"]))[0]
    return value


def _assert_metric(got: float, want: float, what: str, case: object) -> None:
    """Bit-exact float compare (NaN-aware) with a named context."""
    if math.isnan(want):
        assert math.isnan(got), f"{what}: expected NaN on {case}"
    else:
        assert _bits(got) == _bits(want), f"{what}: got {got!r} want {want!r} on {case}"


def test_real_wheel_serves_the_frozen_oracle_values() -> None:
    """Direct kernel-law pins: the flagship pre-flip oracle values (tie +
    clamp arms included) served bit-identically by the LIVE properties."""
    snap = _snap()
    assert snap.percent == 37.5
    assert snap.elapsed_seconds == 56.0
    assert snap.units_per_sec == 0.0625
    assert snap.eta_seconds == 160.0
    payload = snap.to_dict()
    assert payload["percent"] == 37.5
    assert payload["units_per_sec"] == 0.0625
    assert payload["eta_seconds"] == 160.0
    # Tie + clamp arms (6.25% → banker's 6.2; the min-100 clamp; no ETA on
    # a settled run).
    assert _snap(total=16, completed=1, base=0).percent == 6.2
    assert _snap(total=4, completed=9, base=0).percent == 100.0
    assert _snap(state=STATE_DONE).eta_seconds is None


def test_real_wheel_replays_the_frozen_golden() -> None:
    """The LIVE (kernel-backed) properties ≡ the frozen pre-conversion
    oracle capture, row for row, bit for bit."""
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


def _oracle_derived(
    state_running: bool,
    open_ended: bool,
    total: int,
    completed: int,
    base: int,
    updated_at: float,
    started_at: float,
    phase_started_at: float,
) -> tuple[float, float, float, float | None]:
    """The deleted pre-flip oracle law, reimplemented IN TEST CODE
    token-for-token (test code may re-state the law; production may not)."""
    percent = 0.0 if total <= 0 else min(100.0, round(completed / total * 100.0, 1))
    elapsed = updated_at - started_at
    elapsed = elapsed if elapsed > 0.0 else 0.0
    progressed = completed - base
    span = updated_at - phase_started_at
    rate = 0.0 if (span <= 0.0 or progressed <= 0) else progressed / span
    eta: float | None
    if (not state_running or open_ended or total <= 0) or rate <= 0.0:
        eta = None
    else:
        remaining = total - completed
        eta = 0.0 if remaining <= 0 else remaining / rate
    return (percent, elapsed, rate, eta)


def test_real_wheel_hypothesis_differential_vs_inline_law() -> None:
    """The REAL kernel ≡ the inline oracle-law re-statement, bit-for-bit,
    over the full domain — tie-biased count grids (1/16 + 1/2500 land exact
    banker's ties / the round-up-to-clamp boundary, the #1 numeric hazard)
    + arbitrary floats (NaN/inf included)."""
    from hypothesis import given, settings
    from hypothesis import strategies as st

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
        """One drawn case: kernel result ≡ the inline law, slot by slot."""
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
        want = _oracle_derived(
            state_running,
            open_ended,
            total,
            completed,
            base,
            updated_at,
            started_at,
            phase_started_at,
        )
        case = {"t": total, "c": completed, "b": base}
        _assert_metric(got[0], want[0], "percent", case)
        _assert_metric(got[1], want[1], "elapsed", case)
        _assert_metric(got[2], want[2], "units_per_sec", case)
        if want[3] is None:
            assert got[3] is None
        else:
            assert got[3] is not None
            _assert_metric(got[3], want[3], "eta", case)

    _run_given(check)
