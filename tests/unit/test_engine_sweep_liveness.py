"""Unit suite for :mod:`brutex.engine.sweep_liveness` (G4 — DEC-SWEEP-UNKILLABLE-CORE-001).

Pins the full verdict matrix — running / dead-pid / stale-wall / legacy
monotonic-only keys ⇒ UNKNOWN (fail-closed) / corrupt types ⇒ UNKNOWN — and
the G4 law that today's REAL on-disk progress shape (a genuine
``SweepProgressSnapshot.to_dict()``) judges UNKNOWN so the heal caller must
REFUSE to quarantine (the exact live-store-quarantine hazard this fixes).
"""

from __future__ import annotations

import math
import os
from typing import Any, Final

import pytest
from brutex.engine.sweep_liveness import (
    DEFAULT_FRESH_SECONDS,
    RunVerdict,
    default_pid_alive,
    exit_status_running,
    progress_verdict,
)
from brutex.engine.sweep_progress import SweepProgress

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g4_running_fresh_alive_is_running": ("G4",),
    "test_g4_running_false_is_not_running": ("G4",),
    "test_g4_dead_pid_is_not_running": ("G4",),
    "test_g4_stale_wall_is_stale_strict_boundary": ("G4",),
    "test_g4_future_wall_is_running": ("G4",),
    "test_g4_legacy_monotonic_keys_are_unknown": ("G4",),
    "test_g4_real_snapshot_to_dict_is_unknown_fail_closed": ("G4",),
    "test_g4_corrupt_field_types_are_unknown": ("G4",),
    "test_g4_unknown_never_collapses_to_not_running": ("G4",),
    "test_g4_dead_pid_dominates_staleness": ("G4",),
    "test_g4_fresh_seconds_junk_raises": ("G4",),
    "test_g4_default_fresh_seconds_pinned": ("G4",),
    "test_g4_verdict_wire_values_pinned": ("G4",),
    "test_g4_default_pid_alive_matrix": ("G4",),
    "test_g4_default_pid_alive_self_is_alive": ("G4",),
}

_ALIVE = lambda pid: True  # noqa: E731 — tiny injected probe
_DEAD = lambda pid: False  # noqa: E731

#: A fully-modern payload (the additive keys ``publish_progress`` gains).
_MODERN: Final[dict[str, Any]] = {
    "state": "running",
    "running": True,
    "wall_time_utc": 1_000_000.0,
    "pid": 4321,
}


def _verdict(now: float, payload: dict[str, Any], *, alive: bool = True) -> RunVerdict:
    return progress_verdict(payload, now_wall=now, pid_alive=_ALIVE if alive else _DEAD)


def test_g4_running_fresh_alive_is_running() -> None:
    """running=True + fresh wall + alive pid ⇒ RUNNING."""
    assert _verdict(1_000_100.0, dict(_MODERN)) is RunVerdict.RUNNING


def test_g4_running_false_is_not_running() -> None:
    """A clean-exit payload (running=False) ⇒ NOT_RUNNING even fresh + alive."""
    payload = dict(_MODERN) | {"running": False}
    assert _verdict(1_000_100.0, payload) is RunVerdict.NOT_RUNNING


def test_g4_dead_pid_is_not_running() -> None:
    """A dead writer pid ⇒ NOT_RUNNING even with a fresh wall heartbeat."""
    assert _verdict(1_000_100.0, dict(_MODERN), alive=False) is RunVerdict.NOT_RUNNING


def test_g4_stale_wall_is_stale_strict_boundary() -> None:
    """Age STRICTLY beyond fresh_seconds ⇒ STALE; exactly AT the window ⇒ RUNNING."""
    at_window = 1_000_000.0 + DEFAULT_FRESH_SECONDS
    assert _verdict(at_window, dict(_MODERN)) is RunVerdict.RUNNING
    assert _verdict(at_window + 0.001, dict(_MODERN)) is RunVerdict.STALE


def test_g4_future_wall_is_running() -> None:
    """A wall stamp slightly in the future (clock skew) reads fresh, never stale."""
    assert _verdict(999_000.0, dict(_MODERN)) is RunVerdict.RUNNING


def test_g4_legacy_monotonic_keys_are_unknown() -> None:
    """Missing wall/pid/running keys (the legacy shape) ⇒ UNKNOWN, fail-closed."""
    for missing in ("running", "wall_time_utc", "pid"):
        payload = {k: v for k, v in _MODERN.items() if k != missing}
        assert _verdict(1_000_100.0, payload) is RunVerdict.UNKNOWN
    assert _verdict(1_000_100.0, {}) is RunVerdict.UNKNOWN


def test_g4_real_snapshot_to_dict_is_unknown_fail_closed() -> None:
    """Today's REAL on-disk shape (monotonic updated_at, no wall/pid) ⇒ UNKNOWN.

    The G4 regression grounding: the heal region compared ``time.time()``
    against this monotonic ``updated_at`` and could quarantine a LIVE run's
    stores. Under the new verdict the legacy file is UNKNOWN — the heal caller
    must REFUSE, protecting live bytes.
    """
    tracker = SweepProgress()
    tracker.start(enumerated=10, detail="live run")
    tracker.advance(current="combo-1", survived=True)
    legacy = tracker.snapshot().to_dict()
    assert legacy["running"] is True  # the legacy shape DOES carry running…
    assert "wall_time_utc" not in legacy  # …but no wall/pid evidence
    assert "pid" not in legacy
    assert progress_verdict(legacy, now_wall=1_000_100.0, pid_alive=_ALIVE) is RunVerdict.UNKNOWN


@pytest.mark.parametrize(
    "patch",
    [
        {"running": "yes"},
        {"running": 1},
        {"running": None},
        {"wall_time_utc": "1000000"},
        {"wall_time_utc": math.nan},
        {"wall_time_utc": math.inf},
        {"wall_time_utc": True},
        {"wall_time_utc": None},
        {"pid": "4321"},
        {"pid": 4321.0},
        {"pid": True},
        {"pid": 0},
        {"pid": -7},
        {"pid": None},
    ],
)
def test_g4_corrupt_field_types_are_unknown(patch: dict[str, Any]) -> None:
    """Every wrong-typed field (bool-as-number traps included) ⇒ UNKNOWN."""
    payload = dict(_MODERN) | patch
    assert _verdict(1_000_100.0, payload) is RunVerdict.UNKNOWN


def test_g4_unknown_never_collapses_to_not_running() -> None:
    """The enum encodes the heal-refusal law: UNKNOWN is a DISTINCT verdict.

    A heal that quarantines on ``NOT_RUNNING`` must see legacy/corrupt files
    as ``UNKNOWN`` — never the quarantinable verdict.
    """
    distinct_values = {verdict.value for verdict in (RunVerdict.UNKNOWN, RunVerdict.NOT_RUNNING)}
    assert len(distinct_values) == 2
    legacy = {"state": "running", "updated_at": 12.5, "running": True}
    verdict = progress_verdict(legacy, now_wall=1_000_100.0, pid_alive=_ALIVE)
    assert verdict is not RunVerdict.NOT_RUNNING  # never the quarantinable verdict
    assert verdict is RunVerdict.UNKNOWN


def test_g4_dead_pid_dominates_staleness() -> None:
    """Dead pid + stale wall ⇒ NOT_RUNNING (the pid evidence is conclusive)."""
    assert _verdict(9_999_999.0, dict(_MODERN), alive=False) is RunVerdict.NOT_RUNNING


@pytest.mark.parametrize("fresh", [0.0, -1.0, math.nan, math.inf])
def test_g4_fresh_seconds_junk_raises(fresh: float) -> None:
    """A non-positive/non-finite freshness window fails loudly."""
    with pytest.raises(ValueError, match="fresh_seconds"):
        progress_verdict(dict(_MODERN), now_wall=1.0, fresh_seconds=fresh, pid_alive=_ALIVE)


def test_g4_default_fresh_seconds_pinned() -> None:
    """The default freshness window is exactly 600 s."""
    assert DEFAULT_FRESH_SECONDS == 600.0


def test_g4_verdict_wire_values_pinned() -> None:
    """The verdict wire strings are frozen (consumers key on them)."""
    assert RunVerdict.RUNNING.value == "running"
    assert RunVerdict.STALE.value == "stale"
    assert RunVerdict.NOT_RUNNING.value == "not_running"
    assert RunVerdict.UNKNOWN.value == "unknown"


def test_g4_default_pid_alive_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """signal-0 probe: dead ⇒ False; EPERM ⇒ alive; deliverable ⇒ alive."""

    def _raise_lookup(pid: int, sig: int) -> None:
        raise ProcessLookupError

    def _raise_perm(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", _raise_lookup)
    assert default_pid_alive(12345) is False
    monkeypatch.setattr(os, "kill", _raise_perm)
    assert default_pid_alive(12345) is True
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert default_pid_alive(12345) is True


def test_g4_default_pid_alive_self_is_alive() -> None:
    """The genuine (unpatched) probe confirms our own pid alive."""
    assert default_pid_alive(os.getpid()) is True


# ── C6 resume-startup residual: exit_status_running (round-4 hostile-review) ──


def test_c6_exit_status_running_live_pid_true() -> None:
    """state=running + alive pid ⇒ the run is live (closes the resume window)."""
    payload = {"run_id": "r", "exit_status": {"state": "running", "pid": 4321}}
    assert exit_status_running(payload, pid_alive=lambda _pid: True) is True


def test_c6_exit_status_running_dead_pid_false() -> None:
    """state=running but a DEAD pid ⇒ not live (fall back to progress verdict)."""
    payload = {"exit_status": {"state": "running", "pid": 4321}}
    assert exit_status_running(payload, pid_alive=lambda _pid: False) is False


def test_c6_exit_status_running_terminal_state_false() -> None:
    """A terminal (non-running) exit-status is never a live run."""
    payload = {"exit_status": {"state": "complete", "pid": 4321}}
    assert exit_status_running(payload, pid_alive=lambda _pid: True) is False


def test_c6_exit_status_running_missing_or_none_false() -> None:
    """No ``exit_status`` (or an explicit ``None``) ⇒ False (no run to protect)."""
    assert exit_status_running({}, pid_alive=lambda _pid: True) is False
    assert exit_status_running({"exit_status": None}, pid_alive=lambda _pid: True) is False


@pytest.mark.parametrize("bad_pid", [None, "4321", True, 0, -1])
def test_c6_exit_status_running_invalid_pid_false(bad_pid: Any) -> None:
    """A missing / non-int / bool / non-positive pid ⇒ False (never probes)."""
    payload = {"exit_status": {"state": "running", "pid": bad_pid}}

    def _must_not_probe(_pid: int) -> bool:  # pragma: no cover - asserted never called
        raise AssertionError("pid_alive must not be probed for an invalid pid")

    assert exit_status_running(payload, pid_alive=_must_not_probe) is False


def test_c6_exit_status_running_default_probe_self_alive() -> None:
    """The default probe (unpatched) confirms our own live pid as running."""
    payload = {"exit_status": {"state": "running", "pid": os.getpid()}}
    assert exit_status_running(payload) is True


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1b routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the `progress_verdict` decision tail.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    value-identical to the flag-OFF Python law (which every OTHER test in
    this file drives) — including the injected-probe call count (only a
    running=True payload is ever probed) and the fail-closed UNKNOWN arm
    (which never reaches the routed tail).
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        # The shared §11-COMMON builder carries EVERY `_CRATE_API` symbol —
        # a partial fake would latch `crate_stale` and turn this class vacuous.
        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_matches_python_law_all_verdicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine.sweep_liveness import RunVerdict, progress_verdict

        payload = {"running": True, "wall_time_utc": 1000.0, "pid": 4242}

        def cases() -> list[RunVerdict]:
            return [
                progress_verdict(payload, now_wall=1100.0, pid_alive=lambda pid: True),
                progress_verdict(payload, now_wall=1601.0, pid_alive=lambda pid: True),
                progress_verdict(payload, now_wall=1100.0, pid_alive=lambda pid: False),
                progress_verdict(  # running=False — the pre-tail arm
                    {"running": False, "wall_time_utc": 1000.0, "pid": 4242},
                    now_wall=1100.0,
                ),
                progress_verdict({}, now_wall=1100.0),  # legacy → UNKNOWN, pre-tail
            ]

        off = cases()
        assert off == [
            RunVerdict.RUNNING,
            RunVerdict.STALE,
            RunVerdict.NOT_RUNNING,
            RunVerdict.NOT_RUNNING,
            RunVerdict.UNKNOWN,
        ]
        self._arm_fake_crate(monkeypatch)
        try:
            assert cases() == off
        finally:
            self._reset_facade()

    def test_flag_on_probe_called_only_for_running_payloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_liveness import RunVerdict, progress_verdict

        self._arm_fake_crate(monkeypatch)
        try:
            probes: list[int] = []

            def probe(pid: int) -> bool:
                probes.append(pid)
                return False

            verdict = progress_verdict(
                {"running": False, "wall_time_utc": 1000.0, "pid": 4242},
                now_wall=1100.0,
                pid_alive=probe,
            )
            assert verdict is RunVerdict.NOT_RUNNING
            assert probes == []  # a non-running payload is never probed
            verdict = progress_verdict(
                {"running": True, "wall_time_utc": 1000.0, "pid": 4242},
                now_wall=1100.0,
                pid_alive=probe,
            )
            assert verdict is RunVerdict.NOT_RUNNING
            assert probes == [4242]  # probed exactly once
        finally:
            self._reset_facade()


class TestClassCFallbackUnreachable:
    """All three clock operands reach the crate — no CLASS-C degrade.

    DEC-RUST-STRICT-NO-FALLBACK-001. ``routed_liveness_tail`` gates on
    ``type(v) is float``, so before the call-site normalisation an ``int``
    ``now_wall`` (a whole epoch second) or ``fresh_seconds`` (``600``)
    announced ``liveness_tail_input_refused`` and served the Python oracle —
    a fall-back on an input the tail (one subtraction, one comparison) handles
    exactly. ``wall_time_utc`` arrives from JSON and the payload guard admits
    ``int | float``, so it needed normalising too. All three now go through
    ``float()``, which is the same conversion CPython's mixed ``int``/``float``
    arithmetic performed implicitly.
    """

    _PAYLOAD: Final[dict[str, Any]] = {
        "running": True,
        "wall_time_utc": 1000.0,
        "pid": 4321,
    }

    def test_int_clock_operands_reach_the_crate(self) -> None:
        """``int`` ``now_wall``/``fresh_seconds`` route with ZERO fall-backs."""
        from tests.fakes.sweep_ops_crate import routed_fallback_reasons

        verdict, reasons, engaged = routed_fallback_reasons(
            lambda: progress_verdict(
                self._PAYLOAD,
                now_wall=2000,
                fresh_seconds=600,
                pid_alive=lambda _pid: True,
            )
        )
        assert reasons == []
        assert engaged == 1  # anti-vacuity: the crate genuinely ran
        assert verdict is RunVerdict.STALE

    def test_int_wall_time_payload_reaches_the_crate(self) -> None:
        """An ``int`` ``wall_time_utc`` (legal per the payload guard) routes too."""
        from tests.fakes.sweep_ops_crate import routed_fallback_reasons

        payload = {"running": True, "wall_time_utc": 1000, "pid": 4321}
        verdict, reasons, engaged = routed_fallback_reasons(
            lambda: progress_verdict(
                payload, now_wall=1100.0, fresh_seconds=600.0, pid_alive=lambda _pid: True
            )
        )
        assert reasons == []
        assert engaged == 1
        assert verdict is RunVerdict.RUNNING

    def test_int_operands_are_value_identical_to_their_float_twins(self) -> None:
        """Normalisation is value-neutral on both sides of the strict boundary."""
        for now_wall, fresh in ((1600, 600), (1601, 600)):
            as_int = progress_verdict(
                self._PAYLOAD,
                now_wall=now_wall,
                fresh_seconds=fresh,
                pid_alive=lambda _pid: True,
            )
            as_float = progress_verdict(
                self._PAYLOAD,
                now_wall=float(now_wall),
                fresh_seconds=float(fresh),
                pid_alive=lambda _pid: True,
            )
            assert as_int is as_float
        # The strict `>` boundary is preserved: exactly-at-window is RUNNING.
        assert (
            progress_verdict(
                self._PAYLOAD, now_wall=1600, fresh_seconds=600, pid_alive=lambda _pid: True
            )
            is RunVerdict.RUNNING
        )

    def test_fresh_seconds_still_fails_loud_on_junk(self) -> None:
        """Validation is untouched — a non-finite window still raises."""
        with pytest.raises(ValueError, match="fresh_seconds"):
            progress_verdict(self._PAYLOAD, now_wall=1.0, fresh_seconds=math.nan)
